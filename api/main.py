"""Expecta API: barcode/search product resolution + pregnancy-safety verdicts.

Product resolution order:
  1. Barcode Lookup API (https://www.barcodelookup.com/api) when
     BARCODELOOKUP_API_KEY is set — widest coverage incl. cosmetics.
  2. Open Food Facts (food).
  3. Open Beauty Facts (skincare/cosmetics).

Also serves the browser app from ../web at "/".
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from verdict_engine import coverage_report, get_verdict

app = FastAPI(title="Expecta API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BARCODE_KEY = os.getenv("BARCODELOOKUP_API_KEY", "").strip()
BL_URL = "https://api.barcodelookup.com/v3/products"
OFF_URL = "https://world.openfoodfacts.org"
OBF_URL = "https://world.openbeautyfacts.org"
TIMEOUT = 10.0


class ScanRequest(BaseModel):
    barcode: str
    stage: str = "trimester_1"


@app.get("/health")
def health() -> dict:
    """Integration check for the team: which pieces are live."""
    return {
        "status": "ok",
        "reasoner_model": os.getenv("OPENROUTER_MODEL", "google/gemini-3.6-flash"),
        "reasoner_key_present": bool(os.getenv("OPENROUTER_API_KEY")),
        "barcodelookup_enabled": bool(BARCODE_KEY),
        "product_sources": (
            (["Barcode Lookup"] if BARCODE_KEY else [])
            + ["Open Food Facts", "Open Beauty Facts"]
        ),
    }


@app.get("/coverage")
def coverage(limit: int = 20) -> dict:
    """Most-scanned ingredients we couldn't classify — the database roadmap."""
    return {"gaps": coverage_report(limit)}


# ---- scan history (SQLite, same file as the verdict cache) ----

_DB = Path(__file__).resolve().parent / "expecta.db"


def _history_db() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS scan_history ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, barcode TEXT, product TEXT,"
        " stage TEXT, verdict TEXT, ts REAL)"
    )
    return conn


def _log_scan(barcode: str, product: str, stage: str, verdict: str) -> None:
    try:
        with _history_db() as conn:
            conn.execute(
                "INSERT INTO scan_history (barcode, product, stage, verdict, ts)"
                " VALUES (?, ?, ?, ?, ?)",
                (barcode, product, stage, verdict, time.time()),
            )
    except Exception:
        pass


@app.get("/history")
def history(limit: int = 8) -> dict:
    """Most recent checks, deduped by barcode, for the app's recall strip."""
    try:
        with _history_db() as conn:
            rows = conn.execute(
                "SELECT barcode, product, stage, verdict, MAX(ts) AS ts"
                " FROM scan_history WHERE product != 'Unknown product'"
                " GROUP BY barcode ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return {
            "items": [
                {"barcode": r[0], "product": r[1], "stage": r[2], "verdict": r[3]}
                for r in rows
            ]
        }
    except Exception:
        return {"items": []}


# ---- openFDA drug-label enrichment (real medical database, no key) ----


def _openfda_pregnancy(ingredient: str) -> str | None:
    """Pull the pregnancy/nursing section from an FDA drug label, if one exists."""
    try:
        r = httpx.get(
            "https://api.fda.gov/drug/label.json",
            params={
                "search": f'active_ingredient:"{ingredient}"',
                "limit": 1,
            },
            timeout=5.0,
        )
        if r.status_code != 200:
            return None
        results = r.json().get("results") or []
        if not results:
            return None
        doc = results[0]
        for field in (
            "pregnancy",
            "teratogenic_effects",
            "nursing_mothers",
            "pregnancy_or_breast_feeding",
        ):
            val = doc.get(field)
            if val:
                text = val[0] if isinstance(val, list) else str(val)
                text = re.sub(r"\s+", " ", text).strip()
                if len(text) > 260:
                    text = text[:260].rsplit(" ", 1)[0] + "…"
                return text
    except Exception:
        pass
    return None


# Common actives worth an FDA label lookup even when the ingredient string
# is messy OCR text (e.g. "...fever retue buprofen usp...").
_DRUG_TOKENS = (
    "ibuprofen", "aspirin", "acetaminophen", "naproxen", "pseudoephedrine",
    "diphenhydramine", "loratadine", "cetirizine", "salicylic acid",
    "retinol", "tretinoin", "hydroquinone", "minoxidil", "nicotine",
    "doxycycline", "benzoyl peroxide",
)


def _fda_query_terms(name: str) -> list[str]:
    terms: list[str] = []
    if len(name) <= 40:  # clean names query as-is
        terms.append(name)
    lowered = name.lower()
    for token in _DRUG_TOKENS:
        if token in lowered and token not in [t.lower() for t in terms]:
            terms.append(token)
    return terms[:2]


def _enrich_with_openfda(result: dict) -> None:
    """Attach FDA label text to up to 3 flagged CAUTION/AVOID ingredients."""
    hits = 0
    for flag in result.get("flagged_ingredients", []):
        if hits >= 3 or flag.get("risk") not in ("CAUTION", "AVOID"):
            continue
        for term in _fda_query_terms(flag["name"]):
            evidence = _openfda_pregnancy(term)
            if evidence:
                flag["fda_label"] = evidence
                hits += 1
                break
    if hits and "openFDA" not in result.get("sources", []):
        result["sources"] = [*result.get("sources", []), "openFDA"]


