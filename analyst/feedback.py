"""A reader's report on an answer, filed as a Linear issue by the agent squad.

The app posts the report to the conversation's own DinoAI session. The analyst
agent hands it to the feedback agent (its agents_squad) with invoke_agent and a
callback, so the analyst replies twice:

1. at once, a short acknowledgement: the turn that the app waits on;
2. a few minutes later, the Linear issue link, when the feedback agent's
   notify_parent_session wakes the session. Nobody waits on this reply.

The second reply belongs to no turn, so the app watches the session for agent
messages that no turn claimed (``watch``). If the reader asks a follow-up in
the meantime, the reply can land inside that turn instead (``pull_issue_reply``).

Each assistant item in the history records the transcript messages it took,
as ``"transcript": [start, end]``, so the watcher knows which ones are unclaimed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from analyst.backend import AgentMessage, RunSnapshot
from analyst.protocol import VizSegment, parse_message, plain_text
from analyst.sanitize import is_local_url

CATEGORIES = ("Wrong number", "Missing data", "Unclear answer", "Other")
WATCH_SECONDS = 15 * 60.0  # after this, stop waiting for the issue link
MAX_ANSWER_CHARS = 1500  # of the answer's prose in the brief; the SQL always goes in whole

# https://linear.app/<workspace>/issue/<KEY-123>/<optional-slug>
_ISSUE_URL = re.compile(r"https://linear\.app/[\w.-]+/issue/([A-Z][A-Z0-9]*-\d+)(?:/[\w%.-]*)?")


@dataclass
class FeedbackReport:
    """A reader's report on one answer, and where the issue for it goes."""

    category: str
    comment: str
    question: str
    answer: str  # the answer's prose
    visuals: list[dict[str, str]]  # the title and SQL of each visual in the answer
    session_id: str  # the analyst's session: the feedback agent calls back into it
    conversation_url: str
    feedback_agent: str
    team_key: str
    assignee_email: str = ""
    reporter: str = ""


def build_feedback_message(report: FeedbackReport) -> str:
    """The follow-up that asks the analyst to hand the report to the feedback agent."""
    visuals = "\n".join(
        f"- {v.get('title') or 'Untitled visual'}\n```sql\n{(v.get('sql') or '-- not given').strip()}\n```"
        for v in report.visuals
    ) or "- (none)"
    answer = report.answer.strip()
    if len(answer) > MAX_ANSWER_CHARS:
        answer = answer[:MAX_ANSWER_CHARS].rstrip() + " …"
    assignee = (
        f'assignee_email "{report.assignee_email}"'
        if report.assignee_email
        else "no assignee (the team triages the issue)"
    )
    # A link to the reader's own computer helps nobody on the data team.
    shareable = report.conversation_url and not is_local_url(report.conversation_url)
    link = f"the conversation link {report.conversation_url} and " if shareable else ""
    return f"""<feedback>
Category: {report.category}
Reader's comment: {report.comment.strip() or "(none)"}
Reported by: {report.reporter or "a reader who is not signed in"}
Question: {report.question.strip()}
<answer>
{answer}
</answer>
Visuals in the answer:
{visuals}
</feedback>

A reader reported a problem with one of your answers. Do not file the issue yourself, and do not rewrite the answer. Do these steps in order:
1. If one query can confirm or rule out the problem, run it. Run one query at most.
2. Start the `{report.feedback_agent}` agent with invoke_agent, with callback_session_id="{report.session_id}" so that it reports back to you. In its message, give: the reader's category and comment word for word, who reported it, the question, your answer, the SQL of each visual, the dbt models you used, what your query found, the Linear team key "{report.team_key}", {assignee}, {link}the DinoAI session id {report.session_id}.
3. Reply to the reader in one short sentence: the data team has the report, and the Linear issue link follows in this chat.
4. When `{report.feedback_agent}` reports back, reply in one sentence, without tools: the issue key as a Markdown link to the issue, and who it is assigned to. If it could not file the issue, say so and say why."""


def answer_context(history: list[dict[str, Any]], index: int) -> tuple[str, str, list[dict[str, str]]]:
    """The question, the prose and the visuals (title and SQL) of the answer at ``index``."""
    question = next(
        (item["content"] for item in reversed(history[:index]) if item.get("role") == "user"), ""
    )
    contents = history[index].get("answer") or []
    prose = "\n\n".join(plain_text(c).strip() for c in contents).strip()
    visuals = [
        {"title": str(s.spec.get("title") or ""), "sql": str(s.spec.get("sql") or "")}
        for c in contents
        for s in parse_message(c)
        if isinstance(s, VizSegment)
    ]
    return question, prose, visuals


