#!/usr/bin/env python3
"""
Setup Granite 3.3 8B model with Ollama
Configures for maximum iGPU offloading
"""
import subprocess
import json
import requests
import time
import sys


def check_ollama_running():
    """Check if Ollama is running"""
    try:
        response = requests.get("http://localhost:11434/api/tags")
        return response.status_code == 200
    except:
        return False


def start_ollama():
    """Start Ollama service"""
    print("Starting Ollama service...")
    try:
        # Try systemctl first
        subprocess.run(["sudo", "systemctl", "start", "ollama"], check=False)
        time.sleep(3)
        
        if not check_ollama_running():
            # Try direct ollama serve
            subprocess.Popen(["ollama", "serve"], 
                           stdout=subprocess.DEVNULL, 
                           stderr=subprocess.DEVNULL)
            time.sleep(3)
    except Exception as e:
        print(f"Warning: Could not auto-start Ollama: {e}")
        print("Please start Ollama manually: ollama serve")


def pull_granite_model():
    """Pull Granite 3.3 8B model"""
    print("\nPulling Granite 3.3 8B model...")
    print("This may take a while depending on your internet connection...")
    
    try:
        result = subprocess.run(
            ["ollama", "pull", "granite3.3:8b"],
            capture_output=True,
            text=True
        )
        
        if result.returncode == 0:
            print("✓ Granite 3.3 8B model downloaded successfully")
            return True
        else:
            print(f"✗ Failed to pull model: {result.stderr}")
            return False
    except Exception as e:
        print(f"✗ Error pulling model: {e}")
        return False


def create_optimized_modelfile():
    """Create optimized Modelfile for GPU offloading"""
    
    modelfile_content = """# Granite 3.3 8B optimized for Meeting-Ops
FROM granite3.3:8b

# GPU optimization parameters
PARAMETER num_gpu 99
PARAMETER num_thread 8
PARAMETER num_ctx 8192
PARAMETER num_batch 512

# Performance tuning
PARAMETER mirostat 0
PARAMETER mirostat_eta 0.1
PARAMETER mirostat_tau 5.0
PARAMETER repeat_last_n 64
PARAMETER repeat_penalty 1.1

# Temperature for balanced creativity/accuracy
PARAMETER temperature 0.7
PARAMETER top_k 40
PARAMETER top_p 0.9

# Stop sequences for structured output
PARAMETER stop "User:"
PARAMETER stop "Assistant:"
PARAMETER stop "```"

# System message template for meeting intelligence
SYSTEM """You are an AI meeting assistant optimized for real-time transcription enhancement and note generation. 
Focus on clarity, accuracy, and extracting actionable insights from meeting conversations.
Format your responses as structured JSON when requested."""
"""
    
    # Save modelfile
    with open("/tmp/Modelfile.granite", "w") as f:
        f.write(modelfile_content)
    
    print("\n✓ Created optimized Modelfile")
    return "/tmp/Modelfile.granite"


def create_custom_model(modelfile_path):
    """Create custom model with optimizations"""
    print("\nCreating optimized Granite model for Meeting-Ops...")
    
    try:
        result = subprocess.run(
            ["ollama", "create", "meeting-ops-granite", "-f", modelfile_path],
            capture_output=True,
            text=True
        )
        
        if result.returncode == 0:
            print("✓ Custom model 'meeting-ops-granite' created successfully")
            return True
        else:
            print(f"✗ Failed to create custom model: {result.stderr}")
            return False
    except Exception as e:
        print(f"✗ Error creating model: {e}")
        return False


