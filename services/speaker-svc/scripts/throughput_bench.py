#!/usr/bin/env python3
"""Speaker-svc / STT throughput bench.

POST the same WAV at one or more concurrency levels and report per-request
RTF (real-time factor = audio_seconds / wall_seconds) plus p50/p95 wall-clock.
Run from any host with network access to the target service (Tailscale or
direct).

The route is configurable so the same harness can drive either the
speaker-svc `/diarize` endpoint or a Parakeet STT `/transcribe` endpoint;
both take an audio file as a multipart field (`audio` by default).

Usage:
    # Diarization on the speaker-svc (RTX 3090 on bigboy)
    python3 throughput_bench.py \\
        --endpoint http://meet-speaker-svc:8889 \\
        --wav /path/to/sample.wav \\
        --route /diarize \\
        --concurrency 1,2,4

    # STT on a Parakeet endpoint
    python3 throughput_bench.py \\
        --endpoint http://meet-speaker-svc:8890 \\
        --wav /path/to/sample.wav \\
        --route /transcribe \\
        --field audio \\
        --audio-seconds 263 \\
        --concurrency 1,2,4

RTF is read from the WAV header by default; pass --audio-seconds to override
(e.g. when the WAV header is unreliable or you are pointing at a non-WAV).

Companion to `leak_bench.py` (VRAM-leak hammer) in this directory — this one
measures throughput/RTF rather than VRAM trajectory.
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests


def wav_duration_seconds(wav_path: Path) -> float | None:
    """Return the WAV duration in seconds from its header, or None on failure."""
    try:
        with wave.open(str(wav_path), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            if rate <= 0:
                return None
            return frames / float(rate)
    except (wave.Error, OSError, EOFError) as exc:
        print(f"  [wav] could not read header: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None


def one_request(endpoint: str, route: str, wav_path: Path, field: str) -> tuple[float, int]:
    """Send one POST of the WAV. Returns (wall_seconds, http_status).

    On a transport-level exception, returns (wall_seconds, -1)."""
    url = f"{endpoint.rstrip('/')}/{route.lstrip('/')}"
    started = time.time()
    try:
        with wav_path.open("rb") as fh:
            files = {field: (wav_path.name, fh, "audio/wav")}
            r = requests.post(url, files=files, timeout=300)
        return time.time() - started, r.status_code
    except requests.RequestException as exc:
        print(f"  [req] {type(exc).__name__}: {exc}", file=sys.stderr)
        return time.time() - started, -1


def run_level(
    endpoint: str,
    route: str,
    wav_path: Path,
    field: str,
    concurrency: int,
    audio_seconds: float,
) -> None:
    """Fire `concurrency` simultaneous requests and print per-request RTF + p50/p95."""
    print(f"\n[bench] === concurrency={concurrency} ===")
    walls: list[float] = []
    statuses: list[int] = []

    wall_start = time.time()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [
            pool.submit(one_request, endpoint, route, wav_path, field)
            for _ in range(concurrency)
        ]
        for fut in as_completed(futures):
            wall, status = fut.result()
            walls.append(wall)
            statuses.append(status)
    batch_wall = time.time() - wall_start

    failures = sum(1 for s in statuses if s != 200)
    for i, (wall, status) in enumerate(zip(walls, statuses), start=1):
        rtf = audio_seconds / wall if wall > 0 else float("nan")
        flag = "" if status == 200 else f"  !! status={status}"
        print(f"[bench]   req {i:2d}: wall={wall:6.2f}s  rtf={rtf:6.3f} ({1.0 / rtf:5.1f}x realtime){flag}"
              if rtf == rtf and rtf > 0 else
              f"[bench]   req {i:2d}: wall={wall:6.2f}s  rtf=n/a{flag}")

    ok_walls = [w for w, s in zip(walls, statuses) if s == 200]
    if ok_walls:
        p50 = statistics.median(ok_walls)
        p95 = (
            statistics.quantiles(ok_walls, n=20)[-1]
            if len(ok_walls) >= 20
            else max(ok_walls)
        )
        p95_note = "" if len(ok_walls) >= 20 else " (max; <20 samples)"
        agg_rtf = (audio_seconds * len(ok_walls)) / batch_wall if batch_wall > 0 else float("nan")
        print(
            f"[bench]   p50={p50:.2f}s  p95={p95:.2f}s{p95_note}  "
            f"batch_wall={batch_wall:.2f}s  aggregate_rtf={agg_rtf:.3f}  failures={failures}"
        )
    else:
        print(f"[bench]   no successful requests (failures={failures})")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--endpoint", required=True, help="service base URL, e.g. http://meet-speaker-svc:8889")
    p.add_argument("--wav", required=True, type=Path, help="path to a sample .wav (16kHz mono works best)")
    p.add_argument("--field", default="audio", help="multipart field name for the audio file (default: audio)")
    p.add_argument(
        "--concurrency",
        default="1,2,4",
        help="comma-separated concurrency levels, e.g. '1,2,4' (default: 1,2,4)",
    )
    p.add_argument(
        "--route",
        default="/diarize",
        help="path to POST to, e.g. /diarize or /transcribe (default: /diarize)",
    )
    p.add_argument(
        "--audio-seconds",
        type=float,
        default=None,
        help="audio duration in seconds for RTF (default: read from the WAV header)",
    )
    args = p.parse_args()

    if not args.wav.exists():
        print(f"wav not found: {args.wav}", file=sys.stderr)
        return 1

    audio_seconds = args.audio_seconds
    if audio_seconds is None:
        audio_seconds = wav_duration_seconds(args.wav)
        if audio_seconds is None:
            print(
                "could not determine audio duration from the WAV header; "
                "pass --audio-seconds explicitly.",
                file=sys.stderr,
            )
            return 1

    try:
        levels = [int(x) for x in args.concurrency.split(",") if x.strip()]
    except ValueError:
        print(f"bad --concurrency value: {args.concurrency!r}", file=sys.stderr)
        return 1
    if not levels or any(n < 1 for n in levels):
        print(f"--concurrency must be one or more positive ints, got {args.concurrency!r}", file=sys.stderr)
        return 1

    print(
        f"[bench] target={args.endpoint} route={args.route} field={args.field} "
        f"wav={args.wav.name} audio_seconds={audio_seconds:.2f} concurrency={levels}"
    )

    # Best-effort health pre-check (non-fatal — not every service exposes /health).
    try:
        hr = requests.get(f"{args.endpoint.rstrip('/')}/health", timeout=5)
        if hr.ok:
            h = hr.json()
            print(
                f"[bench] /health OK cuda={h.get('cuda_available')} "
                f"backend_ok={h.get('diarizer_available', h.get('ready'))}"
            )
        else:
            print(f"[bench] /health returned {hr.status_code} — continuing anyway")
    except Exception as exc:  # noqa: BLE001
        print(f"[bench] /health unreachable ({type(exc).__name__}) — continuing anyway")

    for n in levels:
        run_level(args.endpoint, args.route, args.wav, args.field, n, audio_seconds)

    print("\n[bench] done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
