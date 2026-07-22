# Expecta — 45-Minute MVP Build Plan

Barcode scanner for pregnancy/breastfeeding product safety. Scan a product → resolve ingredients → verdict with reasoning. Think "halal scanner" mechanics, but the check is pregnancy safety and the verdict changes with pregnancy stage.

**Time box: ~15 minutes of build per person, 3 people in parallel, integrate in the last 10 minutes. Cut anything that threatens the demo.**

## Architecture

```
[Expo app] --barcode+stage--> [FastAPI /scan] --> Open Food Facts / Open Beauty Facts (barcode -> ingredients)
                                    |
                                    v
                        verdict_engine.get_verdict()
                        1. Gemini Flash via OpenRouter = native reasoner
                           (one batched call: per-ingredient risk + stage-aware summary)
                        2. rules.json guardrails (~25 known-bad entries) can only
                           make a verdict STRICTER, never looser
                                    |
                                    v
                              JSON verdict -> app
```

Gemini Flash does the reasoning for every scan; the rules layer exists so known teratogens (retinoids, alcohol, etc.) never depend on model output. If the LLM call fails, the engine falls back to rules-only and marks the rest UNKNOWN.

## The one contract everyone codes against (agree now, never change)

```
POST /scan
Request:  { "barcode": "0123456789012", "stage": "trimester_1" | "trimester_2" | "trimester_3" | "breastfeeding" }

Response:
{
  "product_name": "string",
  "verdict": "SAFE" | "CAUTION" | "AVOID" | "UNKNOWN",
  "flagged_ingredients": [
    { "name": "retinol", "risk": "AVOID", "reason": "Vitamin A derivative, teratogenic", "source": "rules" }
  ],
  "reasoning": "1-2 plain sentences",
  "sources": ["Open Beauty Facts", "rules v1", "gemini-flash"]
}
```

## Non-negotiable safety policy

- No evidence = `UNKNOWN` or `CAUTION`. **Never default to SAFE.**
- LLM call fails or returns garbage → those ingredients are `UNKNOWN` (fail closed).
- Product verdict = worst ingredient verdict: `AVOID > CAUTION > UNKNOWN > SAFE`.
- Verdict screen shows "Informational screening — not medical advice." from build one.

## Who builds what

| Person | File | Deliverable in 15 min |
|--------|------|----------------------|
| A | `personA.md` | Expo app: scan barcode → call `/scan` → designed verdict screen (design spec + tokens included in the file) |
| B | `personB.md` | FastAPI: barcode → Open Food/Beauty Facts → engine → JSON response |
| C | `personC.md` | Gemini Flash reasoner via OpenRouter + `rules.json` guardrails inside `get_verdict()` |

Design direction (Person A owns it): **calm clinical-warm** — warm paper background, one huge serif verdict word, full-screen verdict color wash. Tokens live in `personA.md`.

## Timeline (45 min total)

- **0–5:** All: confirm contract above, Person B shares LAN IP / tunnel URL, Person C shares `get_verdict(ingredients: list[str], stage: str) -> dict` signature with B.
- **5–25:** Parallel build per the three files.
- **25–35:** Integrate: C's module drops into B's `api/`, A points app at B's URL. Test with 2 real barcodes (one food, one skincare).
- **35–45:** Fix whatever broke, demo run-through.

## Repo layout

```
SafeMothers/
├── app/        # Person A (Expo)
├── api/        # Person B (FastAPI) + Person C's verdict_engine.py + rules.json
│   └── .env    # OPENROUTER_API_KEY — gitignored, never committed
```

## Cut list (do NOT build today)

Label photo OCR, auth, subscriptions, history screen, user profiles, LactMed API integration, deployment. Stub or skip all of it.
