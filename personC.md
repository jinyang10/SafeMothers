# Person C — Gemini Flash Reasoner + Safety Guardrails

**Goal (15 min):** `verdict_engine.py` exposing one function. **Gemini Flash (via OpenRouter) is the native reasoner** — every scan goes through it for stage-aware clinical reasoning. A small `rules.json` acts as a deterministic guardrail layer that can only make the verdict stricter, never looser. This module is the product's moat.

Read `PROJECTCONTEXT.md` first, especially the safety policy: **never default to SAFE, fail closed to UNKNOWN.**

## Interface (hand to Person B at minute 5)

```python
def get_verdict(ingredients: list[str], stage: str) -> dict:
    # returns: { "verdict", "flagged_ingredients", "reasoning", "sources" }
    # per the contract in PROJECTCONTEXT.md (everything except product_name)
```

## How the engine works (reasoner-first, guardrails on top)

1. **One batched Gemini Flash call** with the full ingredient list + stage → per-ingredient risk, reason, evidence basis, plus a product-level `reasoning` summary. The LLM writes the reasoning the user reads.
2. **Guardrail pass:** match ingredients against `rules.json`. A rule hit **overrides the LLM only in the stricter direction** (`AVOID > CAUTION > UNKNOWN > SAFE`). If Gemini says CAUTION but the rule says AVOID → AVOID. If Gemini says AVOID but the rule says SAFE → stays AVOID. Known teratogens must never depend on model mood.
3. **Fail closed:** LLM call fails (HTTP error, timeout, bad JSON) → rules-only verdict; ingredients with no rule hit become UNKNOWN and `reasoning` = "Could not complete AI analysis; showing database matches only."

## Steps

### 1. The Gemini Flash call (min 0–8) — this is the core, build it first

OpenRouter chat completions, model slug `google/gemini-flash-latest` (verify the exact Flash slug available to your key at openrouter.ai/models — payload shape is identical regardless):

```python
import httpx, os, json

def call_reasoner(ingredients: list[str], stage: str) -> dict | None:
    try:
        r = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}",
                "HTTP-Referer": "https://github.com/Waleeeeed88/SafeMothers",
                "X-Title": "Expecta",
            },
            json={
                "model": "google/gemini-flash-latest",
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": json.dumps({"stage": stage, "ingredients": ingredients})},
                ],
            },
            timeout=25,
        )
        r.raise_for_status()
        out = json.loads(r.json()["choices"][0]["message"]["content"])
        assert "results" in out and "summary" in out
        return out
    except Exception:
        return None  # caller fails closed
```

System prompt — the medical-caution encoding, copy verbatim:

> You are a clinical pregnancy-safety reasoner. Input is JSON with "stage" (trimester_1/2/3 or breastfeeding) and "ingredients". Return ONLY JSON:
> {"results":[{"name","risk":"SAFE|CAUTION|AVOID|UNKNOWN","reason","source"}], "summary":"2 plain sentences a worried mother can read at a store shelf"}
> Rules: reason about the SPECIFIC stage — thresholds differ by trimester and again in breastfeeding, and say so when relevant. "source" must name a real evidence basis ("FDA category", "LactMed", "ACOG guidance", "no human data"). If evidence is absent or conflicting you MUST return UNKNOWN or CAUTION — never SAFE. Never invent studies. One sentence per reason. The summary must state the single most important ingredient driving the verdict.

### 2. rules.json guardrails (min 8–13) — ~25 entries, worst offenders only

Since Gemini handles breadth, the rules only need the ingredients where a wrong SAFE is unacceptable:

```json
{
  "match": ["retinol", "retinyl", "retinal", "tretinoin", "isotretinoin"],
  "pregnancy": "AVOID",
  "breastfeeding": "CAUTION",
  "reason": "Vitamin A derivative; teratogenic risk",
  "source": "FDA Category X class"
}
```

- **Skincare:** retinoids (above), hydroquinone, formaldehyde, phthalate/DBP, methotrexate — AVOID. Salicylic acid, oxybenzone — CAUTION.
- **Food:** alcohol/ethanol, unpasteurized, swordfish/king mackerel/tilefish/shark — AVOID. Caffeine, licorice root, ginseng, black cohosh, dong quai — CAUTION.
- **Meds:** ibuprofen CAUTION with `"stage_overrides": {"trimester_3": "AVOID"}`, aspirin CAUTION, pseudoephedrine CAUTION.

Substring match on lowercased ingredients. Pick `pregnancy` vs `breastfeeding` field by stage, apply `stage_overrides`.

### 3. Merge + aggregate (min 13–15)

- Per ingredient: `final = stricter_of(llm_risk, rule_risk)`; source `"rules"` when a rule decided, else `"gemini-flash"`.
- Product verdict = worst across ingredients (`AVOID > CAUTION > UNKNOWN > SAFE`).
- `flagged_ingredients` = everything not SAFE. `reasoning` = Gemini's `summary` (or the fail-closed line). `sources` = deduped.
- Test standalone before handoff:
  `get_verdict(["water", "retinol", "polyglyceryl-4 caprate"], "trimester_1")` → AVOID, retinol from rules, obscure ester reasoned by Gemini.
  `get_verdict(["water", "caffeine"], "breastfeeding")` → CAUTION with a breastfeeding-specific reason.

## Don't build

Embeddings/vector search, LactMed scraping, caching, multi-step agent chains, per-ingredient LLM calls. One batched reasoner call + one guardrail pass.
