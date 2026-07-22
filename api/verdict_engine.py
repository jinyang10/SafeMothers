"""Expecta verdict engine.

Gemini 3.6 Flash (OpenRouter) is the native reasoner; rules.json is a
strict-only guardrail layer. On top of the 15-minute spec this adds:

- INCI/label alias normalization (aqua -> water, parfum -> fragrance, ...)
- Structured-output reasoning (json_schema, falls back to json_object) with
  one retry and chunking for long ingredient lists
- SQLite verdict cache keyed on (ingredients, stage, model, rules version) —
  repeat scans are instant and every scan deepens the local database
- Coverage-gap logging: UNKNOWN ingredients are recorded so the team knows
  exactly where the database needs entries next
- Confidence score and stage-specific notes in every verdict

Public contract (superset of PROJECTCONTEXT.md — extra keys are additive):
    get_verdict(ingredients, stage) -> {
        verdict, flagged_ingredients, reasoning, sources,
        confidence, stage_notes
    }
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent
load_dotenv(_HERE / ".env")
load_dotenv()

RULES_PATH = _HERE / "rules.json"
DB_PATH = _HERE / "expecta.db"
CACHE_TTL_SECONDS = 7 * 24 * 3600

RISK_RANK = {"SAFE": 0, "UNKNOWN": 1, "CAUTION": 2, "AVOID": 3}
VALID_RISKS = set(RISK_RANK)
VALID_STAGES = {"trimester_1", "trimester_2", "trimester_3", "breastfeeding"}
LLM_CHUNK_SIZE = 40

STAGE_NOTES = {
    "trimester_1": (
        "First trimester is the organogenesis window — teratogen exposure "
        "carries its highest risk now, so thresholds are strictest."
    ),
    "trimester_2": (
        "Second trimester: organ formation is largely complete, but growth "
        "and neural development continue; most first-trimester cautions "
        "still apply."
    ),
    "trimester_3": (
        "Third trimester: watch NSAIDs (fetal ductus arteriosus), agents "
        "affecting labor, and anything that crosses the placenta near term."
    ),
    "breastfeeding": (
        "Nursing: what matters is transfer into milk and infant clearance — "
        "some pregnancy AVOIDs relax, others (sedatives, iodine) tighten."
    ),
}

# Common INCI / label spellings folded to canonical names before matching.
ALIASES = {
    "aqua": "water",
    "eau": "water",
    "parfum": "fragrance",
    "tocopherol": "vitamin e",
    "tocopheryl acetate": "vitamin e",
    "ascorbic acid": "vitamin c",
    "sodium ascorbate": "vitamin c",
    "retinyl palmitate": "retinyl",
    "vitamin a palmitate": "retinyl",
    "sodium chloride": "salt",
    "saccharum": "sugar",
    "sucrose": "sugar",
    "ethanol": "alcohol",
    "alcohol denat": "alcohol",
    "sd alcohol": "alcohol",
    "acetylsalicylic acid": "aspirin",
    "paracetamol": "acetaminophen",
    "e330": "citric acid",
    "e300": "vitamin c",
    "methyl salicylate": "wintergreen oil",
}

SYSTEM = (
    "You are a clinical pregnancy-safety reasoner. Input is JSON with "
    '"stage" (trimester_1/2/3 or breastfeeding) and "ingredients". Return ONLY JSON:\n'
    '{"results":[{"name","risk":"SAFE|CAUTION|AVOID|UNKNOWN","reason","source"}], '
    '"summary":"2 plain sentences a worried mother can read at a store shelf"}\n'
    "Rules: reason about the SPECIFIC stage — thresholds differ by trimester and again "
    "in breastfeeding, and say so when relevant. \"source\" must name a real evidence "
    'basis ("FDA category", "LactMed", "ACOG guidance", "no human data"). If evidence '
    "is absent or conflicting you MUST return UNKNOWN or CAUTION — never SAFE. Never "
    "invent studies. One sentence per reason. The summary must state the single most "
    "important ingredient driving the verdict."
)

RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "safety_analysis",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "results": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "risk": {
                                "type": "string",
                                "enum": ["SAFE", "CAUTION", "AVOID", "UNKNOWN"],
                            },
                            "reason": {"type": "string"},
                            "source": {"type": "string"},
                        },
                        "required": ["name", "risk", "reason", "source"],
                        "additionalProperties": False,
                    },
                },
                "summary": {"type": "string"},
            },
            "required": ["results", "summary"],
            "additionalProperties": False,
        },
    },
}

# --------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------


def _normalize(name: str) -> str:
    text = str(name).strip().lower()
    text = re.sub(r"\d+(\.\d+)?\s*%", "", text)      # strip "2%" style doses
    text = re.sub(r"\([^)]*\)", "", text)             # strip parentheticals
    text = text.replace("*", "").replace(".", "")
    text = re.sub(r"\s+", " ", text).strip(" ,;:")
    return ALIASES.get(text, text)


def _stricter(a: str, b: str) -> str:
    return a if RISK_RANK.get(a, 1) >= RISK_RANK.get(b, 1) else b


# --------------------------------------------------------------------------
# Rules guardrails (strict-only: can raise risk, never lower it)
# --------------------------------------------------------------------------

_RULES: list[dict] | None = None
_RULES_HASH: str | None = None


def _load_rules() -> list[dict]:
    global _RULES, _RULES_HASH
    if _RULES is None:
        raw = RULES_PATH.read_bytes()
        _RULES_HASH = hashlib.sha256(raw).hexdigest()[:12]
        _RULES = json.loads(raw)
    return _RULES


def _rules_version() -> str:
    _load_rules()
    return _RULES_HASH or "unknown"


def _rule_for(norm_ingredient: str, stage: str) -> dict | None:
    for rule in _load_rules():
        if not any(m in norm_ingredient for m in rule.get("match", [])):
            continue
        if stage == "breastfeeding":
            risk = rule.get("breastfeeding", rule.get("pregnancy", "UNKNOWN"))
        else:
            risk = rule.get("pregnancy", "UNKNOWN")
        risk = (rule.get("stage_overrides") or {}).get(stage, risk)
        if risk not in VALID_RISKS:
            risk = "UNKNOWN"
        return {
            "risk": risk,
            "reason": rule.get("reason", "Matched safety rule"),
            "evidence": rule.get("source", "rules"),
        }
    return None


# --------------------------------------------------------------------------
# Local store: verdict cache + coverage gaps (the moat starts here)
# --------------------------------------------------------------------------


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS verdict_cache ("
        " key TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at REAL NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS coverage_gaps ("
        " ingredient TEXT NOT NULL, stage TEXT NOT NULL, hits INTEGER NOT NULL,"
        " last_seen REAL NOT NULL, PRIMARY KEY (ingredient, stage))"
    )
    return conn


def _cache_key(norm_ingredients: list[str], stage: str, model: str) -> str:
    blob = json.dumps(
        {
            "ingredients": sorted(norm_ingredients),
            "stage": stage,
            "model": model,
            "rules": _rules_version(),
        },
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()


def _cache_get(key: str) -> dict | None:
    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT payload, created_at FROM verdict_cache WHERE key = ?", (key,)
            ).fetchone()
        if row and time.time() - row[1] < CACHE_TTL_SECONDS:
            return json.loads(row[0])
    except Exception:
        pass
    return None


def _cache_put(key: str, payload: dict) -> None:
    try:
        with _db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO verdict_cache VALUES (?, ?, ?)",
                (key, json.dumps(payload), time.time()),
            )
    except Exception:
        pass


def _log_coverage_gaps(unknown_ingredients: list[str], stage: str) -> None:
    if not unknown_ingredients:
        return
    try:
        with _db() as conn:
            for ing in unknown_ingredients:
                conn.execute(
                    "INSERT INTO coverage_gaps VALUES (?, ?, 1, ?) "
                    "ON CONFLICT(ingredient, stage) DO UPDATE SET "
                    "hits = hits + 1, last_seen = excluded.last_seen",
                    (ing, stage, time.time()),
                )
    except Exception:
        pass


def coverage_report(limit: int = 20) -> list[dict]:
    """Most-scanned ingredients we could not classify — fill these next."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT ingredient, stage, hits FROM coverage_gaps "
            "ORDER BY hits DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [{"ingredient": r[0], "stage": r[1], "hits": r[2]} for r in rows]


