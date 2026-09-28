# Meeting-Ops Installation Guide

## System Requirements

### Hardware
- **AMD Ryzen AI CPU with NPU** (Phoenix/Hawk Point with 16 TOPS INT8)
- Minimum 16GB RAM
- 50GB available storage
- USB microphone or audio line-in

### Software
- Ubuntu Server 24.04 LTS or newer (25.04 supported)
- Python 3.13+
- Docker and Docker Compose (for production)
- Node.js 18+ and npm (for development)

### NPU Requirements
- AMD NPU kernel driver (`amdxdna`) - mainlined in Linux 6.14+
- `/dev/accel/accel0` device must be present
- User must have access to render group

## Quick Install (Ubuntu Server)

### 1. Clone the Repository
```bash
git clone https://github.com/Unicorn-Commander/Meeting-Ops.git
cd Meeting-Ops
```

### 2. Run Automated Setup
```bash
chmod +x setup-ubuntu.sh
sudo ./setup-ubuntu.sh
```

This script will:
- Install system dependencies
- Set up Python 3.13 environment
- Configure NPU access permissions
- Download required models
- Set up PostgreSQL and Qdrant
- Configure systemd services
- Build frontend assets

### 3. Start Services

#### Option A: Docker Compose (Recommended)
```bash
./start-postgres-stack.sh
```

Access at:
- Frontend: http://localhost:7777
- API: http://localhost:9050/docs
- Default login: admin / admin123

#### Option B: Systemd Services
```bash
sudo systemctl start meeting-ops
sudo systemctl enable meeting-ops
```

## Manual Installation

### 1. System Dependencies
```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Python 3.13
sudo add-apt-repository ppa:deadsnakes/ppa -y
sudo apt install python3.13 python3.13-venv python3.13-dev -y

# Install build tools
sudo apt install build-essential git curl wget -y

# Install audio libraries
sudo apt install portaudio19-dev libsndfile1 ffmpeg -y

# Install PostgreSQL
sudo apt install postgresql postgresql-contrib -y

# Install Docker (if using Docker deployment)
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo usermod -aG docker $USER

# Install Node.js 18+
curl -fsSL https://deb.nodesource.com/setup_18.x | sudo -E bash -
sudo apt install nodejs -y
```

### 2. NPU Setup
```bash
# Check NPU device
ls -la /dev/accel/accel0

# Add user to render group
sudo usermod -aG render $USER

# Create udev rule for NPU access
sudo tee /etc/udev/rules.d/99-amd-npu.rules << EOF
KERNEL=="accel*", SUBSYSTEM=="accel", MODE="0666", GROUP="render"
EOF

sudo udevadm control --reload-rules
sudo udevadm trigger

# Verify NPU access (logout/login may be required)
groups | grep render
```

### 3. Backend Setup
```bash
cd backend

# Create virtual environment
python3.13 -m venv venv
source venv/bin/activate

# Upgrade pip
pip install --upgrade pip wheel setuptools

# Install dependencies
pip install -r requirements.txt

# Download ONNX models
python download_models.py

# Initialize database
python init_db.py
```

### 4. NPU Binary Files
The NPU binaries are included in the repository:
- `backend/whisperx_npu.bin` - Main NPU binary
- `backend/models/whisper-base.npubin` - Quantized model
- `backend/whisperx_aie2_emulation.xclbin` - AIE2 binary

These files are pre-compiled for AMD Phoenix NPU (AIE v1.1).

### 5. Frontend Setup
```bash
cd frontend

# Install dependencies
npm install

# Build for production
npm run build

# Or run development server
npm run dev
```

### 6. PostgreSQL Setup
```bash
# Create database
sudo -u postgres createdb meeting_ops
sudo -u postgres createuser meeting_ops -P

# Grant permissions
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE meeting_ops TO meeting_ops;"

# Set environment variable
export DATABASE_URL="postgresql://meeting_ops:password@localhost/meeting_ops"
```

