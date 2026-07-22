# Person C — Safety Rules + Reasoning Agent

**Goal (15 min):** `verdict_engine.py` exposing one function, backed by a ~40-entry `rules.json` and a single OpenRouter Gemini Flash call for unmatched ingredients. This module is the product's moat — everything else is plumbing.

Read `PROJECTCONTEXT.md` first, especially the safety policy: **never default to SAFE, fail closed to UNKNOWN.**

## Interface (hand to Person B at minute 5)

```python
def get_verdict(ingredients: list[str], stage: str) -> dict:
    # returns: { "verdict", "flagged_ingredients", "reasoning", "sources" }
    # per the contract in PROJECTCONTEXT.md (everything except product_name)
```

## Steps

### 1. rules.json (min 0–7) — don't gold-plate, 40 entries beats 100

Entry shape:

```json
{
  "match": ["retinol", "retinyl", "retinal"],
  "pregnancy": "AVOID",
  "breastfeeding": "CAUTION",
  "reason": "Vitamin A derivative; teratogenic risk",
  "source": "FDA Category X class"
}
```

Matching = substring against normalized ingredient strings. Cover these buckets:

- **Skincare AVOID:** retinol/retinyl/retinal/tretinoin/isotretinoin, hydroquinone, formaldehyde, phthalate/DBP, methotrexate.
- **Skincare CAUTION:** salicylic acid, benzoyl peroxide, oxybenzone/avobenzone, essential oils (clary sage, rosemary), glycolic acid (SAFE — include positive entries too).
- **Food AVOID:** alcohol/ethanol, unpasteurized (milk/cheese), raw fish markers, high-mercury species (swordfish, king mackerel, tilefish, shark).
- **Food CAUTION:** caffeine, aspartame/saccharin, liver/high vitamin A, herbal extracts (ginseng, black cohosh, dong quai), licorice root.
- **Meds on labels:** ibuprofen (CAUTION → AVOID if stage == trimester_3 — handle with an optional `"stage_overrides": {"trimester_3": "AVOID"}` key), aspirin (CAUTION), pseudoephedrine (CAUTION), acetaminophen/paracetamol (SAFE at label doses).
- **Common SAFE entries** so everything isn't amber: water/aqua, glycerin, sugar, citric acid, salt, hyaluronic acid, niacinamide, zinc oxide, titanium dioxide, vitamin c/ascorbic acid, folic acid.

Pick the `pregnancy` or `breastfeeding` verdict based on `stage`, apply `stage_overrides` if present.

### 2. LLM fallback for unmatched ingredients (min 7–13)

One batched call for ALL unmatched ingredients (never per-ingredient):

```python
import httpx, os, json

resp = httpx.post("https://openrouter.ai/api/v1/chat/completions",
    headers={"Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}"},
    json={
        "model": "google/gemini-flash-latest",
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user_msg}],
    }, timeout=20)
```

System prompt (the medical-caution encoding — copy verbatim):

> You are a clinical pregnancy-safety screener. For each ingredient and the given stage (trimester_1/2/3 or breastfeeding), return JSON: {"results":[{"name","risk":"SAFE|CAUTION|AVOID|UNKNOWN","reason","source"}]}. Risk basis must be real (e.g., "FDA category", "LactMed", "no human data"). If evidence is absent or conflicting you MUST return UNKNOWN or CAUTION — never SAFE. Never invent studies. One sentence per reason.

Parse with `json.loads` inside try/except. **Any failure (HTTP error, timeout, bad JSON, missing keys) → all unmatched ingredients become UNKNOWN.** Tag rule hits `"source": "rules"`, LLM results `"source": "gemini-flash"`.

### 3. Aggregate (min 13–15)

- Product verdict = worst across all ingredients: `AVOID > CAUTION > UNKNOWN > SAFE`.
- `flagged_ingredients` = everything that isn't SAFE.
- `reasoning` = template string, e.g. `"Avoid: contains retinol (vitamin A derivative). 2 other ingredients need caution."`
- `sources` = deduped list from the results.
- Test standalone before handoff: `get_verdict(["water", "retinol", "some-obscure-polymer"], "trimester_1")` → expect AVOID, retinol flagged from rules, polymer resolved by LLM or UNKNOWN.

## Don't build

Embeddings/vector search, LactMed scraping, caching, multi-step agent chains. One rules pass + one LLM call.
