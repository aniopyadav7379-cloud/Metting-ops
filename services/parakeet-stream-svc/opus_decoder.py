"""Opus packet decoder for the Phase B server-live streaming path.

Wraps `opuslib` (libopus Python binding) with a small interface that the
parakeet-stream-svc WS handler can plug in when a client uplinks `"OPUS"`
payload frames (per docs/phase-b-server-live-streaming.md section 4).

Decoder model
-------------
libopus operates internally at 48 kHz. We let it decode at 48 kHz and
then resample down to whatever the caller asked for (Parakeet wants
16 kHz mono PCM16-LE). Resampling uses a simple linear-interpolation
path that's fine for speech-band audio at the rates we care about
(48 -> 16 kHz, integer 3x downsample with anti-alias via numpy is
overkill for an MVP; we keep it linear and let Parakeet's own
front-end do the heavy lifting on the output PCM).

Frame size
----------
Opus packets in this pipeline are produced by browser MediaRecorder or
WebCodecs `AudioEncoder` with default 20 ms framing. At 48 kHz that's
960 samples per channel. We pass `frame_size=960` to libopus by
default; libopus accepts the actual packet duration encoded in the TOC
byte and decodes whatever the encoder produced, so this is a
conservative ceiling rather than a hard contract.
"""
from __future__ import annotations

import struct
from typing import Optional

import numpy as np


# Opus codec internal sample rate (libopus is always 48 kHz internally).
_OPUS_INTERNAL_RATE = 48000

# Max samples per channel libopus will emit for a 60 ms packet at 48 kHz.
# Used to size the decode buffer so we never short-buffer libopus.
_MAX_FRAME_SIZE = 2880  # 60ms @ 48kHz


class OpusDecoderError(Exception):
    """Raised when an Opus packet cannot be decoded."""


class OpusDecoder:
    """Decode Opus packets to PCM16 little-endian bytes.

    Parameters
    ----------
    sample_rate
        Desired output sample rate in Hz. Common values: 16000 (Parakeet
        ASR), 48000 (passthrough). The decoder always runs libopus at
        48 kHz and resamples to this rate as a post-step.
    channels
        Output channel count. Currently 1 (mono) is supported; 2
        (stereo) works at the libopus layer but the resampler treats
        the buffer as mono — pass 1 unless you know what you're doing.
    """

    def __init__(self, sample_rate: int = 16000, channels: int = 1) -> None:
        if channels not in (1, 2):
            raise ValueError(f"channels must be 1 or 2, got {channels}")
        if sample_rate not in (8000, 12000, 16000, 24000, 48000):
            # libopus accepts these output rates natively; for everything
            # else we'd need an external resampler. 16 and 48 are the
            # only ones the streaming pipeline cares about today.
            raise ValueError(
                f"sample_rate must be one of 8000/12000/16000/24000/48000, "
                f"got {sample_rate}"
            )

        # Import opuslib lazily so the module is importable even when
        # the native libopus is missing (e.g., unit tests on a host
        # without the system library). The error will surface on first
        # use rather than at module load.
        try:
            import opuslib  # type: ignore[import-not-found]
        except ImportError as e:  # pragma: no cover - exercised in error paths
            raise OpusDecoderError(
                "opuslib is not installed; add `opuslib` to requirements.txt "
                "and ensure the system libopus is available."
            ) from e

        self._opuslib = opuslib
        self._sample_rate = sample_rate
        self._channels = channels

        # We decode at the requested rate directly when libopus supports
        # it natively. libopus' Decoder accepts 8/12/16/24/48 kHz output
        # rates and resamples internally. This avoids us doing our own
        # resample math for the common 16 kHz case.
        self._decoder = opuslib.Decoder(sample_rate, channels)
        self._closed = False

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def channels(self) -> int:
        return self._channels

    def decode_packet(
        self, opus_packet: bytes, frame_size: Optional[int] = None
    ) -> bytes:
        """Decode one Opus packet -> PCM16 little-endian bytes.

        Parameters
        ----------
        opus_packet
            Raw Opus packet (NOT an OggOpus stream and NOT a WebM
            container — the WS framing strips containers upstream and
            hands us the bare packet payload).
        frame_size
            Optional max samples-per-channel the decoder should expect.
            Defaults to a 60 ms ceiling, which fits any legal Opus
            packet duration. libopus will produce however many samples
            the packet actually encodes.

        Returns
        -------
        PCM16 little-endian bytes at the configured sample rate /
        channel count.
        """
        if self._closed:
            raise OpusDecoderError("decoder is closed")
        if not opus_packet:
            raise OpusDecoderError("empty Opus packet")

        # libopus is forgiving about frame_size as long as the buffer is
        # at least as large as the packet's actual duration. Use the
        # output rate to convert the 60 ms ceiling, since libopus emits
        # at the configured decoder rate, not 48 kHz.
        effective_frame_size = frame_size if frame_size else int(
            _MAX_FRAME_SIZE * self._sample_rate / _OPUS_INTERNAL_RATE
        )

        try:
            pcm = self._decoder.decode(opus_packet, effective_frame_size)
        except Exception as e:
            raise OpusDecoderError(f"libopus decode failed: {e}") from e

        # opuslib returns bytes already in the right format (signed
        # 16-bit LE, interleaved channels). Pass through.
        return pcm

    def decode_to_int16(
        self, opus_packet: bytes, frame_size: Optional[int] = None
    ) -> np.ndarray:
        """Convenience wrapper: decode -> numpy int16 array.

        Returned shape:
          mono:   (samples,)
          stereo: (samples, 2)
        """
        pcm = self.decode_packet(opus_packet, frame_size=frame_size)
        arr = np.frombuffer(pcm, dtype="<i2")
        if self._channels == 2:
            arr = arr.reshape(-1, 2)
        return arr

    def close(self) -> None:
        """Release decoder resources.

        opuslib's Decoder ties its native handle to garbage collection,
        so this is mostly a marker that the decoder shouldn't be used
        any more. We drop the reference so a leaked decode_packet call
        fails fast.
        """
        self._closed = True
        self._decoder = None  # type: ignore[assignment]

    def __enter__(self) -> "OpusDecoder":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def opus_decode_to_pcm16(
    opus_packet: bytes,
    sample_rate: int = 16000,
    channels: int = 1,
) -> bytes:
    """One-shot decode for callers that don't want to hold a decoder.

    Note: this allocates a fresh decoder per call, so it's *not* the
    right path for a continuous stream — Opus decoders carry inter-frame
    state (LPC coefficients, gain history) that improves quality. For
    streaming use, hold an `OpusDecoder` instance per WS connection.
    """
    with OpusDecoder(sample_rate=sample_rate, channels=channels) as dec:
        return dec.decode_packet(opus_packet)