def _split_ingredients(text: str) -> list[str]:
    parts = re.split(r"[,;]", text or "")
    return [p.strip() for p in parts if p.strip()]


def _from_barcodelookup(barcode: str) -> dict | None:
    if not BARCODE_KEY:
        return None
    try:
        r = httpx.get(
            BL_URL,
            params={"barcode": barcode, "formatted": "y", "key": BARCODE_KEY},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return None
        products = r.json().get("products") or []
        if not products:
            return None
        p = products[0]
        return {
            "name": p.get("title") or p.get("product_name") or "Unknown product",
            "ingredients": _split_ingredients(p.get("ingredients") or ""),
            "image": (p.get("images") or [None])[0],
            "source": "Barcode Lookup",
        }
    except Exception:
        return None


def _from_openfacts(barcode: str, base: str, label: str) -> dict | None:
    try:
        r = httpx.get(f"{base}/api/v2/product/{barcode}.json", timeout=TIMEOUT)
        data = r.json()
        if data.get("status") != 1:
            return None
        p = data.get("product") or {}
        ingredients = [
            i.get("text", "").strip()
            for i in (p.get("ingredients") or [])
            if i.get("text", "").strip()
        ] or _split_ingredients(p.get("ingredients_text") or "")
        return {
            "name": p.get("product_name") or p.get("generic_name") or "Unknown product",
            "ingredients": ingredients,
            "image": p.get("image_front_small_url"),
            "source": label,
        }
    except Exception:
        return None


def _resolve_product(barcode: str) -> dict | None:
    """First source that yields ingredients wins; else best name-only hit."""
    name_only: dict | None = None
    for fetch in (
        lambda: _from_barcodelookup(barcode),
        lambda: _from_openfacts(barcode, OFF_URL, "Open Food Facts"),
        lambda: _from_openfacts(barcode, OBF_URL, "Open Beauty Facts"),
    ):
        hit = fetch()
        if hit is None:
            continue
        if hit["ingredients"]:
            return hit
        name_only = name_only or hit
    return name_only


@app.post("/scan")
def scan(req: ScanRequest) -> dict:
    product = _resolve_product(req.barcode.strip())
    if product is None:
        return {
            "product_name": "Unknown product",
            "verdict": "UNKNOWN",
            "flagged_ingredients": [],
            "reasoning": "Product not found in any database. Try the search box, "
            "or check the ingredient label directly.",
            "sources": [],
            "confidence": 0.0,
            "image": None,
        }
    if not product["ingredients"]:
        # No ingredient list — analyze the product NAME as a last resort.
        # A name can prove danger ("Retinol Serum") but never safety, so a
        # SAFE outcome here is downgraded to UNKNOWN.
        named = get_verdict([product["name"]], req.stage)
        if named["verdict"] == "SAFE":
            named["verdict"] = "UNKNOWN"
            named["flagged_ingredients"] = []
        named["reasoning"] = (
            "No ingredient list is available for this product, so this is "
            "based on the product name only. " + (named["reasoning"] or "")
        ).strip()
        named["confidence"] = min(named.get("confidence", 0.0), 0.4)
        named["sources"] = [product["source"], *named.get("sources", [])]
        _log_scan(req.barcode.strip(), product["name"], req.stage, named["verdict"])
        return {
            "product_name": product["name"],
            "image": product.get("image"),
            **named,
        }
    result = get_verdict(product["ingredients"], req.stage)
    result["sources"] = [product["source"], *result.get("sources", [])]
    _enrich_with_openfda(result)
    _log_scan(req.barcode.strip(), product["name"], req.stage, result["verdict"])
    return {"product_name": product["name"], "image": product.get("image"), **result}


@app.get("/search")
def search(q: str) -> dict:
    """Search any product by name; returns candidates to scan by barcode."""
    q = q.strip()
    results: list[dict] = []

    if BARCODE_KEY:
        try:
            r = httpx.get(
                BL_URL,
                params={"search": q, "formatted": "y", "key": BARCODE_KEY},
                timeout=TIMEOUT,
            )
            if r.status_code == 200:
                for p in (r.json().get("products") or [])[:10]:
                    code = p.get("barcode_number")
                    if code:
                        results.append(
                            {
                                "name": p.get("title") or "Unknown product",
                                "barcode": code,
                                "brand": p.get("brand") or "",
                                "image": (p.get("images") or [None])[0],
                            }
                        )
        except Exception:
            pass

    if not results:
        # Food first, then beauty/skincare — both use the same search API.
        for base in (OFF_URL, OBF_URL):
            try:
                r = httpx.get(
                    f"{base}/cgi/search.pl",
                    params={
                        "search_terms": q,
                        "search_simple": 1,
                        "action": "process",
                        "json": 1,
                        "page_size": 8,
                    },
                    timeout=15.0,
                )
                for p in (r.json().get("products") or [])[:8]:
                    code = p.get("code")
                    name = p.get("product_name")
                    if code and name:
                        results.append(
                            {
                                "name": name,
                                "barcode": code,
                                "brand": p.get("brands") or "",
                                "image": p.get("image_front_small_url"),
                            }
                        )
            except Exception:
                pass
            if len(results) >= 8:
                break

    return {"results": results[:10]}


_WEB_DIR = Path(__file__).resolve().parent.parent / "web"
if _WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=_WEB_DIR, html=True), name="web")
