#!/usr/bin/env python3
"""Speaker-svc VRAM leak bench (task #79 v2 verification).

Hammer the /diarize endpoint with the same audio clip in a loop and
report VRAM trajectory. Run from any host with network access to the
target speaker-svc (Tailscale or direct).

Usage:
    python3 leak_bench.py \\
        --url http://meet-speaker-svc:8889 \\
        --audio /path/to/sample.wav \\
        --iterations 100 \\
        --gpu-host deploy@<gpu-node>

If --gpu-host is provided, we shell out to `ssh <host> nvidia-smi ...`
after each batch of 10 to record VRAM. Otherwise we just record the
HTTP latency and assume the operator is watching `nvidia-smi -l 1`
in another terminal.

Pass:   VRAM flat-or-decreasing across batches, or returns to baseline
        after each batch (caching allocator behaviour).
Fail:   VRAM monotonically increases across batches (genuine leak).
"""
from __future__ import annotations

import argparse
import statistics
import subprocess
import sys
import time
from pathlib import Path

import requests


def query_vram(gpu_host: str) -> int | None:
    """Return GPU 0 used VRAM in MiB. If gpu_host == 'local', call nvidia-smi
    directly; otherwise ssh to gpu_host. Returns None on failure."""
    try:
        if gpu_host == "local":
            cmd = ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits", "--id=0"]
        else:
            cmd = [
                "ssh",
                "-o",
                "ConnectTimeout=5",
                gpu_host,
                "nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits --id=0",
            ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            print(f"  [vram] nvidia-smi failed: {result.stderr.strip()}", file=sys.stderr)
            return None
        return int(result.stdout.strip())
    except (subprocess.TimeoutExpired, ValueError) as exc:
        print(f"  [vram] {type(exc).__name__}: {exc}", file=sys.stderr)
        return None


def one_diarize(url: str, audio_path: Path, return_embeddings: bool = True) -> tuple[float, int]:
    """Send one /diarize call. Returns (latency_seconds, http_status)."""
    started = time.time()
    with audio_path.open("rb") as fh:
        files = {"audio": (audio_path.name, fh, "audio/wav")}
        data = {"return_embeddings": str(return_embeddings).lower()}
        r = requests.post(f"{url}/diarize", files=files, data=data, timeout=300)
    return time.time() - started, r.status_code


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True, help="speaker-svc base URL, e.g. http://meet-speaker-svc:8889")
    p.add_argument("--audio", required=True, type=Path, help="path to a sample .wav (16kHz mono works best)")
    p.add_argument("--iterations", type=int, default=100, help="total /diarize requests")
    p.add_argument("--batch-size", type=int, default=10, help="log VRAM every N requests")
    p.add_argument("--gpu-host", default=None, help="ssh target for nvidia-smi, e.g. deploy@<gpu-node>")
    p.add_argument("--no-embeddings", action="store_true", help="set return_embeddings=false to isolate diarize-only path")
    args = p.parse_args()

    if not args.audio.exists():
        print(f"audio not found: {args.audio}", file=sys.stderr)
        return 1

    # Baseline sanity check.
    print(f"[bench] target={args.url} audio={args.audio.name} iter={args.iterations}")
    try:
        hr = requests.get(f"{args.url}/health", timeout=5)
        hr.raise_for_status()
        h = hr.json()
        print(f"[bench] /health OK cuda={h.get('cuda_available')} backend_ok={h.get('diarizer_available')}")
    except Exception as exc:  # noqa: BLE001
        print(f"[bench] /health failed: {exc}", file=sys.stderr)
        return 2

    baseline = query_vram(args.gpu_host) if args.gpu_host else None
    if baseline is not None:
        print(f"[bench] baseline VRAM (pre-run): {baseline} MiB")

    latencies: list[float] = []
    failures = 0
    samples: list[tuple[int, int | None, float | None]] = []  # (iter, vram_mib, batch_p50_latency)

    for i in range(1, args.iterations + 1):
        try:
            latency, status = one_diarize(args.url, args.audio, return_embeddings=not args.no_embeddings)
            if status != 200:
                failures += 1
                print(f"[bench] iter={i} status={status} latency={latency:.2f}s")
            else:
                latencies.append(latency)
        except requests.RequestException as exc:
            failures += 1
            print(f"[bench] iter={i} exception={type(exc).__name__}: {exc}")
            continue

        if i % args.batch_size == 0:
            batch_latencies = latencies[-args.batch_size :]
            p50 = statistics.median(batch_latencies) if batch_latencies else None
            vram = query_vram(args.gpu_host) if args.gpu_host else None
            samples.append((i, vram, p50))
            vram_s = f"{vram} MiB" if vram is not None else "n/a"
            p50_s = f"{p50:.2f}s" if p50 is not None else "n/a"
            print(f"[bench] iter={i:4d}  vram={vram_s:>10}  batch_p50={p50_s}  failures_so_far={failures}")

    print()
    print("[bench] === summary ===")
    print(f"[bench] requests: {args.iterations} (failures={failures})")
    if latencies:
        print(
            f"[bench] latency: p50={statistics.median(latencies):.2f}s "
            f"p95={statistics.quantiles(latencies, n=20)[-1] if len(latencies) >= 20 else float('nan'):.2f}s "
            f"mean={statistics.mean(latencies):.2f}s"
        )
    if samples and args.gpu_host:
        vram_series = [v for (_, v, _) in samples if v is not None]
        if vram_series:
            print(f"[bench] vram: start={vram_series[0]} MiB  end={vram_series[-1]} MiB  "
                  f"max={max(vram_series)} MiB  delta={vram_series[-1] - vram_series[0]:+d} MiB")
            if baseline is not None:
                print(f"[bench] vram delta vs pre-run baseline: {vram_series[-1] - baseline:+d} MiB")
            # Verdict heuristic.
            growth = vram_series[-1] - vram_series[0]
            if growth > 1000:
                print(f"[bench] VERDICT: VRAM grew {growth:+d} MiB across run — possible leak still present")
            elif growth > 200:
                print(f"[bench] VERDICT: VRAM grew {growth:+d} MiB — within noise/allocator-caching range, investigate if it keeps climbing")
            else:
                print(f"[bench] VERDICT: VRAM flat (delta={growth:+d} MiB) — leak likely fixed")

    return 0 if failures == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
