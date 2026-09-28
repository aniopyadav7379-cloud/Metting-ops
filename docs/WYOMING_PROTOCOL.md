# Wyoming Protocol Integration

## Overview

Unicorn Commander Meeting Ops now supports the Wyoming Protocol for seamless integration with Home Assistant satellite microphones and remote activation devices. This enables distributed meeting recording with ESP32-based satellite microphones, wake word detection, and gesture-based controls.

## Features

### 🎙️ Satellite Microphone Support
- **ESP32 Integration**: Connect multiple ESP32-based microphones throughout the meeting room
- **Wireless Audio Streaming**: Real-time audio streaming over WiFi using Wyoming Protocol
- **Automatic Discovery**: Satellites automatically connect to the main unit
- **Multi-Room Support**: Support for satellites in different rooms or areas

### 🎯 Wake Word Detection
- **Configurable Wake Words**: Set custom wake words like "Unicorn", "Commander", "Start Meeting"
- **NPU Acceleration**: Leverages AMD NPU for ultra-fast wake word detection
- **Auto-Recording**: Automatically start recording sessions when wake words are detected
- **Confidence Threshold**: Adjustable sensitivity to prevent false positives

### 👋 Gesture Recognition
- **Hands-Free Control**: Control meeting recording with simple hand gestures
- **NPU-Powered**: Uses the same NPU acceleration as transcription
- **Supported Gestures**:
  - Wave to start recording
  - Thumbs up/down for feedback
  - Stop gesture to end recording
  - Pause gesture for temporary stops

### 🔘 Remote Activation
- **Physical Controls**: Support for hardware buttons and switches
- **Wireless Remotes**: ESP32-based remote control devices
- **Multi-Point Control**: Multiple activation points throughout the room
- **Emergency Stop**: Quick meeting termination from any satellite

## Wyoming Protocol Implementation

### Server Configuration

The Wyoming Protocol server runs on port 10700 by default and accepts WebSocket connections from satellites:

```python
# Server starts automatically on application startup
# Configuration via environment variables:
WYOMING_PORT=10700
WYOMING_HOST=0.0.0.0
```

### Message Types

The implementation supports standard Wyoming Protocol messages:

#### Audio Streaming
```json
{
  "type": "audio-chunk",
  "rate": 16000,
  "width": 2,
  "channels": 1,
  "audio": "<base64_encoded_audio>"
}
```

#### Wake Word Detection
```json
{
  "type": "wake-word-detection",
  "wake_word": {
    "word": "unicorn",
    "confidence": 0.95,
    "audio_seconds": 2.5
  }
}
```

#### Voice Activity Detection
```json
{
  "type": "voice-activity-detection",
  "is_speech": true,
  "probability": 0.87
}
```

## ESP32 Satellite Setup

### Hardware Requirements
- ESP32 or ESP32-S3 microcontroller
- I2S microphone (e.g., INMP441)
- WiFi connectivity
- Optional: LED indicators, buttons, display

### Basic ESP32 Code Structure
```cpp
#include <WiFi.h>
#include <WebSocketsClient.h>
#include <ArduinoJson.h>
#include <driver/i2s.h>

// Wyoming Protocol client implementation
class WyomingSatellite {
private:
    WebSocketsClient webSocket;
    bool isRecording = false;
    
public:
    void connect(const char* host, uint16_t port) {
        webSocket.begin(host, port, "/");
        webSocket.onEvent([this](WStype_t type, uint8_t * payload, size_t length) {
            handleWebSocketEvent(type, payload, length);
        });
    }
    
    void sendAudioChunk(uint8_t* audioData, size_t length) {
        // Convert to Wyoming Protocol format and send
    }
    
    void sendWakeWord(const char* word, float confidence) {
        // Send wake word detection
    }
};
```

### Recommended Hardware Configurations

#### Basic Satellite
- ESP32-DevKitC
- INMP441 I2S microphone
- Status LED
- Power via USB or 5V adapter

#### Advanced Satellite
- ESP32-S3 with more RAM
- Multiple microphones for beamforming
- OLED display for status
- Physical activation button
- Battery backup option

#### Remote Control Unit
- ESP32 with buttons/switches
- IR receiver for universal remote compatibility
- Long-range WiFi antenna
- Low-power design for battery operation

## API Endpoints

### Satellite Management

#### Get Satellite Status
```http
GET /api/wyoming/satellites
```

