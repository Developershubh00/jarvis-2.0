# Jarvis 2.0

A voice-first AI assistant for your Mac. Press a hotkey, say what you need, and Jarvis gets to work: writing and running code, drafting documents, controlling apps and searching the web. Tutor mode adds an on-screen pointer that walks you through things step by step while you learn.

Runs on macOS 12 or newer (Intel or Apple Silicon) and uses Claude as its brain. GitHub stores the code and runs the automatic tests; Jarvis itself runs on your Mac.

> **Status: phase 9 of 10 (tutor mode).** Press ⌃⌥T and ask about anything on your screen: Jarvis looks, explains, and an animated pointer shows you exactly where to click, step by step. Next up: the finishing touches (wake word, start at login, full permission checks).

## Roadmap

| Phase | What it adds | How you check it |
|---|---|---|
| **1. Foundation** ✅ | Installer, settings, `--doctor` health check with a live Claude test, unit tests, GitHub CI | `./run.sh --doctor` |
| **2. Brain** ✅ | Claude agent loop, streaming replies, web search, memory, chat in Terminal | `./run.sh --cli` |
| **3. Files and terminal** ✅ | Read, write and edit files; run commands with safety checks and backups | "Make a Python script that prints the first 20 primes and run it" |
| **4. Mac control** ✅ | Open apps and websites, AppleScript, clipboard, notifications, typing into apps | "Open GitHub in Safari" |
| **5. Ears** ✅ | Microphone with voice detection, offline Whisper speech-to-text | `./run.sh --cli --voice` |
| **6. Voice** ✅ | Spoken replies, the assistant engine, Esc to cancel, follow-up questions | Talk to it in Terminal |
| **7. Menu-bar app** ✅ | Runs in the background with global hotkeys (⌃⌥C talk, ⌃⌥J type, Esc cancel) | Press ⌃⌥C in any app |
| **8. Floating panel** ✅ | Glass panel with an animated orb and live text | Watch it listen, think and speak |
| **9. Tutor mode** ✅ | ⌃⌥T: Jarvis sees your screen and guides you with an animated pointer | "Explain what's on my screen" |
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

## Use Jarvis from any app

```bash
./run.sh
```

A Jarvis icon appears in the menu bar. Then, in any app:

