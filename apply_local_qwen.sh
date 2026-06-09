#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# apply_local_qwen.sh
# Apply to jobs using Qwen 3.5 on MI25 (works with ollama OR llama-server).
# Supports both 4B (default) and 9B models via --model flag.
#
# Uses whatever is running on localhost:11434/v1 — ollama or llama-server.
# If nothing is running, starts ollama automatically.
# The global Hermes config (~/.hermes/config.yaml) is NEVER modified.
#
# Usage:
#   ./apply_local_qwen.sh [--workers N] [--url <url>] [--no-fallback] [--model 4b|9b]
#   ./apply_local_qwen.sh --model 9b       # use Qwen 3.5 9B instead of 4B
# ──────────────────────────────────────────────────────────────────────────────

set -e

# Parse --model flag before other args
MODEL_FLAG="4b"
for arg in "$@"; do
    if [ "$arg" = "--model" ]; then
        # will be handled by the loop below
        true
    fi
done

# Re-parse properly
ARGS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --model)
            shift
            MODEL_FLAG="${1:-4b}"
            shift
            ;;
        *)
            ARGS+=("$1")
            shift
            ;;
    esac
done
set -- "${ARGS[@]}"

# Map model flag to full Ollama model name
case "$MODEL_FLAG" in
    4b|4B)
        MODEL="qwen3.5:4b"
        MODEL_LABEL="Qwen 3.5 4B"
        ;;
    9b|9B|8b|8B)
        MODEL="qwen3.5:9b"
        MODEL_LABEL="Qwen 3.5 9B"
        ;;
    *)
        echo "Unknown model: $MODEL_FLAG (use 4b or 9b)"
        exit 1
        ;;
esac

OLLAMA_PORT=11434
OLLAMA_LOG="/tmp/ollama_apply_qwen.log"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Colors
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${CYAN}[*]${NC} $1"; }
ok()    { echo -e "${GREEN}[✓]${NC} $1"; }
err()   { echo -e "${RED}[✗]${NC} $1"; }

# ── Check if API is already running ─────────────────────────────────────
info "Checking API on port ${OLLAMA_PORT}..."
if curl -s "http://localhost:${OLLAMA_PORT}/v1/models" > /dev/null 2>&1; then
    ok "API server already running on port ${OLLAMA_PORT}"
else
    info "No server running. Starting ollama with MI25 ROCm..."
    export HSA_OVERRIDE_GFX_VERSION=9.0.0
    export ROCR_VISIBLE_DEVICES=0
    export HIP_VISIBLE_DEVICES=0
    export OLLAMA_FLASH_ATTENTION=1

    # Context length: 9B needs less headroom on 8GB VRAM
    if [ "$MODEL_FLAG" = "9b" ] || [ "$MODEL_FLAG" = "9B" ] || [ "$MODEL_FLAG" = "8b" ]; then
        export OLLAMA_CONTEXT_LENGTH=65536
    else
        export OLLAMA_CONTEXT_LENGTH=245760
    fi

    export OLLAMA_NUM_PARALLEL=1

    pkill -f ollama 2>/dev/null || true
    sleep 1
    rm -f "$OLLAMA_LOG"

    env -i \
        PATH="/run/current-system/sw/bin:/usr/bin:/bin" \
        HOME="$HOME" \
        HSA_OVERRIDE_GFX_VERSION="9.0.0" \
        HIP_VISIBLE_DEVICES="0" \
        ROCR_VISIBLE_DEVICES="0" \
        OLLAMA_FLASH_ATTENTION="1" \
        OLLAMA_CONTEXT_LENGTH="${OLLAMA_CONTEXT_LENGTH}" \
        OLLAMA_NUM_PARALLEL="1" \
        ollama serve > "$OLLAMA_LOG" 2>&1 &
    OLLAMA_PID=$!

    for i in $(seq 1 30); do
        if curl -s "http://localhost:${OLLAMA_PORT}/api/tags" > /dev/null 2>&1; then
            ok "Ollama started (PID $OLLAMA_PID)"
            break
        fi
        [ $i -eq 30 ] && { err "Failed to start"; exit 1; }
        sleep 1
    done
fi

# ── Ensure model is pulled ──────────────────────────────────────────────
info "Checking ${MODEL}..."
if curl -s "http://localhost:${OLLAMA_PORT}/api/tags" 2>/dev/null | python3 -c "
import json,sys;d=json.load(sys.stdin);names=[m['name'] for m in d.get('models',[])];sys.exit(0 if '${MODEL}' in names else 1)" 2>/dev/null; then
    ok "Model available"
else
    info "Pulling ${MODEL} (~6.6GB for 9B)..."
    ollama pull "${MODEL}" 2>&1 | tail -1
fi

# ── Warm up ─────────────────────────────────────────────────────────────
info "Warming model in VRAM..."
WARM_OUTPUT=$(curl -s "http://localhost:${OLLAMA_PORT}/api/generate" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"${MODEL}\",\"prompt\":\"hello\",\"stream\":false,\"options\":{\"num_ctx\":${OLLAMA_CONTEXT_LENGTH:-245760}}}" \
    2>/dev/null)

if echo "$WARM_OUTPUT" | python3 -c 'import json,sys;d=json.load(sys.stdin);sys.exit(0 if d.get("response","") else 1)' 2>/dev/null; then
    ok "Model loaded"
    sleep 1
else
    err "Model failed to load"
    echo "$WARM_OUTPUT" | head -3
    exit 1
fi

# ── Verify GPU offloading (ollama only) ─────────────────────────────────
if grep -q "offloaded" "$OLLAMA_LOG" 2>/dev/null; then
    ok "$(grep "offloaded" "$OLLAMA_LOG" | tail -1)"
fi

# ── Launch apply pipeline ───────────────────────────────────────────────
echo ""
echo -e "${GREEN}══════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Model:  ${MODEL_LABEL}                         ${NC}"
echo -e "${GREEN}  API:    http://localhost:${OLLAMA_PORT}/v1        ${NC}"
echo -e "${GREEN}  Global  config NOT modified                     ${NC}"
echo -e "${GREEN}══════════════════════════════════════════════════${NC}"
echo ""

export LLM_PROVIDER=local
export LLM_URL="http://localhost:${OLLAMA_PORT}/v1"
export LLM_MODEL="${MODEL}"

cd "$SCRIPT_DIR"
exec .venv/bin/python3 run_apply.py --provider local --model "${MODEL}" "$@"
