# ApplyPilot — Automated Job Application Pipeline

## Prerequisites

Chrome must be running on port 9515 for the apply pipeline to connect to:

```bash
bash ~/Code/applypilot/start-chrome.sh
```

## Commands

### Run the apply loop

```bash
cd ~/Code/applypilot && python3 run_apply.py [--provider PROVIDER] [--workers N] [--strategy STRATEGY]
```

Starts the continuous apply loop. Picks the highest-scored unprocessed job from the queue, launches a Hermes AI agent to fill and submit the application, then repeats. Polls every 5s for new jobs. Ctrl+C once to skip current job, twice to stop.

**`--workers N`** — Number of parallel Chrome/Hermes agents (default: 1).
  With more workers, multiple jobs are processed simultaneously. Each worker
  gets its own Chrome instance (ports 9515, 9516, ...) and independent Hermes agent.
  Start with 1, bump to 2-3 if you have the CPU/Chrome overhead.

**`--strategy STRATEGY`** — Only apply to jobs from a specific discovery source:
  - `bigtech` — Netflix, OpenAI, Anthropic, Spotify, Uber, Stripe, Databricks, Snowflake, Microsoft, Google, Amazon, Meta
  - `jobspy` — general Indeed/LinkedIn/Google scraped jobs
  - `workday_api` — corporate Workday career site scrapes
  - Omit for all sources.

  Combined with targeted discovery, this lets you run focused pipelines:
  ```bash
  # Discover only bigtech jobs, then apply only to those
  bash run-ap.sh run discover bigtech
  python3 run_apply.py --strategy bigtech

  # Or using the local model shortcut
  ./apply_local_llama.sh --strategy bigtech
  ```

**`--provider PROVIDER`** — Which model backend to use for the apply agent:

| Provider | Default Model | Backend | Key | Cost |
|----------|-------------|---------|-----|------|
| `opencode` | deepseek-v4-flash-free | Zen (free tier) | `OPENCODE_API_KEY` | free |
| `opencode-zen` | deepseek-v4-flash-free | Zen (free or prepaid) | `OPENCODE_API_KEY` | free / varies |
| `opencode-go` | deepseek-v4-flash | Zen Go subscription | `OPENCODE_API_KEY` | $10/month |
| `gemini` | gemini-2.5-flash | Google Gemini API | `GEMINI_API_KEY` | free (1500 req/day) |
| `openrouter` | meta-llama/llama-3.3-70b-instruct:free | OpenRouter free tier | `OPENROUTER_API_KEY` | free |

`opencode` and `opencode-zen` use `HERMES_MODE=zen` (free models like `deepseek-v4-flash-free`, `big-pickle`). `opencode-go` uses `HERMES_MODE=go` (subscription, strips `-free` suffix from model name). The same `opencode-go-key` file at `~/Code/hermes/opencode-go-key` works for all — Go is a subscription tier within Zen.

Examples:
```bash
# Default — Zen free tier with deepseek-v4-flash-free
python3 run_apply.py --provider opencode

# Zen with a specific free model
python3 run_apply.py --provider opencode --model big-pickle

# Go subscription with a Go model
python3 run_apply.py --provider opencode-go --model deepseek-v4-flash

# Google Gemini — free, fast, 1M context (needs GEMINI_API_KEY)
python3 run_apply.py --provider gemini

# OpenRouter free models — separate quota from your Google key
python3 run_apply.py --provider openrouter

# Run 3 parallel workers
python3 run_apply.py --workers 3
```

### Provider setup

You need at least one API key. The apply loop auto-detects which keys are set:

**OpenCode Zen/Go** — deepseek-v4-flash-free / deepseek-v4-flash
  Zen (free/prepaid) and Go (subscription) share the same API key from
  `~/Code/hermes/opencode-go-key`. Set it in `~/.applypilot/.env`:

  ```
  OPENCODE_API_KEY=sk-...
  ```

  - `--provider opencode` → Zen mode, free models (`deepseek-v4-flash-free`, `big-pickle`, etc.)
  - `--provider opencode-zen` → same as opencode (Zen mode)
  - `--provider opencode-go` → Go subscription mode, paid models
  - **Zen free**: no cost, no balance — `deepseek-v4-flash-free`, `big-pickle`, `mimo-v2.5-free`, `nemotron-3-super-free`
  - **Zen paid**: add $20 balance, pay per token. https://opencode.ai/zen
  - **Go**: $10/month subscription (~31,650 DeepSeek V4 Flash req/5h). https://opencode.ai/go

  Override the model with `--model`:
  ```bash
  python3 run_apply.py --provider opencode --model big-pickle
  python3 run_apply.py --provider opencode --model nemotron-3-super-free
  python3 run_apply.py --provider opencode-go --model kimi-k2.5
  ```

