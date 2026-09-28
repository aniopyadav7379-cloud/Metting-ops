# Phase C-1: Native iOS App with Core ML and watchOS Extension

Status: Draft for approval. Doc-only work; no code in this commit.
Implementation tickets at the end of this doc.
Owner: Meeting-Ops team.
Authoring date: 2026-05-22.

## 1. TL;DR

Phase C-1 is the native iOS app for Meeting-Ops. It replaces the browser
PWA path on iPhone and iPad with a Swift + SwiftUI binary that runs
Parakeet 0.6B for live STT and a small LLM (either Apple's Foundation
Models 3B if the user has Apple Intelligence on, or our own bundled
Qwen 3 0.6B / Gemma 4 E2B via Core ML or mlx as a fallback) for rolling
summary, both on the Apple Neural Engine. It uses the same backend
endpoints the desktop browser and PWA already hit, the same Keycloak
`uchub` realm for auth, and the same Garage S3 for chunk storage. What
it adds is everything the browser couldn't give us on a phone:
background recording that doesn't die when the screen locks, lock-screen
and Dynamic Island controls, Siri shortcuts, Spotlight-indexed
transcripts, share-extension intake from Voice Memos and Files,
home-screen widgets, and a watchOS companion that turns an Apple Watch
into a one-tap recorder. The watchOS extension is a separate phase
(C-2), but C-1 lays the foundation: shared bundle, shared protocol
definitions, shared file paths, shared sync code.

This is a months-long commitment, not a sprint. Realistic ship to a
closed TestFlight beta is 8 to 10 weeks of focused work from kickoff.
App Store review for a new audio-recording app with on-device ML adds
another 1 to 2 weeks of polish + review-cycle slack. The doc below
scopes the work, picks the stack, picks the Core ML conversion paths,
walks through the native UX surfaces, and ends with a phased plan
(C-1.1 through C-1.5) Aaron can budget against. The compute-economics
moat from `docs/compute-economics.md` carries forward without
modification: live STT and live summary run on the user's phone, server
GPU minutes are still only spent on (1) Pro/Enterprise mobile users who
opt into server-live via Phase B and (2) the final high-quality
reprocess pass at session completion. The native app does not increase
our per-user GPU cost.

## 2. Why native after browser is mature

Phase A (shipped v0.8.0) and Phase A.6 (shipped) collectively did
everything they could do in a mobile browser. Capture-only on iOS Safari
with chunked uploads, IndexedDB persistence on desktop, on-device
Parakeet 0.6B + Qwen 3 0.6B in privacy mode via onnxruntime-web +
transformers.js + WebGPU. The desktop browser story is excellent. The
mobile browser story is honest but compromised, and the compromises are
load-bearing structural limits of the platform, not bugs we can fix.

The structural limits, in the order they bite:

- **MediaRecorder lifecycle on iOS Safari is unreliable in the background.**
  When the user switches apps, locks the phone, or even just lets the
  screen go dark, Safari throttles or suspends the recorder. Phase A
  tolerates this by setting the expectation that recordings are
  best-effort; v0.8.0 added a "tab is in background" amber banner so
  the user at least knows. A native app gets `AVAudioSession` with the
  `.playAndRecord` category and the `audio` background mode in
  Info.plist, which iOS treats as a first-class background task. The
  app keeps recording across lock, app switch, and even multi-hour
  meetings.

- **Origin storage caps on iOS Safari evict model weights under
  pressure.** Parakeet 0.6B INT8 + Qwen 3 0.6B INT8 together push the
  ceiling on Safari's origin quota, and the next time Safari decides
  it needs the bytes back, the weights are gone. The user pays the
  redownload cost on next launch. A native app puts the models in the
  app's `Documents` or `Application Support` directory, which iOS
  treats as user data and backs up to iCloud; nothing evicts them
  short of the user uninstalling.

- **WebGPU on iOS Safari is real but not fast enough for live STT.**
  WASM fallback on the iPhone 15 Pro is roughly real-time-borderline
  for Parakeet 0.6B; on older devices it's worse. Core ML on the same
  silicon, with the model compiled for the Apple Neural Engine, runs
  at roughly 5 to 10 times the throughput we get from the browser path.
  Real numbers: FluidInference's `parakeet-tdt-0.6b-v3-coreml` measures
  approximately 110x real-time factor on an M4 Pro in batch mode,
  meaning one minute of audio in roughly 0.5 seconds of wall time. The
  iPhone 15 Pro and 16 Pro Max are close to that.

- **Battery and thermals.** A Safari tab running Parakeet + Qwen
  continuously is a real heater. We see 25 to 40 percent battery
  drained per hour of recording on real iPhones in field testing.
  Native Core ML on the ANE is closer to 10 to 15 percent per hour
  because the ANE is the most power-efficient inference path on the
  chip.

- **No background tab JS.** Safari aggressively throttles JS in
  background tabs. Even with the screen on, an iPhone in background
  tab mode drops Parakeet throughput to a fraction of real-time, which
  means live captions stall mid-meeting and never catch up.

What native unlocks on top of fixing those four:

- **Lock-screen media controls** via `MPNowPlayingInfoCenter` and
  `MPRemoteCommandCenter`. Pause/stop the recording from the lock
  screen, from Control Center, from CarPlay, from the AirPods stem,
  and from a paired Apple Watch.
- **Siri shortcuts** via `INVoiceShortcut`. "Hey Siri, start a Meeting-Ops
  recording" turns the phone into a one-sentence recorder.
- **Spotlight indexing** via `CoreSpotlight`. Search "Hina Khan budget
  meeting May 4" from the home screen and land in the session detail
  view.
- **Share extension.** Receive audio files from Voice Memos, Files,
  Mail, AirDrop, and a hundred other apps; convert them into
  Meeting-Ops sessions with the same filename parser the web upload
  page uses.
- **Home-screen widgets** showing the latest meetings and a "Quick
  Record" button.
- **Live Activities** in the Dynamic Island on iPhone 14 Pro+ so the
  recording indicator is visible even while the user is in another
  app.
- **Apple Watch app**, which is literally impossible without native
  (watchOS has no browser). C-2 builds this; C-1 sets the
  shared-bundle foundation.
- **AirDrop and Files integration.** Receive dropped audio files and
  route them through the same session pipeline.
- **App Intents** so other apps and Shortcuts.app can drive Meeting-Ops
  programmatically.

None of those are nice-to-haves. The Watch goal alone forces native.
Once we accept that we're shipping native iOS regardless, all the other
surfaces fall into the same project for roughly the same effort.

## 3. Stack choice: Swift + SwiftUI

The options, with the case for each:

### Swift + SwiftUI + Xcode (native)

Apple's recommended path. SwiftUI is mature enough in 2026 to build the
whole app's UI without falling back to UIKit for most screens; we'll
still need UIKit views in a few corners (the audio recorder visualizer,
the share-extension view controller, possibly the Spotlight result
preview). Xcode is the only IDE that signs apps for App Store
distribution, the only one that runs the watchOS simulator, the only
one that profiles Core ML kernel timings via Instruments. Requires a
Mac for development. Aaron has a Mac Studio + multiple Macs available;
this is not a constraint for us.

The deepest integration with Core ML, mlx, Foundation Models, Live
Activities, App Intents, AVAudioEngine, AVAudioSession, WatchConnectivity,
CoreSpotlight, INVoiceShortcut, MPNowPlayingInfoCenter,
MPRemoteCommandCenter, WidgetKit, ASWebAuthenticationSession. Every
single API mentioned in this document is Swift-first or Swift-only.

### Flutter

Cross-platform with one codebase. We get Android for free if we go this
route. But:

