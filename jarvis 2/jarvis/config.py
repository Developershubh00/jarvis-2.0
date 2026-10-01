"""Configuration: built-in defaults <- config.yaml <- config.local.yaml, plus .env for secrets.

config.yaml documents every setting and ships with Jarvis. Personal changes belong in
config.local.yaml: it overrides config.yaml, git ignores it, and updates never overwrite it.
"""
from __future__ import annotations

import copy
import logging
import logging.handlers
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
LOCAL_CONFIG_PATH = PROJECT_ROOT / "config.local.yaml"
ENV_PATH = PROJECT_ROOT / ".env"

DEFAULTS: dict[str, Any] = {
    "user_name": "sir",
    "assistant_name": "Jarvis",
    "llm": {
        "model": "claude-sonnet-5-5",
        "max_tokens": 16000,
        "max_steps": 30,
        "history_turns": 8,
        "web_search": True,
        "web_search_max_uses": 5,
        "prompt_caching": True,
    },
    "hotkeys": {
        "talk": "ctrl+alt+c",
        "tutor": "ctrl+alt+t",
        "type": "ctrl+alt+j",
        "push_to_talk": "right_option",
    },
    "voice": {
        "stt_model": "small.en",
        "language": "en",
        "beam_size": 5,
        "vocabulary": (
            "Jarvis, Python, JavaScript, TypeScript, React, Node, VS Code, GitHub, terminal, "
            "Xcode, macOS, Safari, Chrome, Notion, assignment"
        ),
        "vad_threshold": 0.012,
        "silence_seconds": 1.2,
        "start_timeout_seconds": 7,
        "max_record_seconds": 45,
        "input_device": None,
        "tts": True,
        "tts_voice": "Daniel",
        "tts_rate": 190,
        "sounds": True,
        "follow_up_listen": True,
    },
    "wake_word": {"enabled": False, "model": "hey_jarvis", "threshold": 0.5},
    "safety": {
        "confirm_shell": "risky",
        "confirm_writes_outside_workspace": True,
        "shell_timeout_seconds": 120,
    },
    "paths": {"workspace": "~/JarvisWorkspace"},
    "ui": {"hud_position": "top-right", "hud_autohide_seconds": 15, "pointer_hide_seconds": 25},
}

_NUMBERS = {
    ("llm", "max_tokens"): (int, 256),
    ("llm", "max_steps"): (int, 1),
    ("llm", "history_turns"): (int, 1),
    ("llm", "web_search_max_uses"): (int, 1),
    ("voice", "beam_size"): (int, 1),
    ("voice", "vad_threshold"): (float, 0.0005),
    ("voice", "silence_seconds"): (float, 0.3),
    ("voice", "start_timeout_seconds"): (float, 1.0),
    ("voice", "max_record_seconds"): (float, 3.0),
    ("voice", "tts_rate"): (int, 80),
    ("wake_word", "threshold"): (float, 0.05),
    ("safety", "shell_timeout_seconds"): (float, 1.0),
    ("ui", "hud_autohide_seconds"): (float, 0.0),
    ("ui", "pointer_hide_seconds"): (float, 0.0),
}


def deep_merge(base: dict, override: dict, warnings: list[str] | None = None, prefix: str = "") -> dict:
    """Return a new dict: ``base`` recursively updated with ``override``.

    A group of settings (like ``llm:``) is never replaced by a single value or by an empty entry,
    so a half-edited file can't break Jarvis.
    """
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        path = f"{prefix}{key}"
        current = out.get(key)
        if isinstance(current, dict):
            if value is None:  # e.g. "llm:" with everything under it commented out
                continue
            if not isinstance(value, dict):
                if warnings is not None:
                    warnings.append(f"'{path}' should be a group of settings, not a single value; ignoring it.")
                continue
            out[key] = deep_merge(current, value, warnings, path + ".")
        else:
            out[key] = copy.deepcopy(value)
    return out


def unknown_keys(defaults: dict, user: dict, prefix: str = "") -> list[str]:
    problems = []
    for key, value in (user or {}).items():
        path = f"{prefix}{key}"
        if key not in defaults:
            problems.append(path)
        elif isinstance(value, dict) and isinstance(defaults[key], dict):
            problems.extend(unknown_keys(defaults[key], value, path + "."))
    return problems


def to_namespace(value: Any) -> Any:
    if isinstance(value, dict):
        return SimpleNamespace(**{str(k): to_namespace(v) for k, v in value.items()})
    return value


