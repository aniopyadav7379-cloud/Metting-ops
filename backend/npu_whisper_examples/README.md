# AMD NPU Whisper Implementation Resources

## Downloaded Packages
- `onnxruntime_vitisai-1.16.0-py3-none-any.whl` - ONNX Runtime with Vitis AI Execution Provider
- `voe-0.1.0-py3-none-any.whl` - Vitis ONNX Engine

## Installation Instructions
```bash
# Install VOE first
pip install ../voe-0.1.0-py3-none-any.whl

# Then install onnxruntime-vitisai
pip install ../onnxruntime_vitisai-1.16.0-py3-none-any.whl

# Verify installation
python -c "import onnxruntime as ort; print(ort.get_available_providers())"
# Should show 'VitisAIExecutionProvider' in the list
```

## Key GitHub Projects Found

### 1. Ryzen AI Subtitling by jchu634
- **URL**: https://github.com/jchu634/RyzenAISubtitles
- **Description**: Windows GUI application for subtitling system audio in soft real-time using Whisper on Ryzen AI NPU
- **Key Points**:
  - Uses AMD Whisper-Tiny model
  - Works with RyzenAI software v1.1.0
  - Based on whisper_real_time project
  - Confirmed working on NPU

### 2. AMD RyzenAI Software Repository
- **URL**: https://github.com/amd/RyzenAI-SW
- **Description**: Official AMD repository for Ryzen AI Software
- **Contains**: Examples, benchmarks, and NPU-GPU pipeline demos

### 3. AMD RyzenAI Cloud-to-Client Demo
- **URL**: https://github.com/amd/RyzenAI-cloud-to-client-demo
- **Description**: Demo showing cloud to edge AI deployment
- **Contains**: Pre-built wheels in `/wheels` directory

### 4. ONNX Runtime RyzenAI Demo
- **URL**: https://github.com/onnxruntime/RyzenAI-Cloud2Client-Onnx-Demo
- **Description**: Official ONNX Runtime demo for Ryzen AI

## Whisper NPU Implementation Example

```python
import onnxruntime as ort
import numpy as np

# Configure for NPU execution
providers = [
    ('VitisAIExecutionProvider', {
        'config_file': './vaip_config.json',
        'log_level': 'info'
    }),
    'CPUExecutionProvider'  # Fallback
]

# Load Whisper model
session = ort.InferenceSession('whisper_tiny_quantized.onnx', providers=providers)

# Check which provider is being used
print(f"Using providers: {session.get_providers()}")

# Example inference
input_name = session.get_inputs()[0].name
input_shape = session.get_inputs()[0].shape

# Prepare audio input (example with dummy data)
# In real use, this would be mel-spectrogram features
audio_features = np.random.randn(*input_shape).astype(np.float32)

# Run inference
outputs = session.run(None, {input_name: audio_features})
```

## NPU Configuration File (vaip_config.json)
```json
{
  "target": "AMD_AIE2",
  "compile_options": {
    "xclbin": "path/to/your.xclbin"
  }
}
```

## Important Notes
1. The NPU hardware (AMD Phoenix, 16 TOPS INT8) is confirmed present on this system
2. The kernel driver `amdxdna` is loaded and device `/dev/accel/accel0` is accessible
3. For optimal performance, use INT8 quantized models
4. The VitisAIExecutionProvider will automatically offload supported operations to NPU

## Next Steps
1. Find or create INT8 quantized Whisper ONNX models
2. Configure the vaip_config.json for your specific NPU
3. Test with the example code above
4. Monitor NPU utilization during inference

## References
- [AMD Ryzen AI Documentation](https://ryzenai.docs.amd.com/)
- [ONNX Runtime Vitis AI EP](https://onnxruntime.ai/docs/execution-providers/Vitis-AI-ExecutionProvider.html)
- [Hackster.io Ryzen AI Subtitling Project](https://www.hackster.io/jchu634/ryzen-ai-subtitling-5ead7f)