- Core ML integration in Flutter is a third-party plugin
  (`flutter_coreml` or similar) that adds a layer between us and the
  bytes. Parakeet streaming inference at <200ms first-word latency is
  tight enough that any extra layer is a risk we can't take on day one.
- AVAudioSession and the audio background mode work via plugins
  (`record` or `flutter_sound`) that we'd have to evaluate per quirk.
  Phase A's MP4-first-on-Safari saga is a preview of what we'd be
  signing up for; each iOS audio quirk is now indirect.
- watchOS apps in Flutter are not supported. There is no production
  story for shipping a watchOS extension from a Flutter codebase.
  Independent watchOS apps technically exist in Flutter via a
  community add-on, but it's not ready for what we need.
- Live Activities, App Intents, Siri shortcuts, Spotlight, share
  extensions, widgets: all third-party plugins, varying maturity, no
  consolidated story.

Flutter is the right choice for an app that needs to be on Android and
iOS day one with the same UX, doesn't use platform-specific advanced
APIs, and doesn't have a Watch story. That's not us. We're shipping
Android in C-3 separately because Android has its own platform-specific
on-device-ML story (NNAPI / GenAI / MediaPipe) and its own Watch story
(Wear OS, which we may or may not pursue).

### React Native

JS team would feel at home. We have a TypeScript-heavy frontend already
and the React Native bridge is well-trodden. But the same arguments
that disqualify Flutter apply, with the additional drawback that React
Native's iOS audio story is even thinner than Flutter's. Bridging
AVAudioEngine to JS over the React Native bridge for 16kHz PCM frames
at 100ms cadence is technically possible and has been done; it's not
what we want to be debugging at 11pm before a TestFlight beta.

### Capacitor / Tauri / Native WebKit shell

Keeps the existing PWA codebase, wraps it in a native shell. Solves
nothing. The browser limits in section 2 are still browser limits;
WKWebView has the same MediaRecorder quirks, the same origin storage
caps, the same WebGPU/WASM constraints. We'd ship a 30 MB shell wrapping
the same compromises Phase A already documents honestly. No.

### Recommendation

**Swift + SwiftUI + Xcode, native.** Single most-compelling reason:
**Apple Watch is a stated product goal, and watchOS apps must be Swift
extensions of iOS apps.** Every other consideration (Core ML
performance, audio background reliability, Live Activities, Siri,
Spotlight) reinforces the choice but doesn't drive it on its own. The
Watch goal alone is dispositive.

The rest of this doc assumes the Swift + SwiftUI stack.

## 4. Core ML model conversion

This is the hardest technical work in C-1. Two models to convert:
Parakeet 0.6B for live STT, and a small LLM (Qwen 3 0.6B or Gemma 4 E2B
or the Foundation Models 3B that Apple gives us for free on iOS 26+)
for rolling summary. Conversion approach below.

### 4.1 Parakeet 0.6B (live STT)

The good news: someone has already done the hard part. FluidInference
has published `FluidInference/parakeet-tdt-0.6b-v3-coreml` on Hugging
Face, with an MIT-licensed conversion script at
`github.com/FluidInference/mobius/tree/main/models/stt/parakeet-tdt-v3-0.6b/coreml`
and an end-to-end Swift package at `github.com/FluidInference/FluidAudio`
that wraps the converted model with a real-time-streaming API. Their
benchmark is 110x RTF on M4 Pro in batch mode, with iPhone 16 Pro Max
and iPhone 13 first-load compile times also published.

That's our path. We either:

1. **Use FluidAudio directly as a Swift Package.** Fastest. We
   integrate via Swift Package Manager, drop the model bundle into
   our app, and call the streaming API. The package is MIT-licensed
   and the maintainer is responsive. Risk: we're tied to their
   release cadence for upgrades, and we don't fully control the
   model file we ship.

2. **Fork the conversion script and run it ourselves.** We pull
   `mobius/.../coreml` into our repo, vendor the NeMo checkpoint,
   run the conversion locally on a Mac, ship the resulting
   `.mlpackage` in the app bundle. We control the model. Risk: more
   work, and the conversion script is a moving target.

3. **Hybrid.** Use FluidAudio for v1, get to TestFlight fast, then
   migrate to in-house conversion for v2 once we know we want
   long-term control (and possibly want to fine-tune for
   meeting-specific vocab).

Recommend **option 3**: ship v1 on FluidAudio, swap to in-house in
C-1.3 or later. The 110x RTF batch number is fine for our use case
(post-meeting reprocess if we want to do it on-device for privacy mode)
but live streaming uses their `parakeet-realtime-eou-120m-coreml`
variant (smaller, optimized for streaming, end-of-utterance detection
built in). We'll need to evaluate both during C-1.2.

Conversion path under the hood, for context (and for option 2/3):

```
NeMo checkpoint (PyTorch)
  -> coremltools.convert(model, ...) with PyTorch front-end
  -> CoreML.MLModel with INT8 weight quantization
  -> .mlpackage bundle shipped in app
```

`coremltools` 8.x supports PyTorch directly without going through ONNX
in most cases, which simplifies the path. The ONNX intermediary is
legacy and the docs explicitly say new features aren't being added to
the ONNX-to-Core-ML path. For the streaming variant, the model is
split into encoder + decoder Core ML models (the streaming runner
maintains state between calls). FluidAudio's wrapper handles the
state-keeping; if we fork, we replicate it.

Model size on disk: the `parakeet-tdt-0.6b-v3-coreml` bundle is on the
order of 600 to 700 MB unquantized, dropping to roughly 150 to 200 MB
with INT8 weight quantization. INT8 W8A8 (weights and activations both
INT8) gives meaningful additional latency wins on A17 Pro and newer
because the ANE has dedicated INT8-INT8 compute paths; older devices
(A14 to A16) get smaller wins but still benefit from the smaller
memory footprint.

Latency target: <200ms first-word latency on iPhone 15 Pro (A17 Pro)
with the streaming model. <300ms on iPhone 12/13/14 (A14/A15/A16) is
acceptable; we degrade gracefully on older silicon.

### 4.2 Small LLM (rolling summary, on-device privacy mode)

Three candidates and we'll likely use them in priority order:

#### 4.2.1 Apple Foundation Models (iOS 26+, free)

Apple shipped the Foundation Models framework at WWDC 2025 and it's
generally available with iOS 26 / iPadOS 26 / macOS 26 / watchOS 26.
A ~3B parameter on-device LLM, the same model that powers Apple
Intelligence, exposed via Swift API. Free. Runs entirely on-device.
Already optimized for ANE + Metal. Already quantized. Already
deployed.

Caveats:
- Requires iOS 26 or newer.
- Requires Apple Intelligence to be enabled on the device, which in
  turn requires an A17 Pro chip or newer (iPhone 15 Pro and later, M1+
  iPads).
- Output is structured via the `@Generable` macro and guided generation;
  free-form text is also available but the framework is opinionated.
- We don't control the model. Apple updates it.

For our rolling summary use case, this is the right tool when
available. It's a ~3B model that already runs on the user's device,
we don't pay disk or download cost, we don't have to maintain the
conversion, and the integration is a single Swift API call. We use it
as the default LLM path when the user is on a supported device with
Apple Intelligence enabled.

#### 4.2.2 Qwen 3 0.6B or Gemma 4 E2B via mlx-swift (iOS 17+, fallback)

