#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# llama-server auto-restart wrapper
# Runs llama-server in a while loop with GPU fault recovery.
# Independent of run_apply.py — start once, leave running.
# Usage:  ./llama-server-wrapper.sh [--model 9b|4b|9bd] [--port 11434]
# ──────────────────────────────────────────────────────────────────────────────
# ── Config ──────────────────────────────────────────────────────────────────
# NOTE: no set -e — the while loop handles errors manually via _exit_code.
# Without this, a llama-server crash (non-zero exit) kills the entire script.
MODEL_FLAG="${1:-9b}"
LLAMA_PORT="${2:-11434}"
HOST="127.0.0.1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="/tmp/llama_apply_qwen.log"

# ── Resolve model config ────────────────────────────────────────────────────
# (mirrors apply_local_llama.sh's model config)
case "$MODEL_FLAG" in
    4b|4B)
        MODEL_GGUF="$HOME/Code/qwen_mi25/Qwen3.5-4B-MTP-Q4_K_M.gguf"
        MTP_FLAGS="--spec-type draft-mtp --spec-draft-n-max 3"
        MODEL_CTX=245760
        NGL=33
        MODEL="qwen3.5:4b"
        ;;
    9b|9B|9bd|9BD|9b-draft|9B-DRAFT)
        MODEL_GGUF="$HOME/Code/qwen_mi25/Qwen3.5-9B-MTP-Q4_K_M.gguf"
        MTP_FLAGS="--spec-type draft-mtp --spec-draft-n-max 3"
        MODEL_CTX=98000
        NGL=33
        MODEL="qwen3.5:9b"
        ;;
    *)
        echo "Unknown model: $MODEL_FLAG"
        exit 1
        ;;
esac

# ── Find llama-server binary ────────────────────────────────────────────────
LLAMA_SERVER=""
for candidate in \
    "$HOME/.nix-profile/bin/llama-server" \
    "$HOME/Code/qwen_mi25/llama.cpp/build/bin/llama-server" \
    "$SCRIPT_DIR/../qwen_mi25/llama.cpp/build/bin/llama-server"; do
    if [ -x "$candidate" ]; then
        LLAMA_SERVER="$candidate"
        break
    fi
done
if [ -z "$LLAMA_SERVER" ]; then
    echo "llama-server binary not found!"
    exit 1
fi

# ── Main restart loop ──────────────────────────────────────────────────────
# This loop is INDEPENDENT of applypilot's restart mechanisms.  It runs
# forever, survives GPU faults, and ensures llama-server is always available
# on the configured port.  The Python GPU monitor in run_apply.py is a
# secondary fallback.
# ────────────────────────────────────────────────────────────────────────────

info()  { echo -e "[$(date '+%H:%M:%S')] \e[36m*\e[0m $*"; }
ok()    { echo -e "[$(date '+%H:%M:%S')] \e[32m✓\e[0m $*"; }
err()   { echo -e "[$(date '+%H:%M:%S')] \e[31m✗\e[0m $*"; }

# Kill stale server on the port
fuser -k "${LLAMA_PORT}/tcp" 2>/dev/null || true
sleep 1
rm -f "$LOG"

info "Starting llama-server ($MODEL_FLAG, port $LLAMA_PORT)..."
info "Server: $LLAMA_SERVER"
info "Model:  $MODEL_GGUF"
info "Log:    $LOG"

while true; do
    rm -f "$LOG"
    fuser -k "${LLAMA_PORT}/tcp" 2>/dev/null || true
    sleep 1
    info "Starting server instance..."

    env -i \
        PATH="/run/wrappers/bin:$HOME/.nix-profile/bin:/nix/profile/default/bin:/run/current-system/sw/bin" \
        HOME="$HOME" \
        HSA_OVERRIDE_GFX_VERSION=9.0.0 \
        HIP_VISIBLE_DEVICES=0 \
        ROCR_VISIBLE_DEVICES=0 \
        "$LLAMA_SERVER" \
        -m "$MODEL_GGUF" \
        -ngl ${NGL} \
        --flash-attn on \
        --cache-prompt \
        --cache-type-k q8_0 \
        --cache-type-v q8_0 \
        --reasoning off \
        --temp 0.3 \
        --parallel 1 \
        -b 32768 \
        --alias "${MODEL}" \
        --timeout 600 \
        ${MTP_FLAGS:-} \
        -c ${MODEL_CTX} \
        --host "$HOST" \
        --port "$LLAMA_PORT" \
        >> "$LOG" 2>&1

    _exit_code=$?
    err "llama-server exited (code $_exit_code) — restarting..."

    # Check GPU health — if VRAM dropped to near-zero, GPU faulted
    _vram_used=$(rocm-smi --showmeminfo vram 2>/dev/null | grep "VRAM Total Used Memory" | grep -oP '\d+' | tail -1)
    if [ -n "$_vram_used" ] && [ "$_vram_used" -lt 1000000000 ] 2>/dev/null; then
        err "GPU VRAM dropped to ${_vram_used} — resetting GPU..."
        sudo rocm-smi --gpureset -d 0 2>/dev/null || true
        sleep 8
    fi

    # Wait for model to finish loading before declaring ready
    info "Waiting for model to load..."
    while ! grep -q "model loaded" "$LOG" 2>/dev/null; do
        sleep 2
    done
    ok "Model loaded — server ready on port $LLAMA_PORT"

    sleep 2
done
