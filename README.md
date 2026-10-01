# Jarvis 2.0

A voice-first AI assistant for your Mac. Press a hotkey, say what you need, and Jarvis gets to work: writing and running code, drafting documents, controlling apps and searching the web. Tutor mode adds an on-screen pointer that walks you through things step by step while you learn.

Runs on macOS 12 or newer (Intel or Apple Silicon) and uses Claude as its brain. GitHub stores the code and runs the automatic tests; Jarvis itself runs on your Mac.

> **Status: phase 3 of 10 (files and terminal).** Jarvis can now create, read and edit files and run terminal commands, so it writes and runs code for you. Risky actions need your OK. Next up: controlling Mac apps.

## Roadmap

| Phase | What it adds | How you check it |
|---|---|---|
| **1. Foundation** ✅ | Installer, settings, `--doctor` health check with a live Claude test, unit tests, GitHub CI | `./run.sh --doctor` |
| **2. Brain** ✅ | Claude agent loop, streaming replies, web search, memory, chat in Terminal | `./run.sh --cli` |
| **3. Files and terminal** ✅ | Read, write and edit files; run commands with safety checks and backups | "Make a Python script that prints the first 20 primes and run it" |
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
- An Anthropic API key from [platform.claude.com](https://platform.claude.com) (Settings, then API keys). Everything installs without one, but Jarvis needs it to think.

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

## Chat with Jarvis

```bash
./run.sh --cli
```

Type a message and the answer streams in as Jarvis writes it. Some things to try:

- "Make a Python script that prints the first 20 prime numbers, and run it."
- "Create a simple to-do list web page in a folder called todo and open it."
- "Read ~/Downloads/assignment.pdf and list what I need to do."
- "What's new in the latest version of Python?" (searches the web)
- "Remember that I'm learning React and prefer short answers."
- "Explain recursion with a simple example, then quiz me."

Commands inside the chat: `/new` starts a fresh conversation, `/memory` shows what Jarvis remembers about you, `/forget <text>` removes a memory, `/usage` shows the tokens used this session, and `/quit` leaves. Press Ctrl+C while Jarvis is answering to stop it.

For a single question without opening the chat: `./run.sh --say "What's the capital of Japan?"`

Without an API key, Jarvis explains how to add one instead of starting.

## Files, commands and safety

Jarvis creates projects and documents in **~/JarvisWorkspace** (one folder per project) unless you name another place. It can also read files anywhere you point it to: code, text, PDFs, Word documents and images.

- **Risky commands need your OK.** Deleting files, `sudo`, `git push`, changing system settings, piping a download into the shell and similar commands are shown to you first with an `Allow? [y/N]` prompt.
- **System and credential folders are off limits**, such as /System, /usr, /etc, ~/.ssh and your Keychains.
- **Existing files outside the workspace** are changed only after you agree, and the old version is kept in `data/backups`.
- **Servers and long jobs** run in the background, with their output in `data/logs/background`.

To be asked before every command, set `safety.confirm_shell: always` in `config.local.yaml`. (`never` turns the prompts off, which isn't recommended.)

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

Jarvis uses your own API key, so you pay Anthropic per request. Prompt caching is on by default to cut the cost of repeated context, and web searches cost $10 per 1,000. Set a monthly spend limit in the Claude Console so there are no surprises. For a cheaper brain, set `llm.model` to `claude-haiku-4-5-20251001`. Type `/usage` in the chat to see how many tokens you've used.

## Updating to the next phase

Each phase arrives as a patch kit. From inside your Jarvis folder, unzip it and run its script:

```bash
cd ~/Projects/jarvis          # your Jarvis folder
unzip -oq ~/Downloads/jarvis-phase-04.zip
bash patches/phase-04/apply.sh --push
```

The script applies the changes (with `git apply`, or by copying files if you edited something, keeping your version in `patches/backups`), tidies leftovers from earlier updates such as a `jarvis 2` folder or old zip files, reinstalls packages only when the requirements changed, and runs the tests. `--push` then commits and pushes, `--commit` only commits, and with neither it just applies and tests. Your `.env`, `config.local.yaml`, `.venv` and `data/` are never touched.

If your browser already unzipped the download, run the script from inside your Jarvis folder instead: `bash ~/Downloads/patches/phase-04/apply.sh --push`.

## Project layout

```
jarvis/
├── setup.sh               installer
├── run.sh                 launcher (./run.sh --cli, --say, --doctor)
├── config.yaml            every setting, with explanations
├── requirements.txt       Python packages
├── jarvis/
│   ├── __main__.py        command-line entry point
│   ├── brain.py           the agent loop: streams Claude's replies and runs its tools
│   ├── prompts.py         Jarvis's instructions, built from the tools it has
│   ├── memory.py          long-term memory about you (data/memory.json)
│   ├── cli.py             terminal chat
│   ├── tools/             what Jarvis can do
│   │   ├── file_tools.py      read, write, edit and list files
│   │   ├── shell_tools.py     run terminal commands, asking first for risky ones
│   │   └── memory_tools.py    remember facts about you
│   ├── ui/                how Jarvis shows things (the terminal now; the menu bar later)
│   ├── config.py          loads settings: defaults, then config.yaml, then config.local.yaml
│   ├── claude_client.py   Claude API client and plain-English error messages
│   └── doctor.py          the ./run.sh --doctor health check
├── tests/                 unit tests, run by GitHub Actions on every push
├── patches/               update kits and their backups (not in git)
└── data/                  logs, backups, speech models and memory (not in git)
```

## Development

Run the tests on your Mac (no API key or internet needed; the Claude API is simulated):

```bash
.venv/bin/python -m unittest discover -s tests -v
```

GitHub Actions runs the same tests on every push; see the **Actions** tab of your repository. Logs are written to `data/logs/jarvis.log`.

## Troubleshooting

- **"Jarvis needs Python 3.11 or 3.12"**: install Python 3.12 as above, then run `./setup.sh` again.
- **The doctor says the key was rejected**: re-copy the key into `.env` with no quotes or spaces around it.
- **"Your API credit balance is too low"**: add credits under Billing in the Claude Console.
- **The model wasn't found**: check `llm.model` in `config.local.yaml` for typos.
- **"macOS blocked access" when reading a file**: allow your terminal app under System Settings, Privacy & Security, Files and Folders (or Full Disk Access).
- **SSL or certificate errors with the python.org installer**: open Applications, then Python 3.12, and run "Install Certificates.command".
