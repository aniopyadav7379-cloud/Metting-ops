"""
Speaker Diarization Service (Lightweight)
Uses acoustic feature clustering for speaker segmentation.
No PyTorch, no HuggingFace token, no CUDA required.
Dependencies: numpy, scipy, soundfile (all already installed)
"""

import logging
import os
import tempfile
import numpy as np
import soundfile as sf
from typing import Dict, List, Optional, Any, Tuple

logger = logging.getLogger(__name__)


class SpeakerSegment:
    """A segment of audio attributed to a speaker"""
    __slots__ = ("start", "end", "speaker", "confidence")

    def __init__(self, start: float, end: float, speaker: str, confidence: float = 0.8):
        self.start = start
        self.end = end
        self.speaker = speaker
        self.confidence = confidence

    def to_dict(self) -> Dict[str, Any]:
        return {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "speaker": self.speaker,
            "confidence": round(self.confidence, 3),
            "duration": round(self.end - self.start, 3),
        }


def _extract_features(audio: np.ndarray, sr: int, frame_len: float = 0.025, hop_len: float = 0.010) -> np.ndarray:
    """
    Extract acoustic features per frame: energy, ZCR, spectral centroid, spectral rolloff.
    Returns (n_frames, 4) feature matrix.
    """
    frame_samples = int(frame_len * sr)
    hop_samples = int(hop_len * sr)
    n_frames = max(1, (len(audio) - frame_samples) // hop_samples + 1)

    features = np.zeros((n_frames, 4), dtype=np.float32)

    for i in range(n_frames):
        start = i * hop_samples
        end = start + frame_samples
        frame = audio[start:end]

        if len(frame) < frame_samples:
            frame = np.pad(frame, (0, frame_samples - len(frame)))

        # 1. Log energy
        energy = np.sum(frame ** 2)
        features[i, 0] = np.log1p(energy)

        # 2. Zero-crossing rate
        signs = np.sign(frame)
        signs[signs == 0] = 1
        zcr = np.sum(np.abs(np.diff(signs))) / (2.0 * len(frame))
        features[i, 1] = zcr

        # 3. Spectral centroid (via FFT)
        magnitude = np.abs(np.fft.rfft(frame))
        freqs = np.fft.rfftfreq(len(frame), d=1.0 / sr)
        mag_sum = np.sum(magnitude)
        if mag_sum > 1e-10:
            features[i, 2] = np.sum(freqs * magnitude) / mag_sum
        else:
            features[i, 2] = 0.0

        # 4. Spectral rolloff (85th percentile frequency)
        cumsum = np.cumsum(magnitude)
        if cumsum[-1] > 1e-10:
            rolloff_idx = np.searchsorted(cumsum, 0.85 * cumsum[-1])
            features[i, 3] = freqs[min(rolloff_idx, len(freqs) - 1)]

    # Normalize features to zero mean, unit variance
    mean = features.mean(axis=0, keepdims=True)
    std = features.std(axis=0, keepdims=True)
    std[std < 1e-8] = 1.0
    features = (features - mean) / std

    return features


def _detect_speech_frames(audio: np.ndarray, sr: int, frame_len: float = 0.025, hop_len: float = 0.010) -> np.ndarray:
    """
    Energy-based VAD with adaptive thresholding.
    Works with both loud and quiet recordings.
    Returns boolean array per frame.
    """
    frame_samples = int(frame_len * sr)
    hop_samples = int(hop_len * sr)
    n_frames = max(1, (len(audio) - frame_samples) // hop_samples + 1)

    energies = np.zeros(n_frames, dtype=np.float32)
    for i in range(n_frames):
        start = i * hop_samples
        end = start + frame_samples
        frame = audio[start:end]
        energies[i] = np.sum(frame ** 2)

    if energies.max() < 1e-15:
        return np.zeros(n_frames, dtype=bool)

    # Use dB scale for better dynamic range
    db_energies = 10 * np.log10(energies + 1e-30)

    # Adaptive threshold: use percentile-based approach
    # Speech typically occupies the upper portion of the energy distribution
    p20 = np.percentile(db_energies, 20)  # Approximate noise floor
    p80 = np.percentile(db_energies, 80)  # Approximate speech level

    # Threshold between noise and speech
    threshold = p20 + 0.35 * (p80 - p20)

    return db_energies > threshold


def _cluster_features(features: np.ndarray, speech_mask: np.ndarray, max_speakers: int = 8, min_cluster_size: int = 200) -> np.ndarray:
    """
    Cluster speech frames into speakers using k-means with BIC model selection.
    Returns speaker label per frame (-1 for silence).
    """
    n_frames = len(features)
    labels = np.full(n_frames, -1, dtype=np.int32)

    # Get speech frame indices
    speech_indices = np.where(speech_mask)[0]
    if len(speech_indices) < min_cluster_size:
        # Not enough speech - assign all to speaker 0
        labels[speech_mask] = 0
        return labels

    # Use windowed features (average over ~0.5s windows) for more stable clustering
    window_size = 50  # 50 frames = 0.5s at 10ms hop
    n_windows = len(speech_indices) // window_size
    if n_windows < 2:
        labels[speech_mask] = 0
        return labels

    windowed_features = np.zeros((n_windows, features.shape[1]), dtype=np.float32)
    window_indices = []
    for w in range(n_windows):
        start_idx = w * window_size
        end_idx = start_idx + window_size
        window_frames = speech_indices[start_idx:end_idx]
        windowed_features[w] = features[window_frames].mean(axis=0)
        window_indices.append(window_frames)

    # Use k-means with BIC to determine number of speakers
    best_labels = None
    best_score = -np.inf
    max_k = min(max_speakers + 1, n_windows // 3 + 1)

    for k in range(1, max_k):
        cluster_labels, score = _kmeans_bic(windowed_features, k, max_iter=30)
        if score > best_score:
            best_score = score
            best_labels = cluster_labels

    # Map window labels back to frame labels
    if best_labels is not None:
        for w in range(n_windows):
            labels[window_indices[w]] = best_labels[w]

        # Fill remaining speech frames with nearest window's label
        remaining = speech_indices[n_windows * window_size:]
        if len(remaining) > 0 and best_labels is not None:
            labels[remaining] = best_labels[-1]

    return labels


def _kmeans_bic(X: np.ndarray, k: int, max_iter: int = 20) -> Tuple[np.ndarray, float]:
    """K-means with BIC score for model selection."""
    n, d = X.shape

    if k == 1:
        labels = np.zeros(n, dtype=np.int32)
        # BIC for single cluster
        var = np.var(X)
        if var < 1e-10:
            var = 1e-10
        ll = -0.5 * n * d * np.log(2 * np.pi * var) - 0.5 * n * d
        bic = ll - 0.5 * (d + 1) * np.log(n)
        return labels, bic

    # Initialize centroids with k-means++
    centroids = np.zeros((k, d), dtype=np.float32)
    centroids[0] = X[np.random.randint(n)]
    for c in range(1, k):
        dists = np.min([np.sum((X - centroids[j]) ** 2, axis=1) for j in range(c)], axis=0)
        probs = dists / (dists.sum() + 1e-10)
        centroids[c] = X[np.random.choice(n, p=probs)]

    labels = np.zeros(n, dtype=np.int32)

    for _ in range(max_iter):
        # Assign
        dists = np.stack([np.sum((X - centroids[j]) ** 2, axis=1) for j in range(k)], axis=1)
        new_labels = np.argmin(dists, axis=1)

        if np.array_equal(labels, new_labels):
            break
        labels = new_labels

        # Update centroids
        for j in range(k):
            mask = labels == j
            if mask.any():
                centroids[j] = X[mask].mean(axis=0)

    # Compute BIC
    total_var = 0.0
    for j in range(k):
        mask = labels == j
        count = mask.sum()
        if count > 1:
            total_var += np.sum((X[mask] - centroids[j]) ** 2)

    var = total_var / max(n - k, 1) / d
    if var < 1e-10:
        var = 1e-10

    ll = -0.5 * n * d * np.log(2 * np.pi * var) - 0.5 * total_var / var
    num_params = k * d + k  # centroids + weights
    bic = ll - 0.5 * num_params * np.log(n)

    return labels, bic


def _smooth_labels(labels: np.ndarray, min_segment_frames: int = 30) -> np.ndarray:
    """Smooth speaker labels: remove very short segments (< min_segment_frames)."""
    smoothed = labels.copy()
    n = len(labels)
    i = 0
    while i < n:
        # Find run of same label
        j = i
        while j < n and labels[j] == labels[i]:
            j += 1
        run_len = j - i

        # If run is too short and not silence, merge with neighbors
        if run_len < min_segment_frames and labels[i] >= 0:
            # Find the neighboring speaker with the longest segment
            left_label = labels[i - 1] if i > 0 else -1
            right_label = labels[j] if j < n else -1
            # Prefer non-silence neighbor
            if left_label >= 0:
                smoothed[i:j] = left_label
            elif right_label >= 0:
                smoothed[i:j] = right_label

        i = j

    return smoothed


def diarize_audio(audio: np.ndarray, sr: int = 16000, max_speakers: int = 6) -> List[SpeakerSegment]:
    """
    Perform speaker diarization on audio array.

    Args:
        audio: 1-D float32 array, mono, any sample rate
        sr: sample rate
        max_speakers: maximum number of speakers to detect

    Returns:
        List of SpeakerSegment with start/end times and speaker labels
    """
    if len(audio) == 0:
        return []

    # Ensure mono
    if audio.ndim > 1:
        audio = audio.mean(axis=1)

    hop_len = 0.010  # 10ms hops

    # Extract features
    features = _extract_features(audio, sr, hop_len=hop_len)
    speech_mask = _detect_speech_frames(audio, sr, hop_len=hop_len)

    # Cluster into speakers
    labels = _cluster_features(features, speech_mask, max_speakers=max_speakers)

    # Aggressive smoothing: min 2s segments to get clean speaker turns
    labels = _smooth_labels(labels, min_segment_frames=200)

    # Convert frame labels to time segments, merging adjacent same-speaker segments
    segments = []
    n = len(labels)
    i = 0
    while i < n:
        if labels[i] < 0:
            i += 1
            continue

        j = i
        while j < n and labels[j] == labels[i]:
            j += 1

        start_time = i * hop_len
        end_time = j * hop_len
        duration = end_time - start_time

        # Skip very short segments (< 0.5s)
        if duration >= 0.5:
            speaker_id = f"Speaker_{labels[i] + 1}"
            segments.append(SpeakerSegment(start_time, end_time, speaker_id, 0.75))

        i = j

    # Merge adjacent segments of the same speaker (separated by short silence)
    if len(segments) > 1:
        merged = [segments[0]]
        for seg in segments[1:]:
            prev = merged[-1]
            gap = seg.start - prev.end
            if seg.speaker == prev.speaker and gap < 1.5:
                # Merge: extend the previous segment
                merged[-1] = SpeakerSegment(prev.start, seg.end, seg.speaker, 0.75)
            else:
                merged.append(seg)
        segments = merged

    return segments


def diarize_file(file_path: str, max_speakers: int = 8) -> Dict[str, Any]:
    """
    Diarize an audio file.

    Args:
        file_path: Path to audio file
        max_speakers: Maximum speakers to detect

    Returns:
        Dict with speakers, timeline, num_speakers
    """
    try:
        audio, sr = sf.read(file_path, dtype="float32")
        segments = diarize_audio(audio, sr, max_speakers)

        # Build speaker summary
        speaker_times: Dict[str, float] = {}
        speaker_counts: Dict[str, int] = {}
        for seg in segments:
            duration = seg.end - seg.start
            speaker_times[seg.speaker] = speaker_times.get(seg.speaker, 0) + duration
            speaker_counts[seg.speaker] = speaker_counts.get(seg.speaker, 0) + 1

        speakers = [
            {
                "id": spk,
                "total_time": round(speaker_times[spk], 2),
                "segments": speaker_counts[spk],
            }
            for spk in sorted(speaker_times.keys())
        ]

        timeline = [seg.to_dict() for seg in segments]

        return {
            "speakers": speakers,
            "timeline": timeline,
            "num_speakers": len(speakers),
            "total_segments": len(timeline),
            "success": True,
        }
    except Exception as e:
        logger.error(f"Diarization failed: {e}")
        return {
            "speakers": [],
            "timeline": [],
            "num_speakers": 0,
            "total_segments": 0,
            "success": False,
            "error": str(e),
        }


def assign_speakers_to_transcript(
    segments: List[Dict[str, Any]], diarization: List[SpeakerSegment]
) -> List[Dict[str, Any]]:
    """
    Assign speaker labels to transcript segments by matching timestamps.

    Args:
        segments: Transcript segments with 'start', 'end', 'text'
        diarization: Speaker diarization segments

    Returns:
        Transcript segments with 'speaker' field added
    """
    if not diarization:
        return segments

    result = []
    for seg in segments:
        seg_start = seg.get("start", 0)
        seg_end = seg.get("end", seg_start)
        seg_mid = (seg_start + seg_end) / 2

        # Find the diarization segment that overlaps most with this transcript segment
        best_speaker = None
        best_overlap = 0

        for d in diarization:
            overlap_start = max(seg_start, d.start)
            overlap_end = min(seg_end, d.end)
            overlap = max(0, overlap_end - overlap_start)

            if overlap > best_overlap:
                best_overlap = overlap
                best_speaker = d.speaker

        # Fallback: use midpoint match
        if best_speaker is None:
            for d in diarization:
                if d.start <= seg_mid <= d.end:
                    best_speaker = d.speaker
                    break

        enriched = dict(seg)
        enriched["speaker"] = best_speaker
        result.append(enriched)

    return result