# --------------------------------------------------------------------------
# Reasoner: Gemini 3.6 Flash via OpenRouter
# --------------------------------------------------------------------------


def _post_completion(payload: dict, api_key: str) -> dict:
    r = httpx.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://github.com/Waleeeeed88/SafeMothers",
            "X-Title": "Expecta",
        },
        json=payload,
        timeout=30,
    )
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"].strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
    out = json.loads(content)
    assert "results" in out and "summary" in out
    return out


def _reason_chunk(ingredients: list[str], stage: str, api_key: str, model: str) -> dict | None:
    base = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {
                "role": "user",
                "content": json.dumps({"stage": stage, "ingredients": ingredients}),
            },
        ],
    }
    # Attempt 1: strict json_schema. Attempt 2 (after brief backoff): plain
    # json_object, since some providers reject strict schemas.
    for attempt, response_format in enumerate(
        (RESPONSE_SCHEMA, {"type": "json_object"})
    ):
        try:
            return _post_completion({**base, "response_format": response_format}, api_key)
        except Exception:
            if attempt == 0:
                time.sleep(1.0)
    return None


def call_reasoner(ingredients: list[str], stage: str) -> dict | None:
    """Batched stage-aware analysis; chunks long lists; None = fail closed."""
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        return None
    model = os.getenv("OPENROUTER_MODEL", "google/gemini-3.6-flash")

    chunks = [
        ingredients[i : i + LLM_CHUNK_SIZE]
        for i in range(0, len(ingredients), LLM_CHUNK_SIZE)
    ]
    all_results: list[dict] = []
    best_summary, best_rank = None, -1
    for chunk in chunks:
        out = _reason_chunk(chunk, stage, api_key, model)
        if out is None:
            return None  # partial analysis is worse than honest failure
        results = out.get("results") or []
        all_results.extend(results)
        worst = max(
            (RISK_RANK.get(str(r.get("risk", "")).upper(), 1) for r in results),
            default=0,
        )
        if worst > best_rank:
            best_rank, best_summary = worst, str(out.get("summary") or "").strip()
    return {"results": all_results, "summary": best_summary or ""}


