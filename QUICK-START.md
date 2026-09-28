# 🚀 Meeting-Ops Quick Start Guide

## ⚡ TL;DR - Get Running in 2 Minutes

```bash
# 1. Start the system
cd /srv/meeting-ops
./start-meeting-ops.sh

# 2. Open in browser
# Frontend: http://localhost:7778
# Login: admin / admin123

# 3. Start recording!
# Click purple Record button → speak → see live transcription
```

---

## 🎯 What You Get

**Meeting-Ops** is an AI-powered meeting transcription platform with:
- ⚡ **Live transcription in the browser** (Parakeet 0.6B INT8 via WebGPU/WASM — no per-minute server cost)
- 🎙️ **Server completion pass** at meeting end (Parakeet 1.1B + pyannote diarization)
- 🤖 **Live AI analysis** during meetings
- 📝 **Smart meeting notes** and summaries
- 🔐 **Secure authentication** system

---

## 📋 Prerequisites Check

```bash
# Check you have these installed:
docker --version          # Docker 20.10+
docker compose version    # Docker Compose 2.0+
node --version            # Node.js 18+
python3 --version         # Python 3.13+

# Check USB microphone is detected
arecord -l | grep -i usb
```

---

## 🚀 Step-by-Step Setup

### 1. Clone & Navigate
```bash
git clone https://github.com/Unicorn-Commander/Meeting-Ops.git
cd Meeting-Ops
```

### 2. Start All Services
```bash
# This starts everything: backend, frontend, database
./start-meeting-ops.sh
```

**Expected Output:**
```
🚀 Starting Meeting-Ops...
✅ Docker services started
✅ Backend running on port 9050
✅ Frontend running on port 7778
✅ All systems ready!
```

### 3. Access the Application
- **Frontend**: http://localhost:7778
- **API Documentation**: http://localhost:9050/docs

### 4. Login
- **Username**: `admin`
- **Password**: `admin123`
- **Role**: Full access to all features

### 5. Test Recording
1. Click **Dashboard** in sidebar
2. Click purple **Record** button
3. Speak into your microphone
4. Watch real-time transcription appear
5. Click **Stop** when done

---

## 🌐 Network Access Setup

### Accessing from Another Computer

1. **Find Server IP**:
```bash
hostname -I | cut -d' ' -f1
# Example output: 192.168.1.145
```

2. **Configure Frontend**:
```bash
# Create .env file in frontend directory
echo "VITE_API_URL=http://192.168.1.145:9050" > frontend/.env
```

3. **Restart Frontend**:
```bash
cd frontend && npm run dev
```

4. **Access URLs**:
- **Frontend**: http://192.168.1.145:7778
- **Backend API**: http://192.168.1.145:9050

---

## 🛠️ Manual Component Startup

If you prefer to start components individually:

### Backend Only
```bash
cd backend
./start-backend.sh
# Runs on: http://localhost:9050
```

### Frontend Only  
```bash
cd frontend
npm install
npm run dev
# Runs on: http://localhost:7778
```

### Docker Services Only
```bash
docker compose -f docker-compose-full-stack.yml up -d
# Starts: PostgreSQL, Redis, Qdrant
```

---

## 🎙️ Audio Setup

### Check USB Microphone
```bash
# List audio devices
arecord -l

# Should show something like:
# card 0: M305 [AT2020USB+-M305], device 0: USB Audio [USB Audio]
```

### Test Recording
```bash
# Record 5 seconds of audio
arecord -D hw:0,0 -f S16_LE -r 44100 -c 1 -d 5 test.wav

# Play it back
aplay test.wav
```

### Common Issues
- **No microphone detected**: Check USB connection
- **Permission denied**: Add user to audio group: `sudo usermod -a -G audio $USER`
- **Device busy**: Stop other audio applications

---

## 🧠 Where inference actually runs

Meeting-Ops is **browser-first**. There is no NPU in this product — the per-minute
work happens on the user's own device, and the server does one pass per finished
meeting.

| Stage | Where | Model |
|---|---|---|
| Live transcript | Browser (WebGPU/WASM) | Parakeet 0.6B INT8 via `onnxruntime-web` |
| Live summary | Browser | small on-device LLM (transformers.js / web-llm) |
| Completion pass | Server, once at meeting end | Parakeet 1.1B |
| Diarization | Server | pyannote + `wespeaker` embeddings |
| Summary / actions | Server | Qwen 3.6 35B-A3B via LiteLLM |

### Check the pipeline
```bash
curl http://localhost:9050/api/health | jq
```

