"""Agent backends.

ParadimeBackend drives a DinoAI programmable agent through the Paradime API
(the "harness"). ReplayBackend replays recorded DinoAI sessions so the UI can
be demoed and developed without credentials. Both expose the same three calls
the harness has: start a session, send a follow-up, read the run.
"""

from __future__ import annotations

import itertools
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, Sequence

from analyst.protocol import FOLLOW_UP_REMINDER
from analyst.sanitize import defang_local_urls, is_firewall_page

TERMINAL_STATUSES = frozenset({"COMPLETED", "FAILED", "EXPIRED", "STOPPED"})
MIN_REPLAY_STEP_SECONDS = 2.0
_NON_AGENT_ROLES = frozenset({"user", "human", "system", "tool"})


class BackendError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentMessage:
    ts: float
    role: str
    content: str

    @property
    def from_agent(self) -> bool:
        return self.role.lower() not in _NON_AGENT_ROLES


@dataclass(frozen=True)
class AgentStep:
    """One transcript step (a tool call, or text), from APIs that expose `steps`."""

    index: int
    role: str
    tool_name: str | None = None
    tool_input: str | None = None
    content: str | None = None


@dataclass(frozen=True)
class StartupStep:
    label: str
    done: bool


@dataclass(frozen=True)
class RunSnapshot:
    status: str
    messages: Sequence[AgentMessage]
    child_session_ids: Sequence[str] = ()
    # Empty when the API predates `steps`, or the run has none yet.
    steps: Sequence[AgentStep] = ()
    startup: Sequence[StartupStep] = ()

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


class AgentBackend(Protocol):
    label: str

    def start(self, message: str) -> tuple[str, str | None]:
        """Start a new agent session with an opening message.

        Returns the session id, and a warning when Paradime adjusted the request
        (for example, an unknown model family that fell back to the default).
        """
        ...

    def send(self, session_id: str, message: str) -> None:
        """Post a follow-up message to an existing session."""
        ...

    def get(
        self, session_id: str, *, steps_after: int | None = None, with_steps: bool = True
    ) -> RunSnapshot:
        """Read the session's status, its messages and (where the API has them) its steps.

        `steps_after` keeps only the steps after that index: a turn needs only its
        own. `with_steps=False` skips them, for reads that need messages only.
        """
        ...


