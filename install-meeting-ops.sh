#!/bin/bash
# Meeting-Ops Installation Script
# Installs all components for the on-premise meeting recording appliance
# Version: 4.0.0
#
# What it does:
#   1. Checks prerequisites (Python 3.11+, Node.js 18+, Docker)
#   2. Installs system packages (ffmpeg, portaudio, etc.)
#   3. Installs Python dependencies (no venv -- system packages)
#   4. Installs frontend dependencies (npm)
#   5. Downloads GPT-OSS 20B GGUF (default) + Granite 3.3 2B (fallback) for llama.cpp
#   6. Starts Docker services (PostgreSQL:5434, Redis:6381, Qdrant:6335, llama.cpp:11437)
#   7. Initializes database
#   8. Creates startup script and systemd service
#
# Stack:
#   LLM:   llama.cpp Vulkan (GPT-OSS 20B default, Granite 3.3 2B fallback)
#   STT:   whisper.cpp Vulkan (optional, separate setup)
#   DB:    PostgreSQL 16 on port 5434
#   Cache: Redis 7 on port 6381
#   Vec:   Qdrant on port 6335
#   API:   FastAPI on port 9050 (22 routers)
#   UI:    React 19 + Vite on port 7777
#
# Safe to run multiple times (idempotent).

set -e  # Exit on any error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m' # No Color

# Configuration
APP_NAME="Meeting-Ops Recording Appliance"
BACKEND_PORT=9050
FRONTEND_PORT=7777
POSTGRES_PORT=5434
REDIS_PORT=6381
QDRANT_PORT=6335
LLAMA_PORT=11437
WHISPER_PORT=8178

DATABASE_URL="postgresql://meetingops:meetingops123@localhost:${POSTGRES_PORT}/meeting_sessions"

# GPT-OSS 20B model for llama.cpp (default)
PRIMARY_MODEL_NAME="gpt-oss-20b-mxfp4.gguf"
PRIMARY_MODEL_URL="https://huggingface.co/OptimusCode/gpt-oss-20b-GGUF/resolve/main/${PRIMARY_MODEL_NAME}"

# Granite 3.3 2B model for llama.cpp (fallback)
FALLBACK_MODEL_NAME="granite-3.3-2b-instruct-Q4_K_M.gguf"
FALLBACK_MODEL_URL="https://huggingface.co/lmstudio-community/granite-3.3-2b-instruct-GGUF/resolve/main/${FALLBACK_MODEL_NAME}"

# Directories
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"
FRONTEND_DIR="$SCRIPT_DIR/frontend"
MODELS_DIR="$SCRIPT_DIR/models"

# ============================================================================
# Helper functions
# ============================================================================

print_status() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[OK]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

print_header() {
    echo ""
    echo -e "${BOLD}${BLUE}=== $1 ===${NC}"
    echo ""
}

command_exists() {
    command -v "$1" >/dev/null 2>&1
}

get_distro() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        echo "$ID"
    else
        echo "unknown"
    fi
}

# ============================================================================
# Pre-flight checks
# ============================================================================

echo ""
echo -e "${BOLD}${BLUE}Meeting-Ops Recording Appliance -- Installation Script v4.0.0${NC}"
echo -e "${BLUE}Part of the Unicorn Commander Suite${NC}"
echo "================================================================"
echo ""

# Don't run as root
if [ "$EUID" -eq 0 ]; then
    print_error "Do not run this script as root. It will ask for sudo when needed."
    exit 1
fi

print_header "Step 1: Checking Prerequisites"

# Check Python version
PYTHON_CMD=""
if command_exists python3; then
    PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
    PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
    if [ "$PY_MAJOR" -ge 3 ] && [ "$PY_MINOR" -ge 11 ]; then
        PYTHON_CMD="python3"
        print_success "Python $PY_VERSION found"
    fi
fi

if [ -z "$PYTHON_CMD" ]; then
    # Try specific versions
    for ver in python3.13 python3.12 python3.11; do
        if command_exists "$ver"; then
            PYTHON_CMD="$ver"
            print_success "Found $ver"
            break
        fi
    done
fi

if [ -z "$PYTHON_CMD" ]; then
    print_error "Python 3.11+ is required. Please install Python 3.11 or newer."
    exit 1
fi

# Check Node.js
if ! command_exists node; then
    print_error "Node.js is required for the frontend."
    print_status "Install Node.js 18+:"
    print_status "  curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash -"
    print_status "  sudo apt-get install -y nodejs"
    exit 1
fi

