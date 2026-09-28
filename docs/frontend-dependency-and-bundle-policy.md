# Frontend dependency and bundle policy

## Browser model and audio runtimes

`@huggingface/transformers`, `@mlc-ai/web-llm`, `onnxruntime-web`, and
`@ricky0123/vad-web` are exact-version pins. They are security- and
compatibility-sensitive browser execution dependencies, not routine UI
packages. Their code is loaded only from the recording or model-settings
features; importing a settings/dashboard route must not fetch a runtime or a
model asset.

Review them monthly, and within two business days of a relevant high/critical
advisory. Each update must include: release-note review, `npm audit`, the
focused recording/offline-transcription tests, a production build, and a
manual model-load check on a supported WebGPU browser. Do not use `npm audit
fix --force` for these packages.

## Bundle budget

`npm run check:bundle-budget` builds the production app and fails when the
JavaScript referenced directly by `dist/index.html` exceeds 650 KiB gzip. The
budget intentionally measures only initial assets: large model, graph, and
report code may be lazy, but must never return to the bootstrap graph.

An intentional exception requires a linked issue or release approval:

```bash
MEETING_OPS_BUNDLE_BUDGET_OVERRIDE='P-00055 approved by <name>: <reason>' npm run check:bundle-budget
```

The override is visible in CI output and is not a permanent configuration.

## Current audit disposition

`npm audit --omit=dev` was run on 2026-07-24 after the lockfile update. It
reports the findings below. `jspdf` was removed because no source path imports
it; `posthog-js`, `react-router-dom`, `postcss`, and the direct `sharp` build
helper were updated with focused tests and a production build. Do not use
`npm audit fix --force` to resolve the remaining tree.

| Finding group | Severity | Exploitability and owner | Bounded follow-up |
| --- | --- | --- | --- |
| `@huggingface/transformers` → `sharp` | High | The affected `sharp@0.34.5` is nested under Transformers; it is not emitted in the browser bundle. Owner: browser-model runtime lane. | Re-evaluate on the next monthly runtime review; upgrade Transformers only after model-load regression tests. |
| Transformers → `onnxruntime-node` → `tar` | Critical | Node-only ONNX support is installed by Transformers but is not imported by the Vite browser build. Exploit needs a process to parse attacker-controlled tar input. Owner: browser-model runtime lane. | Remove or update the Node-only path when a compatible Transformers release is verified; target next runtime review. |
| `onnxruntime-web` / Transformers → `protobufjs` | High | Browser runtime parses only model assets selected from controlled model URLs today. Owner: browser-model runtime lane. | Verify a compatible ONNX/Transformers upgrade with offline transcription before the next release. |
| `react-router-dom` → `react-router` | High | Current advisory covers RSC/server-action code paths. Meeting-Ops is a client-only `HashRouter` SPA and does not enable RSC, but the package remains shipped. Owner: frontend platform lane. | Watch upstream for a patched non-RSC release; retest auth and deep links before upgrading. |
| `posthog-js` → `dompurify` | Moderate | Telemetry runs client-side; no Meeting-Ops sanitizer configuration is supplied to it. Owner: telemetry lane. | Update when PostHog adopts a patched DOMPurify version; validate analytics initialization. |
| `glob`, `minimatch`, `brace-expansion`, `picomatch`, `yaml` | High / Moderate | Reached through Tailwind, Vite, PWA, and lint/build tooling; not shipped in the static browser artifact. Exploit requires a developer/CI build processing hostile patterns or config. Owner: frontend tooling lane. | Upgrade the owning build tools in a dedicated toolchain compatibility pass within 30 days. |

The committed lockfile is the source of truth for exact resolved versions.
