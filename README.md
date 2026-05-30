# ApplyPilot

AI-powered job application pipeline. Scrapes job boards, scores listings by fit, and auto-submits applications via an autonomous AI agent.

## What's different in this fork

This fork replaces the original Claude Code-based apply agent with **Hermes Agent** running on **opencode.ai** (deepseek-v4-flash), making it drastically more affordable — approximately free versus $3-5 per Claude Code session.

### Key changes

- **Hermes Agent** — replaces Claude Code CLI for form-filling and submission
- **Smart site routing** — jobs scraped from Indeed/LinkedIn are auto-rerouted to the company's own ATS (Workday, Greenhouse, Lever, etc.), bypassing Cloudflare blocks on job boards
- **Fast failure** — the agent bails in 1-2 calls on Cloudflare blocks, expired listings, or login walls (was 50-150 calls burning the full timeout)
- **Process group cleanup** — Hermes child processes (Playwright MCP, terminal sessions) are properly reaped on timeout, no more stuck `in_progress` jobs
- **Rescore flag** — `--rescore` to re-evaluate all jobs after a resume update
- **NixOS support** — `run-ap.sh` wrapper auto-detects store paths for numpy/pandas compatibility

## Quick start

```bash
# 1. Install
pip install applypilot

# 2. Set up profile
applypilot init

# 3. Start Chrome on debug port
bash start-chrome.sh

# 4. Find jobs
bash run-ap.sh run discover

# 5. Score them by fit
bash run-ap.sh run score

# 6. Apply — picks highest scores first
python3 run_apply.py
```

## Commands

| Command | What it does |
|---------|-------------|
| `run-ap.sh run discover` | Scrape job boards (Indeed, LinkedIn, Workday corporate sites) |
| `run-ap.sh run enrich` | Scrape full descriptions and apply URLs from each listing |
| `run-ap.sh run score` | LLM-evaluate each job vs your resume, assign 1-10 fit score |
| `run-ap.sh run score --rescore` | Re-score all jobs (use after resume update) |
| `run-ap.sh run tailor` | Generate per-job customized resumes (optional) |
| `run-ap.sh run cover` | Generate per-job cover letters (optional) |
| `run-ap.sh run all` | Run discover → enrich → score → tailor → cover → pdf |
| `run-ap.sh status` | Show pipeline stats |
| `python3 run_apply.py` | Start auto-apply loop (continuous, highest scores first) |

## How site routing works

When JobSpy finds a job on Indeed, it also returns a `job_url_direct` — the company's actual career page URL. This fork derives the apply destination from that URL:

| Scraped from | Direct URL | Stored as | Can apply? |
|-------------|-----------|-----------|-----------|
| Indeed | `nvidia.wd5.myworkdayjobs.com/...` | `Nvidia` | Yes — on Workday |
| Indeed | `jobs.lever.co/company/...` | `Company` | Yes — on Lever |
| Indeed | `www.indeed.com/job/...` | `indeed` | No — blocked (Cloudflare) |
| LinkedIn | *(none)* | `linkedin` | No — blocked (needs account) |

Blocked jobs are skipped automatically. Only jobs at real company ATS pages get submitted.

## Requirements

- Python 3.11+
- Chrome/Chromium (for Playwright MCP)
- Hermes Agent (install separately)
- opencode.ai API key (or any OpenAI-compatible endpoint)

## Environment

Set in `~/.applypilot/.env`:

```
LLM_URL=https://opencode.ai/zen/go/v1
LLM_API_KEY=your_key
LLM_MODEL=deepseek-v4-flash
```