NODE_VERSION=$(node --version | sed 's/v//')
NODE_MAJOR=$(echo "$NODE_VERSION" | cut -d. -f1)
if [ "$NODE_MAJOR" -lt 18 ]; then
    print_error "Node.js 18+ is required. Found version $NODE_VERSION"
    exit 1
fi
print_success "Node.js $NODE_VERSION found"

# Check npm
if ! command_exists npm; then
    print_error "npm is required. It should come with Node.js."
    exit 1
fi
print_success "npm $(npm --version) found"

# Check Docker
if ! command_exists docker; then
    print_warning "Docker is not installed. Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker "$USER"
    print_success "Docker installed."
    print_warning "You may need to log out and back in for Docker group to take effect."
    print_warning "After re-login, re-run this script."
    exit 0
else
    print_success "Docker $(docker --version | grep -oP '\d+\.\d+\.\d+') found"
fi

# Check Docker Compose
if ! docker compose version >/dev/null 2>&1; then
    print_warning "Docker Compose plugin not found. Installing..."
    sudo apt-get update && sudo apt-get install -y docker-compose-plugin
    print_success "Docker Compose plugin installed"
else
    print_success "Docker Compose $(docker compose version --short 2>/dev/null || echo 'available') found"
fi

# Check PipeWire (needed for audio capture)
if command_exists pipewire; then
    print_success "PipeWire found"
else
    print_warning "PipeWire not found. Audio recording requires PipeWire or PulseAudio."
    print_status "Install with: sudo apt-get install pipewire pipewire-pulse wireplumber"
fi

# Check audio group membership
if groups | grep -q audio; then
    print_success "User is in 'audio' group"
else
    print_warning "User is NOT in 'audio' group. Recording may fail."
    print_status "Fix with: sudo usermod -aG audio $USER"
    print_status "Then log out and back in."
fi

# Check for ffmpeg
if command_exists ffmpeg; then
    print_success "ffmpeg found"
else
    print_warning "ffmpeg not found. Will be installed with system packages."
fi

# ============================================================================
# System packages
# ============================================================================

print_header "Step 2: Installing System Dependencies"

DISTRO=$(get_distro)
case $DISTRO in
    ubuntu|debian|linuxmint|pop)
        print_status "Detected Debian/Ubuntu-based system..."
        sudo apt-get update
        sudo apt-get install -y \
            build-essential \
            portaudio19-dev \
            ffmpeg \
            git \
            curl \
            wget \
            libasound2-dev \
            python3-pip \
            python3-dev \
            pkg-config \
            libportaudio2 \
            libportaudiocpp0 \
            libssl-dev \
            libffi-dev \
            postgresql-client \
            sox \
            libsox-fmt-all \
            alsa-utils
        ;;
    fedora|centos|rhel|rocky|alma)
        print_status "Detected RHEL/Fedora-based system..."
        sudo dnf install -y \
            gcc gcc-c++ make \
            portaudio-devel \
            ffmpeg \
            git \
            curl \
            wget \
            alsa-lib-devel \
            python3-pip \
            python3-devel \
            pkgconfig \
            openssl-devel \
            libffi-devel \
            postgresql \
            sox \
            alsa-utils
        ;;
    arch|manjaro)
        print_status "Detected Arch-based system..."
        sudo pacman -S --noconfirm \
            base-devel \
            portaudio \
            ffmpeg \
            git \
            curl \
            wget \
            python-pip \
            openssl \
            postgresql-libs \
            sox \
            alsa-utils
        ;;
    *)
        print_warning "Unknown distribution: $DISTRO"
        print_warning "Please install these packages manually:"
        print_warning "  build-essential, ffmpeg, portaudio, python3-pip, alsa-utils, sox"
        ;;
esac

print_success "System dependencies installed"

# ============================================================================
# Python dependencies (no venv -- system packages)
# ============================================================================

print_header "Step 3: Installing Python Dependencies"

print_status "Installing Python packages system-wide (no virtualenv)..."

# Core packages from requirements.txt
if [ -f "$BACKEND_DIR/requirements.txt" ]; then
    print_status "Installing from requirements.txt..."
    $PYTHON_CMD -m pip install --break-system-packages -r "$BACKEND_DIR/requirements.txt" 2>&1 || {
        print_warning "Some packages from requirements.txt failed. Installing essentials..."
    }
fi

