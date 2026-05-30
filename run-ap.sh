#!/usr/bin/env bash
# ApplyPilot wrapper — sets up NixOS library paths for numpy then runs
# Usage: bash run-ap.sh run discover
#        bash run-ap.sh run all
#        bash run-ap.sh status

# NixOS needs these for numpy's C extensions (pandas dependency)
# Auto-detect current store paths to avoid stale hardcoded paths
GCC_LIB=$(ls -d /nix/store/*-gcc-*-lib/lib 2>/dev/null | head -1)
ZLIB_LIB=$(ls -d /nix/store/*-zlib-*/lib 2>/dev/null | head -1)
PYTHON_LIB=$(dirname "$(readlink -f "$(which python3)" 2>/dev/null || echo "")")/lib 2>/dev/null || true

if [ -n "$GCC_LIB" ] && [ -n "$ZLIB_LIB" ]; then
    export LD_LIBRARY_PATH="${PYTHON_LIB}:${GCC_LIB}:${ZLIB_LIB}"
fi

cd "$(dirname "$0")"
exec .venv/bin/python3 -m applypilot "$@"
