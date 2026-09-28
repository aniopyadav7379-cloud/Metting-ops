#!/bin/bash

# Meeting-Ops Frontend Production Server
# This script builds and serves the production version of the frontend

set -e

echo "🚀 Starting Meeting-Ops Frontend Production Server..."
echo "================================================"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
FRONTEND_PORT=${FRONTEND_PORT:-7777}
BACKEND_URL=${BACKEND_URL:-http://localhost:9050}
MODE=${1:-production}  # Default to production, can pass 'dev' as argument

# Function to check if port is in use
check_port() {
    if lsof -Pi :$1 -sTCP:LISTEN -t >/dev/null 2>&1; then
        return 0
    else
        return 1
    fi
}

# Function to find available port
find_available_port() {
    local port=$1
    while check_port $port; do
        echo -e "${YELLOW}Port $port is in use, trying next port...${NC}"
        port=$((port + 1))
    done
    echo $port
}

# Change to frontend directory
cd "$(dirname "$0")"

# Check for node_modules
if [ ! -d "node_modules" ]; then
    echo -e "${YELLOW}📦 Installing dependencies...${NC}"
    npm install
fi

# Check if backend is running
echo -e "${BLUE}🔍 Checking if backend is running...${NC}"
if ! nc -z localhost 9050 2>/dev/null; then
    echo -e "${YELLOW}⚠️  Backend not detected on port 9050${NC}"
    echo -e "${YELLOW}💡 Start backend with: cd ../backend && ./start-backend.sh${NC}"
    echo ""
fi

# Create .env file for production if needed
if [ "$MODE" = "production" ] && [ ! -f ".env.production" ]; then
    echo -e "${BLUE}📝 Creating production environment file...${NC}"
    cat > .env.production << EOF
VITE_API_URL=$BACKEND_URL
VITE_WS_URL=${BACKEND_URL/http/ws}
EOF
    echo -e "${GREEN}✅ Production environment configured${NC}"
fi

if [ "$MODE" = "dev" ]; then
    # Development mode
    echo -e "${BLUE}🔧 Starting development server...${NC}"
    echo -e "${GREEN}🌐 Access at: http://localhost:7777${NC}"
    echo -e "${GREEN}👥 Login credentials:${NC}"
    echo "   Admin: admin / admin123"
    echo "   User:  user / user123"
    npm run dev
else
    # Production mode
    echo -e "${BLUE}🔨 Building production version...${NC}"
    npm run build:production

    if [ $? -ne 0 ]; then
        echo -e "${RED}❌ Build failed!${NC}"
        exit 1
    fi

    echo -e "${GREEN}✅ Build completed successfully${NC}"

    # Find available port
    AVAILABLE_PORT=$(find_available_port $FRONTEND_PORT)

    # Install serve if not available
    if ! command -v serve &> /dev/null; then
        echo -e "${YELLOW}📦 Installing 'serve' to run production server...${NC}"
        npm install -g serve
    fi

    echo -e "${BLUE}🌐 Starting production server on port $AVAILABLE_PORT...${NC}"
    echo -e "${GREEN}================================================${NC}"
    echo -e "${GREEN}🎉 Meeting-Ops Frontend Production Server${NC}"
    echo -e "${GREEN}🌐 Access at: http://localhost:$AVAILABLE_PORT${NC}"
    echo -e "${GREEN}🌐 Network: http://$(hostname -I | awk '{print $1}'):$AVAILABLE_PORT${NC}"
    echo -e "${GREEN}👥 Login credentials:${NC}"
    echo "   Admin: admin / admin123"
    echo "   User:  user / user123"
    echo -e "${GREEN}================================================${NC}"
    
    # Start production server
    npx serve -s dist -l $AVAILABLE_PORT --no-clipboard
fi