class ParadimeBackend:
    """DinoAI programmable agents over the paradime-io SDK."""

    # The SDK does not pass modelFamily yet, so a run with a model family
    # goes through the SDK's GraphQL transport directly.
    _TRIGGER_WITH_MODEL = """
        mutation TriggerDinoaiAgentRun($agent: String, $message: String, $modelFamily: String) {
            triggerDinoaiAgentRun(agent: $agent, message: $message, modelFamily: $modelFamily) {
                ok
                agentSessionId
                status
                warning
            }
        }
    """

    # Each step's text is capped: the app shows one line for each step.
    STEP_MAX_CHARS = 400

    def __init__(
        self,
        *,
        api_endpoint: str,
        api_secret: str,
        workspace_uid: str | None = None,
        api_key: str | None = None,
        agent: str | None = None,
        model_family: str | None = None,
    ) -> None:
        from paradime import Paradime

        self._client = Paradime(
            api_endpoint=api_endpoint,
            api_secret=api_secret,
            workspace_uid=workspace_uid or None,
            api_key=api_key or None,
        )
        self.agent = agent or None
        self.model_family = model_family or None
        self._steps_supported: bool | None = None  # unknown until the first read
        self.workspace = workspace_uid or None  # None: the workspace the API key belongs to
        label = f"DinoAI agent `{self.agent}`" if self.agent else "DinoAI default agent"
        self.label = f"{label}, model family `{self.model_family}`" if self.model_family else label

    def start(self, message: str) -> tuple[str, str | None]:
        message = defang_local_urls(message)  # the API firewall blocks links to local hosts
        if not self.model_family:
            result = self._call(self._client.dinoai_agents.trigger_run, agent=self.agent, message=message)
            ok, session_id, warning = result.ok, result.agent_session_id, None
        else:
            variables = {"agent": self.agent, "message": message, "modelFamily": self.model_family}
            data = self._call(self._client._call_gql, query=self._TRIGGER_WITH_MODEL, variables=variables)
            run = data["triggerDinoaiAgentRun"]
            ok, session_id, warning = run["ok"], run["agentSessionId"], run.get("warning")
        if not ok:
            raise BackendError("Paradime did not accept the agent run.")
        return session_id, warning

    def key_identity(self) -> dict | None:
        """The API key's capabilities and expiry date, or None when this API cannot say.

        The query also returns `isValid`, but that came back False for a working
        account key on the dev API (2026-10-07), so nothing here relies on it.
        """
        try:
            data = self._call(self._client._call_gql, query="query { apiKeyIdentity { capabilities expiresAt } }")
        except BackendError:
            return None
        return (data or {}).get("apiKeyIdentity")

    def send(self, session_id: str, message: str) -> None:
        result = self._call(
            self._client.dinoai_agents.send_message,
            agent_session_id=session_id,
            message=defang_local_urls(message),  # the API firewall blocks links to local hosts
        )
        if not result.ok:
            raise BackendError("Paradime did not accept the follow-up message.")

    @property
    def steps_supported(self) -> bool | None:
        """Whether this API serves the agent's steps: None until a read has told."""
        return self._steps_supported

    def get(
        self, session_id: str, *, steps_after: int | None = None, with_steps: bool = True
    ) -> RunSnapshot:
        agents = self._client.dinoai_agents
        if with_steps and self._steps_supported is not False:
            try:
                run = self._call(
                    agents.get_run,
                    agent_session_id=session_id,
                    include_steps=True,
                    include_tool_io=True,  # the input of each tool call: the SQL, the search, the model
                    max_chars=self.STEP_MAX_CHARS,
                    steps_after=steps_after if steps_after is not None and steps_after >= 0 else None,
                )
            except BackendError as exc:
                if "Cannot query field" not in str(exc):
                    raise
                self._steps_supported = False  # an API without steps: read the plain run from now on
            else:
                self._steps_supported = True
                return _snapshot(run)
        return _snapshot(self._call(agents.get_run, agent_session_id=session_id))

    @staticmethod
    def _call(method, **kwargs):
        """Run an SDK call, turning API and network failures into BackendError."""
        import requests
        from paradime.client.api_exception import ParadimeException

        try:
            return method(**kwargs)
        except (ParadimeException, requests.RequestException) as exc:
            if is_firewall_page(str(exc)):
                raise BackendError(
                    "Paradime's API firewall blocked the request: something in the message "
                    "looked like an attack to it. Rephrase the message, or ask Paradime "
                    "support to allow this app."
                ) from exc
            raise BackendError(str(exc)) from exc


def _snapshot(run) -> RunSnapshot:
    """The SDK's run (paradime-io 6.8 or later), in the app's own types."""
    return RunSnapshot(
        status=str(run.status.value).upper(),
        messages=[
            AgentMessage(ts=_to_float(m.ts), role=m.role, content=m.content) for m in run.messages
        ],
        child_session_ids=list(run.child_session_ids or []),
        steps=[
            AgentStep(
                index=s.index,
                role=str(s.role).lower(),  # the API sends USER, AGENT and TOOL
                tool_name=s.tool_name,
                tool_input=s.tool_input,
                content=s.content,
            )
            for s in run.steps or []
        ],
        startup=[StartupStep(label=s.label, done=s.done) for s in run.startup_steps or []],
    )


def _to_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


# --- Offline replay -------------------------------------------------------------


@dataclass
class _ScheduledMessage:
    due: float
    role: str
    content: str
    # A delegated agent's callback, posted outside any turn: the session sleeps
    # until wake_at, then runs until the message is due.
    wake_at: float | None = None


@dataclass
class _ReplaySession:
    started: float
    queue: list[_ScheduledMessage] = field(default_factory=list)
    reports: int = 0  # numbers the demo's Linear issues


_QUESTION_TAG = re.compile(r"<question>\s*(.*?)\s*</question>", re.DOTALL)
_ASSIGNEE = re.compile(r'assignee_email "([^"]+)"')
_TEAM = re.compile(r'Linear team key "([^"]+)"')
_WAKE_SECONDS = 3.0  # how long a woken session runs before it posts, at speed 1
_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    "a an and are as at by did do does for from has have how in is it me of on or "
    "show the their to was were what which who with".split()
)


