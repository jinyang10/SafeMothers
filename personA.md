# Person A — Mobile App: Scan Flow + Frontend Design (Expo)

> **STATUS: BUILT AS A BROWSER APP AND TESTED LIVE** — see `web/index.html` (frontend) + `api/main.py` (backend). Run `uvicorn main:app --host 0.0.0.0 --port 8000` from `api/`, then open `http://localhost:8000` (or your LAN IP from a phone; camera needs HTTPS or localhost).
>
> - **Single-page web app**, no build step: camera barcode scanning via html5-qrcode (EAN-13/UPC-A/EAN-8/UPC-E), the full design spec below implemented as CSS tokens — warm paper bg, Fraunces/Inter, stage pills, full-screen verdict wash, flagged-ingredient cards, pulsing loader, disclaimer footer.
> - **Search any product**: the search box hits `GET /search?q=` — Barcode Lookup API when `BARCODELOOKUP_API_KEY` is set, otherwise Open Food Facts then Open Beauty Facts. Typing 8–14 digits into the box checks it directly as a barcode. Tap a result → verdict.
> - **Backend** `POST /scan`: resolves via Barcode Lookup → Open Food Facts → Open Beauty Facts, then runs Person C's `get_verdict()`. If a product has no ingredient list, the product NAME is analyzed as a last resort — it can prove danger ("Retinol Serum" → AVOID) but never safety (SAFE downgrades to UNKNOWN, confidence capped at 0.4).
> - Verified live: Nutella scan → SAFE with listeria-aware reasoning; "retinol serum" search → AVOID from name analysis; camera-less fallback message points to search.

**Goal (15 min core + 5 min polish):** app in Expo Go that scans a barcode, POSTs to `/scan`, renders a designed verdict screen — not a debug screen.

Read `PROJECTCONTEXT.md` first for the request/response contract.

## Design spec (commit to this, don't improvise)

**Direction: calm clinical-warm.** This app is opened by an anxious person in a store aisle. The UI's job is to lower heart rate: warm paper background, one huge unambiguous verdict, soft full-screen color wash — no purple gradients, no glass cards, no confetti.

Design tokens — paste as `theme.js` and import everywhere:

```js
export const theme = {
  bg: "#FAF6F0",          // warm paper, never pure white
  ink: "#1F2933",          // near-black text
  inkSoft: "#6E7A87",      // secondary text
  card: "#FFFFFF",
  verdict: {
    SAFE:    { main: "#2E7D5B", wash: "#E7F2EC", label: "Safe" },
    CAUTION: { main: "#B45309", wash: "#FBF0E1", label: "Caution" },
    AVOID:   { main: "#B3382E", wash: "#F9E9E7", label: "Avoid" },
    UNKNOWN: { main: "#5B6672", wash: "#EDEFF2", label: "Unknown" },
  },
  radius: 16,
  spacing: 16,
};
```

Typography: `Fraunces` (serif display) for the verdict word and product name, `Inter` for everything else — `npx expo install @expo-google-fonts/fraunces @expo-google-fonts/inter expo-font`. If fonts eat more than 3 minutes, ship system fonts and move on; layout and color carry the design.

**Screen layouts:**

- **Scan screen:** camera full-bleed. Top: stage selector as 4 pill buttons (`T1 · T2 · T3 · Nursing`) on a translucent dark strip — active pill filled white, ink text; inactive pills outlined white. Center: rounded-corner reticle (a `View` with border, `theme.radius`, ~70% width) with the line "Point at a barcode" beneath in white `Inter`. Bottom: small wordmark "expecta" lowercase.
- **Verdict screen:** entire screen background = `verdict.wash`, everything sits on it (this is the memorable move — the whole room changes color, no banner needed).
  1. Verdict word in `Fraunces`, ~64pt, `verdict.main` — "Avoid", "Safe", etc.
  2. Product name under it, 20pt `Fraunces`, `ink`.
  3. One-line stage context in `inkSoft`: "Checked for: 1st trimester".
  4. `reasoning` paragraph, 16pt `Inter`, `ink`, max ~3 lines.
  5. Flagged ingredients as white cards (`card`, `radius`, subtle shadow): ingredient name bold + small risk chip in that risk's `main`/`wash` colors on the right, `reason` in `inkSoft` below.
  6. Pinned footer: "Informational screening — not medical advice." 12pt `inkSoft`, and a full-width "Scan again" button — `ink` background, white text, `radius`.
- **Loading state:** wash-gray screen, pulsing text "Checking ingredients…" (loop `Animated` opacity 0.4→1). No spinner-on-white.
- **Error/timeout (15s):** UNKNOWN styling, "Couldn't check this product", retry button.

**Feel:** `expo-haptics` medium impact on successful barcode read; verdict screen fades in (`Animated` opacity 250ms). Skip anything fancier.

## Build steps

### 1. Scaffold (min 0–3)

```bash
npx create-expo-app expecta-app --template blank
cd expecta-app
npx expo install expo-camera expo-haptics @expo-google-fonts/fraunces @expo-google-fonts/inter expo-font
npx expo start
```

Single `App.js`, one `useState` for view (`"scan" | "loading" | "result"`), plus `theme.js`. No navigation library.

### 2. Scan screen (min 3–8)

- `CameraView` from `expo-camera`, `onBarcodeScanned`, `barcodeTypes: ["ean13", "upc_a", "ean8"]`.
- Set a `scanned` flag on first fire so it doesn't trigger 30 times; haptic on read.
- Stage pills → `trimester_1 | trimester_2 | trimester_3 | breastfeeding`, default `trimester_1`.

### 3. Fetch + verdict screen (min 8–14)

```js
const API_URL = "http://<PERSON_B_LAN_IP>:8000"; // get at minute 5

const res = await fetch(`${API_URL}/scan`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ barcode, stage }),
});
```

Render the verdict screen exactly per the design spec above — the tokens make this mostly mechanical.

### 4. Test (min 14–15), polish pass (min 15–20 if clock allows)

Scan a real barcode against Person B's endpoint (B has a stub engine early — integrate before C lands). If B isn't up, hardcode one mock AVOID response and verify every element of the verdict layout.

Polish order if time remains: fonts loaded → fade-in animation → pill press states. Cut from the bottom.

## Don't build

History, AsyncStorage, label photo capture, onboarding, dark mode, tab bars. Stage pills + scan + designed verdict is the whole app today.
