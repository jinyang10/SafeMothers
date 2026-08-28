# Expecta

**Scan a product. Get a pregnancy-safety verdict in seconds.**

Expecta is a barcode scanner for food, skincare, and medicine. Point a phone at a package, pick a pregnancy stage (first / second / third trimester, or nursing), and get a single unambiguous verdict — **Safe**, **Caution**, **Avoid**, or **Unknown** — with the ingredients that drove it and the clinical reasoning behind them.

Built as a production-minded systems project: a FastAPI backend, a fail-closed verdict engine, and a mobile-first web client. The interesting part is not the scan. It is making a **safety-critical classification** under incomplete data, model failure, and conflicting sources — and never guessing toward "safe."

---

## Why this exists

A pregnant person in a store aisle should not have to cross-reference Reddit, a 40-ingredient INCI list, and an FDA label. Existing tools are either food-only, not stage-aware, or silent when evidence is missing. Missing evidence is the dangerous case. Expecta is designed around that case.

---

## What you can do

- **Scan** a barcode with the camera, type digits, or upload a photo
- **Search** by product or drug name across food, beauty, and openFDA
- **Stage-aware verdicts** — ibuprofen is Caution in T1 and Avoid in T3; retinoids stay Avoid
- **Cited reasoning** — per-ingredient risk, plain-language summary, FDA label excerpts when available
- **Recent checks** stored locally so a second look at the same product is instant

The UI is built for an anxious user: warm paper background, one huge serif verdict word, full-screen color wash. No dashboards. No "it depends" dump.

---

## Architecture

```
  Phone / browser
        |  barcode, name, or photo
        v
  FastAPI  ─────────────────────────────────────────────┐
        |                                               |
        |  1. Resolve product                           |
        |     Barcode Lookup → Open Food Facts          |
        |     → Open Beauty Facts → Open Products Facts |
        |     → openFDA (drugs)                         |
        |     UPC-A / EAN-13 variants tried in parallel |
        |                                               |
        |  2. Clean ingredients                         |
        |     OCR junk filter, INCI aliases,            |
        |     mine known actives from garbage labels    |
        |                                               |
        |  3. Verdict engine                            |
        |     Gemini Flash (structured JSON)            |
        |     + rules.json (strict-only guardrails)     |
        |     + SQLite cache + coverage-gap log         |
        |     + openFDA pregnancy-section enrichment    |
        v                                               |
  SAFE | CAUTION | AVOID | UNKNOWN  <───────────────────┘
       product verdict = worst ingredient
```

One process serves the API and the static client (`web/`) on port 8000.

---

## Engineering decisions 

These are the choices that would transfer to a production safety, search, or applied-ML system.

**Fail closed. Never default to Safe.**
No evidence → `UNKNOWN` or `CAUTION`. LLM timeout, bad JSON, or missing ingredient list cannot produce Safe. A product *name* can prove danger ("Retinol Serum" → Avoid); it can never prove safety (Safe is downgraded to Unknown, confidence capped).

**Hybrid reasoner + monotonic guardrails.**
Gemini Flash reasons over the full ingredient list, stage-aware, in one batched call. A deterministic `rules.json` (36 entries: retinoids, NSAIDs, ACE inhibitors, high-mercury fish, abortifacient herbs, …) can **only raise** risk, never lower it. Known teratogens do not depend on model mood.

**Structured output, then a second chance, then honest failure.**
The model is constrained to a JSON schema. If the provider rejects strict schema, retry as `json_object`. If that fails, degrade to rules-only and mark the rest Unknown. Partial analysis is treated as worse than an honest miss — a failed chunk aborts the LLM path.

**Cache the good answers, never the degraded ones.**
Verdicts are keyed on `(ingredients, stage, model, rules version)` in SQLite. Repeat scans return in ~1ms. Fail-closed results are not cached, so a later healthy reasoner call can fill them in. Unknown ingredients are logged with hit counts (`GET /coverage`) so the next rule to write is the one users actually hit.

**Barcode resolution is a data problem, not a camera problem.**
UPC-A vs EAN-13 leading-zero mismatch is the #1 miss. Every code is expanded to variants and tried against four product databases. Ingredient lists that are actually Drug Facts OCR dumps are stripped; known actives (ibuprofen, retinol, …) are mined out of the junk.

**Product verdict is a lattice, not a score.**
`AVOID > CAUTION > UNKNOWN > SAFE`. One bad ingredient wins. Confidence is the *minimum* across ingredients (rules 0.95 / model 0.7 / fail-closed 0.3), not an average that can hide a teratogen.

**The contract is the product.**

```
POST /scan
{ "barcode": "0123456789012", "stage": "trimester_1" | "trimester_2" | "trimester_3" | "breastfeeding" }

→ product_name, verdict, flagged_ingredients[], reasoning, sources, confidence
```

Frontend, resolver, and engine were built against this shape. Extra fields (`confidence`, `stage_notes`, `fda_label`) are additive.

---

## Stack

| Layer | Choice | Why |
|---|---|---|
| Client | Vanilla HTML/CSS/JS | Zero build step, camera via `getUserMedia` + `BarcodeDetector`, works on a phone against the LAN |
| API | FastAPI + Pydantic + httpx | Typed contract, parallel source fan-out, one-port static+API serve |
| Reasoner | Gemini Flash via OpenRouter | Stage-aware clinical prose; temperature 0; schema-constrained |
| Guardrails | `rules.json` | Deterministic, reviewable, monotonic |
| Store | SQLite | Verdict cache, scan history, coverage gaps — no extra infra |
| Sources | Open Food / Beauty / Products Facts, Barcode Lookup, openFDA | Food, cosmetics, and drugs in one scan path |

---

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/scan` | Barcode → product → verdict |
| `POST` | `/scan-name` | Name / active-ingredient verdict (no barcode, e.g. an FDA drug hit) |
| `GET` | `/search?q=` | Parallel search across product DBs + openFDA |
| `GET` | `/health` | Which keys, model, and sources are live |
| `GET` | `/coverage` | Most-scanned unclassified ingredients |
| `GET` | `/history` | Recent checks, deduped by barcode |

---

## Run locally

```bash
cd api
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Optional `.env` in `api/`:

```
OPENROUTER_API_KEY=...          # enables Gemini reasoning; without it, rules-only + Unknown
BARCODELOOKUP_API_KEY=...       # wider barcode coverage; Open Facts still work without it
```

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

Open [http://localhost:8000](http://localhost:8000). Camera needs localhost or HTTPS.

Smoke test (Nutella):

```bash
curl -s -X POST localhost:8000/scan \
  -H "Content-Type: application/json" \
  -d '{"barcode":"3017624010701","stage":"trimester_1"}'
```

Standalone engine:

```bash
cd api && python verdict_engine.py
```

---

## Repo

```
SafeMothers/
├── api/
│   ├── main.py            # resolution, search, openFDA enrichment, history
│   ├── verdict_engine.py  # reasoner + guardrails + cache
│   ├── rules.json         # strict-only safety rules
│   └── requirements.txt
└── web/
    └── index.html         # landing → stage → scan → verdict
```

---

## Disclaimer

Informational screening — **not medical advice**. Verdicts are a first-pass screen against public databases and a constrained language model, with deterministic rules on known high-risk ingredients. They are not a diagnosis, an FDA determination, or a substitute for a clinician.
