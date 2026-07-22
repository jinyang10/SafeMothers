# Person B — Backend API (FastAPI)

**Goal (15 min):** `POST /scan` running on your LAN: barcode → Open Food Facts / Open Beauty Facts → ingredient list → Person C's `get_verdict()` → contract JSON.

Read `PROJECTCONTEXT.md` first for the request/response contract.

## Steps

### 1. Scaffold (min 0–3)

```bash
mkdir api && cd api
pip install fastapi uvicorn httpx python-dotenv
```

`main.py`: FastAPI app, CORS middleware allowing `*` (Expo needs it), one route.

```python
from pydantic import BaseModel

class ScanRequest(BaseModel):
    barcode: str
    stage: str  # trimester_1 | trimester_2 | trimester_3 | breastfeeding
```

### 2. Product resolution (min 3–9)

Async chain with `httpx`, 8s timeout per call:

1. `GET https://world.openfoodfacts.org/api/v2/product/{barcode}.json` — hit if `status == 1`.
2. Fallback: `GET https://world.openbeautyfacts.org/api/v2/product/{barcode}.json` (skincare/cosmetics — important for this audience).
3. Neither → return the contract shape with `verdict: "UNKNOWN"`, `reasoning: "Product not found in database."`

From a hit, extract `product.product_name` and ingredients:
- Prefer `product.ingredients` array (`[i["text"] for i in ...]`), fallback to splitting `product.ingredients_text` on commas.
- Normalize: lowercase, strip `*`, percentages, and parenthetical content. Product found but zero ingredients → `UNKNOWN` with `reasoning: "No ingredient data for this product."`

### 3. Wire the engine (min 9–12)

Person C hands you `verdict_engine.py` + `rules.json` (drop both into `api/`):

```python
from verdict_engine import get_verdict

@app.post("/scan")
async def scan(req: ScanRequest):
    name, ingredients = await resolve_product(req.barcode)  # your part
    result = get_verdict(ingredients, req.stage)             # C's part
    return {"product_name": name, **result}
```

Until C delivers, stub `get_verdict` to return a hardcoded CAUTION response so Person A can integrate against you early.

`.env` with `OPENROUTER_API_KEY=...` (C's module reads it via `os.getenv`). Add `.gitignore` with `.env` before anything else.

### 4. Run + share (min 12–15)

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

- Give Person A your LAN IP (`ipconfig` → IPv4). Phone and laptop must be on the same Wi-Fi. If that fails, `cloudflared tunnel --url http://localhost:8000` and share the URL instead.
- Smoke test: `curl -X POST localhost:8000/scan -H "Content-Type: application/json" -d '{"barcode":"3017624010701","stage":"trimester_1"}'` (that's Nutella — should resolve).

## Don't build

Database/caching, `/scan-label` OCR endpoint, auth, rate limiting, deployment. In-memory only today.
