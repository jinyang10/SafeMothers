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
        return {
            "product_name": product["name"],
            "image": product.get("image"),
            **named,
        }
    result = get_verdict(product["ingredients"], req.stage)
    result["sources"] = [product["source"], *result.get("sources", [])]
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