| Do this | To |
|---|---|
| ⌃⌥C (Control+Option+C) | start talking; press it again to finish early |
| Hold the right ⌥ Option key | talk while you hold it, release to send |
| ⌃⌥T | tutor mode: ask about anything on your screen |
| ⌃⌥J | type a request in a small box instead |
| Esc | stop listening or speaking (press it twice to stop a task that's running) |

While Jarvis works, a glass panel appears in the top-right corner. Its orb shows the state at a glance: an ice-blue halo that swells with your voice while listening, circling periwinkle arcs while thinking, amber ripples while speaking, and coral when something needs your attention. Next to it you see what Jarvis heard, each step it takes, and its reply as it's written. The panel hides itself a few seconds after Jarvis finishes, but stays while your mouse is over it; drag it anywhere, or bring it back with Show panel in the menu.

The menu-bar icon shows the same states (waveform, microphone, sparkles, speaker), and the menu has Talk, Type a request, Stop, Show panel, Copy last reply, New conversation, Open workspace folder, Edit settings, Open log and Quit.

Panel settings: `ui.hud_position` (top-right, top-left, bottom-right or bottom-left) and `ui.hud_autohide_seconds`.

The first time, macOS asks for **Accessibility** permission for your terminal app (System Settings, Privacy & Security, Accessibility). Turn it on and the hotkeys start working within a few seconds; no restart needed. Keep the Terminal window open while Jarvis runs, because closing it quits Jarvis (starting at login arrives in phase 10).

Why not just "c"? A single letter would fire every time you type it, so hotkeys need modifiers. You can change them in `config.local.yaml`, for example `talk: "f5"` under `hotkeys:` (on a MacBook, press fn+F5, or turn on "Use F1, F2, etc. keys as standard function keys" in System Settings, Keyboard).

Without an API key you can still try the hotkeys: Jarvis listens, shows what it heard in the menu, and then tells you it needs a key.

## Tutor mode: learn with a pointer

Press ⌃⌥T (or choose Tutor mode in the menu) and ask about whatever is on your screen. Jarvis takes a screenshot, explains, and a glowing amber pointer glides to each thing it mentions, with a short label that it reads out. Some things to try:

- "How do I commit my changes in VS Code?"
- "What does this error mean, and how do I fix it?"
- "Walk me through submitting this form."
- "Where do I change the font size in this app?"

For homework and assignments Jarvis teaches first: it explains the idea and gives hints, and gives a full solution when you ask for one, with every step explained. The pointer disappears after `ui.pointer_hide_seconds` (25 by default) or when you press Esc. Tutor mode works in the terminal chat too: `/tutor <your question>`.

The first time, macOS asks for **Screen Recording** permission for your terminal app. Turn it on in System Settings, Privacy & Security, Screen Recording, then quit and reopen the terminal (macOS only applies this permission after a restart). Without it, Jarvis only sees your desktop background.

## Chat with Jarvis in the terminal

```bash
./run.sh --cli
```

Type a message and the answer streams in as Jarvis writes it. Some things to try:

- "Make a Python script that prints the first 20 prime numbers, and run it."
- "Create a simple to-do list web page in a folder called todo and open it."
- "Read ~/Downloads/assignment.pdf and list what I need to do."
- "Open GitHub in Safari."
- "Set the volume to 30% and turn on dark mode."
- "Add a reminder to submit my assignment tomorrow at 9 am."
- "Open TextEdit and type a short poem about the rain."
- "What's new in the latest version of Python?" (searches the web)
- "Remember that I'm learning React and prefer short answers."
- "Explain recursion with a simple example, then quiz me."

Commands inside the chat: `/new` starts a fresh conversation, `/memory` shows what Jarvis remembers about you, `/forget <text>` removes a memory, `/usage` shows the tokens used this session, and `/quit` leaves. Press Ctrl+C while Jarvis is answering to stop it.

For a single question without opening the chat: `./run.sh --say "What's the capital of Japan?"`

Without an API key, Jarvis explains how to add one instead of starting.

## Talk to Jarvis

First, test your microphone. This needs no API key, and your voice never leaves your Mac:

```bash
./run.sh --listen
```

Press Enter, say something, then pause; your words appear on screen. The first time, Jarvis downloads its speech model (about 480 MB, once), and macOS asks to let your terminal use the microphone: click OK.

Then talk to Jarvis with `./run.sh --cli --voice`. Press Enter on an empty line, speak, and pause when you're done; you can still type messages too. Jarvis speaks its replies, and when it asks you a question it listens for your answer straight away. Ctrl+C stops it while it's listening, thinking or speaking.

Quick phrases work without calling Claude: "never mind" cancels, "new conversation" (or "start over") clears the conversation, "repeat that" says the last answer again, and "thanks" gets a polite reply.

Tips:

- Recording stops about a second after you stop talking. If it cuts you off, raise `voice.silence_seconds`; if it keeps listening in a noisy room, raise `voice.vad_threshold` (try 0.02).
- `voice.stt_model: base.en` is faster but less accurate; `medium.en` is more accurate but slower.
- Add names and jargon it should recognise to `voice.vocabulary`.
- To use another microphone, set `voice.input_device` to its name (`./run.sh --doctor` shows the current one).
- Change the speaking voice with `voice.tts_voice` and its speed with `voice.tts_rate`. List your voices with `say -v '?'`; nicer ones can be downloaded in System Settings, Accessibility, Spoken Content, System Voice, Manage Voices. `voice.tts: false` keeps Jarvis quiet.
- `voice.follow_up_listen: false` turns off listening for answers automatically.

## Files, commands and safety

Jarvis creates projects and documents in **~/JarvisWorkspace** (one folder per project) unless you name another place. It can also read files anywhere you point it to: code, text, PDFs, Word documents and images.

- **Risky commands need your OK.** Deleting files, `sudo`, `git push`, changing system settings, piping a download into the shell and similar commands are shown to you first with an `Allow? [y/N]` prompt.
- **System and credential folders are off limits**, such as /System, /usr, /etc, ~/.ssh and your Keychains.
- **Existing files outside the workspace** are changed only after you agree, and the old version is kept in `data/backups`.
- **Servers and long jobs** run in the background, with their output in `data/logs/background`.
- **AppleScript that deletes, sends, empties the Trash or runs shell commands** is shown to you first, like risky commands.

To be asked before every command, set `safety.confirm_shell: always` in `config.local.yaml`. (`never` turns the prompts off, which isn't recommended.)

## Mac permissions

macOS asks before Jarvis can control things, and each permission belongs to the app you run Jarvis from (Terminal or iTerm):

- **Automation**: the first time Jarvis controls an app such as Music, Notes or System Events, macOS asks "Terminal wants access to control…". Click OK. You can change this later in System Settings, Privacy & Security, Automation.
- **Microphone**: macOS asks the first time Jarvis listens. If it only hears silence, turn on your terminal app in System Settings, Privacy & Security, Microphone, then quit and reopen the terminal.
- **Accessibility**: needed to type into other apps and read selected text. Turn on your terminal app in System Settings, Privacy & Security, Accessibility, then restart Jarvis.
- **Files and Folders**: macOS may ask the first time Jarvis reads your Desktop, Documents or Downloads.

`./run.sh --doctor` shows whether Accessibility is on. In terminal mode Jarvis never types into its own Terminal window; it brings the right app forward first.

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
│   │   ├── mac_tools.py       open apps and sites, AppleScript, clipboard, typing, notifications
│   │   ├── screen_tools.py    screenshots and the tutor pointer
│   │   └── memory_tools.py    remember facts about you
│   ├── ui/                how Jarvis shows things
│   │   ├── app.py             the menu-bar app (icon, menu, notifications)
│   │   ├── hotkeys.py         global hotkeys and hold-to-talk
│   │   ├── hud.py             the floating glass panel (AppKit drawing)
│   │   ├── hud_logic.py       the panel's orb shapes, text layout and placement (plain Python)
│   │   ├── menu_panel.py      status in the menu bar, and the fallback if the panel can't start
│   │   ├── overlay.py         the tutor pointer (AppKit drawing)
│   │   └── overlay_logic.py   the pointer's shape, label placement and glide (plain Python)
│   ├── mac.py             macOS helpers: AppleScript, clipboard, keystrokes, permissions
│   ├── assistant.py       the assistant engine: listening, thinking, speaking, quick phrases, cancelling
│   ├── voice/             the microphone (recorder.py), speech recognition (stt.py) and speech (tts.py)
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
- **The hotkeys don't do anything**: turn on your terminal app in System Settings, Privacy & Security, Accessibility. Also make sure Terminal's Secure Keyboard Entry (in the Terminal menu) is off, because it hides keystrokes from every other app.
- **Tutor mode only sees the desktop background**: allow Screen Recording for your terminal app, then quit and reopen the terminal.
- **"macOS blocked access" when reading a file**: allow your terminal app under System Settings, Privacy & Security, Files and Folders (or Full Disk Access).
- **"The microphone is giving pure silence"**: allow Microphone access for your terminal app (see Mac permissions), then quit and reopen the terminal.
- **The speech model download fails**: it needs internet the first time only; check your connection and run `./run.sh --listen` again.
- **SSL or certificate errors with the python.org installer**: open Applications, then Python 3.12, and run "Install Certificates.command".