def test_model():
    """Test the model with a sample request"""
    print("\nTesting Granite model...")
    
    test_prompt = """Format this meeting transcript and extract key information:

"So john was saying we need to finish the api integration by friday and sarah mentioned 
she needs two more days for testing mike said the ui is done but needs review action item 
for everyone is to review the pr by tomorrow"

Respond with JSON containing: formatted_transcript, action_items, deadline"""
    
    try:
        response = requests.post(
            "http://localhost:11434/api/generate",
            json={
                "model": "meeting-ops-granite",
                "prompt": test_prompt,
                "options": {
                    "num_gpu": 99,
                    "temperature": 0.7
                },
                "stream": False
            },
            timeout=30
        )
        
        if response.status_code == 200:
            result = response.json()
            print("\n✓ Model test successful!")
            print("\nSample response:")
            print("-" * 50)
            print(result.get("response", "")[:500])
            print("-" * 50)
            return True
        else:
            print(f"✗ Model test failed: {response.status_code}")
            return False
    except Exception as e:
        print(f"✗ Error testing model: {e}")
        return False


def check_gpu_usage():
    """Check GPU memory usage"""
    print("\nChecking GPU utilization...")
    
    try:
        # Try AMD GPU check
        result = subprocess.run(
            ["rocm-smi", "--showmeminfo", "vram"],
            capture_output=True,
            text=True
        )
        
        if result.returncode == 0:
            print("AMD GPU Memory Info:")
            print(result.stdout)
            return
    except:
        pass
    
    try:
        # Try Intel GPU check
        result = subprocess.run(
            ["intel_gpu_top", "-l", "1"],
            capture_output=True,
            text=True
        )
        
        if result.returncode == 0:
            print("Intel GPU Info:")
            print(result.stdout[:500])
            return
    except:
        pass
    
    print("Note: GPU monitoring tools not found. Model will still use available GPU.")


def configure_environment():
    """Set environment variables for optimal performance"""
    
    env_vars = {
        "OLLAMA_NUM_GPU": "99",
        "OLLAMA_GPU_LAYERS": "99", 
        "OLLAMA_HOST": "0.0.0.0:11434",
        "OLLAMA_MODELS": "/srv/.ollama/models",
        "OLLAMA_KEEP_ALIVE": "10m",
        "HSA_OVERRIDE_GFX_VERSION": "11.0.0",  # For AMD GPUs
        "CUDA_VISIBLE_DEVICES": "0"  # For NVIDIA GPUs
    }
    
    print("\nRecommended environment variables for optimal performance:")
    print("-" * 50)
    for key, value in env_vars.items():
        print(f"export {key}={value}")
    print("-" * 50)
    print("\nAdd these to your ~/.bashrc or /etc/environment for persistence")
    
    # Try to set them for current session
    import os
    for key, value in env_vars.items():
        os.environ[key] = value


def main():
    """Main setup process"""
    print("=" * 60)
    print("Meeting-Ops Granite 3.3 8B Setup")
    print("=" * 60)
    
    # Check and start Ollama
    if not check_ollama_running():
        print("\n⚠ Ollama is not running")
        start_ollama()
        
        if not check_ollama_running():
            print("\n✗ Could not start Ollama automatically")
            print("Please start Ollama manually: ollama serve")
            print("Then run this script again")
            sys.exit(1)
    else:
        print("\n✓ Ollama is running")
    
    # Configure environment
    configure_environment()
    
    # Pull base model
    if not pull_granite_model():
        print("\n✗ Failed to pull Granite model")
        print("Please check your internet connection and try again")
        sys.exit(1)
    
    # Create optimized version
    modelfile_path = create_optimized_modelfile()
    if not create_custom_model(modelfile_path):
        print("\n⚠ Could not create optimized model, using base model")
        print("You can still use 'granite3.3:8b' directly")
    
    # Test the model
    test_model()
    
    # Check GPU usage
    check_gpu_usage()
    
    print("\n" + "=" * 60)
    print("✓ Setup Complete!")
    print("=" * 60)
    print("\nYour Granite model is ready to use:")
    print("  - Optimized model: meeting-ops-granite")
    print("  - Base model: granite3.3:8b")
    print("\nThe model is configured for maximum GPU offloading (99 layers)")
    print("Adjust num_gpu parameter if you experience memory issues")
    print("\nTo use in Meeting-Ops, update your agent configuration to use:")
    print("  Provider: ollama")
    print("  Model: meeting-ops-granite")


if __name__ == "__main__":
    main()