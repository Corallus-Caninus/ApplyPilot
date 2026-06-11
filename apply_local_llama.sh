#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# apply_local_llama.sh
# Apply to jobs using Qwen 3.5 on MI25 via llama-server (GitHub master).
# Uses the ApplyPilot pipeline with 100% GPU and KV prompt caching.
#
# Features:
#   • 100% GPU via HSA_OVERRIDE_GFX_VERSION=9.0.0
#   • --cache-prompt — KV prompt caching across requests
#   • --flash-attn on — halves KV-cache VRAM usage
#   • --reasoning off — prevents empty content responses from Qwen3.5 CoT
#   • --monitor — shows where to tail model output in real-time
#   • Clean exit — SIGINT/EXIT kills llama-server + Chrome for fresh restart
#
# The global Hermes config (~/.hermes/config.yaml) is NEVER modified.
# Chrome is launched automatically by run_apply.py as needed.
#
# Usage:
#   ./apply_local_llama.sh [--workers N] [--url <url>] [--no-fallback] [--model 4b|9b]
#   ./apply_local_llama.sh run discover                              # discover only (no LLM)
#   ./apply_local_llama.sh run score                                 # score with local model
#   ./apply_local_llama.sh run discover score                        # discover + score
#   ./apply_local_llama.sh run all                                   # all pipeline stages
#   ./apply_local_llama.sh --model 9b                                # use Qwen 3.5 9B instead of 4B
#   ./apply_local_llama.sh --model 9b run score                     # score with 9B
#   ./apply_local_llama.sh --monitor                                 # with live model output box
#
# Prerequisites:
#   - GGUF downloaded: ~/Code/qwen_mi25/Qwen3.5-{4B,9B}-Q4_K_M.gguf
#   - llama-server built at ~/Code/qwen_mi25/llama.cpp/build/bin/
#   - Chrome/Chromium installed
# ──────────────────────────────────────────────────────────────────────────────

set -e

# ── Parse --model flag ────────────────────────────────────────────────────
MODEL_FLAG="lfm"
PASSTHROUGH_ARGS=()
SERVER_ONLY=false
while [ $# -gt 0 ]; do
    case "$1" in
        --model)
            shift
            MODEL_FLAG="${1:-4b}"
            shift
            ;;
        --server-only)
            SERVER_ONLY=true
            shift
            ;;
        *)
            PASSTHROUGH_ARGS+=("$1")
            shift
            ;;
    esac
done
set -- "${PASSTHROUGH_ARGS[@]}"

