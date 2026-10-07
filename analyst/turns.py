"""Track one question/answer turn against an agent session's transcript.

The harness exposes a session as one growing transcript plus a status, so a
turn is "the agent messages that appeared after we sent our message". The
tricky part is follow-ups: right after sending one, the run can still report
the previous turn's COMPLETED for a moment, so a terminal status only ends a
follow-up turn after we have seen the run go active again (or the new
messages have stopped arriving for a while).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from analyst.backend import AgentMessage, AgentStep, RunSnapshot
from analyst.protocol import VizError, VizSegment, parse_message

SETTLE_SECONDS = 8.0
NO_REPLY_SECONDS = 120.0
TURN_TIMEOUT_SECONDS = 20 * 60.0


@dataclass
class PendingTurn:
    session_id: str
    question: str
    baseline: int
    sent_at: float
    first_turn: bool
    saw_active: bool = False
    message_count: int = 0
    last_growth_at: float = 0.0
    status: str = "QUEUED"
    # Highest transcript step index before this turn, for APIs that expose steps.
    step_baseline: int = -1
    # "question", or "report" for the reader's report on an answer (analyst/feedback.py).
    kind: str = "question"

    def __post_init__(self) -> None:
        self.last_growth_at = self.last_growth_at or self.sent_at


@dataclass
class TurnOutcome:
    status: str
    answer: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    error: str | None = None


def turn_messages(turn: PendingTurn, snapshot: RunSnapshot) -> list[AgentMessage]:
    return [m for m in snapshot.messages[turn.baseline :] if m.from_agent]


def turn_steps(turn: PendingTurn, snapshot: RunSnapshot) -> list[AgentStep]:
    """This turn's tool calls and commentary, without the question itself."""
    return [s for s in snapshot.steps if s.index > turn.step_baseline and s.role != "user"]


def observe(turn: PendingTurn, snapshot: RunSnapshot, now: float) -> bool:
    """Fold one poll into the turn. Returns True when the turn is over."""
    turn.status = snapshot.status
    count = len(turn_messages(turn, snapshot))
    if count > turn.message_count:
        turn.message_count = count
        turn.last_growth_at = now

    if not snapshot.terminal:
        turn.saw_active = True
        return now - turn.sent_at >= TURN_TIMEOUT_SECONDS

    if turn.first_turn or turn.saw_active:
        return True
    # Terminal, but possibly still the previous turn's status.
    if count:
        return now - turn.last_growth_at >= SETTLE_SECONDS
    return now - turn.sent_at >= NO_REPLY_SECONDS


def outcome(turn: PendingTurn, snapshot: RunSnapshot, now: float) -> TurnOutcome:
    """Split the turn's messages into the answer and the agent's working notes.

    The answer is the last message, plus any earlier message that carries a
    visual (in case the agent split its reply). Everything else is a note.
    """
    contents = [m.content for m in turn_messages(turn, snapshot) if m.content.strip()]
    status = snapshot.status

    if not snapshot.terminal:
        minutes = int((now - turn.sent_at) // 60)
        return TurnOutcome(
            status="TIMEOUT",
            notes=contents,
            error=(
                f"DinoAI is still working after {minutes} minutes. The run continues in "
                f"Paradime (session `{turn.session_id}`); ask again later to pick up its answer."
            ),
        )
    if status != "COMPLETED":
        reason = {
            "FAILED": "The agent run failed.",
            "EXPIRED": "The agent never started (the run expired in the queue).",
            "STOPPED": "The agent run was stopped.",
        }.get(status, f"The agent run ended with status {status}.")
        return TurnOutcome(status=status, notes=contents, error=reason)
    if not contents:
        return TurnOutcome(status=status, error="The agent finished without replying.")

    *earlier, last = contents
    answer = [c for c in earlier if _has_visual(c)] + [last]
    notes = [c for c in earlier if not _has_visual(c)]
    return TurnOutcome(status=status, answer=answer, notes=notes)


def _has_visual(content: str) -> bool:
    return any(isinstance(s, (VizSegment, VizError)) for s in parse_message(content))
