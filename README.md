# QA LLM Lab — Telegram Chatbot QA & Security Testing (Promptfoo + Garak + n8n)

## Problem Statement (Motivation)

A typical QA cycle for a Telegram-based LLM chatbot looks like this:

- prepare a list of test questions (functional, edge-case, adversarial)
- manually send each message to the bot in Telegram
- wait for the bot's response
- compare the response with the expected result
- separately run security/red-teaming checks
- log the outcome by hand

This does not scale: it's slow, error-prone, and gets worse as the number of
test cases and the surface area (prompt injection, jailbreaks, policy
bypass) grows.

This project automates the whole cycle using **Promptfoo** (functional +
LLM-as-a-judge evaluation), **NVIDIA Garak** (automated security/red-team
probing), and a custom **Telethon-based Telegram bridge** that exposes any
Telegram bot as a plain HTTP REST target — so both tools can talk to a bot
that otherwise only speaks Telegram.

---

## Architecture

```
Promptfoo / Garak  --HTTP POST-->  Telegram Bridge (aiohttp)  --Telethon user session-->  Bot under test
                    <--JSON reply--                            <--Telegram message-------
```

- **Telegram Bridge** (`bridge/telegram_bridge.py`) — an aiohttp server that
  wraps a Telethon *user* session (not a bot API token). It exposes:
  - `POST /prompt` — send a message to the bot under test and wait for its
    reply, returned as `{"output": "..."}`. This is what Promptfoo/Garak
    call.
  - `POST /send_message` — send a message to an arbitrary recipient
    (legacy/manual use).
  - `POST /telegram_response` / `GET /next_message` — legacy queue-based
    interface kept for backward compatibility.
- **Promptfoo** — deterministic assertions + LLM-as-a-judge grading, driven
  by `promptfooconfig.yaml` and the test suites in `tests/`.
- **Garak** — automated red-teaming (jailbreaks, prompt injection,
  harassment probes, etc.), driven by `garak/garak_config.yaml`, talking to
  the bridge as a generic REST generator.
- The bridge is deliberately generator-agnostic — the bot under test is
  whatever `TG_BOT_USERNAME` in `.env` points at, so the same bridge is
  reused across different bots/projects.

---

## Repository Layout

```
.
│   .env                     # root env (Promptfoo/Garak-facing config, if any)
│   promptfooconfig.yaml     # Promptfoo entry point
│   README.md
│
├───bridge
│       .env                 # Telegram credentials (NOT committed)
│       asp_tg.session       # Telethon user session (NOT committed)
│       env.example          # template for bridge/.env
│       telegram_bridge.py   # HTTP <-> Telegram bridge
│
├───docs
│       policybot-spec.md    # SUT spec / requirements
│
├───garak
│       garak_config.yaml    # Garak REST generator + probe selection
│
├───graders
│       graders.js           # custom Promptfoo grader logic
│
├───prompts
│       policybot_system.txt
│
├───results
│       promptfoo-results.html
│       promptfoo-results.json
│
└───tests
        policybot-tests.yaml
        policybot-tests-fast.yaml
```

---

## Prerequisites

| Tool   | Required version | Check with          |
|--------|-------------------|----------------------|
| Python | 3.10 – 3.12        | `python --version`   |
| pip    | latest             | `pip --version`      |
| Node.js| 18+                | `node -v`             |
| npm    | latest             | `npm -v`               |
| Git    | any recent         | `git --version`        |

You'll also need:
- A **Telegram API ID / API hash** (from https://my.telegram.org) for the
  Telethon user session used by the bridge.
- The username of the bot you want to test.

---

## Installation

### 1. Clone the repo

```powershell
git clone <your-repo-url>.git
cd QA-LLM-Lab
```

### 2. Create and activate a Python virtual environment

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

### 3. Install Garak

```powershell
pip install -U garak
```

### 4. Install the bridge's Python dependencies

```powershell
pip install telethon aiohttp python-dotenv
```

### 5. Install Promptfoo

```powershell
npm install -g promptfoo
```
(or skip the global install and just use `npx promptfoo ...` everywhere below)

### 6. Configure environment variables

```powershell
copy bridge\env.example bridge\.env
```

Fill in `bridge\.env`:

```
TG_API_ID=your_api_id
TG_API_HASH=your_api_hash
TG_BOT_USERNAME=your_test_bot_username
WEBHOOK_URL=http://localhost:5001/telegram_response
```

### 7. First-time Telegram login

The bridge uses a Telethon **user** session (`asp_tg.session`), not a bot
token — this lets it read the target bot's replies like a real user would.
The first run is interactive:

```powershell
cd bridge
python telegram_bridge.py
```

Enter your phone number and the login code Telegram sends you. This
creates `bridge\asp_tg.session` — after that, startup is non-interactive.
Leave this console running; open a second one for the next steps.

### 8. Verify versions

```powershell
python --version
pip show garak
node -v
npm -v
npm ls -g promptfoo
git --version
```

---

## Running the Suite

### Console 1 — start the bridge (keep this running)

```powershell
.venv\Scripts\Activate.ps1
cd bridge
python telegram_bridge.py
```

Wait for:
```
Telegram client started.
Target bot:
  username = your_test_bot_username
  chat_id  = ...
HTTP server started:
  POST http://localhost:5000/send_message
  POST http://localhost:5000/prompt
```

### Console 2 — run the tests

#### Functional / LLM-as-a-judge testing (Promptfoo)

```powershell
.venv\Scripts\Activate.ps1
cd C:\Projects\QA-LLM-Lab

npx promptfoo eval -c promptfooconfig.yaml -o results\promptfoo-results.json
```

View results as an interactive dashboard in the browser:

```powershell
npx promptfoo view
```

Or open the static report directly:

```powershell
start results\promptfoo-results.html
```

#### Security / red-team testing (Garak)

```powershell
cd garak
garak --config garak_config.yaml
```

Garak writes three files per run into `results\`, prefixed with
`garak-results`:
- `garak-results.<run_id>.report.jsonl` — full machine-readable evidence
- `garak-results.<run_id>.hitlog.jsonl` — only the detected hits
- `garak-results.<run_id>.report.html` — human-readable visual summary

Open the HTML report:

```powershell
start ..\results\garak-results.<run_id>.report.html
```

---

## Known Limitations

- **Telegram's 4096-character message limit** — some Garak probes (e.g.
  `promptinject.HijackLongPrompt`) generate prompts longer than a single
  Telegram message can hold. The bridge truncates outgoing prompts to 4096
  characters before sending, so these probes run without crashing but are
  evaluated against a truncated payload. This is a hard constraint of
  testing through a real chat client rather than a raw API, and is
  documented here rather than silently masked.
- The bridge uses a Telethon **user session**, so response capture relies
  on the tester's Telegram account actually being in a private chat with
  the bot under test.

---

## Tech Stack

```
Python (Telethon, aiohttp)
NVIDIA Garak
Promptfoo
Node.js / npm
n8n (workflow orchestration for the broader QA pipeline)
Google Sheets (test case storage, in the n8n-based variant of this pipeline)
```