# ── Model config ──────────────────────────────────────────────────────────
case "$MODEL_FLAG" in
    4b|4B)
        MODEL="qwen3.5:4b"
        MODEL_LABEL="Qwen 3.5 4B (MTP)"
        MODEL_GGUF="$HOME/Code/qwen_mi25/Qwen3.5-4B-MTP-Q4_K_M.gguf"
        # 4B-MTP Q4_K_M ~2.7GB — self-drafts via MTP heads
        MODEL_CTX=245760
        NGL=33
        MTP_FLAGS="--spec-type draft-mtp --spec-draft-n-max 3"
        ;;
    9b|9B|8b|8B)
        MODEL="qwen3.5:9b"
        MODEL_LABEL="Qwen 3.5 9B (MTP)"
        MODEL_GGUF="$HOME/Code/qwen_mi25/Qwen3.5-9B-MTP-Q4_K_M.gguf"
        # 9B-MTP Q4_K_M ~5.6GB — self-drafts via MTP heads, no separate draft
        # 96K context — Hermes needs min 64K; 96K with self-MTP fits easily
        MODEL_CTX=96000
        NGL=33
        MTP_FLAGS="--spec-type draft-mtp --spec-draft-n-max 3"
        ;;
    9bd|9BD|9b-draft|9B-DRAFT)
        MODEL="qwen3.5:9b"
        MODEL_LABEL="Qwen 3.5 9B (20-token spec-draft)"
        MODEL_GGUF="$HOME/Code/qwen_mi25/Qwen3.5-9B-MTP-Q4_K_M.gguf"
        # 9B-MTP Q4_K_M ~5.6GB + 0.8B draft ~0.8GB
        # 64K context — Hermes requires minimum 64K or it exits with "Goodbye!"
        MODEL_CTX=64000
        NGL=33
        MTP_FLAGS=""
        ;;
    0.8b|0.8B|tiny|micro)
        MODEL="qwen3.5:0.8b"
        MODEL_LABEL="Qwen 3.5 0.8B (MTP)"
        MODEL_GGUF="$HOME/Code/qwen_mi25/Qwen3.5-0.8B-MTP-Q8_0.gguf"
        # 0.8B Q8_0 ~0.8GB — tiny, no MTP (self-draft broken on 0.8B)
        MODEL_CTX=128000
        NGL=33
        MTP_FLAGS=""
        ;;
    llama|Llama|llama3.1|llama-8b)
        MODEL="llama3.1:8b"
        MODEL_LABEL="LLaMA 3.1 8B"
        MODEL_GGUF="$HOME/Code/qwen_mi25/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"
        # 8B Q4_K_M ~4.9GB — native 128K context, zero quirks
        MODEL_CTX=128000
        NGL=33
        MTP_FLAGS=""
        ;;
    lfm|LFM|lfm2.5)
        MODEL="lfm2.5:8b"
        MODEL_LABEL="LFM 2.5 8B A1B (MoE)"
        MODEL_GGUF="$HOME/Code/qwen_mi25/LFM2.5-8B-A1B-Q4_K_M.gguf"
        # Liquid AI LFM 2.5 — 8B MoE with 1B active params
        # Q4_K_M ~5.15GB — fits MI25 16GB with 128K KV cache
        MODEL_CTX=128000
        NGL=33
        MTP_FLAGS=""
        ;;
    qwenmoe|QwenMOE|qwen3moe|qwen2x4b)
        MODEL="qwen3-moe:8b"
        MODEL_LABEL="Qwen3 MoE 2x4B (4B active)"
        MODEL_GGUF="$HOME/Code/qwen_mi25/Qwen3-MOE-2x4B-8B-Jan-Nano-Instruct-II.Q4_K_M.gguf"
        # Qwen3 MoE — 2 experts of 4B, 4B active per token
        # Q4_K_M ~4.13GB — fits MI25 with lots of KV cache headroom
        MODEL_CTX=128000
        NGL=33
        MTP_FLAGS=""
        ;;
    hermes|Hermes|hermes3)
        MODEL="hermes3:8b"
        MODEL_LABEL="Hermes 3 Llama 3.1 8B"
        MODEL_GGUF="$HOME/Code/qwen_mi25/Hermes-3-Llama-3.1-8B.Q4_K_M.gguf"
        # NousResearch Hermes 3 — agentic fine-tune of Llama 3.1 8B
        # Q4_K_M ~4.9GB + speculative draft ~1.3GB + both KV caches
        # = ~12GB total. Use 64K context to leave headroom.
        MODEL_CTX=64000
        NGL=33
        MTP_FLAGS=""
        ;;
    *)
        echo "Unknown model: $MODEL_FLAG (use 0.8b, 4b, 9b, 9bd, lfm, qwenmoe, hermes, or llama)"
        exit 1
        ;;
esac

LLAMA_PORT=11434
HOST="127.0.0.1"
LOG="/tmp/llama_apply_qwen.log"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}" 2>/dev/null)" && pwd)"

# ── Draft model for speculative decoding (0.8B-MTP, same tokenizer) ────
DRAFT_GGUF="$HOME/Code/qwen_mi25/Qwen3.5-0.8B-MTP-Q8_0.gguf"
DRAFT_FLAGS=""
if echo "$MODEL" | grep -q 'qwen' && [ -f "$DRAFT_GGUF" ] && ! echo "$MODEL" | grep -q '9b' && ! echo "$MODEL" | grep -q '0.8b' && ! echo "$MODEL" | grep -q '4b' && ! echo "$MODEL" | grep -q 'moe'; then
    DRAFT_FLAGS="--spec-draft-model $DRAFT_GGUF --spec-draft-n-max 24 --spec-draft-n-min 3 --spec-draft-type-k q8_0 --spec-draft-type-v q8_0"
fi

# ── LLaMA draft model (Llama 3.2 1B Instruct, standard speculative decoding) ──
LLAMA_DRAFT_GGUF="$HOME/Code/qwen_mi25/llama-3.2-1b-instruct-q8_0.gguf"
if echo "$MODEL" | grep -qE 'llama|hermes' && [ -f "$LLAMA_DRAFT_GGUF" ]; then
    DRAFT_FLAGS="--spec-draft-model $LLAMA_DRAFT_GGUF --spec-draft-n-max 8 --spec-draft-n-min 2 --spec-draft-type-k q8_0 --spec-draft-type-v q8_0"