For users on older devices or with Apple Intelligence disabled, we fall
back to the same models the browser path already uses: Qwen 3 0.6B or
Gemma 4 E2B. Conversion path is **mlx**, Apple's research-oriented ML
framework that has shipped mature support for both architectures via
`mlx-swift`. As of MLX v0.19.0 the runner supports Gemma 3, Qwen 3,
Qwen 3.5, and GLM-4 MoE Lite among others; both Qwen 3 0.6B and Gemma 4
E2B have community-validated mlx ports.

Why mlx over Core ML for the LLM path:

- LLM inference is the use case mlx was built for. Apple's research
  blog (January 2026) showed Qwen3-14B-4bit at 4.06x faster TTFT on
  M5 vs M4 via mlx. For our smaller models on phones the win is
  smaller but the direction is the same.
- mlx supports the latest LLM architectures faster than Core ML does.
  Qwen 3 0.6B and Gemma 4 E2B are both in mlx today; Core ML
  conversion of either requires more bespoke work.
- mlx-swift is the integration point. Same Swift code calls into the
  same model runtime on both iOS and macOS. Same memory format,
  same KV cache, same quantization.
- Quantization: 4-bit weight quantization is the standard and gives
  us model files in the 350 to 500 MB range for these sizes.

Risk: mlx is officially "research-oriented" and Apple's "preferred for
researchers" framing at WWDC 2025 makes it less of an App Store
default than Core ML or Foundation Models. We'll need to confirm App
Review's posture on shipping mlx in a consumer app. As of late 2025
multiple shipped App Store apps use it, so the precedent exists.

#### 4.2.3 GGML / GGUF via llama.cpp + Metal (last-resort fallback)

If both Core ML and mlx hit a wall for some specific model size or
device, llama.cpp on Metal is the no-surprises path that always works.
GGUF model file, llama.cpp compiled for iOS as a static library,
inference runs on Metal. Slower than mlx, slower than Core ML, but
proven to ship and runs on every iOS device that supports Metal
(iOS 12+). This is the escape hatch, not the plan.

#### 4.2.4 Recommendation

**Priority cascade at runtime:**

1. If `iOS >= 26.0` and `SystemLanguageModel.default.availability ==
   .available`, use Foundation Models. ~3B params, free, on-device,
   no model file shipped.
2. Else, if `iOS >= 17.0`, use mlx-swift with the same Qwen 3 0.6B or
   Gemma 4 E2B model file the browser path uses, in 4-bit quantization.
   Model file downloaded on first launch, stored in
   `Application Support`.
3. Else, fall back to capture-only (no live summary) and let the
   server reprocess pass at session completion produce the final
   summary. Same Phase A contract the PWA already documents.

The fallback chain means we don't have to support every old device with
on-device LLM. iOS 17+ is the floor (released September 2023), iOS 26+
unlocks the free Apple model, iOS 26+ on A17 Pro+ is the premium path.

### 4.3 Model distribution strategy

Two options for getting the model bytes onto the user's device:

- **Bundle in app at build time.** App download is large (~200 to
  500 MB depending on what we ship), first launch is instant, no
  network required. App Store cellular download limit was raised to
  unlimited in 2025, but TestFlight builds and StoreKit refunds and
  cellular constraints still make a 500 MB app a worse first
  impression than a 50 MB app.
- **Download on first launch.** App is small (~30 MB), first launch
  needs WiFi to fetch ~150-500 MB of models, then everything works
  offline forever. User sees a progress UI on first launch.

Recommend **download on first launch**, with the following polish:

- Show progress as "Setting up on-device AI (one-time, requires WiFi):
  150 MB of 500 MB".
- Store the model in `Application Support/Models/` (not `Documents`,
  because we don't want it cluttering Files.app or backing up to
  iCloud).
- SHA-256 verify the downloaded files against a manifest we ship with
  the app binary.
- Mirror the model files on `models.magicunicorn.dev` (Garage S3) so
  we can update them independently of App Store submissions.
- If Foundation Models is available, skip the LLM download entirely;
  we only need Parakeet weights (~150-200 MB).

This keeps the App Store install small, lets us update models out of
band, and matches what other on-device-ML apps in the App Store do
(e.g., Whisper.app, MacWhisper, OllamaUI).

## 5. Performance targets

Targets are concrete and measurable. We'll set up a benchmark harness
in C-1.2 that runs through a fixed test corpus and records numbers on
each supported device.

### Latency

- **First-word latency** (microphone tap to display): <200ms on
  iPhone 15 Pro / 16 / 17 (A17 Pro and newer). <300ms on iPhone
  12/13/14 (A14-A16). <500ms on iPhone 11 and older (A13 and below),
  if we choose to support them.
- **Live transcript update cadence:** Every 100-200ms during active
  speech. Same as the browser path's 100ms window.
- **Rolling summary update cadence:** Every 3 to 5 seconds during
  active recording. Each summary delta should compute in <2 seconds
  on A17 Pro, <4 seconds on A14-A16.
- **Stop-to-final-summary:** <5 seconds on-device finalization
  (re-run Parakeet against the assembled audio for the high-quality
  transcript, then one final LLM pass for the summary). Server
  reprocess pass starts in parallel and lands its own pass in 30 to
  90 seconds depending on session length.

### Battery

- **Recording with live STT + live summary:** <15% battery per hour
  on iPhone 15 Pro. We measure against the browser PWA's 25-40% per
  hour baseline. Target is roughly half.
- **Capture-only mode (no live ML):** <8% battery per hour.
- **Background recording with screen off:** <5% battery per hour
  (no display, ANE only).

### Storage

- **App binary on disk:** <50 MB (without bundled models).
- **Model files (Parakeet only, Foundation Models LLM):** ~200 MB.
- **Model files (Parakeet + mlx LLM):** ~500 MB.
- **Per-session audio + metadata:** matches the desktop browser
  IndexedDB path; capture chunks at ~30 MB/hour AAC.
- **Total app footprint with models + 50 hours of recordings:** <2.5 GB.

### Memory

- **Live recording with both models loaded:** <800 MB resident on
  iPhone 15 Pro. iOS jetsam will reap us above 1.5 GB; we have to
  stay well below that ceiling. mlx and Core ML both unload weights
  cleanly between sessions; we exploit this.

## 6. Native UX components

Each surface, what it does, what API powers it.

### 6.1 Background recording

The single most important platform feature for us. Configuration:

- `Info.plist`: `UIBackgroundModes` includes `audio`.
- `AVAudioSession` configured with category `.playAndRecord`, mode
  `.default`, options `[.mixWithOthers, .allowBluetooth, .defaultToSpeaker]`.
- `AVAudioEngine` for the live STT pipeline (we want the input node
  tap for raw PCM frames, not a file-based recorder).
- Mixing-with-others is critical: the user might be on a Zoom call
  while we record. We don't want to duck or interrupt their other
  audio.

On iOS 26+ we also have `BGContinuedProcessingTask` for finalizing a
recording after the user stops it (compute on-device summary,
upload last chunks, etc.) in the background. We adopt this for the
post-stop finalize.

### 6.2 Lock-screen + Control Center + AirPods controls

- `MPNowPlayingInfoCenter.default().nowPlayingInfo` populated with
  session title (or "Recording in progress"), elapsed time, and a
  recording icon (artwork).
- `MPRemoteCommandCenter.shared()` wired to pause/resume/stop
  commands. AirPods stem press, lock-screen tap, CarPlay button,
  paired Watch button all route through this.
- Live Activity (next section) keeps the same controls visible in
  Dynamic Island.

### 6.3 Live Activities + Dynamic Island

iPhone 14 Pro and newer. We register a `Live Activity` for the
recording session showing:

- Elapsed time
- Live waveform / level meter (subtle, animated)
- A "Stop" button
- Optional: latest sentence transcribed (last 5-6 words)

