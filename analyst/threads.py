"""Conversations on disk, so a reload, a new tab or a server restart keeps the thread.

A conversation is keyed by its DinoAI agent session id, which the app also
puts in the page URL (?thread=<id>). Anyone with the link can open the thread,
and the History page lists every thread on this server, so this store is for
a team that may see each other's answers.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from analyst.protocol import plain_text
from analyst.turns import PendingTurn

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TITLE_CHARS = 80
UNTITLED = "Untitled conversation"


def earlier_turns(history: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """(question, answer text) pairs, to brief a new session on the conversation so far."""
    pairs: list[tuple[str, str]] = []
    question = None
    for item in history:
        if item["role"] == "user":
            question = item["content"]
        elif item["role"] == "assistant" and question and item.get("answer") and _is_answer(item):
            pairs.append((question, "\n\n".join(plain_text(c) for c in item["answer"])))
            question = None
    return pairs


def conversation_title(history: list[dict[str, Any]]) -> str:
    """The first question, on one line."""
    question = next((item["content"] for item in history if item["role"] == "user"), "")
    text = " ".join(str(question).split())
    if not text:
        return UNTITLED
    return text if len(text) <= _TITLE_CHARS else text[:_TITLE_CHARS].rsplit(" ", 1)[0] + " …"


@dataclass(frozen=True)
class ThreadSummary:
    session_id: str
    title: str
    created_at: float
    updated_at: float
    questions: int
    answered: int
    failed: int
    pending: bool
    thumbs_up: int
    thumbs_down: int
    owner: str | None
    reports: int = 0  # problems that readers reported with an answer
    issues: tuple[str, ...] = ()  # the Linear issues filed for them


class ThreadStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def save(
        self,
        session_id: str,
        history: list[dict[str, Any]],
        pending: PendingTurn | None,
        owner: str | None = None,
    ) -> None:
        path = self._path(session_id)
        if path is None:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        previous = self._read(path) or {}
        now = time.time()
        payload = {
            "session_id": session_id,
            "title": conversation_title(history),
            "created_at": previous.get("created_at", now),
            "updated_at": now,
            # The first person to ask keeps the thread, even if others follow up on it.
            "owner": previous.get("owner") or owner,
            "history": history,
            "pending": asdict(pending) if pending else None,
        }
        partial = path.with_suffix(".partial")
        partial.write_text(json.dumps(payload))
        partial.replace(path)  # atomic: a reader never sees half a file

    def load(self, session_id: str) -> tuple[list[dict[str, Any]], PendingTurn | None] | None:
        path = self._path(session_id)
        payload = self._read(path) if path else None
        if payload is None:
            return None
        try:
            pending = payload.get("pending")
            return list(payload["history"]), PendingTurn(**pending) if pending else None
        except (KeyError, TypeError):
            return None

    def list(self, limit: int = 500) -> list[ThreadSummary]:
        """Saved threads, most recently active first."""
        if not self.root.exists():
            return []
        paths = sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        summaries = []
        for path in paths[:limit]:
            payload = self._read(path)
            if payload is not None:
                summaries.append(_summary(path, payload))
        return sorted(summaries, key=lambda s: s.updated_at, reverse=True)

    def _path(self, session_id: str) -> Path | None:
        """The file for a session id, or None for an id that is not safe as a file name."""
        if not isinstance(session_id, str) or not _SAFE_ID.match(session_id):
            return None
        return self.root / f"{session_id}.json"

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        try:
            payload = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None


def _summary(path: Path, payload: dict[str, Any]) -> ThreadSummary:
    history = payload.get("history") or []
    answers = [item for item in history if item.get("role") == "assistant" and _is_answer(item)]
    reports = [item for item in history if item.get("role") == "report"]
    ratings = [item.get("rating") for item in answers]
    # Files saved before titles and times were recorded fall back to the file's own.
    modified = path.stat().st_mtime
    return ThreadSummary(
        session_id=str(payload.get("session_id") or path.stem),
        title=payload.get("title") or conversation_title(history),
        created_at=float(payload.get("created_at") or modified),
        updated_at=float(payload.get("updated_at") or modified),
        questions=sum(1 for item in history if item.get("role") == "user"),
        answered=sum(1 for item in answers if item.get("answer") and not item.get("error")),
        failed=sum(1 for item in answers if item.get("error")),
        pending=bool(payload.get("pending")),
        thumbs_up=ratings.count(1),
        thumbs_down=ratings.count(0),
        owner=payload.get("owner"),
        reports=len(reports),
        issues=tuple(r["issue"]["key"] for r in reports if r.get("issue")),
    )


def _is_answer(item: dict[str, Any]) -> bool:
    """An answer to a question, not the reply to a reader's report."""
    return item.get("kind", "answer") == "answer"
