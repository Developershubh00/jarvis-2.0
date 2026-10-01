# Jarvis

A voice-first AI assistant for your Mac. Press a hotkey, say what you need, and Jarvis gets to work: writing and running code, drafting documents, controlling apps and searching the web. Tutor mode adds an on-screen pointer that walks you through things step by step while you learn.

Runs on macOS 12 or newer (Intel or Apple Silicon) and uses Claude as its brain. GitHub stores the code and runs the automatic tests; Jarvis itself runs on your Mac.

> **Status: phase 1 of 10 (foundation).** This phase sets up the installer, the settings and a health check. You can chat with Jarvis from phase 2.

## Roadmap

| Phase | What it adds | How you check it |
|---|---|---|
| **1. Foundation** ✅ | Installer, settings, `--doctor` health check with a live Claude test, unit tests, GitHub CI | `./run.sh --doctor` |
| 2. Brain | Claude agent loop, streaming replies, web search, memory, chat in Terminal | `./run.sh --cli` |
| 3. Files and terminal | Read, write and edit files; run commands with safety checks and backups | "Make a Python script that prints the first 20 primes and run it" |
| 4. Mac control | Open apps and websites, AppleScript, clipboard, notifications, typing into apps | "Open GitHub in Safari" |
| 5. Ears | Microphone with voice detection, offline Whisper speech-to-text | `./run.sh --cli --voice` |
| 6. Voice | Spoken replies, the assistant engine, Esc to cancel, follow-up questions | Talk to it in Terminal |
| 7. Menu-bar app | Runs in the background with global hotkeys (⌃⌥C talk, ⌃⌥J type, Esc cancel) | Press ⌃⌥C in any app |
| 8. Floating panel | Glass panel with an animated orb and live text | Watch it listen, think and speak |
| 9. Tutor mode | ⌃⌥T: Jarvis sees your screen and guides you with an animated pointer | "Explain what's on my screen" |
| 10. Polish | "Hey Jarvis" wake word, hold-to-talk, launch at login, full permission checks | `./run.sh --doctor` all green |

## What you need

- A Mac with macOS 12 (Monterey) or newer.
- **Python 3.12** (3.11 also works). Python 3.13 and newer aren't supported yet because a speech library has no Intel-Mac build for them.
- An Anthropic API key from [platform.claude.com](https://platform.claude.com) (Settings, then API keys).

## Install

1. Install Python 3.12 with [Homebrew](https://brew.sh) (`brew install python@3.12`) or the macOS installer from [python.org](https://www.python.org/downloads/macos/).
2. In the Jarvis folder, run the installer. It creates a private `.venv` folder and installs everything (a few minutes the first time):
   ```bash
   ./setup.sh
   ```
3. Add your API key: run `open -e .env`, paste your key after `ANTHROPIC_API_KEY=`, and save.
4. Check your setup:
   ```bash
   ./run.sh --doctor
   ```
   Every check should show ✓, and the last section should say **Claude replied: Jarvis online.**

If you see "permission denied", run `chmod +x setup.sh run.sh` once.

## Keep your API key private

Your key lives only in `.env`, which `.gitignore` keeps out of git, so it never reaches GitHub. Never paste it into `config.yaml` or any other file you commit. If a key ever leaks, delete it in the Claude Console and create a new one.

## Settings

`config.yaml` lists every setting with a comment explaining it. To change something, copy just that setting into **`config.local.yaml`** (created by `./setup.sh`). Your local file overrides `config.yaml`, git ignores it, and updates never overwrite it.

```yaml
# config.local.yaml
user_name: "Tony"
llm:
  model: claude-haiku-4-5-20251001
```

Restart Jarvis after changing settings. `./run.sh --doctor` reports typos in setting names.

## Costs

Jarvis uses your own API key, so you pay Anthropic per request. Prompt caching is on by default to cut the cost of repeated context, and web searches cost $10 per 1,000. Set a monthly spend limit in the Claude Console so there are no surprises. For a cheaper brain, set `llm.model` to `claude-haiku-4-5-20251001`.

## Updating to the next phase

Each phase arrives as a zip of the whole project. Unzip it over your copy, then commit and push:

```bash
cd ~/Projects
unzip -o ~/Downloads/jarvis-phase-02.zip
cd jarvis
git add -A && git commit -m "Phase 2: brain" && git push
```

Your `.env`, `config.local.yaml`, `.venv` and `data/` are never in the zip, so they stay untouched. Run `./setup.sh` again only when a phase says its requirements changed.

## Project layout

```
jarvis/
├── setup.sh               installer
├── run.sh                 launcher (./run.sh --doctor, and more in later phases)
├── config.yaml            every setting, with explanations
├── requirements.txt       Python packages
├── jarvis/
│   ├── __main__.py        command-line entry point
│   ├── config.py          loads settings: defaults, then config.yaml, then config.local.yaml
│   ├── claude_client.py   Claude API client and plain-English error messages
│   └── doctor.py          the ./run.sh --doctor health check
├── tests/                 unit tests, run by GitHub Actions on every push
└── data/                  logs, speech models and memory (created at runtime, not in git)
```

## Development

Run the tests on your Mac:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

GitHub Actions runs the same tests on every push; see the **Actions** tab of your repository. Logs are written to `data/logs/jarvis.log`.

## Troubleshooting

- **"Jarvis needs Python 3.11 or 3.12"**: install Python 3.12 as above, then run `./setup.sh` again.
- **The doctor says the key was rejected**: re-copy the key into `.env` with no quotes or spaces around it.
- **"Your API credit balance is too low"**: add credits under Billing in the Claude Console.
- **The model wasn't found**: check `llm.model` in `config.local.yaml` for typos.
- **SSL or certificate errors with the python.org installer**: open Applications, then Python 3.12, and run "Install Certificates.command".
# jarvis-2.0