class ReplayBackend:
    """Replays recorded DinoAI transcripts from JSON fixtures.

    A fixture is {"question": str, "turn": [{"delay": seconds, "content": str}, ...]}.
    The fixture whose question shares the most words with the incoming question
    answers it, revealed on the recorded timing (scaled by ``speed``).
    """

    label = "Demo replay (recorded DinoAI sessions)"

    def __init__(
        self, fixtures_dir: Path, speed: float = 1.0, min_step: float = MIN_REPLAY_STEP_SECONDS
    ) -> None:
        self._fixtures = [
            json.loads(path.read_text()) for path in sorted(Path(fixtures_dir).glob("*.json"))
        ]
        self._speed = max(speed, 0.01)
        self._min_step = min_step
        self._sessions: dict[str, _ReplaySession] = {}
        # Unique per process, so a thread saved before a restart never meets a new namesake.
        run = uuid.uuid4().hex[:6]
        self._ids = (f"replay-{run}-{n}" for n in itertools.count(1))

    @property
    def questions(self) -> list[str]:
        return [f["question"] for f in self._fixtures]

    def start(self, message: str) -> tuple[str, str | None]:
        session_id = next(self._ids)
        self._sessions[session_id] = _ReplaySession(started=time.monotonic())
        self._schedule(session_id, message)
        return session_id, None

    def send(self, session_id: str, message: str) -> None:
        if session_id not in self._sessions:
            raise BackendError(f"Unknown replay session {session_id}.")
        self._schedule(session_id, message)

    def get(
        self, session_id: str, *, steps_after: int | None = None, with_steps: bool = True
    ) -> RunSnapshot:
        session = self._sessions.get(session_id)
        if session is None:
            raise BackendError(f"Unknown replay session {session_id}.")
        now = time.monotonic()
        revealed = [m for m in session.queue if m.due <= now]
        waiting = [m for m in session.queue if m.due > now]
        # Like the real harness: the run completes after its turn, idles while a
        # delegated agent works, and runs again when the callback wakes it.
        busy = [m for m in waiting if m.wake_at is None or now >= m.wake_at]
        if not busy:
            status = "COMPLETED"
        elif now - session.started < 1.0 and not revealed:
            status = "QUEUED"
        else:
            status = "RUNNING"
        return RunSnapshot(
            status=status,
            messages=[AgentMessage(ts=m.due, role=m.role, content=m.content) for m in revealed],
        )

    def _schedule(self, session_id: str, message: str) -> None:
        session = self._sessions[session_id]
        now = time.monotonic()
        tail = max([now] + [m.due for m in session.queue])
        if "<feedback>" in message:
            self._schedule_feedback(session, tail, message)
            return
        fixture = self._match(message)
        if fixture is None:
            listing = "\n".join(f"- {q}" for q in self.questions) or "- (no recordings found)"
            turn = [
                {
                    "delay": 1.5,
                    "content": (
                        "Demo mode only has recorded answers. Try one of these questions:\n\n"
                        + listing
                    ),
                }
            ]
        else:
            turn = fixture["turn"]
        due = tail
        for step in turn:
            # Like the real harness, never answer faster than one poll can see the run active.
            due += max(float(step.get("delay", 2.0)) / self._speed, self._min_step)
            session.queue.append(_ScheduledMessage(due, step.get("role", "agent"), step["content"]))

    def _schedule_feedback(self, session: _ReplaySession, tail: float, message: str) -> None:
        """The squad flow, simulated: an acknowledgement now, the filed issue later."""
        email = _ASSIGNEE.search(message)
        assignee = f"assigned to {email.group(1)}" if email else "unassigned, for the team to triage"
        key = _TEAM.search(message)
        team = key.group(1) if key else "DNA"
        session.reports += 1
        issue = f"{team}-{session.reports}"
        ack = max(4.0 / self._speed, self._min_step)
        session.queue.append(
            _ScheduledMessage(
                tail + ack,
                "agent",
                "Thanks for flagging this. The data team has your report, "
                "and the Linear issue link follows in this chat.",
            )
        )
        due = tail + ack + max(30.0 / self._speed, 2 * self._min_step)
        session.queue.append(
            _ScheduledMessage(
                due,
                "agent",
                f"The data team's issue is [{issue}](https://linear.app/demo/issue/{issue}), "
                f"{assignee}. (Demo mode: no real issue was created.)",
                wake_at=due - max(_WAKE_SECONDS / self._speed, self._min_step),
            )
        )

    def _match(self, message: str) -> dict | None:
        found = _QUESTION_TAG.search(message)
        question = found.group(1) if found else message.replace(FOLLOW_UP_REMINDER, "")
        words = _keywords(question)
        best, best_score = None, 0.0
        for fixture in self._fixtures:
            candidate = _keywords(fixture["question"])
            overlap = len(words & candidate)
            if overlap < 2:
                continue
            score = overlap / min(len(words), len(candidate))
            if score > best_score:
                best, best_score = fixture, score
        return best if best_score >= 0.5 else None


def _keywords(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOPWORDS}
