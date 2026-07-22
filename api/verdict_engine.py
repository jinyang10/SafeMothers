"""Expecta verdict engine: Gemini Flash (OpenRouter) + strict-only rules guardrails."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")
load_dotenv()  # also pick up process / parent env

RULES_PATH = Path(__file__).resolve().parent / "rules.json"
RISK_RANK = {"SAFE": 0, "UNKNOWN": 1, "CAUTION": 2, "AVOID": 3}
VALID_RISKS = set(RISK_RANK)
VALID_STAGES = {"trimester_1", "trimester_2", "trimester_3", "breastfeeding"}

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

_RULES: list[dict] | None = None


def _load_rules() -> list[dict]:
    global _RULES
    if _RULES is None:
        with RULES_PATH.open(encoding="utf-8") as f:
            _RULES = json.load(f)
    return _RULES


def _normalize(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def _stricter(a: str, b: str) -> str:
    return a if RISK_RANK.get(a, 1) >= RISK_RANK.get(b, 1) else b


def _rule_for(ingredient: str, stage: str) -> dict | None:
    text = _normalize(ingredient)
    for rule in _load_rules():
        if not any(m in text for m in rule.get("match", [])):
            continue
        if stage == "breastfeeding":
            risk = rule.get("breastfeeding", rule.get("pregnancy", "UNKNOWN"))
        else:
            risk = rule.get("pregnancy", "UNKNOWN")
        overrides = rule.get("stage_overrides") or {}
        if stage in overrides:
            risk = overrides[stage]
        if risk not in VALID_RISKS:
            risk = "UNKNOWN"
        return {
            "name": ingredient,
            "risk": risk,
            "reason": rule.get("reason", "Matched safety rule"),
            "source": "rules",
            "rule_source": rule.get("source", "rules"),
        }
    return None


def call_reasoner(ingredients: list[str], stage: str) -> dict | None:
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        return None

    model = os.getenv("OPENROUTER_MODEL", "google/gemini-2.5-flash")
    try:
        r = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "https://github.com/Waleeeeed88/SafeMothers",
                "X-Title": "Expecta",
            },
            json={
                "model": model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": SYSTEM},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"stage": stage, "ingredients": ingredients}
                        ),
                    },
                ],
            },
            timeout=25,
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        # Some models wrap JSON in fences; strip if present.
        content = content.strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```$", "", content)
        out = json.loads(content)
        assert "results" in out and "summary" in out
        return out
    except Exception:
        return None


def get_verdict(ingredients: list[str], stage: str) -> dict:
    """
    Return contract fields (everything except product_name):
    { verdict, flagged_ingredients, reasoning, sources }
    """
    if stage not in VALID_STAGES:
        stage = "trimester_1"

    cleaned = [_normalize(i) for i in ingredients if i and str(i).strip()]
    # Preserve original casing for display where possible
    originals = [str(i).strip() for i in ingredients if i and str(i).strip()]
    if not cleaned:
        return {
            "verdict": "UNKNOWN",
            "flagged_ingredients": [],
            "reasoning": "No ingredients provided to analyze.",
            "sources": [],
        }

    llm = call_reasoner(originals, stage)
    llm_by_norm: dict[str, dict] = {}
    summary = None
    llm_ok = llm is not None

    if llm_ok:
        summary = str(llm.get("summary") or "").strip() or None
        for item in llm.get("results") or []:
            name = str(item.get("name") or "").strip()
            risk = str(item.get("risk") or "UNKNOWN").upper()
            if risk not in VALID_RISKS:
                risk = "UNKNOWN"
            # Fail closed: model cannot invent SAFE without evidence — leave as-is;
            # guardrails will still tighten. Unknown/empty name skipped.
            if not name:
                continue
            llm_by_norm[_normalize(name)] = {
                "name": name,
                "risk": risk,
                "reason": str(item.get("reason") or "No reason provided").strip(),
                "source": "gemini-flash",
                "evidence": str(item.get("source") or "no human data").strip(),
            }

    flagged: list[dict] = []
    sources: list[str] = []
    worst = "SAFE"

    for original, norm in zip(originals, cleaned):
        rule_hit = _rule_for(original, stage)
        llm_hit = llm_by_norm.get(norm)

        # Fuzzy: LLM may rename slightly; try substring match on keys
        if llm_hit is None:
            for key, val in llm_by_norm.items():
                if key in norm or norm in key:
                    llm_hit = val
                    break

        if llm_hit is None and not llm_ok:
            # Fail closed when reasoner down and no rule
            if rule_hit is None:
                entry = {
                    "name": original,
                    "risk": "UNKNOWN",
                    "reason": "Could not analyze; AI unavailable and no rule match.",
                    "source": "fail-closed",
                }
            else:
                entry = {
                    "name": original,
                    "risk": rule_hit["risk"],
                    "reason": rule_hit["reason"],
                    "source": "rules",
                }
        elif llm_hit is None and rule_hit is not None:
            entry = {
                "name": original,
                "risk": rule_hit["risk"],
                "reason": rule_hit["reason"],
                "source": "rules",
            }
        elif llm_hit is None:
            entry = {
                "name": original,
                "risk": "UNKNOWN",
                "reason": "No specific evidence returned for this ingredient.",
                "source": "gemini-flash",
            }
        elif rule_hit is None:
            entry = {
                "name": original,
                "risk": llm_hit["risk"],
                "reason": llm_hit["reason"],
                "source": "gemini-flash",
            }
        else:
            # Strict-only: rules can only make risk worse
            final_risk = _stricter(llm_hit["risk"], rule_hit["risk"])
            if RISK_RANK[rule_hit["risk"]] > RISK_RANK[llm_hit["risk"]]:
                entry = {
                    "name": original,
                    "risk": final_risk,
                    "reason": rule_hit["reason"],
                    "source": "rules",
                }
            else:
                entry = {
                    "name": original,
                    "risk": final_risk,
                    "reason": llm_hit["reason"],
                    "source": "gemini-flash",
                }

        worst = _stricter(worst, entry["risk"])
        if entry["source"] not in sources:
            sources.append(entry["source"])
        if entry["risk"] != "SAFE":
            flagged.append(entry)

    if not llm_ok:
        reasoning = (
            "Could not complete AI analysis; showing database matches only."
        )
        if "rules" not in sources and any(f["source"] == "rules" for f in flagged):
            sources.append("rules")
    else:
        reasoning = summary or "Analysis complete for the listed ingredients."
        if "gemini-flash" not in sources:
            sources.insert(0, "gemini-flash")

    # Dedup sources, preserve order
    seen = set()
    deduped = []
    for s in sources:
        if s not in seen:
            seen.add(s)
            deduped.append(s)

    return {
        "verdict": worst if cleaned else "UNKNOWN",
        "flagged_ingredients": flagged,
        "reasoning": reasoning,
        "sources": deduped,
    }


if __name__ == "__main__":
    import pprint

    cases = [
        (["water", "retinol", "polyglyceryl-4 caprate"], "trimester_1"),
        (["water", "caffeine"], "breastfeeding"),
        (["ibuprofen", "water"], "trimester_3"),
    ]
    for ingredients, stage in cases:
        print("\n===", ingredients, stage, "===")
        pprint.pp(get_verdict(ingredients, stage))