# --------------------------------------------------------------------------
# Merge + aggregate
# --------------------------------------------------------------------------

_CONFIDENCE = {"rules": 0.95, "gemini-flash": 0.7, "fail-closed": 0.3}


def _merge_entry(
    original: str, norm: str, rule_hit: dict | None, llm_hit: dict | None, llm_ok: bool
) -> dict:
    if llm_hit is None and rule_hit is None:
        if llm_ok:
            return {
                "name": original,
                "risk": "UNKNOWN",
                "reason": "No specific evidence returned for this ingredient.",
                "source": "gemini-flash",
            }
        return {
            "name": original,
            "risk": "UNKNOWN",
            "reason": "Could not analyze; AI unavailable and no rule match.",
            "source": "fail-closed",
        }
    if llm_hit is None:
        return {
            "name": original,
            "risk": rule_hit["risk"],
            "reason": rule_hit["reason"],
            "source": "rules",
            "evidence": rule_hit["evidence"],
        }
    if rule_hit is None:
        return {
            "name": original,
            "risk": llm_hit["risk"],
            "reason": llm_hit["reason"],
            "source": "gemini-flash",
            "evidence": llm_hit.get("evidence", "no human data"),
        }
    # Both hit: strict-only merge — the rule may only raise the risk.
    if RISK_RANK[rule_hit["risk"]] > RISK_RANK[llm_hit["risk"]]:
        return {
            "name": original,
            "risk": rule_hit["risk"],
            "reason": rule_hit["reason"],
            "source": "rules",
            "evidence": rule_hit["evidence"],
        }
    return {
        "name": original,
        "risk": llm_hit["risk"],
        "reason": llm_hit["reason"],
        "source": "gemini-flash",
        "evidence": llm_hit.get("evidence", "no human data"),
    }


