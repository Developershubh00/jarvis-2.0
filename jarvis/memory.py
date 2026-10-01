"""Long-term memory: short facts about the user, saved in data/memory.json."""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

log = logging.getLogger(__name__)


class Memory:
    MAX_FACTS = 200
    MAX_FACT_CHARS = 300

    def __init__(self, path: str | os.PathLike) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._facts: list[str] = self._load()

    def _load(self) -> list[str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except Exception as e:
            log.warning("Couldn't read %s (%s); starting with empty memory", self.path, e)
            return []
        facts = data.get("facts", []) if isinstance(data, dict) else data
        return [str(f) for f in facts if str(f).strip()][: self.MAX_FACTS]

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"facts": self._facts}, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def add(self, fact: str) -> str:
        fact = " ".join(str(fact).split())[: self.MAX_FACT_CHARS]
        if not fact:
            raise ValueError("empty fact")
        with self._lock:
            if any(f.lower() == fact.lower() for f in self._facts):
                return f"Already remembered: {fact}"
            self._facts.append(fact)
            if len(self._facts) > self.MAX_FACTS:
                self._facts = self._facts[-self.MAX_FACTS:]
            self._save()
        return f"Remembered: {fact}"

    def remove(self, query: str) -> list[str]:
        """Remove by 1-based number or by (case-insensitive) text match."""
        query = str(query).strip()
        with self._lock:
            if query.isdigit():
                index = int(query) - 1
                removed = [self._facts.pop(index)] if 0 <= index < len(self._facts) else []
            else:
                q = query.lower()
                removed = [f for f in self._facts if q in f.lower()]
                self._facts = [f for f in self._facts if q not in f.lower()]
            if removed:
                self._save()
        return removed

    def list(self) -> list[str]:
        with self._lock:
            return list(self._facts)

    def as_prompt(self) -> str:
        facts = self.list()
        if not facts:
            return ""
        lines = "\n".join(f"- {f}" for f in facts)
        return ("Things you remember about the user from earlier conversations "
                "(use them when relevant; don't recite them):\n" + lines)
