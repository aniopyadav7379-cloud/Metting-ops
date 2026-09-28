#!/usr/bin/env python3
"""
Create test audio with speech synthesis
"""

import numpy as np
import wave
import subprocess
import os

def create_test_audio():
    """Create test audio file with TTS"""
    
    text = "Hello, this is a test of the Unicorn Commander meeting transcription system. The NPU acceleration is working properly."
    output_file = "/tmp/test_speech.wav"
    
    # Use espeak to generate speech
    try:
        # Generate speech with espeak
        subprocess.run([
            "espeak",
            "-w", output_file,
            "-s", "150",  # Speed
            "-p", "50",   # Pitch
            text
        ], check=True)
        
        print(f"✅ Created test audio: {output_file}")
        
        # Convert to 16kHz mono if needed
        temp_file = "/tmp/test_speech_16k.wav"
        subprocess.run([
            "ffmpeg", "-i", output_file,
            "-ar", "16000",
            "-ac", "1",
            "-y", temp_file
        ], check=True)
        
        os.rename(temp_file, output_file)
        print(f"✅ Converted to 16kHz mono")
        
        return output_file
        
    except subprocess.CalledProcessError as e:
        print(f"❌ Failed to create test audio: {e}")
        
        # Fallback: create a tone
        duration = 3.0
        sample_rate = 16000
        frequency = 440.0
        
        t = np.linspace(0, duration, int(sample_rate * duration))
        audio = np.sin(2 * np.pi * frequency * t) * 0.5
        audio_int16 = (audio * 32767).astype(np.int16)
        
        with wave.open(output_file, 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(audio_int16.tobytes())
            
        print(f"✅ Created test tone: {output_file}")
        return output_file

if __name__ == "__main__":
    create_test_audio()