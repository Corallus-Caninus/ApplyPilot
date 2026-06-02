#!/usr/bin/env bash
# ApplyPilot setup script — installs everything needed for a fresh machine.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/Corallus-Caninus/ApplyPilot/main/setup.sh | bash
#
# Or locally:
#   bash setup.sh
#
# After running, edit ~/.applypilot/.env with your API keys, then:
#   applypilot init   # configure profile, resume, searches

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

log()  { echo -e "${GREEN}[✓]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[✗]${NC} $1"; }
info() { echo -e "${CYAN}[i]${NC} $1"; }

# --- Detect OS ---
OS="$(uname -s)"
ARCH="$(uname -m)"

echo ""
echo -e "${CYAN}========================================${NC}"
echo -e "${CYAN}  ApplyPilot Setup${NC}"
echo -e "${CYAN}========================================${NC}"
echo ""

# --- 1. System dependencies ---
install_system_deps() {
  info "Installing system dependencies..."

  if [ "$OS" = "Linux" ]; then
    if [ -f /etc/nixos/configuration.nix ] || command -v nixos-version &>/dev/null; then
      log "NixOS detected — Chromium should already be available"
      warn "If Chromium is missing: nix-shell -p chromium"
    elif command -v apt-get &>/dev/null; then
      sudo apt-get update -qq
      sudo apt-get install -y -qq chromium-browser python3 python3-venv python3-pip curl git 2>/dev/null ||
      sudo apt-get install -y -qq chromium python3 python3-venv python3-pip curl git
      log "System packages installed"
    elif command -v dnf &>/dev/null; then
      sudo dnf install -y chromium python3 python3-pip curl git
      log "System packages installed"
    elif command -v pacman &>/dev/null; then
      sudo pacman -S --noconfirm chromium python python-pip curl git
      log "System packages installed"
    else
      warn "Unknown package manager. Install: chromium, python3, python3-pip, git, curl"
    fi
  elif [ "$OS" = "Darwin" ]; then
    if ! command -v brew &>/dev/null; then
      warn "Homebrew not found. Install from https://brew.sh first."
    fi
    brew install --cask chromium 2>/dev/null || brew install chromium
    log "System packages installed"
  else
    warn "Unsupported OS: $OS. Install dependencies manually."
  fi
}

# --- 2. Clone repo ---
REPO_DIR="$HOME/Code/ApplyPilot"
if [ ! -d "$REPO_DIR" ]; then
  info "Cloning repo..."
  mkdir -p "$HOME/Code"
  git clone https://github.com/Corallus-Caninus/ApplyPilot.git "$REPO_DIR"
  log "Cloned to $REPO_DIR"
else
  log "Repo already exists at $REPO_DIR"
  info "Pulling latest..."
  cd "$REPO_DIR" && git pull
fi

cd "$REPO_DIR"

# --- 3. Python venv ---
if [ ! -d ".venv" ]; then
  info "Creating Python virtual environment..."
  python3 -m venv .venv
  log "Virtual environment created"
fi

source .venv/bin/activate

info "Installing applypilot and dependencies..."
pip install --quiet --upgrade pip
pip install --quiet -e .
log "applypilot installed"

# --- 4. JobSpy + optional extras ---
info "Installing job board scraper (JobSpy)..."
pip install --quiet --no-deps python-jobspy
pip install --quiet pydantic tls-client requests markdownify regex bs4 lxml
log "JobSpy installed"

# --- 5. Create ~/.applypilot directory ---
mkdir -p "$HOME/.applypilot"
mkdir -p "$HOME/.applypilot/logs"
mkdir -p "$HOME/.applypilot/apply-workers"
cd "$REPO_DIR"

# --- 6. Config files (copy defaults, never overwrite existing) ---
copy_example() {
  local src="$1" dest="$2"
  if [ ! -f "$dest" ]; then
    cp "$src" "$dest"
    log "Created $dest"
  else
    info "$dest already exists, keeping your config"
  fi
}

copy_example "profile.example.json" "$HOME/.applypilot/profile.json"
copy_example ".env.example" "$HOME/.applypilot/.env"
copy_example "src/applypilot/config/searches.example.yaml" "$HOME/.applypilot/searches.yaml"

# --- 7. Make scripts executable ---
chmod +x run_apply.py start-chrome.sh run-ap.sh cdp_upload.py applypilot.sh credentials_manager.py 2>/dev/null || true

# --- 8. Install Playwright (for smart-extract enrichment) ---
if command -v npx &>/dev/null; then
  info "Playwright MCP available via npx (will install on first use)"
fi

# --- 9. Create a convenience symlink ---
if [ ! -L "$HOME/Code/applypilot" ]; then
  ln -sf "$REPO_DIR" "$HOME/Code/applypilot" 2>/dev/null || true
fi

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  ApplyPilot setup complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo -e "Next steps:"
echo ""
echo -e "  1. ${YELLOW}Edit your API keys:${NC}"
echo -e "     nano $HOME/.applypilot/.env"
echo -e "     (set LLM_API_KEY, GEMINI_API_KEY, CAPSOLVER_API_KEY, etc.)"
echo ""
echo -e "  2. ${YELLOW}Configure your profile:${NC}"
echo -e "     nano $HOME/.applypilot/profile.json"
echo -e "     (name, email, phone, password for ATS accounts)"
echo ""
echo -e "  3. ${YELLOW}Add your resume:${NC}"
echo -e "     cp /path/to/your/resume.txt $HOME/.applypilot/resume.txt"
echo ""
echo -e "  4. ${YELLOW}Review search config:${NC}"
echo -e "     nano $HOME/.applypilot/searches.yaml"
echo ""
echo -e "  5. ${YELLOW}Initialize the database:${NC}"
echo -e "     cd $REPO_DIR"
echo -e "     source .venv/bin/activate"
echo -e "     applypilot init"
echo ""
echo -e "  6. ${YELLOW}Discover jobs:${NC}"
echo -e "     bash run-ap.sh run discover"
echo ""
echo -e "  7. ${YELLOW}Score jobs:${NC}"
echo -e "     bash run-ap.sh run score --rescore"
echo ""
echo -e "  8. ${YELLOW}Start Chrome + apply:${NC}"
echo -e "     bash start-chrome.sh"
echo -e "     python3 run_apply.py"
echo ""
echo -e "  Or all at once:"
echo -e "     bash start-chrome.sh &"
echo -e "     bash run-ap.sh run discover && bash run-ap.sh run score --rescore"
echo -e "     python3 run_apply.py"
echo ""
