#!/usr/bin/env python3
"""Create test speech audio for transcription testing"""

import numpy as np
import librosa
import soundfile as sf
import tempfile
import os

def create_test_speech():
    """Create a test speech file for better transcription testing"""
    
    # Since we can't generate real speech, let's create a more realistic audio signal
    # that might have different characteristics than pure sine waves
    
    sample_rate = 16000
    duration = 5.0  # 5 seconds
    
    # Create a more complex signal that might be interpreted as speech
    t = np.linspace(0, duration, int(sample_rate * duration))
    
    # Create multiple frequency components that might resemble speech patterns
    # Human speech typically has formants (resonant frequencies) around:
    # - F1: 200-800 Hz
    # - F2: 800-2200 Hz 
    # - F3: 2200-3000 Hz
    
    # Base frequency (like fundamental frequency of voice)
    f0 = 150  # Hz (male voice range)
    
    # Create signal with speech-like characteristics
    signal = np.zeros_like(t)
    
    # Add fundamental frequency
    signal += 0.3 * np.sin(2 * np.pi * f0 * t)
    
    # Add formants (simplified)
    signal += 0.2 * np.sin(2 * np.pi * 500 * t)   # F1
    signal += 0.15 * np.sin(2 * np.pi * 1200 * t)  # F2
    signal += 0.1 * np.sin(2 * np.pi * 2500 * t)   # F3
    
    # Add some noise and amplitude modulation to make it more speech-like
    noise = np.random.normal(0, 0.05, len(t))
    signal += noise
    
    # Add amplitude modulation (like speech rhythm)
    envelope = 0.5 + 0.3 * np.sin(2 * np.pi * 3 * t)  # 3 Hz modulation
    signal *= envelope
    
    # Add some pauses (like natural speech)
    pause_mask = np.ones_like(t)
    pause_mask[int(1.5*sample_rate):int(2.0*sample_rate)] = 0.1  # Quiet section
    pause_mask[int(3.5*sample_rate):int(3.8*sample_rate)] = 0.1  # Another quiet section
    signal *= pause_mask
    
    # Normalize
    signal = signal / np.max(np.abs(signal)) * 0.7
    
    # Save to file
    output_path = "test_speech.wav"
    sf.write(output_path, signal, sample_rate)
    
    print(f"Created test speech file: {output_path}")
    print(f"Duration: {duration} seconds")
    print(f"Sample rate: {sample_rate} Hz")
    print(f"Signal characteristics:")
    print(f"  - Fundamental frequency: {f0} Hz")
    print(f"  - Formant frequencies: 500, 1200, 2500 Hz")
    print(f"  - Amplitude modulation: 3 Hz")
    print(f"  - Contains pauses and noise")
    
    return output_path

if __name__ == "__main__":
    create_test_speech()