The activity stays in Dynamic Island while the app is backgrounded,
shows on the lock screen, and lives in the new "Activities" widget.
This is the single best UX win for ambient meeting recording: the
user never wonders if it's still going.

### 6.4 Siri shortcuts + App Intents

- `INVoiceShortcut` donations on app launch ("Start a Meeting" /
  "Stop the Recording" / "Find my last meeting").
- `AppIntent` definitions so users can wire Meeting-Ops into
  Shortcuts.app workflows (e.g., "When I leave the office, stop my
  recording and start a summary").
- App Intents are the iOS 16+ replacement for Intents extensions
  and are the recommended path forward.

### 6.5 Spotlight indexing

- `CoreSpotlight.default().indexSearchableItems(...)` called after
  each session is finalized.
- Index session title, attendees, full transcript, AI summary, tags,
  and meeting date.
- Spotlight result opens the app to the session detail view via a
  `NSUserActivity` deep link.
- We honor the user's Spotlight settings (they can disable indexing
  per-app from Settings.app).

### 6.6 Share extension

A separate target in the Xcode project. The user shares an audio file
from anywhere (Voice Memos, Files, Mail, AirDrop, third-party apps)
and our extension appears in the share sheet. The extension:

1. Receives the file via `NSExtensionContext.inputItems`.
2. Runs our existing filename parser (`POST /api/recordings/parse-filename`)
   to extract title + meeting date + meeting time.
3. Shows a small UI confirming the parsed values, lets the user
   override.
4. POSTs the file to `/api/recordings/sessions/{id}/full-audio` to
   create the session and start the server reprocess pipeline.
5. Returns the user to wherever they were.

This is the killer feature for Aaron's 526-file audio archive backfill
and for ongoing Voice Memos intake. Share-extension is also how we
play nicely with iOS's privacy posture: we never touch the user's
file system without an explicit user-initiated share.

### 6.7 Home-screen widgets

- A "Recent Meetings" widget showing the latest N sessions with title,
  date, and a tap-to-open deep link.
- A "Quick Record" widget, a single big button that launches the app
  and immediately starts recording (via `AppIntent` from the widget).

Built with WidgetKit. Sizes: small (Quick Record only), medium (3
recent), large (6 recent + Quick Record).

### 6.8 AirDrop + Files integration

- Register our app as a handler for `public.audio` and `public.mp3`,
  `public.mpeg-4-audio`, `public.waveform-audio` (UTType audio family).
- AirDropped files land via the same share-extension path.
- Files.app shows our app as an option for "Open with" on audio
  files.

### 6.9 Spotlight Quick Action + Notification Actions

- After a session completes, an interactive notification: "Your
  meeting with Hina is summarized. Open" / "Share summary" /
  "Dismiss". Acts on the notification without launching the app
  to the foreground when possible.

## 7. Audio capture pipeline

The pipeline mirrors the browser path's contract (chunks land on
`POST /audio-chunks`, finalize via `POST /finalize-audio` or
`/full-audio`) but uses native iOS audio APIs underneath.

### 7.1 AVAudioEngine vs AVAudioRecorder

- `AVAudioRecorder` is the high-level, file-based recorder. Simple to
  use. No tap for live PCM frames; the only way to do live STT is to
  read the file as it grows, which is fragile.
- `AVAudioEngine` is the low-level graph. We attach an
  `AVAudioInputNode`, install a tap on it for live PCM frames at our
  chosen sample rate (16kHz mono for Parakeet), and simultaneously
  route the input through an `AVAudioMixerNode` into an
  `AVAudioFile` for the AAC/MP4 chunks we upload to the server.

We use `AVAudioEngine`. The tap-based live PCM path is the same
technique FluidAudio uses, and it's the only way to get the
sub-200ms first-word latency we target.

### 7.2 Chunk format

- **Live frames to Parakeet:** 16kHz mono Float32 PCM, 100ms windows
  (1600 samples per frame). Tap delivers these directly from
  AVAudioEngine.
- **Chunks to server:** AAC in MP4 container, 30-second windows.
  Encoded via `AVAudioEngine` + `AVAudioFile` with the AAC encoder.
  This is the same format the iOS Safari browser path already produces
  (after the v0.8.0 MP4-first fix), so the server-side ffmpeg reassembly
  in `/finalize-audio` is unchanged.

We do not use Opus on iOS even though iOS 17+ supports it via Audio
Toolbox. AAC is what every iOS audio app uses and it's what our server
pipeline is well-tested against. No reason to introduce variance.

### 7.3 Local storage

- Chunks land in `FileManager.default.urls(for: .documentDirectory)`
  organized as `<session_id>/chunks/<sequence>.m4a`.
- Per-session metadata (title, meeting_date, sha256, chunk_count,
  bytes_total) in a GRDB SQLite database (section 8).
- Same A.5 contract as the browser path: on stop, compute SHA-256,
  call `/finalize-audio` with verification, fall back to
  `/full-audio` on mismatch.

### 7.4 Background uploads

- `URLSession` with `URLSessionConfiguration.background(withIdentifier:)`.
  Bundle ID + ".uploads" as the identifier.
- Each chunk upload is a `URLSessionUploadTask` with the chunk file as
  the request body (background uploads must be file-backed, not
  in-memory).
- iOS 17 introduced resumable upload support (RFC 9110 partial
  uploads); we use it on iOS 17+ for any chunk over 1 MB.
- The OS continues uploads when the app is suspended, terminated, or
  the device is locked.
- Delegate callbacks (success/failure/progress) are handled via the
  `application(_:handleEventsForBackgroundURLSession:completionHandler:)`
  app delegate hook.

This is the single biggest reliability win over the browser path. iOS
Safari can't continue uploads when the tab is backgrounded; a native
URLSession just keeps going.

### 7.5 Endpoints touched

No new endpoints. We hit the exact same wire contract as desktop:

- `POST /api/recordings/start-always-on`: create the session row,
  return session_id.
- `POST /api/recordings/sessions/{id}/audio-chunks`: one per chunk.
- `POST /api/recordings/sessions/{id}/finalize`: mark the session
  done in the always-on FSM.
- `POST /api/recordings/sessions/{id}/finalize-audio`: kick the
  server reprocess pipeline with optional verification.
- `POST /api/recordings/sessions/{id}/full-audio`: fallback whole-file
  upload.
- `POST /api/recordings/parse-filename`: for the share extension
  and the manual upload form.
- `GET /api/recordings/sessions`: list, paginated, with the new
  `?sort=meeting_date_desc` option.
- `GET /api/recordings/sessions/{id}`: detail.
- `PUT /api/recordings/sessions/{id}`: title / tags / meeting_date /
  meeting_time edits.

Phase B's `/ws/sessions/{id}/live` lands separately when Phase B ships;
section 9 covers WS auth.

## 8. Sync model and local database

The desktop browser uses IndexedDB. The iOS app needs an analog. Two
real options.

### 8.1 GRDB vs SwiftData vs Core Data

- **Core Data**: Apple's incumbent. Object graph + persistent store
  coordinator. Heavyweight, awkward concurrency model, schema
  migrations are painful. Has been the official answer for a decade
  but in 2026 the community consensus is that Core Data feels dated
  next to modern Swift.
- **SwiftData**: Apple's new declarative wrapper on top of Core Data,
  shipped iOS 17. Still has some rough edges in 2026, particularly
  around concurrency and complex queries. Best for greenfield SwiftUI
  apps without complex needs.
- **GRDB**: Third-party. SQLite + a thin Swift wrapper + Combine /
  async-stream integration. Battle-tested, easy schema migrations,
  fast, no magic. The community 2026 consensus is that GRDB is the
  best choice for apps that want a relational model and don't need
  the object-graph features of Core Data.

