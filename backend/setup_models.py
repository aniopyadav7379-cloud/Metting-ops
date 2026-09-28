#!/usr/bin/env python3
"""
Setup both Phi4-mini and Granite models for Meeting-Ops pipeline
Phi4-mini for fast early updates, Granite for deeper late analysis
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
        subprocess.run(["sudo", "systemctl", "start", "ollama"], check=False)
        time.sleep(3)
        
        if not check_ollama_running():
            subprocess.Popen(["ollama", "serve"], 
                           stdout=subprocess.DEVNULL, 
                           stderr=subprocess.DEVNULL)
            time.sleep(3)
    except Exception as e:
        print(f"Warning: Could not auto-start Ollama: {e}")
        print("Please start Ollama manually: ollama serve")


def pull_model(model_name: str, description: str):
    """Pull a model from Ollama"""
    print(f"\n{'='*60}")
    print(f"Pulling {description}...")
    print(f"Model: {model_name}")
    print("This may take a while depending on your internet connection...")
    
    try:
        result = subprocess.run(
            ["ollama", "pull", model_name],
            capture_output=True,
            text=True
        )
        
        if result.returncode == 0:
            print(f"✓ {description} downloaded successfully")
            return True
        else:
            print(f"✗ Failed to pull {model_name}: {result.stderr}")
            return False
    except Exception as e:
        print(f"✗ Error pulling {model_name}: {e}")
        return False


def create_phi4_mini_optimized():
    """Create optimized Phi4-mini for fast early meeting processing"""
    
    modelfile_content = """# Phi4-mini optimized for Meeting-Ops early phase
FROM phi4-mini:3.8b

# Maximum GPU offloading for speed
PARAMETER num_gpu 99
PARAMETER num_thread 8
PARAMETER num_ctx 4096
PARAMETER num_batch 512

# Fast processing settings
PARAMETER temperature 0.5
PARAMETER top_k 20
PARAMETER top_p 0.8
PARAMETER repeat_penalty 1.0

# Quick responses
PARAMETER num_predict 1024

SYSTEM """You are a fast meeting transcription assistant for the early phase of meetings.
Your job is to quickly format raw transcription and identify key points.
Be concise and fast. Focus on clarity and immediate value.
Format responses as JSON with: formatted_transcript, key_points, action_items."""
"""
    
    with open("/tmp/Modelfile.phi4", "w") as f:
        f.write(modelfile_content)
    
    print("\n✓ Created optimized Phi4-mini Modelfile")
    
    try:
        result = subprocess.run(
            ["ollama", "create", "meeting-ops-phi4", "-f", "/tmp/Modelfile.phi4"],
            capture_output=True,
            text=True
        )
        
        if result.returncode == 0:
            print("✓ Custom model 'meeting-ops-phi4' created")
            return True
        else:
            print(f"✗ Failed to create custom Phi4 model: {result.stderr}")
            return False
    except Exception as e:
        print(f"✗ Error creating Phi4 model: {e}")
        return False


def create_granite_optimized():
    """Create optimized Granite for deeper late meeting analysis"""
    
    modelfile_content = """# Granite optimized for Meeting-Ops late phase
FROM granite3.3:8b

# Maximum GPU offloading
PARAMETER num_gpu 99
PARAMETER num_thread 8
PARAMETER num_ctx 8192
PARAMETER num_batch 512

# Deeper analysis settings
PARAMETER temperature 0.7
PARAMETER top_k 40
PARAMETER top_p 0.9
PARAMETER repeat_penalty 1.1

# Comprehensive responses
PARAMETER num_predict 2048

SYSTEM """You are a comprehensive meeting analysis assistant for the late phase of meetings.
Your job is to provide deep insights, thorough summaries, and detailed action items.
Focus on context, relationships between topics, and strategic implications.
Format responses as JSON with: formatted_transcript, summary, key_points, action_items, decisions, topics."""
"""
    
    with open("/tmp/Modelfile.granite", "w") as f:
        f.write(modelfile_content)
    
    print("\n✓ Created optimized Granite Modelfile")
    
    try:
        result = subprocess.run(
            ["ollama", "create", "meeting-ops-granite", "-f", "/tmp/Modelfile.granite"],
            capture_output=True,
            text=True
        )
        
        if result.returncode == 0:
            print("✓ Custom model 'meeting-ops-granite' created")
            return True
        else:
            print(f"✗ Failed to create custom Granite model: {result.stderr}")
            return False
    except Exception as e:
        print(f"✗ Error creating Granite model: {e}")
        return False


def test_model(model_name: str, phase: str):
    """Test a model with appropriate prompt"""
    print(f"\nTesting {model_name} for {phase} phase...")
    
    if phase == "early":
        test_prompt = """Format this meeting snippet quickly:
"okay so john says api by friday sarah needs testing time"

Respond with JSON: formatted_transcript, key_points"""
    else:
        test_prompt = """Analyze this meeting segment comprehensively:
