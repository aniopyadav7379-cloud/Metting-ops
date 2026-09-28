"""Unit tests for opus_decoder.

We construct synthetic Opus packets in-test via opuslib's encoder, then
roundtrip through OpusDecoder. No external audio files required.

If `opuslib` cannot import (system libopus missing), the tests are
skipped with a clear reason — the production container has libopus, so
this only triggers on bare dev hosts.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

import numpy as np
import pytest


# Make `import opus_decoder` work when pytest runs from anywhere.
SVC_ROOT = Path(__file__).resolve().parent.parent
if str(SVC_ROOT) not in sys.path:
    sys.path.insert(0, str(SVC_ROOT))


# Skip the whole module cleanly if opuslib can't be imported. We do this
# at collection time so a missing system library doesn't blow up pytest
# with a confusing OSError on libopus.so.0.
opuslib = pytest.importorskip("opuslib")

from opus_decoder import (  # noqa: E402  -- import after skip-check
    OpusDecoder,
    OpusDecoderError,
    opus_decode_to_pcm16,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_tone_pcm16(
    frequency_hz: float, duration_ms: int, sample_rate: int
) -> bytes:
    """Build a sine-wave PCM16 LE byte buffer at the given duration.

    Returned bytes are mono signed 16-bit little-endian.
    """
    n_samples = int(sample_rate * duration_ms / 1000)
    t = np.arange(n_samples) / float(sample_rate)
    samples = (np.sin(2 * np.pi * frequency_hz * t) * 16384.0).astype("<i2")
    return samples.tobytes()


@pytest.fixture
def encoder_48k_mono():
    """A vanilla 48 kHz mono VoIP-tuned Opus encoder."""
    enc = opuslib.Encoder(48000, 1, opuslib.APPLICATION_VOIP)
    enc.bitrate = 24000
    return enc


@pytest.fixture
def opus_packet_20ms(encoder_48k_mono) -> bytes:
    """A single 20 ms Opus packet of a 440 Hz tone at 48 kHz mono."""
    pcm = _make_tone_pcm16(frequency_hz=440.0, duration_ms=20, sample_rate=48000)
    # 20ms @ 48kHz = 960 samples per channel
    packet = encoder_48k_mono.encode(pcm, 960)
    return packet


# ---------------------------------------------------------------------------
# Happy-path roundtrip
# ---------------------------------------------------------------------------


class TestRoundtrip:
    def test_decode_at_16k_returns_pcm16_bytes(self, opus_packet_20ms):
        dec = OpusDecoder(sample_rate=16000, channels=1)
        out = dec.decode_packet(opus_packet_20ms)
        assert isinstance(out, bytes)
        # 20 ms @ 16 kHz mono PCM16 = 16000 * 0.020 * 2 bytes = 640 bytes.
        # libopus emits exactly the decoded packet's duration at the
        # configured output rate, so this is deterministic.
        assert len(out) == 640, f"expected 640 bytes (20ms @ 16k mono), got {len(out)}"
        dec.close()

    def test_decode_at_48k_returns_pcm16_bytes(self, opus_packet_20ms):
        dec = OpusDecoder(sample_rate=48000, channels=1)
        out = dec.decode_packet(opus_packet_20ms)
        # 20 ms @ 48 kHz mono PCM16 = 1920 bytes
        assert len(out) == 1920
        dec.close()

    def test_decoded_audio_preserves_tone_energy_and_frequency(
        self, encoder_48k_mono
    ):
        """Sanity-check the decoded signal carries the original tone.

        Opus is lossy AND introduces a fractional-sample phase shift
        (LPC analysis frame alignment), so direct sample-level
        correlation is the wrong metric. The right metric for a clean
        sine input is:

          1. RMS energy preserved (loose tolerance — Opus VBR drifts)
          2. Dominant FFT bin unchanged (the 440 Hz tone still peaks
             where it should)

        This catches degenerate cases like all-zeros, wrong sample
        rate, or channel-count mismatch.
        """
        # 480 ms of audio so the algorithmic delay (~6.5 ms) is a
        # rounding error in the FFT.
        pcm_in = _make_tone_pcm16(
            frequency_hz=440.0, duration_ms=480, sample_rate=48000
        )
        # 480ms @ 48k mono = 23040 samples. 24 x 20ms packets.
        packets = []
        for i in range(24):
            start = i * 960 * 2
            chunk = pcm_in[start : start + 960 * 2]
            packets.append(encoder_48k_mono.encode(chunk, 960))

        dec = OpusDecoder(sample_rate=48000, channels=1)
        decoded_bytes = b"".join(dec.decode_packet(p) for p in packets)
        dec.close()

        arr_in = np.frombuffer(pcm_in, dtype="<i2").astype(np.float32)
        arr_out = np.frombuffer(decoded_bytes, dtype="<i2").astype(np.float32)
        assert len(arr_in) == len(arr_out)

        # Drop the first 60 ms (Opus warm-up / look-ahead).
        skip = 2880
        in_s = arr_in[skip:]
        out_s = arr_out[skip:]

        # RMS energy — should be within +/-10% for a steady tone.
        rms_in = float(np.sqrt(np.mean(in_s**2)))
        rms_out = float(np.sqrt(np.mean(out_s**2)))
        assert rms_in > 100, "input tone went silent — fixture bug"
        ratio = rms_out / rms_in
        assert 0.9 < ratio < 1.1, (
            f"RMS energy drifted after Opus roundtrip: ratio={ratio:.3f}"
        )

        # FFT — dominant bin should be at 440 Hz in both signals.
        fft_in = np.abs(np.fft.rfft(in_s))
        fft_out = np.abs(np.fft.rfft(out_s))
        peak_in = int(np.argmax(fft_in))
        peak_out = int(np.argmax(fft_out))
        # Allow up to +/-1 bin of slack for very long signals where bin
        # width is ~2.6 Hz — generous to keep the test stable.
        assert abs(peak_in - peak_out) <= 1, (
            f"dominant frequency bin shifted after roundtrip: "
            f"input={peak_in}, output={peak_out}"
        )

    def test_decode_to_int16_mono_shape(self, opus_packet_20ms):
        dec = OpusDecoder(sample_rate=16000, channels=1)
        arr = dec.decode_to_int16(opus_packet_20ms)
        assert arr.dtype == np.int16
        assert arr.shape == (320,)  # 20ms @ 16kHz = 320 samples
        dec.close()

    def test_context_manager(self, opus_packet_20ms):
        with OpusDecoder(sample_rate=16000, channels=1) as dec:
            out = dec.decode_packet(opus_packet_20ms)
            assert len(out) == 640
        # After __exit__ the decoder should be closed
        with pytest.raises(OpusDecoderError):
            dec.decode_packet(opus_packet_20ms)

    def test_one_shot_helper(self, opus_packet_20ms):
        out = opus_decode_to_pcm16(opus_packet_20ms, sample_rate=16000, channels=1)
        assert isinstance(out, bytes)
        assert len(out) == 640


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestErrors:
    def test_empty_packet_raises(self):
        dec = OpusDecoder(sample_rate=16000, channels=1)
        with pytest.raises(OpusDecoderError, match="empty"):
            dec.decode_packet(b"")
        dec.close()

    def test_corrupt_packet_raises_decoder_error(self):
        dec = OpusDecoder(sample_rate=16000, channels=1)
        # Random bytes are very unlikely to be a valid Opus packet. Even
        # if libopus happens to accept the TOC byte it would error on
        # the underlying frame structure.
        bad = b"\xff" * 32
        with pytest.raises(OpusDecoderError, match="libopus decode failed"):
            dec.decode_packet(bad)
        dec.close()

    def test_invalid_channels_rejected(self):
        with pytest.raises(ValueError, match="channels"):
            OpusDecoder(sample_rate=16000, channels=3)

    def test_invalid_sample_rate_rejected(self):
        with pytest.raises(ValueError, match="sample_rate"):
            OpusDecoder(sample_rate=22050, channels=1)

    def test_decode_after_close_raises(self, opus_packet_20ms):
        dec = OpusDecoder(sample_rate=16000, channels=1)
        dec.close()
        with pytest.raises(OpusDecoderError, match="closed"):
            dec.decode_packet(opus_packet_20ms)


# ---------------------------------------------------------------------------
# Streaming-ish: many consecutive packets share decoder state
# ---------------------------------------------------------------------------


class TestStreaming:
    def test_many_packets_in_a_row(self, encoder_48k_mono):
        """Drive 100 consecutive 20ms packets through one decoder.

        Verifies the decoder doesn't accumulate state in a way that
        produces shorter / longer output over time, and doesn't leak.
        """
        dec = OpusDecoder(sample_rate=16000, channels=1)
        total_out = 0
        for i in range(100):
            # Vary frequency slightly so each packet is distinct
            freq = 220.0 + (i % 16) * 50.0
            pcm = _make_tone_pcm16(
                frequency_hz=freq, duration_ms=20, sample_rate=48000
            )
            packet = encoder_48k_mono.encode(pcm, 960)
            out = dec.decode_packet(packet)
            assert len(out) == 640, (
                f"frame {i}: expected 640 bytes (20ms @ 16k), got {len(out)}"
            )
            total_out += len(out)
        dec.close()
        # 100 frames * 640 bytes = 64000 bytes ≈ 2 seconds of 16 kHz mono audio
        assert total_out == 64000