fi

# ── Qwen spec-draft (0.8B MTP, standard autoregressive mode) ─────────────
# Uses the 0.8B as a fast autoregressive draft for up to 20 tokens.
# Same tokenizer as the 9B target, so acceptance rate is high.
# Draft KV cache quantized to Q8 to save VRAM.
QWEN_DRAFT_GGUF="$HOME/Code/qwen_mi25/Qwen3.5-0.8B-MTP-Q8_0.gguf"
if (echo "$MODEL_FLAG" | grep -qiE 'draft|9bd' ) && echo "$MODEL" | grep -qE 'qwen.*5.*9b|qwen.*5.*4b' && [ -f "$QWEN_DRAFT_GGUF" ]; then
    DRAFT_FLAGS="--spec-draft-model $QWEN_DRAFT_GGUF --spec-draft-n-max 30 --spec-draft-n-min 5 --spec-draft-type-k q8_0 --spec-draft-type-v q8_0"
fi

# ── Auto-discover llama-server binary ────────────────────────────────────
LLAMA_SERVER=""
for candidate in \
    "$HOME/.nix-profile/bin/llama-server" \
    "$HOME/Code/qwen_mi25/llama.cpp/build/bin/llama-server" \
    "$SCRIPT_DIR/../qwen_mi25/llama.cpp/build/bin/llama-server" \
    "$SCRIPT_DIR/../qwen_mi25/llama-server-built"; do
    if [ -x "$candidate" ]; then
        LLAMA_SERVER="$candidate"
        break
    fi
done

# ── NixOS library paths for numpy (pandas dependency) ────────────────────
GCC_LIB=$(ls -d /nix/store/*-gcc-*-lib/lib 2>/dev/null | head -1)
ZLIB_LIB=$(ls -d /nix/store/*-zlib-*/lib 2>/dev/null | head -1)
PYTHON_LIB=$(dirname "$(readlink -f "$(which python3)" 2>/dev/null || echo "")")/lib 2>/dev/null || true
if [ -n "$GCC_LIB" ] && [ -n "$ZLIB_LIB" ]; then
    export LD_LIBRARY_PATH="${PYTHON_LIB}:${GCC_LIB}:${ZLIB_LIB}:${LD_LIBRARY_PATH-}"
fi

# ── ROCm env for MI25 ────────────────────────────────────────────────────
export HSA_OVERRIDE_GFX_VERSION=9.0.0
export ROCR_VISIBLE_DEVICES=0
export HIP_VISIBLE_DEVICES=0

# ── Colors ───────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; NC='\033[0m'