> **Note:** the NPU-accelerated Whisper path belongs to the separate **UC-1 hardware
> appliance** line (`Meeting-Ops-UC1`), not to this codebase.

---

## 🔐 User Accounts

### Current Accounts
| Username | Password | Role | Access |
|----------|----------|------|--------|
| admin | admin123 | superuser | Full system access |
| user | user123 | user | Recording & viewing |

### Create New Users
```bash
# Access PostgreSQL
docker exec -it meeting_sessions_db psql -U meetingops -d meeting_sessions

# Add new user (password will be hashed)
INSERT INTO users (username, email, role, hashed_password, is_active) 
VALUES ('newuser', 'new@example.com', 'user', '$2b$12$...', true);
```

---

## 📊 System Health Check

### Quick Status Check
```bash
# Run automated test
./test-auth-flow.sh

# Expected output:
# ✅ Backend login successful
# ✅ Authenticated API call successful
# ✅ Frontend is accessible
# ✅ Backend API is healthy
```

### Service Status
```bash
# Check all Docker services
docker compose ps

# Check backend logs
docker compose logs backend

# Check frontend logs  
tail -f frontend/frontend.log
```

### API Health
```bash
# Test API endpoint
curl http://localhost:9050/api/status

# Should return:
# {"status": "healthy", ...}
```

---

## 🎛️ Key Features to Test

### 1. Real-time Transcription
- Start recording
- Speak clearly into microphone  
- Watch live transcription appear
- Notice speaker diarization (Speaker A, B, etc.)

### 2. AI Meeting Intelligence
- Record a longer conversation (2+ minutes)
- Navigate to **Sessions** → Select your session
- View AI-generated insights:
  - Meeting summary
  - Action items
  - Key decisions
  - Sentiment analysis

### 3. Export Features
- Go to session details
- Download transcript as TXT
- Download original audio as WAV

### 4. Live Analytics
- While recording, check the dashboard
- View real-time metrics:
  - Recording duration
  - Live transcription status
  - System resources

---

## ⚙️ Configuration Options

### Backend Settings
```bash
# Edit backend configuration
nano backend/.env

# Key settings:
DATABASE_URL=postgresql://meetingops:meetingops123@localhost:5432/meeting_sessions
JWT_SECRET_KEY=super-secret-jwt-key-for-meeting-ops-change-in-production
STT_MODEL=parakeet-1.1b
```

### Frontend Settings
```bash
# Edit frontend configuration  
nano frontend/.env

# Key settings:
VITE_API_URL=http://localhost:9050
VITE_WS_URL=ws://localhost:9050
```

---

## 🐛 Troubleshooting

### Common Issues & Solutions

#### "Frontend not loading"
```bash
# Check if frontend is running
curl http://localhost:7778
# If not running: cd frontend && npm run dev
```

#### "API not responding"
```bash
# Check backend status
curl http://localhost:9050/api/status
# If not running: cd backend && ./start-backend.sh
```

#### "Authentication failed"
```bash
# Test login directly
curl -X POST "http://localhost:9050/api/auth/login" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=admin&password=admin123"
```

#### "No audio devices"
```bash
# Check USB microphone
lsusb | grep -i audio
arecord -l
# Reconnect USB microphone or try different port
```

#### "Port conflicts"
```bash
# If ports 7778 or 9050 are in use
sudo netstat -tulpn | grep :7778
sudo netstat -tulpn | grep :9050
# Kill conflicting processes or change ports in config
```

---

## 📚 Next Steps

### Production Deployment
1. **Security**: Change default passwords and JWT secret
2. **HTTPS**: Configure SSL certificates  
3. **Backup**: Set up database backups
4. **Monitoring**: Add system monitoring

### Advanced Features
1. **Custom Vocabulary**: Add industry-specific terms
2. **Email Integration**: Automated meeting summaries
3. **Calendar Sync**: Auto-recording from calendar events
4. **User Management**: Create team accounts

### Development
1. **API Integration**: Build custom applications using the API
2. **Webhooks**: Set up event notifications
3. **Custom Templates**: Create meeting summary templates
4. **Analytics**: Build custom reporting dashboards

---

## 🎉 You're Ready!

Your Meeting-Ops system is now running and ready to transform your meetings into searchable, actionable intelligence. 

**Key URLs to bookmark:**
- 🌐 **App**: http://localhost:7778 (Login: admin/admin123)
- 📚 **API Docs**: http://localhost:9050/docs
- 🧪 **Test Page**: file:///srv/meeting-ops/test-login.html

**Need help?** Check the full documentation in `CLAUDE.md` or `README.md`.

---

*Built with ❤️ by Magic Unicorn Unconventional Technology & Stuff Inc.*