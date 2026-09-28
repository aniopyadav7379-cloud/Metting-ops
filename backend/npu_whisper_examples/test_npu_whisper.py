#!/usr/bin/env python3
"""
Test script for running Whisper on AMD NPU using onnxruntime-vitisai
"""

import os
import sys
import numpy as np

def test_onnxruntime_vitisai():
    """Test if onnxruntime-vitisai is properly installed"""
    try:
        import onnxruntime as ort
        providers = ort.get_available_providers()
        print("Available ONNX Runtime providers:")
        for provider in providers:
            print(f"  - {provider}")
        
        if 'VitisAIExecutionProvider' in providers:
            print("\n✅ VitisAIExecutionProvider is available!")
            return True
        else:
            print("\n❌ VitisAIExecutionProvider NOT found")
            print("Make sure you installed onnxruntime-vitisai wheel")
            return False
    except ImportError as e:
        print(f"❌ Failed to import onnxruntime: {e}")
        return False

def check_npu_device():
    """Check if NPU device is accessible"""
    device_path = "/dev/accel/accel0"
    if os.path.exists(device_path):
        print(f"✅ NPU device found at {device_path}")
        # Check permissions
        if os.access(device_path, os.R_OK | os.W_OK):
            print("✅ NPU device is accessible")
            return True
        else:
            print("❌ NPU device exists but not accessible (check permissions)")
            return False
    else:
        print(f"❌ NPU device not found at {device_path}")
        return False

def check_kernel_module():
    """Check if amdxdna kernel module is loaded"""
    try:
        with open('/proc/modules', 'r') as f:
            modules = f.read()
            if 'amdxdna' in modules:
                print("✅ amdxdna kernel module is loaded")
                return True
            else:
                print("❌ amdxdna kernel module not loaded")
                return False
    except:
        print("❌ Could not check kernel modules")
        return False

def create_vaip_config():
    """Create a basic vaip_config.json for NPU"""
    config = {
        "target": "AMD_AIE2",
        "compile_options": {
            "xclbin": ""  # Will be auto-generated
        }
    }
    
    import json
    config_path = "vaip_config.json"
    with open(config_path, 'w') as f:
        json.dump(config, f, indent=2)
    print(f"✅ Created {config_path}")
    return config_path

def test_whisper_inference():
    """Test Whisper inference on NPU (if model available)"""
    import onnxruntime as ort
    
    # Check for whisper model
    model_paths = [
        "whisper_tiny_quantized.onnx",
        "whisper_base_quantized.onnx",
        "whisper_tiny.onnx",
        "whisper_base.onnx"
    ]
    
    model_path = None
    for path in model_paths:
        if os.path.exists(path):
            model_path = path
            break
    
    if not model_path:
        print("\n⚠️  No Whisper ONNX model found. To test inference:")
        print("   1. Download a quantized Whisper ONNX model")
        print("   2. Place it in this directory")
        print("   3. Run this script again")
        return
    
    print(f"\n🔧 Testing inference with {model_path}")
    
    # Create config
    config_path = create_vaip_config()
    
    # Set up providers
    providers = [
        ('VitisAIExecutionProvider', {
            'config_file': config_path,
            'log_level': 'info'
        }),
        'CPUExecutionProvider'
    ]
    
    try:
        # Create session
        print("Creating inference session...")
        session = ort.InferenceSession(model_path, providers=providers)
        
        # Check which provider is actually being used
        actual_providers = session.get_providers()
        print(f"Session using providers: {actual_providers}")
        
        if 'VitisAIExecutionProvider' in actual_providers:
            print("✅ Model loaded on NPU!")
        else:
            print("⚠️  Model running on CPU (some ops may not be supported on NPU)")
        
        # Get input info
        input_info = session.get_inputs()[0]
        print(f"\nModel input:")
        print(f"  Name: {input_info.name}")
        print(f"  Shape: {input_info.shape}")
        print(f"  Type: {input_info.type}")
        
        # Create dummy input for testing
        # Real Whisper expects mel-spectrogram features
        if len(input_info.shape) == 3:  # Typical whisper input shape
            batch_size = 1
            n_mels = input_info.shape[1] if input_info.shape[1] > 0 else 80
            time_steps = input_info.shape[2] if input_info.shape[2] > 0 else 3000
            dummy_input = np.random.randn(batch_size, n_mels, time_steps).astype(np.float32)
        else:
            # Fallback for unknown shape
            dummy_input = np.random.randn(*[1 if d < 0 else d for d in input_info.shape]).astype(np.float32)
        
        print(f"\nRunning inference with input shape: {dummy_input.shape}")
        outputs = session.run(None, {input_info.name: dummy_input})
        print(f"✅ Inference successful! Output shape: {outputs[0].shape}")
        
    except Exception as e:
        print(f"❌ Inference failed: {e}")

def main():
    print("=== AMD NPU Whisper Test ===\n")
    
    # Check NPU hardware
    print("1. Checking NPU hardware...")
    npu_ok = check_npu_device()
    kernel_ok = check_kernel_module()
    
    print("\n2. Checking ONNX Runtime...")
    ort_ok = test_onnxruntime_vitisai()
    
    if ort_ok and npu_ok:
        print("\n3. Testing Whisper inference...")
        test_whisper_inference()
    else:
        print("\n❌ Prerequisites not met. Please:")
        if not ort_ok:
            print("   - Install onnxruntime-vitisai wheel")
        if not npu_ok:
            print("   - Check NPU device and permissions")
        if not kernel_ok:
            print("   - Load amdxdna kernel module")

if __name__ == "__main__":
    main()