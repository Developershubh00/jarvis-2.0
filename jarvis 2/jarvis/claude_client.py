"""Shared Claude API helpers: finding the API key, creating the client, plain-English errors."""
from __future__ import annotations

import os

CONSOLE_URL = "https://platform.claude.com"


class MissingKeyError(RuntimeError):
    """ANTHROPIC_API_KEY isn't set."""


def get_api_key() -> str:
    return (os.environ.get("ANTHROPIC_API_KEY") or "").strip().strip('"').strip("'")


def mask_key(key: str) -> str:
    """Show just enough of a key to recognise it, never the whole thing."""
    key = key or ""
    if len(key) <= 16:
        return "(too short to be a real key)"
    return f"{key[:10]}…{key[-4:]}"


def make_client(timeout: float = 300.0, max_retries: int = 2):
    key = get_api_key()
    if not key:
        raise MissingKeyError(
            "No API key found. Open the .env file in the Jarvis folder and paste your key after "
            f"ANTHROPIC_API_KEY= (create one at {CONSOLE_URL})."
        )
    import anthropic

    return anthropic.Anthropic(api_key=key, timeout=timeout, max_retries=max_retries)


def _short(text: str, limit: int = 220) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def explain_api_error(exc: BaseException, model: str = "") -> str:
    """Turn an exception from the Anthropic SDK into one plain-English sentence."""
    if isinstance(exc, MissingKeyError):
        return str(exc)
    try:
        import anthropic
    except ImportError:
        return f"{type(exc).__name__}: {_short(str(exc))}"
    message = str(getattr(exc, "message", "") or exc)
    lower = message.lower()
    if "credit balance" in lower:
        return f"Your API credit balance is too low. Add credits under Billing in the Claude Console ({CONSOLE_URL})."
    if isinstance(exc, anthropic.AuthenticationError):
        return "The API key was rejected. Re-copy it into .env after ANTHROPIC_API_KEY= with no quotes or spaces."
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "This API key isn't allowed to do that. Check its workspace and permissions in the Claude Console."
    if isinstance(exc, anthropic.NotFoundError):
        name = f"'{model}' " if model else ""
        return f"The model {name}wasn't found. Check llm.model in config.local.yaml for typos."
    if isinstance(exc, anthropic.RateLimitError):
        return "Rate limited by the API. Wait a few seconds and try again."
    if isinstance(exc, anthropic.APITimeoutError):
        return "The request to Claude timed out. Check your internet connection and try again."
    if isinstance(exc, anthropic.APIConnectionError):
        return "Can't reach the Claude API. Check your internet connection, VPN or proxy."
    if isinstance(exc, anthropic.APIStatusError):
        status = getattr(exc, "status_code", 0) or 0
        if status == 529 or "overloaded" in lower:
            return "Claude is overloaded right now. Try again in a minute."
        if status >= 500:
            return f"The Claude API had a server error ({status}). Try again shortly."
        return f"The Claude API returned an error ({status}): {_short(message)}"
    return f"{type(exc).__name__}: {_short(message)}"