def get_verdict(ingredients: list[str], stage: str, use_cache: bool = True) -> dict:
    """Contract fields plus additive extras (confidence, stage_notes)."""
    if stage not in VALID_STAGES:
        stage = "trimester_1"

    originals = [str(i).strip() for i in ingredients if i and str(i).strip()]
    norms = [_normalize(i) for i in originals]
    if not norms:
        return {
            "verdict": "UNKNOWN",
            "flagged_ingredients": [],
            "reasoning": "No ingredients provided to analyze.",
            "sources": [],
            "confidence": 0.0,
            "stage_notes": STAGE_NOTES[stage],
        }

    model = os.getenv("OPENROUTER_MODEL", "google/gemini-3.6-flash")
    key = _cache_key(norms, stage, model)
    if use_cache:
        cached = _cache_get(key)
        if cached is not None:
            return cached

    llm = call_reasoner(originals, stage)
    llm_ok = llm is not None
    summary = None
    llm_by_norm: dict[str, dict] = {}
    if llm_ok:
        summary = str(llm.get("summary") or "").strip() or None
        for item in llm.get("results") or []:
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            risk = str(item.get("risk") or "UNKNOWN").upper()
            llm_by_norm[_normalize(name)] = {
                "name": name,
                "risk": risk if risk in VALID_RISKS else "UNKNOWN",
                "reason": str(item.get("reason") or "No reason provided").strip(),
                "evidence": str(item.get("source") or "no human data").strip(),
            }

    flagged: list[dict] = []
    sources: list[str] = []
    unknowns: list[str] = []
    worst = "SAFE"
    confidence = 1.0

    for original, norm in zip(originals, norms):
        rule_hit = _rule_for(norm, stage)
        llm_hit = llm_by_norm.get(norm)
        if llm_hit is None:  # LLM may rename slightly; try substring match
            for k, v in llm_by_norm.items():
                if k in norm or norm in k:
                    llm_hit = v
                    break

        entry = _merge_entry(original, norm, rule_hit, llm_hit, llm_ok)
        worst = _stricter(worst, entry["risk"])
        confidence = min(confidence, _CONFIDENCE.get(entry["source"], 0.5))
        if entry["source"] not in sources:
            sources.append(entry["source"])
        if entry["risk"] != "SAFE":
            flagged.append(entry)
        if entry["risk"] == "UNKNOWN":
            unknowns.append(norm)

    _log_coverage_gaps(unknowns, stage)

    if llm_ok:
        reasoning = summary or "Analysis complete for the listed ingredients."
    else:
        reasoning = "Could not complete AI analysis; showing database matches only."

    result = {
        "verdict": worst,
        "flagged_ingredients": flagged,
        "reasoning": reasoning,
        "sources": sources,
        "confidence": round(confidence, 2),
        "stage_notes": STAGE_NOTES[stage],
    }
    if use_cache and llm_ok:  # never cache a degraded fail-closed verdict
        _cache_put(key, result)
    return result


if __name__ == "__main__":
    import pprint

    cases = [
        (["Aqua", "RETINOL 0.3%", "polyglyceryl-4 caprate"], "trimester_1"),
        (["water", "caffeine"], "breastfeeding"),
        (["ibuprofen", "water"], "trimester_3"),
        (["Aqua", "Glycerin", "Niacinamide"], "trimester_2"),
    ]
    for ings, stg in cases:
        t0 = time.perf_counter()
        out = get_verdict(ings, stg)
        dt = time.perf_counter() - t0
        print(f"\n=== {ings} @ {stg}  ({dt:.2f}s) ===")
        pprint.pp(out)
        t0 = time.perf_counter()
        get_verdict(ings, stg)
        print(f"cached repeat: {time.perf_counter() - t0:.4f}s")

    print("\nCoverage gaps (fill these next):")
    pprint.pp(coverage_report())