def _read_yaml(path: Path, warnings: list[str]) -> dict:
    if not path.exists():
        return {}
    try:
        import yaml

        with open(path, "r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f)
    except Exception as e:
        warnings.append(f"Couldn't read {path.name}: {e}. Using the other settings.")
        return {}
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        warnings.append(f"{path.name} should contain settings (key: value); ignoring it.")
        return {}
    return loaded


def _validate(merged: dict, warnings: list[str]) -> None:
    mode = str(merged["safety"].get("confirm_shell", "risky")).strip().lower()
    if mode not in ("always", "risky", "never"):
        warnings.append(f"safety.confirm_shell must be always, risky or never (got '{mode}'); using 'risky'.")
        mode = "risky"
    merged["safety"]["confirm_shell"] = mode
    for (section, key), (kind, minimum) in _NUMBERS.items():
        default = DEFAULTS[section][key]
        try:
            merged[section][key] = max(minimum, kind(merged[section][key]))
        except (TypeError, ValueError):
            warnings.append(f"{section}.{key} should be a number; using {default}.")
            merged[section][key] = default
    for key in ("talk", "tutor", "type", "push_to_talk"):
        value = merged["hotkeys"].get(key)
        merged["hotkeys"][key] = "" if value in (None, False) else str(value).strip()


def load_config(path: str | os.PathLike | None = None, overrides: dict | None = None,
                data_dir: str | os.PathLike | None = None,
                local_path: str | os.PathLike | None = None) -> SimpleNamespace:
    """Load settings. Never raises for a bad file; problems are collected in ``cfg.warnings``.

    Order (later wins): built-in defaults, config.yaml, config.local.yaml, the JARVIS_MODEL
    environment variable, then ``overrides`` (command-line flags).
    """
    warnings: list[str] = []
    try:
        from dotenv import load_dotenv

        load_dotenv(ENV_PATH, override=False)
    except Exception:  # python-dotenv missing is not fatal
        pass

    cfg_path = Path(path) if path else CONFIG_PATH
    local = Path(local_path) if local_path else LOCAL_CONFIG_PATH
    merged = copy.deepcopy(DEFAULTS)
    loaded_files: list[Path] = []
    for file in (cfg_path, local) if local != cfg_path else (cfg_path,):
        data = _read_yaml(file, warnings)
        if file.exists():
            loaded_files.append(file)
        for key in unknown_keys(DEFAULTS, data):
            warnings.append(f"Unknown setting '{key}' in {file.name} (typo?). It is ignored.")
        merged = deep_merge(merged, data, warnings)

    env_model = os.environ.get("JARVIS_MODEL", "").strip()
    if env_model:
        merged["llm"]["model"] = env_model
    if overrides:
        merged = deep_merge(merged, overrides, warnings)
    _validate(merged, warnings)

    cfg = to_namespace(merged)
    base = Path(data_dir) if data_dir else DATA_DIR
    workspace = Path(os.path.expanduser(str(merged["paths"]["workspace"]))).resolve()
    cfg.paths = SimpleNamespace(
        workspace=workspace,
        data=base,
        logs=base / "logs",
        models=base / "models",
        backups=base / "backups",
        memory_file=base / "memory.json",
        config_file=cfg_path,
        local_config_file=local,
        project_root=PROJECT_ROOT,
    )
    cfg.workspace = workspace
    cfg.loaded_files = loaded_files
    cfg.raw = merged
    cfg.warnings = warnings
    return cfg


def ensure_dirs(cfg: SimpleNamespace) -> None:
    for folder in (cfg.paths.data, cfg.paths.logs, cfg.paths.models, cfg.paths.backups):
        Path(folder).mkdir(parents=True, exist_ok=True)
    try:
        Path(cfg.paths.workspace).mkdir(parents=True, exist_ok=True)
    except OSError:
        logging.getLogger(__name__).warning("Couldn't create workspace %s", cfg.paths.workspace)


def setup_logging(cfg: SimpleNamespace, debug: bool = False, console: bool = False) -> None:
    logs = Path(cfg.paths.logs)
    logs.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    file_handler = logging.handlers.RotatingFileHandler(
        logs / "jarvis.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    stream = logging.StreamHandler(sys.stderr)
    stream.setLevel(logging.DEBUG if debug else (logging.INFO if console else logging.WARNING))
    stream.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(stream)
    for noisy in ("httpx", "httpx2", "httpcore", "httpcore2", "anthropic", "faster_whisper", "urllib3", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