### 7. Environment Configuration
Create `.env` file in backend directory:
```bash
DATABASE_URL=postgresql://meeting_ops:password@localhost/meeting_ops
SECRET_KEY=your-secret-key-here
OLLAMA_BASE_URL=http://localhost:11434
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your-email@gmail.com
SMTP_PASSWORD=your-app-password
```

## Verification

### 1. Check NPU Status
```bash
cd backend
python test_npu_hardware.py
```

Expected output:
```
✅ NPU device detected at /dev/accel/accel0
✅ NPU is accessible
✅ AIE Version: 1.1
✅ 16 TOPS INT8 performance available
```

### 2. Test Backend
```bash
cd backend
source venv/bin/activate
python -m uvicorn main:app --host 0.0.0.0 --port 9050
```

Visit http://localhost:9050/docs

### 3. Test Recording
```bash
cd backend
python test_recording_pipeline.py
```

## Troubleshooting

### NPU Not Detected
1. Check kernel version: `uname -r` (should be 6.14+)
2. Verify driver loaded: `lsmod | grep amdxdna`
3. Check device permissions: `ls -la /dev/accel/accel0`
4. Ensure user in render group: `groups | grep render`

### Audio Issues
1. List audio devices: `python backend/check_audio_devices.py`
2. Configure USB mic: `python backend/configure_usb_mic.py`
3. Test audio levels: `python backend/test_audio_levels.py`

### Database Connection
1. Check PostgreSQL status: `sudo systemctl status postgresql`
2. Test connection: `psql -h localhost -U meeting_ops -d meeting_ops`
3. Verify migrations: `cd backend && python create_all_tables.py`

### Model Download Failures
1. Check internet connection
2. Manually download from Hugging Face:
   ```bash
   cd backend
   python -c "from huggingface_hub import snapshot_download; snapshot_download('onnx-community/whisper-base', cache_dir='./whisper_onnx_cache')"
   ```

### Permission Errors
1. NPU access: Add user to render group and logout/login
2. Audio access: Add user to audio group
3. File permissions: Check ownership of data directories

## Production Deployment

### Using Docker
```bash
# Build and start all services
docker-compose -f docker-compose.yml -f docker-compose.prod.yml up -d

# Check status
docker-compose ps

# View logs
docker-compose logs -f backend
```

### Using Systemd
```bash
# Install service
sudo cp deployment/meeting-ops.service /etc/systemd/system/
sudo systemctl daemon-reload

# Start service
sudo systemctl start meeting-ops
sudo systemctl enable meeting-ops

# Check status
sudo systemctl status meeting-ops
journalctl -u meeting-ops -f
```

### Nginx Setup (Optional)
```bash
# Install nginx
sudo apt install nginx -y

# Configure reverse proxy
sudo ./setup-nginx.sh

# Enable SSL (requires domain)
sudo ./setup-ssl.sh
```

## Performance Tuning

### NPU Optimization
- Ensure NPU binary matches your hardware version
- Monitor NPU utilization in System Monitor
- Batch size affects throughput (default: 1 for low latency)

### Database Optimization
```sql
-- Increase connections
ALTER SYSTEM SET max_connections = 200;

-- Optimize for SSD
ALTER SYSTEM SET random_page_cost = 1.1;

-- Increase shared buffers
ALTER SYSTEM SET shared_buffers = '1GB';
```

### Audio Buffer Tuning
Edit `backend/services/audio_capture.py`:
```python
CHUNK_SIZE = 3200  # Adjust for latency/quality tradeoff
BUFFER_SECONDS = 2  # Reduce for lower latency
```

## Support

For issues or questions:
- Check logs: `journalctl -u meeting-ops -n 100`
- NPU diagnostics: `python backend/test_npu_complete.py`
- Contact: aaron@magicunicorn.tech

Part of the Unicorn Commander Suite by Magic Unicorn Unconventional Technology & Stuff Inc.