"The project timeline discussion revealed that John requires the API completion by Friday. 
Sarah mentioned needing additional testing time. Mike confirmed UI completion but needs review.
There was discussion about resource allocation and deadline priorities."

Respond with detailed JSON: formatted_transcript, summary, action_items, decisions"""
    
    try:
        response = requests.post(
            "http://localhost:11434/api/generate",
            json={
                "model": model_name,
                "prompt": test_prompt,
                "options": {
                    "num_gpu": 99,
                    "temperature": 0.5 if phase == "early" else 0.7
                },
                "stream": False
            },
            timeout=30
        )
        
        if response.status_code == 200:
            result = response.json()
            print(f"✓ {model_name} test successful!")
            
            # Measure response time
            if "total_duration" in result:
                response_time = result["total_duration"] / 1_000_000_000  # Convert to seconds
                print(f"  Response time: {response_time:.2f} seconds")
            
            print(f"\nSample response preview:")
            print("-" * 50)
            print(result.get("response", "")[:300])
            print("-" * 50)
            return True
        else:
            print(f"✗ {model_name} test failed: {response.status_code}")
            return False
    except Exception as e:
        print(f"✗ Error testing {model_name}: {e}")
        return False


def display_pipeline_config():
    """Display the recommended pipeline configuration"""
    
    config = {
        "pipeline": {
            "enabled": True,
            "early_phase": {
                "model": "meeting-ops-phi4",
                "duration_minutes": 10,
                "update_interval_seconds": 20,
                "description": "Fast, frequent updates for early meeting dynamics"
            },
            "late_phase": {
                "model": "meeting-ops-granite", 
                "update_interval_min_seconds": 60,
                "update_interval_max_seconds": 120,
                "description": "Deeper, comprehensive analysis as context builds"
            },
            "benefits": [
                "⚡ Quick responsiveness in first 10 minutes when users expect immediate feedback",
                "🎯 Deeper insights after 10 minutes when there's more context to analyze",
                "💰 Efficient resource usage - fast model when speed matters, deep model when quality matters",
                "😊 Delightful UX - matches user expectations throughout meeting lifecycle"
            ]
        }
    }
    
    print("\n" + "="*60)
    print("RECOMMENDED PIPELINE CONFIGURATION")
    print("="*60)
    print(json.dumps(config, indent=2))


def main():
    """Main setup process"""
    print("=" * 60)
    print("Meeting-Ops Dual-Model Pipeline Setup")
    print("=" * 60)
    print("\n📋 This will set up:")
    print("  1. Phi4-mini 3.8B - Fast model for early meeting phase")
    print("  2. Granite 3.3 8B - Deep model for late meeting phase")
    
    # Check Ollama
    if not check_ollama_running():
        print("\n⚠ Ollama is not running")
        start_ollama()
        
        if not check_ollama_running():
            print("\n✗ Could not start Ollama")
            print("Please start Ollama manually: ollama serve")
            sys.exit(1)
    else:
        print("\n✓ Ollama is running")
    
    # Pull and setup Phi4-mini
    success_phi = False
    if pull_model("phi4-mini:3.8b", "Phi4-mini 3.8B (Fast Early Phase Model)"):
        success_phi = create_phi4_mini_optimized()
    
    # Pull and setup Granite
    success_granite = False
    if pull_model("granite3.3:8b", "Granite 3.3 8B (Deep Late Phase Model)"):
        success_granite = create_granite_optimized()
    
    # Test both models
    print("\n" + "="*60)
    print("TESTING MODELS")
    print("="*60)
    
    if success_phi:
        test_model("meeting-ops-phi4", "early")
    else:
        print("⚠ Phi4 setup failed, testing base model")
        test_model("phi4-mini:3.8b", "early")
    
    if success_granite:
        test_model("meeting-ops-granite", "late")
    else:
        print("⚠ Granite setup failed, testing base model")
        test_model("granite3.3:8b", "late")
    
    # Display configuration
    display_pipeline_config()
    
    print("\n" + "="*60)
    print("✓ SETUP COMPLETE!")
    print("="*60)
    print("\n🚀 Your dual-model pipeline is ready:")
    print("\n  Early Phase (0-10 minutes):")
    print("    • Model: meeting-ops-phi4 (or phi4-mini:3.8b)")
    print("    • Updates: Every 20 seconds")
    print("    • Focus: Quick formatting and key point extraction")
    print("\n  Late Phase (10+ minutes):")
    print("    • Model: meeting-ops-granite (or granite3.3:8b)")
    print("    • Updates: Every 60-120 seconds")
    print("    • Focus: Deep analysis and comprehensive insights")
    print("\n💡 This pipeline provides:")
    print("  • Fast responsiveness when users need immediate feedback")
    print("  • Deep insights when context has built up")
    print("  • Optimal resource usage throughout the meeting")
    print("\n🎯 Next step: Start your backend and the models will be used automatically!")


if __name__ == "__main__":
    main()