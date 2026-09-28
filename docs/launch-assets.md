# Meeting-Ops — storefront launch assets

For the UC / Ops-Center storefront. The page rendering is on the Ops-Center side;
this is the content.

## Logo (launch-critical)
- **`frontend/public/brand/meeting-ops.png`** — 512×512, RGBA, transparent
  corners. The square app mark (purple gradient + white mic). Derived from the
  shipped PWA icon (`frontend/public/icons/icon-512.png`) so it matches the
  in-app/installed icon exactly. Use this for the storefront card that currently
  shows a broken icon.
- Smaller square variants if needed: `icons/icon-192.png`,
  `brand/apple-touch-icon.png` (180), `brand/favicon-32.png`.

## Screenshots (nice-to-have — already in the repo root)
- `meeting-ops-login.png`
- `meeting-ops-dashboard.png`
- `meeting-ops-live-recording.png`
- `meeting-ops-sessionmanager.png`
- `meeting-ops-aichat.png`
- `meeting-ops-fullmenu.png`

## One-line value prop
> Record any meeting and get an instant transcript and summary — live on your
> device for free, with an optional studio-quality server pass for diarized
> transcripts, AI summaries, and cross-meeting chat.

## Three headline features
1. **Private by default, on-device.** Live transcription + summary run in your
   browser — nothing leaves your device, and it's free (Privacy Mode).
2. **Studio-quality server pass ($15/mo).** One end-of-meeting pass:
   Parakeet 1.1B transcription, speaker diarization, and a Qwen 3.6 summary with
   action items and decisions.
3. **Cross-meeting AI chat & search.** Ask questions across your whole meeting
   library, with a person-centric knowledge graph that knows who said what.