Recommend **GRDB**. Reasons:

1. Our data model is genuinely relational (sessions, chunks, tags,
   speakers, transcript segments). GRDB models that directly.
2. Migrations: we'll be evolving the schema as we ship. GRDB's
   migration story is "write SQL, give it a name, done." Core Data
   migrations are notorious.
3. Performance: SQLite is fast and predictable. We can write the same
   queries the backend writes (with sqlite3 syntax), reason about
   indexes the same way.
4. We can share code between the iOS app and other future Apple-side
   tools (the macOS Companion App, the eventual native macOS
   Meeting-Ops app) that all want the same local-DB shape.

### 8.2 Schema

Mirrors the server's session schema with a few iOS-specific additions:

```sql
CREATE TABLE sessions (
  id              TEXT PRIMARY KEY,    -- server session_id (UUID)
  org_id          TEXT NOT NULL,
  title           TEXT,
  meeting_date    TEXT,                 -- ISO 8601 date
  meeting_time    TEXT,                 -- HH:MM:SS
  duration_sec    INTEGER,
  started_at      TEXT NOT NULL,
  ended_at        TEXT,
  state           TEXT NOT NULL,        -- idle|recording|stopping|complete|failed
  transcript      TEXT,                 -- assembled transcript JSON
  summary         TEXT,                 -- AI summary
  tags            TEXT,                 -- JSON array
  is_local_only   INTEGER NOT NULL DEFAULT 0,
  upload_state    TEXT,                 -- pending|partial|complete|failed
  bytes_total     INTEGER,
  sha256          TEXT,
  chunk_count     INTEGER,
  last_synced_at  TEXT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);

CREATE INDEX sessions_org_meeting_date ON sessions(org_id, meeting_date DESC);
CREATE INDEX sessions_org_started_at ON sessions(org_id, started_at DESC);

CREATE TABLE chunks (
  session_id      TEXT NOT NULL,
  sequence        INTEGER NOT NULL,
  file_path       TEXT NOT NULL,
  bytes           INTEGER NOT NULL,
  duration_sec    REAL NOT NULL,
  upload_state    TEXT NOT NULL,        -- pending|uploading|complete|failed
  upload_error    TEXT,
  PRIMARY KEY (session_id, sequence),
  FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE TABLE transcript_segments (
  session_id      TEXT NOT NULL,
  start_ms        INTEGER NOT NULL,
  end_ms          INTEGER NOT NULL,
  speaker         TEXT,
  text            TEXT NOT NULL,
  is_final        INTEGER NOT NULL,
  PRIMARY KEY (session_id, start_ms, end_ms),
  FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE TABLE pending_edits (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id      TEXT NOT NULL,
  field           TEXT NOT NULL,
  new_value       TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  synced_at       TEXT,
  FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
```

The `is_local_only` flag is critical: privacy-mode sessions live only
in the iOS DB, never sync to the server. The desktop browser's
`/local-sessions` view becomes the iOS Local Sessions tab.

### 8.3 Sync model

Server is source of truth for shared sessions; client is source of
truth for `is_local_only=1` sessions and for unsynced edits.

**Pull cadence:**
- App foreground: pull /sessions list every 60s if active.
- App background: no pull (sync on resume).
- Pull-to-refresh in the session list view: immediate pull.

**Push cadence:**
- Chunks: as soon as they're produced (URLSession background upload).
- Finalize: when the user taps Stop.
- Pending edits (title, tags, meeting_date overrides): batched on
  network availability, retried with exponential backoff.

**Conflict resolution:**
- Transcripts and summaries: server wins (the server's reprocess pass
  is higher quality than anything we computed live).
- Title, tags, meeting_date, meeting_time edits: most recent write
  wins by `updated_at`. Conflicts are rare given single-user usage;
  if they happen we surface a "your edit and a remote edit
  disagreed" banner and let the user pick.

### 8.4 App Group sharing for the watchOS extension and share extension

`Application Support` is per-target. The share extension and the
watchOS extension live in different sandboxes. To share the same
GRDB database (or at least the same staged-upload files), we create an
App Group:

- App Group identifier: `group.dev.magicunicorn.meetingops`.
- Both the iOS app target, the share extension target, the watchOS
  app target, and any widget targets are members of the group.
- The GRDB database lives at `FileManager.default.containerURL(
  forSecurityApplicationGroupIdentifier: "group.dev.magicunicorn.meetingops")`.

Decision deferred to C-1.4: do we put the model files in the App Group
too (so the Watch can use them) or keep them per-target (so each
target downloads its own)? Watch storage is constrained enough that
sharing is probably the right call, but it depends on whether Watch
gets its own STT (Phase C-2) or proxies through the iPhone.

## 9. WebSocket auth (when Phase B ships)

Phase B's design doc (`docs/phase-b-server-live-streaming.md`,
section 4 and Q6) settles the cross-cutting question. Native iOS
clients cannot use the OIDC cookie flow that browsers use; we need a
JWT.

**Auth flow:**

1. App launch: check Keychain for a stored refresh token.
2. If none, open `ASWebAuthenticationSession` against Keycloak's
   authorization endpoint with PKCE. User logs in via the system
   browser (uses the same SSO session as Safari, so SSO with
   meetingops.magicunicorn.dev's web login if the user is signed in
   there).
3. On callback, exchange the auth code for an access token + refresh
   token. Store both in Keychain (`kSecAttrAccessibleWhenUnlockedThisDeviceOnly`,
   biometric protection optional).
4. For REST calls: `Authorization: Bearer <access_token>` header.
5. For WebSocket: per the Phase B doc, `Sec-WebSocket-Protocol:
   bearer.<JWT>` subprotocol. Backend's `get_current_user_optional`
   already trusts this path (Phase B Q6 locked).
6. Refresh: when access token is <60s from expiry, refresh in the
   background using the stored refresh token. If refresh fails (revoked,
   expired, network), fall back to step 2.

**Keychain handling:**

- Access token: short-lived, ephemeral. We may keep in memory only.
- Refresh token: persisted, biometric-locked. Use `LocalAuthentication`
  to require Face ID / Touch ID before unlocking it on app launch if
  the user enables it.
- Logout: wipe Keychain, wipe local DB (optional, prompt user), wipe
  background URLSession state.

**Multi-org support:**

- The iOS app supports multiple orgs the user belongs to. The active
  org is stored in `UserDefaults` and tagged into every API call via
  the `X-Org-ID` header the backend already supports.
- Org switching in the app: tap the org name in the settings sheet,
  pick a new one, the session list refreshes.

## 10. watchOS extension architecture (foundation for C-2)

C-1 doesn't build the Watch app, but it does set the foundation. Three
foundational decisions land in C-1:

### 10.1 Bundle structure

The watchOS app is a separate target inside the same Xcode project as
the iOS app. They share:

- An "App Group" identifier for shared storage.
- A "MeetingOpsCore" Swift package (in-repo, not on SPM) with shared
  models, protocol definitions, GRDB schema, network code, and
  Keycloak auth helpers.
- The same Apple Developer team, the same App ID prefix, the same
  signing certificate.

The watchOS app cannot ship standalone today; it ships inside the iOS
app bundle. iOS users who install Meeting-Ops on their iPhone get a
prompt to install the Watch companion on their paired Apple Watch.
Independent Apple Watch installations (Series 9+ cellular models, no
paired iPhone) are technically possible in 2026 but the engineering
load is meaningfully larger and we defer to C-2 or later.

### 10.2 Audio capture strategy on Watch

