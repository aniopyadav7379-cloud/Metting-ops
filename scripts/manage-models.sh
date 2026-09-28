#!/bin/bash
#
# Meeting-Ops Model Manager
# Manages GGUF models for llama.cpp Vulkan
#
# Usage:
#   ./scripts/manage-models.sh list       - List models in ./models/ with sizes
#   ./scripts/manage-models.sh status     - Show active model and llama.cpp health
#   ./scripts/manage-models.sh download   - Download GPT-OSS 20B and/or Granite 3.3 2B
#   ./scripts/manage-models.sh switch <model-key>  - Switch active LLM model via API
#
# Model keys: gpt-oss-20b, granite-3.3-2b

set -e

# Resolve project root from script location
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
MODELS_DIR="$PROJECT_DIR/models"

BACKEND_URL="http://localhost:9050"
LLAMA_URL="http://localhost:11437"

# GPT-OSS 20B (default)
PRIMARY_MODEL_FILE="gpt-oss-20b-mxfp4.gguf"
PRIMARY_MODEL_URL="https://huggingface.co/OptimusCode/gpt-oss-20b-GGUF/resolve/main/${PRIMARY_MODEL_FILE}"

# Granite 3.3 2B (fallback)
FALLBACK_MODEL_FILE="granite-3.3-2b-instruct-Q4_K_M.gguf"
FALLBACK_MODEL_URL="https://huggingface.co/lmstudio-community/granite-3.3-2b-instruct-GGUF/resolve/main/${FALLBACK_MODEL_FILE}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