def report_item(category: str, comment: str, about: int, question: str, now: float) -> dict[str, Any]:
    """The history item for a report: the reader's side of the exchange."""
    return {
        "role": "report",
        "category": category,
        "comment": comment.strip(),
        "about": about,  # the history index of the answer it is about
        "question": question,
        "sent_at": now,
        # waiting -> filed (with an issue link) | replied (no link) | failed | expired
        "status": "waiting",
        "issue": None,
    }


def waiting_report(history: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The oldest report that still waits for its issue link.

    Replies are matched to reports in the order the reports were sent.
    """
    return next(
        (i for i in history if i.get("role") == "report" and i.get("status") == "waiting"), None
    )


def reported(history: list[dict[str, Any]], index: int) -> dict[str, Any] | None:
    """The report about the answer at ``index``, if the reader sent one that went through."""
    return next(
        (
            i
            for i in reversed(history)
            if i.get("role") == "report" and i.get("about") == index and i.get("status") != "failed"
        ),
        None,
    )


def find_issue(text: str) -> dict[str, str] | None:
    """The first Linear issue linked in ``text``, as {"key", "url"}."""
    found = _ISSUE_URL.search(text)
    return {"key": found.group(1), "url": found.group(0)} if found else None


def record_reply(report: dict[str, Any], contents: list[str]) -> None:
    """Mark the report answered, with the issue that the reply links to, if any."""
    issue = next((found for c in contents if (found := find_issue(c))), None)
    report["status"] = "filed" if issue else "replied"
    report["issue"] = issue


def pull_issue_reply(messages: list[AgentMessage]) -> AgentMessage | None:
    """A turn's message that links to a Linear issue: a callback reply that landed in the turn."""
    return next((m for m in reversed(messages) if find_issue(m.content)), None)


@dataclass
class WatchResult:
    state: str  # "waiting", "posting", "done" or "expired"
    reply: list[str] = field(default_factory=list)
    transcript: list[int] | None = None  # [start, end] of the reply in the transcript


def watch(
    history: list[dict[str, Any]],
    report: dict[str, Any],
    snapshot: RunSnapshot,
    now: float,
    previous_count: int | None,
) -> WatchResult:
    """One poll of a session that owes the reader an issue link.

    The reply is the first run of messages after the report's acknowledgement
    that no history item claims. It is done when the run is idle and the
    transcript did not grow since the previous poll.
    """
    claimed = _claimed(history)
    start = _acknowledged_at(history, report)
    found = None if start is None else _unclaimed_run(len(snapshot.messages), start, claimed)
    if not snapshot.terminal:
        return WatchResult("posting")  # the callback woke the session up
    if found:
        lo, hi = found
        reply = [m.content for m in snapshot.messages[lo:hi] if m.from_agent and m.content.strip()]
        if reply and previous_count == len(snapshot.messages):
            return WatchResult("done", reply, [lo, hi])
        if reply:
            return WatchResult("waiting")  # one more poll, to see that it stopped growing
    if now - float(report.get("sent_at") or now) >= WATCH_SECONDS:
        return WatchResult("expired")
    return WatchResult("waiting")


def _claimed(history: list[dict[str, Any]]) -> set[int]:
    claimed: set[int] = set()
    for item in history:
        span = item.get("transcript")
        if item.get("role") == "assistant" and span:
            claimed.update(range(span[0], span[1]))
    return claimed


def _acknowledged_at(history: list[dict[str, Any]], report: dict[str, Any]) -> int | None:
    """Where the transcript stood when the analyst acknowledged the report."""
    after = history[next(i for i, item in enumerate(history) if item is report) + 1 :]
    ack = next((i for i in after if i.get("role") == "assistant"), None)
    span = ack.get("transcript") if ack else None
    return span[1] if span else None


def _unclaimed_run(total: int, start: int, claimed: set[int]) -> tuple[int, int] | None:
    lo = next((i for i in range(start, total) if i not in claimed), None)
    if lo is None:
        return None
    hi = next((i for i in range(lo, total) if i in claimed), total)
    return lo, hi