Apple Watch has `AVAudioRecorder` but does not have `AVAudioEngine`
(compute and battery reasons, the Watch SoC isn't built for live ML
inference). So:

- **Capture path on Watch:** `AVAudioRecorder` writes a single AAC/MP4
  file to local storage. No live transcription on the Watch itself.
- **Streaming to iPhone:** `WatchConnectivity` framework provides
  `transferFile(_:metadata:)` for moving files to the paired iPhone.
  As the Watch records, we split the file into ~30s segments and
  transfer each one as it lands. The iPhone-side delegate receives
  the chunks and routes them into the iOS app's existing chunk
  pipeline.
- **Direct upload fallback:** For cellular Apple Watches (Series 5+
  with cellular plan) when the iPhone is unreachable (out of
  Bluetooth/WiFi range, off, etc.), the Watch can upload chunks
  directly to the server using `URLSession`. The auth token must
  already be available on the Watch (synced from iPhone via
  `WatchConnectivity` keychain proxy).

### 10.3 Watch UI sketch (defer detailed design to C-2)

The Watch app is intentionally minimal:

- Main view: a big "Record" button with elapsed time when active.
- Tap to start, tap to stop.
- Complication on the watch face: tap to launch and start recording.
- Optionally show last 1-2 sentences from the iPhone-side live
  transcript via `WatchConnectivity` updates.
- That's the whole app.

Use cases we're optimizing for:

- A meeting starts in a hallway and the user doesn't want to pull
  out the phone. Tap the wrist, recording starts.
- The user is driving and a phone call turns into a meeting they
  want to keep. Tap the wrist, recording starts.
- A field interview, walking around with the phone in a bag.

What we're deliberately NOT optimizing for:

- Full transcript review on the Watch. Too much text for the form
  factor.
- Live AI summary on the Watch. Compute-prohibitive.
- Long-form review or editing. That's the iPhone or desktop view.

## 11. Apple Developer Program, signing, and distribution

### 11.1 Enrollment

- $99/year Apple Developer Program (individual) or $299/year
  Apple Developer Enterprise Program (organization).
- Recommend **individual** for v1: Aaron's signing identity, all
  apps under his name. Switch to org enrollment when we incorporate
  or when we hire engineers who need to sign builds.
- D-U-N-S number required for org enrollment; individual just needs
  an Apple ID + payment method.

### 11.2 App ID and Provisioning

- App ID: `dev.magicunicorn.meetingops` (matches our domain
  convention).
- App ID prefix: assigned by Apple, used for App Groups and Keychain
  sharing.
- Capabilities to enable: Background Modes (audio + processing),
  Push Notifications (for the Phase B WS reconnect prompt and
  reprocess-complete notifications), Sign in with Apple (optional,
  defer), App Groups, Keychain Sharing (between targets), Associated
  Domains (universal links).