info()  { echo -e "${CYAN}[*]${NC} $1"; }
ok()    { echo -e "${GREEN}[✓]${NC} $1"; }
err()   { echo -e "${RED}[✗]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }

# ── Cleanup handler — kill llama-server + Chrome on exit ──────────────
LLAMA_PID=""
CHROME_PID=""
cleanup() {
    local exit_code=$?
    # Kill Chrome if we started it
    if [ -n "$CHROME_PID" ] && kill -0 "$CHROME_PID" 2>/dev/null; then
        info "Stopping Chrome (PID $CHROME_PID)..."
        kill -9 "$CHROME_PID" 2>/dev/null
    fi
    # Kill llama-server if we started it
    if [ -n "$LLAMA_PID" ] && kill -0 "$LLAMA_PID" 2>/dev/null; then
        echo ""
        info "Cleanup — force-stopping llama-server (PID $LLAMA_PID)..."
        kill -9 "$LLAMA_PID" 2>/dev/null
    fi
    # Kill monitor proxy if we started it
    if [ -n "$MONITOR_PID" ] && kill -0 "$MONITOR_PID" 2>/dev/null; then
        kill "$MONITOR_PID" 2>/dev/null
        fuser -k 11435/tcp 2>/dev/null
    fi
    # Remove the auto-restart flag so the server doesn't respawn
    rm -f "/tmp/llama_apply_restart_${LLAMA_PORT}"
    exit $exit_code
}
trap cleanup SIGINT SIGTERM EXIT

# Parse flags
MONITOR_MODE=false
RUN_MODE=false
RUN_STAGES=()
PIPELINE_ARGS=()
for arg in "$@"; do
    if [ "$arg" = "--monitor" ]; then
        MONITOR_MODE=true
    elif [ "$arg" = "run" ]; then
        RUN_MODE=true
    elif [ "$RUN_MODE" = true ]; then
        RUN_STAGES+=("$arg")
    else
        PIPELINE_ARGS+=("$arg")
    fi
done

# Default to all stages if "run" with no specific stages
if [ "$RUN_MODE" = true ] && [ ${#RUN_STAGES[@]} -eq 0 ]; then
    RUN_STAGES=("all")
fi

# ── Check model GGUF file ───────────────────────────────────────────────
if [ ! -f "$MODEL_GGUF" ]; then
    err "Model GGUF not found at $MODEL_GGUF"
    info "Download it: curl -L -o ~/Code/qwen_mi25/Qwen3.5-${MODEL_FLAG^^}-Q4_K_M.gguf \"https://huggingface.co/lmstudio-community/Qwen3.5-${MODEL_FLAG^^}-GGUF/resolve/main/Qwen3.5-${MODEL_FLAG^^}-Q4_K_M.gguf\""
    exit 1
fi

# ── Check llama-server binary ───────────────────────────────────────────
if [ -z "$LLAMA_SERVER" ]; then
    err "llama-server binary not found!"
    info "Build it: cd ~/Code/qwen_mi25/llama.cpp/build && cmake -DGGML_HIP=ON -DAMDGPU_TARGETS=gfx900 .. && make -j\$(nproc) llama-server"
    exit 1
fi
ok "Using: $LLAMA_SERVER"

# ── Start llama-server if not running ────────────────────────────────────
if curl -s "http://${HOST}:${LLAMA_PORT}/v1/models" > /dev/null 2>&1; then
    ok "llama-server already running on port ${LLAMA_PORT}"
else
    info "Starting llama-server with 100% GPU + prompt caching (auto-restart on crash)..."
    fuser -k "${LLAMA_PORT}/tcp" 2>/dev/null || true
    sleep 1
    rm -f "$LOG"

    # Background restart loop — llama-server sometimes crashes on grammar
    # errors (the model generates unexpected tokens).  Auto-restart keeps
    # Hermes alive; it retries the API call on connection failure.
    _llama_restart_flag="/tmp/llama_apply_restart_${LLAMA_PORT}"
    touch "$_llama_restart_flag"
    (
        while [ -f "$_llama_restart_flag" ]; do
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
                -b 8192 \
                --alias "${MODEL}" \
                --timeout 1800 \
                ${MTP_FLAGS:-} ${DRAFT_FLAGS:-} \
                -c ${MODEL_CTX} \
                --host "$HOST" \
                --port "$LLAMA_PORT" \
                >> "$LOG" 2>&1
            _exit_code=$?
            # Only restart if the flag still exists (we weren't trying to stop)
            if [ -f "$_llama_restart_flag" ]; then
                echo "[$(date '+%H:%M:%S')] llama-server exited (code $_exit_code) — restarting in 2s..." >> "$LOG"
                # Check GPU health — if VRAM dropped to near-zero, GPU faulted
                _vram_used=$(rocm-smi --showmeminfo vram 2>/dev/null | grep "VRAM Total Used Memory" | grep -oP '\d+' | tail -1)
                if [ -n "$_vram_used" ] && [ "$_vram_used" -lt 1000000000 ] 2>/dev/null; then
                    echo "[$(date '+%H:%M:%S')] GPU VRAM dropped to ${_vram_used} — resetting GPU..." >> "$LOG"
                    sudo rocm-smi --gpureset -d 0 2>/dev/null || true
                    sleep 8
                fi
                sleep 2
            fi
        done
    ) &
    LLAMA_PID=$!
    for i in $(seq 1 30); do
        if curl -s "http://${HOST}:${LLAMA_PORT}/v1/models" > /dev/null 2>&1; then
            ok "llama-server HTTP started (PID $LLAMA_PID)"
            break
        fi
        if [ $i -eq 30 ]; then
            err "llama-server failed to start. Check $LOG"
            tail -20 "$LOG" 2>/dev/null
            exit 1
        fi
        sleep 1
    done

    # Wait for model to finish loading into VRAM
    info "Waiting for model to load into VRAM (no timeout)..."
    while ! grep -q "model loaded" "$LOG" 2>/dev/null; do
        sleep 1
    done
    ok "Model loaded into VRAM"
    grep -i "hip\\|gpu\\|roc\\|cache\\|memory\\|layer\\|flash\\|listen\\|model loaded" "$LOG" 2>/dev/null | tail -10 || true
fi

# ── Warm up the model ─────────────────────────────────────────────────────
info "Warming model in VRAM..."
WARM_OUTPUT=$(curl -s "http://${HOST}:${LLAMA_PORT}/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"${MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"hello\"}],\"max_tokens\":5,\"stream\":false}" \
    2>/dev/null)

if echo "$WARM_OUTPUT" | python3 -c 'import json,sys;d=json.load(sys.stdin);sys.exit(0 if d.get("choices",[{}])[0].get("message",{}).get("content","") else 1)' 2>/dev/null; then
    ok "Model loaded into VRAM"
else
    err "Model failed to warm up:"
    echo "$WARM_OUTPUT" | head -3
    exit 1
fi

# ── Verify prompt caching ─────────────────────────────────────────────────
info "Verifying prompt caching..."
sleep 1
CACHE_TEST=$(curl -s "http://${HOST}:${LLAMA_PORT}/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"${MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"hello\"}],\"max_tokens\":5,\"stream\":false}" \
    2>/dev/null)

CACHED=$(echo "$CACHE_TEST" | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
    print(d['usage']['prompt_tokens_details']['cached_tokens'])
except:
    print('0')" 2>/dev/null)

if [ "$CACHED" -gt 0 ]; then
    ok "Prompt caching: ${CACHED} tokens cached ✓"
else
    ok "Model ready"
fi

# ── VRAM usage ───────────────────────────────────────────────────────────
info "VRAM usage:"
if command -v rocm-smi &>/dev/null; then
    rocm-smi --showmeminfo vram 2>/dev/null | grep -E "VRAM.*Used|VRAM.*Total" | head -2
fi

# ── API port (may be overridden by monitor proxy) ──────────────────────
API_PORT="${LLAMA_PORT}"

# ── Print banner ─────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Model:   ${MODEL_LABEL} via llama-server (GitHub master)   ${NC}"
echo -e "${GREEN}  GPU:     ${NGL}/32 layers on MI25 (100%)                   ${NC}"
echo -e "${GREEN}  Context: ${MODEL_CTX} tokens (flash attn ON)                 ${NC}"
echo -e "${GREEN}  Prompt   caching: ON                                   ${NC}"
echo -e "${GREEN}  API:     http://${HOST}:${API_PORT}/v1                         ${NC}"
if [ "$MONITOR_MODE" = true ]; then
    echo -e "${GREEN}  Monitor: see ~/Code/ApplyPilot/data/logs/worker-0.log       ${NC}"
fi
echo -e "${GREEN}  Config   NOT modified                                   ${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
echo ""

if [ "$MONITOR_MODE" = true ]; then
    info "Starting model output monitor..."
    rm -f /tmp/llama_model.log
    # Minimal proxy: logs all model responses, forwards everything else
    (python3 -c "
import sys, json
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen

class P(BaseHTTPRequestHandler):
    def do_(self, m):
        cl = self.headers.get('Content-Length')
        body = self.rfile.read(int(cl)) if cl else b''
        h = {k:v for k,v in self.headers.items() if k.lower()!='host'}
        r = Request('http://127.0.0.1:11434'+self.path, data=body or None, headers=h, method=m)
        try:
            resp = urlopen(r)
        except Exception as e:
            self.send_response(502); self.end_headers(); self.wfile.write(str(e).encode()); return
        self.send_response(resp.status)
        for k,v in resp.headers.items(): self.send_header(k,v)
        self.end_headers()
        data = resp.read()
        self.wfile.write(data); self.wfile.flush()
        # Log model response to file
        if b'choices' in data:
            try:
                j = json.loads(data)
                for c in j.get('choices',[]):
                    m = c.get('message') or c.get('delta',{})
                    t = (m.get('reasoning_content','') or '') + (m.get('content','') or '')
                    if t: open('/tmp/llama_model.log','a').write(t+'\n')
            except: pass
    def do_GET(self): self.do_('GET')
    def do_POST(self): self.do_('POST')
    def log_message(self,*a): pass

HTTPServer.allow_reuse_address = True
s = HTTPServer(('127.0.0.1', 11435), P)
s.serve_forever()
" &
    )
    MONITOR_PID=$!
    sleep 1
    if curl -s http://127.0.0.1:11435/v1/models > /dev/null 2>&1; then
        ok "Monitor proxy on :11435 -> :11434"
    else
        warn "Monitor proxy failed — falling back to direct"
        MONITOR_PID=""
    fi
fi

# Use proxy port if monitor is running
if [ -n "$MONITOR_PID" ]; then
    API_PORT=11435
fi

# ── Start Chrome on port 9516 (with auto-restart loop) ──────────────────
CHROME_PORT=9516
if pgrep -f "start-chrome.*${CHROME_PORT}" > /dev/null 2>&1; then
    ok "Chrome already running on port ${CHROME_PORT}"
else
    info "Starting Chrome on port ${CHROME_PORT} (auto-restart loop)..."
    bash "${SCRIPT_DIR}/start-chrome.sh" "${CHROME_PORT}" &
    CHROME_PID=$!
    for i in $(seq 1 15); do
        if curl -s "http://127.0.0.1:${CHROME_PORT}/json/version" > /dev/null 2>&1; then
            ok "Chrome started (PID $CHROME_PID)"
            break
        fi
        if [ $i -eq 15 ]; then
            err "Chrome failed to start"
            exit 1
        fi
        sleep 1
    done
fi

# ── Playwright MCP server is managed by Hermes (config.yaml mcp_servers) ──
# Do NOT start it externally — Hermes spawns it automatically.
# The external start-mcp.sh was removed because duplicate MCP servers
# (one from us, one from Hermes) crash each other over Chrome's CDP.

# ── NixOS library paths for numpy/pandas ─────────────────────────────
GCC_LIB=$(ls -d /nix/store/*-gcc-*-lib/lib 2>/dev/null | head -1)
ZLIB_LIB=$(ls -d /nix/store/*-zlib-*/lib 2>/dev/null | head -1)
PYTHON_LIB=$(dirname "$(readlink -f "$(which python3)" 2>/dev/null || echo "")")/lib 2>/dev/null || true
if [ -n "$GCC_LIB" ] && [ -n "$ZLIB_LIB" ]; then
    export LD_LIBRARY_PATH="${PYTHON_LIB}:${GCC_LIB}:${ZLIB_LIB}"
fi

# ── Server-only mode: skip pipeline, just keep server running ────────
if [ "$SERVER_ONLY" = true ]; then
    info "Server-only mode — waiting for llama-server on port ${API_PORT}..."
    # The restart loop in the background keeps it alive
    wait
    exit 0
fi

if [ "$RUN_MODE" = true ]; then
    # ── Pipeline mode: run stages via applypilot run ────────────────────
    # Discover doesn't need the LLM; score/tailor/cover do.
    export LLM_PROVIDER=local
    export LLM_URL="http://${HOST}:${API_PORT}/v1"
    export LLM_MODEL="${MODEL}"

    cd "$SCRIPT_DIR"
    info "Running: applypilot run ${RUN_STAGES[*]} (local model: ${MODEL_LABEL})"
    .venv/bin/python3 -m applypilot run \
        --provider local --model "${MODEL}" \
        "${PIPELINE_ARGS[@]}" \
        "${RUN_STAGES[@]}"
    EXIT_CODE=$?
else
    # ── Apply mode (original behavior) — launches browser + auto-apply ─
    cd "$SCRIPT_DIR"
    .venv/bin/python3 run_apply.py --provider local --model "${MODEL}" "${PIPELINE_ARGS[@]}"
    EXIT_CODE=$?
fi

# Show final output note
if [ "$MONITOR_MODE" = true ]; then
    echo ""
    echo -e "${CYAN}╔═══ Pipeline finished. Model output saved to worker log ═══╗${NC}"
    echo -e "${CYAN}║  ~/Code/ApplyPilot/data/logs/worker-0.log                ║${NC}"
    echo -e "${CYAN}╚═══════════════════════════════════════════════════════════╝${NC}"
fi

exit $EXIT_CODE