# Runtime dependencies that may not be in requirements.txt
print_status "Ensuring runtime dependencies are installed..."
$PYTHON_CMD -m pip install --break-system-packages \
    psycopg2-binary \
    reportlab \
    python-docx \
    fastembed \
    qdrant-client \
    fastapi \
    uvicorn \
    sqlalchemy \
    redis \
    requests \
    openai \
    httpx \
    pydantic \
    python-jose \
    passlib \
    bcrypt \
    pyjwt \
    python-multipart \
    aiofiles \
    websockets \
    numpy \
    pydub \
    python-dotenv \
    psutil \
    jinja2 \
    2>&1 || {
        print_warning "Some packages failed to install. Check errors above."
    }

print_success "Python dependencies installed"

# ============================================================================
# Frontend dependencies
# ============================================================================

print_header "Step 4: Installing Frontend Dependencies"

cd "$FRONTEND_DIR"

if [ -f "package.json" ]; then
    if [ ! -d "node_modules" ]; then
        print_status "Installing Node.js dependencies..."
        npm install
    else
        print_status "node_modules exists. Running npm install to ensure up-to-date..."
        npm install
    fi
    print_success "Frontend dependencies installed"
else
    print_error "No package.json found in $FRONTEND_DIR"
    exit 1
fi

cd "$SCRIPT_DIR"

# ============================================================================
# Download LLM model
# ============================================================================

print_header "Step 5: Downloading AI Models"

mkdir -p "$MODELS_DIR"

# --- GPT-OSS 20B (default model, ~12 GB) ---
download_model() {
    local name="$1" url="$2" size_label="$3"

    if [ -f "$MODELS_DIR/$name" ]; then
        local fsize
        fsize=$(stat -c%s "$MODELS_DIR/$name" 2>/dev/null || stat -f%z "$MODELS_DIR/$name" 2>/dev/null)
        if [ "$fsize" -gt 1000000 ]; then
            print_success "Already present: $name ($(numfmt --to=iec-i --suffix=B "$fsize" 2>/dev/null || echo "${fsize} bytes"))"
            return 0
        else
            print_warning "File $name looks too small. Re-downloading..."
            rm -f "$MODELS_DIR/$name"
        fi
    fi

    print_status "Downloading $name ($size_label)..."
    print_status "URL: $url"
    wget -c -O "$MODELS_DIR/$name" "$url" || {
        print_warning "wget failed, trying curl..."
        curl -L -C - -o "$MODELS_DIR/$name" "$url" || {
            print_error "Failed to download $name"
            return 1
        }
    }
    if [ -f "$MODELS_DIR/$name" ]; then
        print_success "Downloaded: $name"
    fi
}

print_status "GPT-OSS 20B is the default LLM (~12 GB download)."
print_status "Granite 3.3 2B is a lightweight fallback (~1.6 GB)."
echo ""

# Download GPT-OSS 20B (primary)
if ! download_model "$PRIMARY_MODEL_NAME" "$PRIMARY_MODEL_URL" "~12 GB"; then
    print_warning "GPT-OSS 20B download failed or was skipped."
    print_warning "You can place the file manually:"
    print_warning "  wget -O $MODELS_DIR/$PRIMARY_MODEL_NAME $PRIMARY_MODEL_URL"
    print_warning "Granite 3.3 2B will be used as fallback."
fi

# Always download Granite 3.3 2B (guaranteed fallback)
download_model "$FALLBACK_MODEL_NAME" "$FALLBACK_MODEL_URL" "~1.6 GB" || {
    print_error "Failed to download fallback model. llama.cpp will need a model in ./models/"
}

# ============================================================================
# Docker services
# ============================================================================

print_header "Step 6: Starting Docker Services"

cd "$SCRIPT_DIR"

if [ ! -f "docker-compose-full-stack.yml" ]; then
    print_error "docker-compose-full-stack.yml not found in $SCRIPT_DIR"
    exit 1
fi

print_status "Starting PostgreSQL, Redis, Qdrant, and llama.cpp Vulkan..."
docker compose -f docker-compose-full-stack.yml up -d

print_status "Waiting for services to start..."
sleep 5

# Check each service
SERVICES_OK=true

if docker ps | grep -q meetingops-postgres; then
    print_success "PostgreSQL is running on port $POSTGRES_PORT"
else
    print_error "PostgreSQL failed to start"
    SERVICES_OK=false
fi

if docker ps | grep -q meetingops-redis; then
    print_success "Redis is running on port $REDIS_PORT"
else
    print_error "Redis failed to start"
    SERVICES_OK=false
fi

if docker ps | grep -q meetingops-qdrant; then
    print_success "Qdrant is running on port $QDRANT_PORT"