**Google Gemini** — gemini-2.5-flash (free tier)
  ```
  GEMINI_API_KEY=AIza...
  ```
  Free tier: 1500 requests/day, 1M context. Resets at midnight PT.
  If rate-limited, switch model or use OpenRouter's Gemini route instead.

**OpenRouter** — free models
  ```
  OPENROUTER_API_KEY=sk-or-v1-...
  ```
  Free models include meta-llama/llama-3.3-70b-instruct:free, nvidia/nemotron-3-super-120b-a12b:free,
  google/gemini-2.0-flash-exp:free, and others. Separate quota from direct Google API.

**Explicit provider selection** — set `LLM_PROVIDER` to override auto-detect:
  ```bash
  LLM_PROVIDER=gemini python3 run_apply.py --workers 2
  # or using the flag:
  python3 run_apply.py --provider openrouter --workers 1
  ```

### Discover new jobs

```bash
bash ~/Code/applypilot/run-ap.sh run discover
```
Scrapes Indeed, LinkedIn, and 48+ corporate Workday career sites for new job listings. Jobs from Indeed that have a direct URL to the company's own ATS (Workday, Greenhouse, Lever, etc.) get stored with the company name as the apply destination — not "indeed" — so they bypass Indeed's bot blocking.

**Targeted discovery:**
```bash
# Big tech only (Netflix, OpenAI, Anthropic, Spotify, Stripe, etc.)
bash ~/Code/applypilot/run-ap.sh run discover bigtech

# Workday career sites only
bash ~/Code/applypilot/run-ap.sh run discover workday

# General job boards (Indeed, LinkedIn, Google) only
bash ~/Code/applypilot/run-ap.sh run discover jobspy
```
Jobs discovered via targeted discovery are tagged with a `strategy` value. Combined with `run_apply.py --strategy`, you can apply only to jobs from a specific source.

### Score jobs by fit

```bash
bash ~/Code/applypilot/run-ap.sh run score
```
Runs an LLM evaluator against every unscored job that has a full description. Assigns a fit score (1-10) comparing your resume to the job description. The apply pipeline picks highest scores first automatically.

```bash
bash ~/Code/applypilot/run-ap.sh run score --rescore
```
Re-scores all jobs, not just unscored ones. Use if you updated your resume.

### Enrich job descriptions

```bash
bash ~/Code/applypilot/run-ap.sh run enrich
```
Navigates to each job URL with a browser and scrapes the full description and direct apply URL. Required before scoring can work on most jobs.

### Run everything end-to-end

```bash
bash ~/Code/applypilot/run-ap.sh run all
```
Runs discover → enrich → score → tailor → cover → pdf in sequence.

**Targeted pipeline (bigtech only):**
```bash
# Discover + score bigtech jobs only
bash ~/Code/applypilot/run-ap.sh run discover bigtech score

# Then apply only to bigtech jobs
python3 run_apply.py --strategy bigtech
```

### Check status

```bash
bash ~/Code/applypilot/run-ap.sh status
```
Shows a dashboard of total jobs, how many are scored, how many have descriptions, how many applied, and score distribution.

## Monitoring

```bash
# Live worker log
tail -f ~/.applypilot/logs/worker-0.log

# Per-job agent transcripts
ls ~/.applypilot/logs/claude_*.txt
```

## Database quick commands

```bash
# Apply stats
python3 -c "
import sqlite3; c = sqlite3.connect('$HOME/.applypilot/applypilot.db')
for r in c.execute('SELECT apply_status, COUNT(*) FROM jobs GROUP BY apply_status'): print(f'{r[0]}: {r[1]}')
"

# Reset all non-applied jobs for retry
python3 -c "
import sqlite3; c = sqlite3.connect('$HOME/.applypilot/applypilot.db')
c.execute('UPDATE jobs SET apply_status = NULL, apply_error = NULL, apply_attempts = 0, agent_id = NULL WHERE apply_status IS NOT NULL AND apply_status != \"applied\"')
c.commit(); print(f'Reset {c.rowcount} jobs')
"
```

## Configuration files

- **Search queries**: `~/.applypilot/searches.yaml` — add/remove job titles and locations
- **Blocked sites**: `~/.applypilot/applypilot/config/sites.yaml` — sites the apply pipeline skips
- **Resume**: `~/.applypilot/resume.txt` — updated this, regenerate PDF via `run pdf`
