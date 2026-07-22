# Person A — Mobile Scan Flow (Expo)

**Goal (15 min):** app in Expo Go that scans a barcode, POSTs to `/scan`, renders a color-coded verdict. Two screens, no polish.

Read `PROJECTCONTEXT.md` first for the request/response contract.

## Steps

### 1. Scaffold (min 0–3)

```bash
npx create-expo-app expecta-app --template blank
cd expecta-app
npx expo install expo-camera
npx expo start
```

Put everything in `App.js`. Do not add navigation libraries — use a single `useState` to switch between "scan" and "result" views.

### 2. Scan view (min 3–8)

- `CameraView` from `expo-camera`, full screen, `onBarcodeScanned` with `barcodeTypes: ["ean13", "upc_a", "ean8"]`.
- Debounce: after first scan fires, set a `scanned` flag so it doesn't fire 30 times.
- Stage selector: 4 buttons in a row at the top — `T1 / T2 / T3 / Nursing` — mapping to `trimester_1 | trimester_2 | trimester_3 | breastfeeding`. Plain `useState`, default `trimester_1`.

### 3. Fetch + verdict view (min 8–13)

```js
const API_URL = "http://<PERSON_B_LAN_IP>:8000"; // get this at minute 5

const res = await fetch(`${API_URL}/scan`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ barcode, stage }),
});
```

Verdict view renders:
- Full-width banner colored by verdict: SAFE green, CAUTION amber, AVOID red, UNKNOWN gray. Verdict word in huge text, product name under it.
- `flagged_ingredients` as simple rows: name, risk, reason.
- `reasoning` paragraph.
- Fixed footer text: **"Informational screening — not medical advice."**
- "Scan again" button → reset to scan view.

Loading state: plain `ActivityIndicator`. On fetch error or 15s timeout: show UNKNOWN banner with "Couldn't check this product" + retry.

### 4. Test (min 13–15)

Scan any real barcode (water bottle, snack) against Person B's live endpoint. If B isn't ready, hardcode a mock response object and verify rendering.

## Don't build

History, AsyncStorage, label photo capture, animations, onboarding. A stage selector + scan + verdict is the whole app today.