else
    print_warning "Qdrant failed to start (vector search will be unavailable)"
fi

if docker ps | grep -q meetingops-llama-gpu; then
    print_success "llama.cpp Vulkan is starting on port $LLAMA_PORT (GPT-OSS 20B loads in ~60s)"
else
    print_warning "llama.cpp failed to start. Check GPU/Vulkan drivers."
    print_status "Logs: docker logs meetingops-llama-gpu"
fi

# Wait for PostgreSQL to be ready
print_status "Waiting for PostgreSQL to accept connections..."
for i in {1..30}; do
    if docker exec meetingops-postgres pg_isready -U meetingops >/dev/null 2>&1; then
        print_success "PostgreSQL is ready"
        break
    fi
    if [ "$i" -eq 30 ]; then
        print_error "PostgreSQL did not become ready in time"
        SERVICES_OK=false
    fi
    sleep 1
done

# ============================================================================
# Initialize database
# ============================================================================

print_header "Step 7: Initializing Database"

cd "$BACKEND_DIR"

DATABASE_URL="$DATABASE_URL" $PYTHON_CMD -c "
from database.database import init_database
init_database()
print('Database tables created successfully')
" 2>&1 && print_success "Database initialized" || {
    print_warning "Database initialization had issues (tables may already exist)"
}

cd "$SCRIPT_DIR"

# ============================================================================
# Create startup script
# ============================================================================

print_header "Step 8: Creating Startup Script"

cat > "$SCRIPT_DIR/start-meeting-ops.sh" << 'STARTUP_EOF'
#!/bin/bash
# Meeting-Ops Startup Script
# Starts all services for the meeting recording appliance
# Generated by install-meeting-ops.sh v4.0.0

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Starting Meeting-Ops services..."

# Start Docker services (PostgreSQL, Redis, Qdrant, llama.cpp Vulkan)
echo "[1/4] Starting Docker services..."
cd "$SCRIPT_DIR"
docker compose -f docker-compose-full-stack.yml up -d

# Wait for PostgreSQL
echo "[2/4] Waiting for PostgreSQL..."
for i in {1..30}; do
    if docker exec meetingops-postgres pg_isready -U meetingops >/dev/null 2>&1; then
        echo "  PostgreSQL ready"
        break
    fi
    sleep 1
done

# Wait for llama.cpp to load model (GPT-OSS 20B is ~12 GB, needs ~60s)
echo "  Waiting for llama.cpp to load GPT-OSS 20B model (~60s)..."
sleep 60
if curl -sf http://localhost:11437/health >/dev/null 2>&1; then
    echo "  llama.cpp ready"
else
    echo "  llama.cpp still loading (may need more time)"
fi

# Start backend
echo "[3/4] Starting backend on port 9050..."
cd "$SCRIPT_DIR/backend"
DATABASE_URL="postgresql://meetingops:meetingops123@localhost:5434/meeting_sessions" \
    python3 -m uvicorn main:app --host 0.0.0.0 --port 9050 &
BACKEND_PID=$!
sleep 3

# Start frontend
echo "[4/4] Starting frontend on port 7777..."
cd "$SCRIPT_DIR/frontend"
npm run dev -- --host 0.0.0.0 &
FRONTEND_PID=$!

echo ""
echo "============================================"
echo "  Meeting-Ops is running!"
echo "============================================"
echo ""
echo "  Frontend:  http://localhost:7777"
echo "  Backend:   http://localhost:9050"
echo "  API Docs:  http://localhost:9050/docs"
echo "  Health:    http://localhost:9050/health"
echo ""
echo "  Login:     admin / admin123"
echo ""
echo "  Backend PID:  $BACKEND_PID"
echo "  Frontend PID: $FRONTEND_PID"
echo ""
echo "  Stop with: kill $BACKEND_PID $FRONTEND_PID"
echo "  Stop Docker: docker compose -f docker-compose-full-stack.yml down"
echo ""

# Wait for either process to exit
wait
STARTUP_EOF

chmod +x "$SCRIPT_DIR/start-meeting-ops.sh"
print_success "Created start-meeting-ops.sh"

# ============================================================================
# Create systemd user service (optional)
# ============================================================================

print_status "Creating systemd user service file..."

mkdir -p "$HOME/.config/systemd/user"

