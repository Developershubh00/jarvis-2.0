"""Command-line entry point: ./run.sh [--cli [--voice] | --listen | --say TEXT | --doctor | --version]."""
from __future__ import annotations

import argparse
import sys

from . import PHASE, __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jarvis", description="Jarvis: a voice-first AI assistant for your Mac.")
    parser.add_argument("--cli", action="store_true", help="chat with Jarvis in the terminal")
    parser.add_argument("--voice", action="store_true", help="talk instead of typing: press Enter on an empty line")
    parser.add_argument("--listen", action="store_true", help="test the microphone and speech recognition (no API key needed)")
    parser.add_argument("--say", metavar="TEXT", help="send one message, print the reply and exit")
    parser.add_argument("--doctor", action="store_true", help="check your setup and API key, and explain fixes")
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    parser.add_argument("--config", metavar="PATH", help="use a different config.yaml")
    parser.add_argument("--model", help="use a different Claude model for this run")
    parser.add_argument("--debug", action="store_true", help="verbose logging in the terminal")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.version:
        print(f"Jarvis {__version__} (phase {PHASE} of 10)")
        return 0

    from .config import ensure_dirs, load_config, setup_logging

    overrides = {"llm": {"model": args.model}} if args.model else None
    cfg = load_config(args.config, overrides=overrides)
    ensure_dirs(cfg)
    setup_logging(cfg, debug=args.debug, console=args.debug)

    if args.doctor:
        from .doctor import run_doctor

        return run_doctor(cfg)
    if args.listen:
        from .cli import run_listen

        return run_listen(cfg)
    if args.say:
        from .cli import run_once

        return run_once(cfg, args.say)
    if args.cli or args.voice:
        from .cli import run_cli

        return run_cli(cfg, voice=args.voice)

    print(f"Jarvis {__version__} (phase {PHASE} of 10).")
    print("  Chat in the terminal:  ./run.sh --cli")
    print("  Talk to it:            ./run.sh --cli --voice")
    print("  Test the microphone:   ./run.sh --listen")
    print("  Check your setup:      ./run.sh --doctor")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