print_status() { echo -e "${BLUE}[INFO]${NC} $1"; }
print_success() { echo -e "${GREEN}[OK]${NC} $1"; }
print_warning() { echo -e "${YELLOW}[WARN]${NC} $1"; }
print_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# ---- list: Show GGUF models in ./models/ ----
cmd_list() {
    echo ""
    echo -e "${BOLD}GGUF Models in $MODELS_DIR/${NC}"
    echo "-------------------------------------------"

    if [ ! -d "$MODELS_DIR" ]; then
        print_warning "Models directory not found: $MODELS_DIR"
        return
    fi

    local found=0
    for f in "$MODELS_DIR"/*.gguf; do
        [ -f "$f" ] || continue
        found=1
        local name size
        name=$(basename "$f")
        size=$(du -h "$f" | cut -f1)
        local marker=""
        if [ "$name" = "$PRIMARY_MODEL_FILE" ]; then
            marker=" ${GREEN}(default)${NC}"
        elif [ "$name" = "$FALLBACK_MODEL_FILE" ]; then
            marker=" ${YELLOW}(fallback)${NC}"
        fi
        echo -e "  $name  [$size]$marker"
    done

    if [ "$found" -eq 0 ]; then
        print_warning "No .gguf files found. Run: $0 download"
    fi
    echo ""
}

# ---- status: Show active model and llama.cpp health ----
cmd_status() {
    echo ""
    echo -e "${BOLD}LLM Status${NC}"
    echo "-------------------------------------------"

    # Check llama.cpp health
    echo -n "  llama.cpp (port 11437): "
    if curl -sf "$LLAMA_URL/health" >/dev/null 2>&1; then
        echo -e "${GREEN}running${NC}"
    else
        echo -e "${RED}not responding${NC}"
    fi

    # Check active model via backend API
    echo -n "  Active model:           "
    if curl -sf "$BACKEND_URL/api/settings/models" >/dev/null 2>&1; then
        local model_info
        model_info=$(curl -sf "$BACKEND_URL/api/settings/models")
        local active
        active=$(echo "$model_info" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('active_model','unknown'))" 2>/dev/null || echo "unknown")
        echo -e "${GREEN}$active${NC}"
    else
        echo -e "${YELLOW}backend not running${NC}"
    fi

    # List available models via API
    echo ""
    echo -n "  Available models:       "
    if curl -sf "$BACKEND_URL/api/settings/models/available" >/dev/null 2>&1; then
        local models
        models=$(curl -sf "$BACKEND_URL/api/settings/models/available")
        echo "$models" | python3 -c "
import sys, json
data = json.load(sys.stdin)
models = data if isinstance(data, list) else data.get('models', [])
for m in models:
    name = m.get('name', m.get('key', 'unknown'))
    active = ' *' if m.get('active', m.get('is_active', False)) else ''
    print(f'                            {name}{active}')
" 2>/dev/null || echo "could not parse"
    else
        echo -e "${YELLOW}backend not running${NC}"
    fi

    echo ""
}

# ---- download: Download model files ----
cmd_download() {
    local target="${1:-all}"

    mkdir -p "$MODELS_DIR"

    download_file() {
        local name="$1" url="$2" size_label="$3"

        if [ -f "$MODELS_DIR/$name" ]; then
            local fsize
            fsize=$(stat -c%s "$MODELS_DIR/$name" 2>/dev/null || stat -f%z "$MODELS_DIR/$name" 2>/dev/null)
            if [ "$fsize" -gt 1000000 ]; then
                print_success "Already present: $name ($(numfmt --to=iec-i --suffix=B "$fsize" 2>/dev/null || echo "${fsize} bytes"))"
                return 0
            fi
        fi

        print_status "Downloading $name ($size_label)..."
        wget -c -O "$MODELS_DIR/$name" "$url" || {
            print_warning "wget failed, trying curl..."
            curl -L -C - -o "$MODELS_DIR/$name" "$url" || {
                print_error "Failed to download $name"
                print_error "Manual download: wget -O $MODELS_DIR/$name $url"
                return 1
            }
        }
        print_success "Downloaded: $name"
    }

    case "$target" in
        gpt-oss|gpt-oss-20b|primary)
            download_file "$PRIMARY_MODEL_FILE" "$PRIMARY_MODEL_URL" "~12 GB"
            ;;
        granite|granite-3.3|fallback)
            download_file "$FALLBACK_MODEL_FILE" "$FALLBACK_MODEL_URL" "~1.6 GB"
            ;;
        all|"")
            echo ""
            print_status "Downloading GPT-OSS 20B (default, ~12 GB)..."
            download_file "$PRIMARY_MODEL_FILE" "$PRIMARY_MODEL_URL" "~12 GB" || true
            echo ""
            print_status "Downloading Granite 3.3 2B (fallback, ~1.6 GB)..."
            download_file "$FALLBACK_MODEL_FILE" "$FALLBACK_MODEL_URL" "~1.6 GB" || true
            ;;
        *)
            print_error "Unknown model: $target"
            echo "  Available: gpt-oss-20b, granite-3.3-2b, all"
            return 1
            ;;
    esac
}

# ---- switch: Change active model via backend API ----
cmd_switch() {
    local model_key="$1"

    if [ -z "$model_key" ]; then
        print_error "Usage: $0 switch <model-key>"
        echo "  Available keys: gpt-oss-20b, granite-3.3-2b"
        return 1
    fi

    print_status "Switching active model to: $model_key"

    local response
    response=$(curl -sf -X POST "$BACKEND_URL/api/settings/models/active" \
        -H "Content-Type: application/json" \
        -d "{\"model\": \"$model_key\"}" 2>&1) || {
        print_error "Failed to switch model. Is the backend running on port 9050?"
        return 1
    }

    echo "$response" | python3 -m json.tool 2>/dev/null || echo "$response"
    print_success "Model switched to: $model_key"
    print_warning "Note: llama.cpp must be restarted with the new model for it to take effect."
    print_status "Restart: docker compose -f docker-compose-full-stack.yml restart llama-gpu"
}

# ---- Main ----
echo ""
echo -e "${BOLD}Meeting-Ops Model Manager${NC}"
echo ""

case "${1:-help}" in
    list)
        cmd_list
        ;;
    status)
        cmd_status
        ;;
    download)
        cmd_download "$2"
        ;;
    switch)
        cmd_switch "$2"
        ;;
    help|--help|-h|*)
        echo "Usage: $0 <command> [args]"
        echo ""
        echo "Commands:"
        echo "  list                     List GGUF models in ./models/"
        echo "  status                   Show active model and llama.cpp health"
        echo "  download [model]         Download models (gpt-oss-20b, granite-3.3-2b, all)"
        echo "  switch <model-key>       Switch active LLM (gpt-oss-20b, granite-3.3-2b)"
        echo ""
        echo "Examples:"
        echo "  $0 list"
        echo "  $0 download gpt-oss-20b"
        echo "  $0 switch granite-3.3-2b"
        ;;
esac