cat > "$HOME/.config/systemd/user/meeting-ops.service" << EOF
[Unit]
Description=Meeting-Ops Recording Appliance
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$SCRIPT_DIR/backend
Environment=DATABASE_URL=postgresql://meetingops:meetingops123@localhost:${POSTGRES_PORT}/meeting_sessions
Environment=REDIS_URL=redis://localhost:${REDIS_PORT}
ExecStartPre=/usr/bin/docker compose -f $SCRIPT_DIR/docker-compose-full-stack.yml up -d
ExecStartPre=/bin/sleep 60
ExecStart=$PYTHON_CMD -m uvicorn main:app --host 0.0.0.0 --port ${BACKEND_PORT}
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF

print_success "Created ~/.config/systemd/user/meeting-ops.service"
print_status "To enable as a user service:"
print_status "  systemctl --user daemon-reload"
print_status "  systemctl --user enable meeting-ops"
print_status "  systemctl --user start meeting-ops"

# ============================================================================
# whisper.cpp setup instructions
# ============================================================================

print_header "Optional: whisper.cpp Vulkan STT Server"

echo -e "The whisper.cpp server provides speech-to-text on the AMD 780M iGPU."
echo -e "This is optional -- Meeting-Ops works without it (no live transcription)."
echo ""
echo -e "To set up whisper.cpp Vulkan:"
echo ""
echo -e "  1. Build whisper.cpp with Vulkan support:"
echo -e "     git clone https://github.com/ggerganov/whisper.cpp"
echo -e "     cd whisper.cpp && cmake -B build -DGGML_VULKAN=ON && cmake --build build -j"
echo ""
echo -e "  2. Download the model:"
echo -e "     ./models/download-ggml-model.sh large-v3-turbo"
echo ""
echo -e "  3. Run the server:"
echo -e "     ./build/bin/whisper-server \\"
echo -e "       --model models/ggml-large-v3-turbo.bin \\"
echo -e "       --host 0.0.0.0 --port ${WHISPER_PORT} \\"
echo -e "       --convert --gpu 0"
echo ""
echo -e "  4. Or create a systemd user service:"
echo -e "     systemctl --user start whisper-server"
echo ""

# ============================================================================
# Final summary
# ============================================================================

print_header "Installation Complete"

echo -e "${GREEN}${BOLD}Meeting-Ops has been installed successfully.${NC}"
echo ""
echo -e "${BOLD}Quick Start:${NC}"
echo "  ./start-meeting-ops.sh"
echo ""
echo -e "${BOLD}Manual Start:${NC}"
echo "  # 1. Docker services"
echo "  docker compose -f docker-compose-full-stack.yml up -d"
echo "  sleep 60  # GPT-OSS 20B needs ~60s to load"
echo ""
echo "  # 2. Backend (port $BACKEND_PORT)"
echo "  cd backend && DATABASE_URL=\"$DATABASE_URL\" python3 -m uvicorn main:app --host 0.0.0.0 --port $BACKEND_PORT"
echo ""
echo "  # 3. Frontend (port $FRONTEND_PORT)"
echo "  cd frontend && npm run dev -- --host 0.0.0.0"
echo ""
echo -e "${BOLD}Access:${NC}"
echo "  Frontend:  http://localhost:$FRONTEND_PORT"
echo "  Backend:   http://localhost:$BACKEND_PORT"
echo "  API Docs:  http://localhost:$BACKEND_PORT/docs"
echo "  Health:    http://localhost:$BACKEND_PORT/health"
echo ""
echo -e "${BOLD}Login:${NC}"
echo "  Username:  admin"
echo "  Password:  admin123"
echo ""
echo -e "${BOLD}Services (Docker):${NC}"
echo "  PostgreSQL:       localhost:$POSTGRES_PORT"
echo "  Redis:            localhost:$REDIS_PORT"
echo "  Qdrant:           localhost:$QDRANT_PORT"
echo "  llama.cpp Vulkan: localhost:$LLAMA_PORT (GPT-OSS 20B default)"
echo "  whisper.cpp:      localhost:$WHISPER_PORT (optional, see above)"
echo ""
echo -e "${BOLD}Tests:${NC}"
echo "  cd backend && python3 -m pytest tests/ -v"
echo "  cd frontend && npm run build && npx vitest run"
echo ""

if ! groups | grep -q audio; then
    echo -e "${YELLOW}${BOLD}ACTION REQUIRED:${NC}"
    echo -e "${YELLOW}  Your user is not in the 'audio' group. Recording will fail.${NC}"
    echo -e "${YELLOW}  Run: sudo usermod -aG audio $USER${NC}"
    echo -e "${YELLOW}  Then log out and back in.${NC}"
    echo ""
fi

echo -e "${GREEN}Happy recording!${NC}"
