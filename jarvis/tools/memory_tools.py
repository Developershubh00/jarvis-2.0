"""Memory tool: let Claude remember facts about the user across conversations."""
from __future__ import annotations

from . import ToolContext, ToolError, require, short


def memory(ctx: ToolContext, a: dict) -> str:
    store = ctx.memory
    if store is None:
        raise ToolError("Memory isn't available.")
    action = str(a.get("action") or "list").lower()
    if action == "add":
        try:
            return store.add(str(require(a, "text")))
        except ValueError as e:
            raise ToolError(str(e)) from e
    if action == "remove":
        removed = store.remove(str(require(a, "text")))
        return ("Forgot: " + "; ".join(removed)) if removed else "Nothing in memory matched that."
    if action == "list":
        facts = store.list()
        return "\n".join(f"{i}. {f}" for i, f in enumerate(facts, 1)) if facts else "Nothing saved yet."
    raise ToolError("action must be add, remove or list.")


def register(reg) -> None:
    reg.add(
        "memory",
        """Long-term memory that survives restarts. add: save a short durable fact about the user or their
        preferences (their name, course, editor, projects, how they like answers) when they share one or ask
        you to remember something. remove: forget facts matching text (or a number from list). list: show
        everything saved. Never store passwords or other secrets.""",
        {
            "action": {"type": "string", "enum": ["add", "remove", "list"]},
            "text": {"type": "string", "description": "The fact to add, or text/number to remove."},
        },
        required=("action",), func=memory,
        status=lambda a: {"add": "Remembering: " + short(a.get("text", ""), 60),
                          "remove": "Forgetting: " + short(a.get("text", ""), 60)}.get(a.get("action"), "Checking memory"),
        activity="Updating memory…",
    )