Response:
```json
{
  "satellites": {
    "192.168.1.100:12345": {
      "id": "192.168.1.100:12345",
      "status": "active",
      "connected_at": "2025-01-19T10:30:00Z",
      "name": "Conference Room East",
      "capabilities": ["audio", "wake_word", "vad"],
      "voice_active": false
    }
  },
  "server_running": true,
  "server_port": 10700
}
```

#### Broadcast Commands
```http
POST /api/wyoming/broadcast/{command}
```

Commands: `start`, `stop`, `pause`

Optional query parameter: `satellite_id` to target specific satellite

### Wake Word Configuration

#### Configure Wake Words
```http
POST /api/wyoming/wake-word/configure
```

Request:
```json
{
  "wake_words": ["unicorn", "commander", "start meeting"],
  "threshold": 0.8
}
```

#### Get Wake Word Status
```http
GET /api/wyoming/wake-word/status
```

## Home Assistant Integration

### Configuration

Add to Home Assistant's `configuration.yaml`:

```yaml
# Wyoming Protocol integration
wyoming:
  - host: your-unicorn-commander-ip
    port: 10700
    protocol: unicorn_meeting

# Automation example
automation:
  - alias: "Start Meeting on Wake Word"
    trigger:
      platform: event
      event_type: wyoming_wake_word_detected
      event_data:
        wake_word: "start meeting"
    action:
      service: notify.mobile_app_your_phone
      data:
        message: "Meeting recording started via wake word"
```

### Voice Assistant Integration

```yaml
# Create custom voice commands
intent_script:
  StartMeetingIntent:
    speech:
      text: "Starting meeting recording"
    action:
      service: rest_command.unicorn_start_recording

rest_command:
  unicorn_start_recording:
    url: "http://your-unicorn-commander:9050/api/recording-sessions"
    method: POST
    headers:
      Authorization: "Bearer {{ states('input_text.unicorn_api_token') }}"
    payload: '{"name": "Voice Activated Meeting", "meeting_type": "voice_activated"}'
```

## Troubleshooting

### Common Issues

#### Satellites Not Connecting
1. Check WiFi network connectivity
2. Verify Wyoming Protocol server is running on port 10700
3. Check firewall rules
4. Ensure ESP32 has correct server IP/hostname

#### Wake Words Not Detected
1. Verify microphone is working and positioned correctly
2. Check confidence threshold (lower for more sensitivity)
3. Ensure background noise isn't interfering
4. Test with simpler, distinct wake words

#### Audio Quality Issues
1. Verify I2S microphone connections
2. Check sample rate configuration (should be 16kHz)
3. Monitor network bandwidth and latency
4. Adjust audio buffer sizes if needed

### Diagnostics

#### Check Wyoming Protocol Server Status
```bash
# Check if server is listening
netstat -ln | grep 10700

# Monitor Wyoming Protocol logs
sudo journalctl -u unicorn-commander-backend.service -f | grep Wyoming
```

#### Test Satellite Connection
```bash
# Test WebSocket connection
wscat -c ws://your-unicorn-commander:10700

# Send test message
{"type": "ping", "timestamp": "2025-01-19T10:30:00Z"}
```

## Performance Optimization

### NPU Acceleration
- Wake word detection uses NPU acceleration for sub-millisecond response times
- Gesture recognition leverages same NPU kernels as transcription
- Multiple satellites can be processed simultaneously

### Network Optimization
- Use 5GHz WiFi for better bandwidth and lower latency
- Configure QoS to prioritize audio streaming traffic
- Consider wired Ethernet for critical satellites

### Power Management
- ESP32 deep sleep when not actively recording
- Wake on network activity or button press
- Battery backup for critical remote controls

## Security Considerations

### Network Security
- Use WPA3 security for WiFi connections
- Consider VPN for remote satellite access
- Implement device authentication tokens

### Data Privacy
- Audio data encrypted during transmission
- No permanent storage on satellite devices
- Configurable data retention policies

### Access Control
- Satellite registration and approval process
- Role-based access to Wyoming Protocol features
- Audit logging for all satellite activities

## Future Enhancements

### Planned Features
- **Mesh Networking**: Satellites can relay through each other
- **Advanced Beamforming**: Multiple microphones for better directional audio
- **Visual Wake Words**: Camera-based gesture activation
- **Bluetooth Integration**: Support for Bluetooth satellites
- **Mobile App Control**: Smartphone as satellite/remote

### Community Extensions
- **Arduino Libraries**: Easy-to-use ESP32 libraries
- **3D Printable Cases**: Community-designed enclosures
- **Integration Templates**: Pre-built Home Assistant configurations
- **Multi-Protocol Support**: Zigbee, Z-Wave satellite options