- Provisioning profiles: development (Aaron's devices + simulator),
  Ad Hoc (closed beta, capped at 100 devices), App Store (TestFlight
  + App Store distribution).

### 11.3 TestFlight strategy

TestFlight limits in 2026:
- **Internal testers:** Up to 100 members of the Apple Developer team
  with the relevant role; they can test on up to 30 devices each.
  Internal builds are available immediately, no App Review.
- **External testers:** Up to 10,000 testers per app. Requires Apple
  to approve the first build (beta App Review, typically 24 hours).
  Subsequent builds can ship without re-review unless they materially
  change.

Phased rollout:
- **Phase 1 (weeks 1-2 of C-1.5):** Internal-only, Aaron + Shafen +
  any other Magic Unicorn team members who want to dogfood. <5
  testers.
- **Phase 2 (weeks 3-4 of C-1.5):** External closed beta. 20-50
  hand-picked testers (Kevin Honeycutt, friends/family, Discord
  community, opt-in early-access list from the meetingops.magicunicorn.dev
  marketing site). 2-3 build cycles to bake feedback in.
- **Phase 3 (post-C-1.5):** External public beta. Up to 10,000
  testers. Promoted on the marketing site, our newsletter, Aaron's
  Twitter/LinkedIn presence.
- **Phase 4 (post-public-beta):** App Store submission. Full review
  cycle, typically 24-72 hours for a well-prepared submission. May
  bounce a few times on first submission; budget 1-2 weeks for
  review-cycle slack.

### 11.4 App Store review preparation

The two review concerns that bite audio-recording apps:

**Audio recording consent (Guideline 5.1.1):**
- We must request explicit consent before recording.
- We must show a clear visual indicator while recording (the iOS
  system orange dot + our own in-app indicator + the Live Activity).
- We must NOT record without user knowledge, ever.
- App Privacy details must mark "Audio Data" and "Other User Content"
  in the App Store Connect privacy questionnaire.
- Microphone usage description string (`NSMicrophoneUsageDescription`
  in Info.plist) must be specific: "Meeting-Ops records audio during
  meetings to produce transcripts and summaries. Recording is opt-in
  per session and only happens when you tap Record."

**Background audio mode (Guideline 2.5.4):**
- Background modes can only be used for "intended purposes." Audio
  recording is one of the explicitly allowed reasons.
- We must NOT use background audio mode for anything other than the
  actual audio recording task. Don't reuse it to do background
  network work unrelated to recording.

**On-device AI (no specific guideline, but worth getting ahead of):**
- Apple's late-2025 review guidelines tightened on sharing user data
  with third-party AI. Our model is the opposite: we run AI locally
  and let the user opt into server-side processing per session. We'll
  document this in the App Store description ("All AI runs on your
  device by default; server processing is opt-in and tied to your
  Meeting-Ops account").
- Disclose any third-party AI providers we route to (none for the
  on-device path, OpenRouter / our own midboy1 for the server path).

**Common review bounces and how to avoid them:**
- Missing or vague usage description strings: be specific.
- App crashes on cold launch on reviewer's device: TestFlight with
  internal team first.
- "Account required for first-launch experience": offer a demo mode
  that records locally without an account, only require login when
  the user wants to sync.
- "Functionality is not apparent": include screenshots and a brief
  video in the App Store listing that show the live transcription.

### 11.5 Pricing model on the App Store

C-1 ships free. Monetization is handled by the existing Pro/Enterprise
SaaS tiers tied to the user's Meeting-Ops account; the app is the
window into that account. In-app purchase via StoreKit is a follow-up
decision (see open question 13.3).

## 12. Implementation plan

Five phases, ~8 to 10 weeks total. Each phase ends with a working
TestFlight internal build.

### C-1.1: Bootstrap and core recording (2 weeks)

Goal: a buildable app that records audio, uploads chunks, and shows
the existing server-produced transcript + summary.

- Xcode project scaffold (iOS app target, shared MeetingOpsCore
  Swift package).
- App Group + Keychain Sharing entitlements.
- SwiftUI shell: tab bar (Sessions / Local / Settings).
- Keycloak auth via ASWebAuthenticationSession + PKCE.
- Session list view pulling from `GET /sessions`.
- Session detail view rendering server-produced transcript + summary
  (read-only for now).
- Recording view with AVAudioEngine, level meter, elapsed time.
- Background recording mode wired up.
- Chunk upload via URLSession background task.
- Finalize flow against `/finalize-audio`.
- GRDB schema + migrations.

Deliverable: closed alpha to Aaron + Shafen for a week of dogfooding.

### C-1.2: Live STT via FluidAudio + Parakeet (2-3 weeks)

Goal: live captions on-device, sub-200ms first-word latency on A17+
devices.

- Integrate FluidAudio Swift package.
- Wire AVAudioEngine tap to Parakeet streaming model.
- Live transcript view with rolling-window display.
- Final-on-stop pass to produce the full transcript (with the
  high-quality model in batch mode).
- Performance benchmarks against the targets in section 5.
- Per-device tuning: fall back to capture-only on devices below the
  perf floor.
- Decide FluidAudio-as-package vs in-house fork (carry to C-1.3 if we
  defer).

Deliverable: internal TestFlight build with live captions.

### C-1.3: Local LLM summary (2 weeks)

Goal: rolling AI summary on-device.

- Foundation Models integration (iOS 26+, A17 Pro+).
- mlx-swift integration with Qwen 3 0.6B / Gemma 4 E2B for the
  fallback path.
- Model download + verification on first launch.
- Rolling summary view with delta updates every 3-5 seconds.
- Final-on-stop summary pass against the assembled transcript.
- True privacy mode (`is_local_only=1` sessions) end-to-end on iOS.

Deliverable: TestFlight build with full on-device privacy mode.

### C-1.4: Native UX polish (1-2 weeks)

Goal: every native surface working end-to-end.

- Lock-screen + Control Center + AirPods controls.
- Live Activity / Dynamic Island for active recordings.
- Siri shortcuts + App Intents.
- Spotlight indexing of completed sessions.
- Share extension (Voice Memos / Files / Mail intake).
- Home-screen widgets (Recent + Quick Record).
- Background BGContinuedProcessingTask for post-stop finalize.
- Push notifications for server reprocess completion.

Deliverable: TestFlight build with the full native UX surface.

### C-1.5: TestFlight closed beta and App Store prep (1 week)

Goal: external closed beta and submission to App Review.

- Bug bash from C-1.1 through C-1.4.
- Crash-reporting integration (Apple's built-in MetricKit + a
  third-party like Sentry if we want richer breadcrumbs).
- App Store screenshots, preview video, description copy.
- Privacy questionnaire in App Store Connect.
- Review-team-facing notes explaining the recording-consent UX.
- TestFlight external beta launch to 20-50 invited testers.
- One round of feedback bake.
- App Store submission.

Deliverable: app shipped to closed external beta. App Store review
in flight or approved.

### Total

**8 to 10 weeks of focused engineering work** from kickoff to closed
beta. App Store approval and public launch is another 2 to 4 weeks
of polish + review-cycle slack after C-1.5 ends.

Resource assumption: one full-time engineer (or Aaron + Claude pairing
at roughly 60-70% of Aaron's time). Adding a second engineer at C-1.4
could compress C-1.4 to 1 week and overlap C-1.5 prep work; not worth
hiring just for this phase.

## 13. Open questions

### 13.1 Apple Developer Program enrollment timing

Do we enroll Aaron in the $99/year individual Apple Developer Program
immediately, or wait until C-1.1 is partway through and we know we're
committing to native? Enrollment can take 24-48 hours and sometimes
gets delayed by identity verification; we don't want to be blocked at
the first signing step. Recommend: enroll on day one of C-1, the
moment the project gets a green light. The fee is rounding error
against the engineering cost.

### 13.2 Core ML vs mlx for the LLM path

The doc recommends a runtime cascade (Foundation Models > mlx > Core
ML > capture-only). C-1.2's perf benchmarks may push us to bias
differently. Specifically:

- If mlx-swift turns out to have significant App Store review
  friction (not currently expected, but possible), we'll fall back
  to Core ML conversion for Qwen 3 0.6B / Gemma 4 E2B. Doable but
  more work.
- If Foundation Models' constrained generation turns out to be too
  rigid for our rolling-summary use case, we may want to skip it
  even on supported devices and go straight to mlx. We'll test this
  in C-1.3.

Open until C-1.3 performance work lands.

### 13.3 Storage layout: App Group vs Documents

Section 8.4 leans App Group for Watch sharing. If C-2's Watch app
ends up not needing to share the DB (because we route everything
through WatchConnectivity and the Watch just becomes a thin remote),
we could drop back to per-target Documents + a small App-Group-shared
keychain for auth tokens. Decision deferred to C-1.4 once Watch
integration scope is clearer.

### 13.4 iPad treatment

For v1, do we ship a true iPad-native UI (split view, hover state,
keyboard shortcuts, Apple Pencil annotation of transcripts), or treat
iPad as "iPhone app running on iPad" (which works fine but doesn't
exploit the form factor)?

Recommend: ship v1 as "iPhone app on iPad" with a few iPad-specific
quality-of-life touches (split view for the session list + detail,
keyboard shortcut for record/stop, multitasking-friendly layout).
Full iPad-native design (multi-column NavigationSplitView, Pencil
markup, Stage Manager) is a v2 enhancement. iPad is a large enough
audience to justify the polish but small enough as a percentage of
our total mobile users that we shouldn't sink another month into it
for v1.

### 13.5 In-app purchase via StoreKit

Should Pro/Enterprise upgrades be available as in-app purchase
(StoreKit, with Apple's 15-30% cut), or only via web (our existing
Stripe billing flow)?

Apple's "small business program" caps the cut at 15% for the first
year on each developer account under $1M/year in App Store revenue.
Even at 15%, the lost margin on Pro subscriptions ($X/month) is
non-trivial. But the conversion impact of having in-app purchase as
an option may exceed the lost margin. Open question; A/B-able after
launch. For v1, recommend web-only purchase via a webview that
bounces to our Stripe flow; revisit StoreKit at v1.5.

### 13.6 Sign in with Apple

Apple's App Review guidelines require apps that offer third-party
sign-in (Google, Facebook, etc.) to also offer Sign in with Apple.
We currently offer Keycloak + Google + Microsoft 365. Strictly, we
should add Sign in with Apple to meet the guideline.

Sign in with Apple integrates with Keycloak via the OIDC bridge
(Keycloak supports Apple as an identity provider out of the box).
Implementation cost is low (an afternoon of integration work in
Keycloak); the bigger consideration is the UX of presenting four
sign-in options on a small phone screen. Recommend: include in
C-1.1's auth integration work, hide behind a "More options" link if
the screen gets crowded.

### 13.7 Watch app independence

The doc plans the Watch app as a paired-iPhone companion (chunks
relay via WatchConnectivity). Series 9+ cellular Watch can technically
run standalone, call APIs directly, no paired iPhone needed. This
unlocks the "I left my phone at home" scenario but adds significant
complexity: independent auth on the Watch (no easy Keychain proxy),
independent storage, independent uploads, independent error handling.

C-2 will decide. For C-1, the foundation we build (shared App Group,
shared Swift package, shared GRDB schema) supports both modes.

### 13.8 Offline-first vs server-required behavior

What's the user experience when the app has never been online (first
launch on an airplane, etc.)? Today's design requires Keycloak auth
on first launch to do anything. Should we allow a "use offline first,
sign in later" flow that creates a guest profile and stores all
sessions as `is_local_only=1` until the user signs in?

Recommend: yes, for the demo / first-launch experience. The user can
record their first meeting without an account, see what the app does,
then sign in to sync to the server. Sessions recorded as guest
migrate to the signed-in account on first auth. Adds maybe 3-5 days
to C-1.1 but materially improves the "what is this app even for"
first-impression.

## 14. Risks and mitigations

- **Core ML conversion of Parakeet doesn't perform on iPhone the way
  FluidInference's M4 Pro benchmark suggests.** Mitigation: bench
  early in C-1.2 on real iPhones (Aaron's 15 Pro, an iPhone 13 if we
  can borrow one, an iPhone SE for the floor). If perf doesn't hit
  <300ms on A14-A16, we ship capture-only on older devices and live
  STT on A17+ only. Phase A's capture-only fallback already exists
  in the brand promise.
- **Foundation Models constrained generation doesn't fit our
  rolling-summary use case.** Mitigation: prototype in week 1 of
  C-1.3. If it's too rigid, fall back to mlx-swift directly on all
  iOS 17+ devices regardless of Apple Intelligence support.
- **App Store rejects on first submission.** Mitigation: prepare
  review-team-facing notes, screenshots, video. Submit a TestFlight
  build to beta App Review first to surface issues. Budget 1-2 weeks
  of review-cycle slack.
- **Apple Developer enrollment delayed.** Mitigation: enroll day one.
  Have a backup plan to ship via Ad Hoc to a small group while we
  resolve.
- **mlx-swift has unexpected App Store rejection precedent.**
  Mitigation: research extant App Store apps using mlx before
  committing; have Core ML conversion of Qwen 3 0.6B as a fallback.
- **Background URLSession behavior changes in a future iOS release
  and breaks our chunk upload path.** Mitigation: heavily monitor in
  TestFlight, surface upload failures in the UI, never silently lose
  data (we always have the local chunks until the server ACKs).

## 15. Out of scope for C-1

- **Android.** C-3.
- **Independent watchOS app.** C-2 or later.
- **iPad-native UI.** v2.
- **Apple TV app.** No.
- **macOS-native app.** Separate project; the existing Companion App
  on Mac plus the web frontend covers macOS today.
- **Vision Pro.** Plausible future scope; not a v1 concern.
- **CarPlay.** Plausible future scope; the lock-screen controls and
  Siri shortcuts cover the urgent driving use case.
- **Custom widgets beyond Recent and Quick Record.** v1.5.
- **Apple Pencil markup of transcripts.** v2.
- **In-app purchase (StoreKit).** Deferred per 13.5.
- **Local Network discovery for the desk-microphone Conference Room
  scenario (`docs/conference-room-design.md`).** Different surface,
  different project.

## 16. Implementation tickets (preview)

The actual JIRA / GitHub Issues tickets will be created at C-1 kickoff.
A preview of the major work items so Aaron can scope:

- C-1-001: Xcode project scaffold + App Group + Keychain Sharing.
- C-1-002: Keycloak auth via ASWebAuthenticationSession + PKCE +
  Keychain refresh-token storage.
- C-1-003: Session list view with pull-to-refresh + GRDB local cache.
- C-1-004: Session detail view (read-only).
- C-1-005: Recording view UI + AVAudioEngine + level meter.
- C-1-006: Background audio mode + AVAudioSession configuration.
- C-1-007: Chunk upload via URLSession background task.
- C-1-008: Finalize-audio flow + SHA-256 verification + full-audio
  fallback.
- C-1-009: GRDB schema + migrations + ORM glue.
- C-1-010: FluidAudio integration + Parakeet streaming live STT.
- C-1-011: Live transcript view + rolling window.
- C-1-012: Per-device perf benchmarking + graceful degradation.
- C-1-013: Foundation Models integration (iOS 26+).
- C-1-014: mlx-swift integration + Qwen 3 0.6B / Gemma 4 E2B.
- C-1-015: Model download + manifest + SHA-256 verification.
- C-1-016: Rolling summary view + delta updates.
- C-1-017: Privacy mode (is_local_only) end-to-end.
- C-1-018: Lock-screen + Control Center controls
  (MPNowPlayingInfoCenter / MPRemoteCommandCenter).
- C-1-019: Live Activity / Dynamic Island recording indicator.
- C-1-020: Siri shortcuts + App Intents.
- C-1-021: CoreSpotlight indexing of completed sessions.
- C-1-022: Share extension target + filename parser integration.
- C-1-023: Home-screen widgets (Recent + Quick Record) via WidgetKit.
- C-1-024: Background BGContinuedProcessingTask for post-stop work.
- C-1-025: Push notifications for reprocess completion.
- C-1-026: Crash reporting (MetricKit + optional Sentry).
- C-1-027: App Store screenshots + preview video + listing copy.
- C-1-028: App Store Connect privacy questionnaire.
- C-1-029: TestFlight internal beta launch.
- C-1-030: TestFlight external beta launch.
- C-1-031: App Store submission.

Estimated 31 issues; some are 1-day, some are 1-week. Total
engineering load lines up with the 8-10 week estimate in section 12.

## Appendix A: Key API references

For reviewers' convenience, the Apple frameworks and libraries this
doc commits to:

- **SwiftUI**: UI framework.
- **AVFoundation / AVAudioEngine / AVAudioSession**: audio capture
  and routing.
- **Core ML / CoreML**: on-device ML inference (Parakeet,
  optionally Qwen / Gemma if mlx path doesn't work out).
- **mlx-swift** (Apple): research-oriented ML framework, our
  primary LLM inference path.
- **FoundationModels** (Apple, iOS 26+): free 3B on-device LLM via
  Swift API.
- **FluidAudio** (FluidInference, MIT): Swift package wrapping
  Parakeet Core ML for streaming STT.
- **GRDB** (third-party, MIT): SQLite wrapper, our local DB.
- **URLSession / URLSessionConfiguration.background**: background
  uploads.
- **WatchConnectivity**: iPhone to Watch communication (C-2).
- **MPNowPlayingInfoCenter / MPRemoteCommandCenter**: lock-screen
  and remote controls.
- **ActivityKit**: Live Activities for Dynamic Island.
- **AppIntents** (iOS 16+): Siri shortcuts and Shortcuts.app
  integration.
- **CoreSpotlight**: system-wide search indexing.
- **WidgetKit**: home-screen widgets.
- **CallKit** (optional, defer): integration with phone-call UI.
- **ASWebAuthenticationSession**: OAuth flows via the system browser.
- **LocalAuthentication**: Face ID / Touch ID gating of the refresh
  token in Keychain.
- **MetricKit**: Apple-provided crash and metrics reporting.
- **StoreKit 2** (deferred): in-app purchase.

## Appendix B: Glossary

- **ANE**: Apple Neural Engine. The dedicated ML inference block on
  A11+ and M1+ Apple Silicon.
- **App Group**: A shared container identifier that lets multiple
  targets (main app, share extension, watchOS app, widgets) read and
  write the same files.
- **App Intent**: Apple's modern API for exposing app actions to
  Siri and Shortcuts. Replaces Intents extensions.
- **Capture-only**: Phase A's mobile contract: record audio, upload
  chunks, server produces transcript + summary at session completion.
- **Core ML**: Apple's on-device ML inference framework. Compiles
  models to a binary `.mlpackage` and runs them on ANE / Metal / CPU.
- **Foundation Models**: Apple's iOS 26+ Swift API exposing the ~3B
  on-device LLM powering Apple Intelligence.
- **FluidAudio**: MIT-licensed Swift package wrapping Parakeet Core
  ML for streaming STT.
- **GRDB**: Third-party Swift SQLite wrapper.
- **Live Activity**: iOS 16.1+ persistent UI element shown on lock
  screen and Dynamic Island.
- **mlx**: Apple's research-oriented ML framework, optimized for
  Apple Silicon. Has Swift bindings (`mlx-swift`).
- **Parakeet**: NVIDIA NeMo's ASR model family. We use the 0.6B v3
  variant for our STT.
- **PKCE**: Proof Key for Code Exchange. OAuth security extension
  for public clients (mobile apps that can't keep a client secret).
- **RTF**: Real-Time Factor. 110x RTF means 1 minute of audio
  processes in 1/110 minute of wall time.
- **TestFlight**: Apple's beta distribution platform. Up to 10,000
  external testers per app.
- **WatchConnectivity**: Apple's framework for iPhone to Apple
  Watch communication.

---

End of Phase C-1 design document.
