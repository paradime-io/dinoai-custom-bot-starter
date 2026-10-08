"""Ask-your-data chat for stakeholders: Streamlit frontend, DinoAI agent backend.

Each conversation is one DinoAI agent session. The first question starts the
session with the response contract (analyst/protocol.py); follow-ups are posted
to the same session, so the agent keeps the context. The app polls the run and
renders the agent's final message, turning its ```viz blocks into charts.

The session id is the thread id: it goes in the URL (?thread=<id>) and the
conversation is saved to disk, so a reload, a new tab or a server restart
reopens the same thread instead of starting over. The History page lists
every saved thread, and a thread's link can be shared.

A reader can report a problem with an answer. The report goes to the same
session; the analyst agent hands it to its feedback agent, which files a Linear
issue, and the issue link comes back into the chat (analyst/feedback.py).

Run:  streamlit run app.py
"""

from __future__ import annotations

import dataclasses
import logging
import os
import time
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from analyst.backend import (
    MIN_REPLAY_STEP_SECONDS,
    AgentBackend,
    BackendError,
    ParadimeBackend,
    ReplayBackend,
)
from analyst.export import conversation_markdown, export_file_name
from analyst.feedback import (
    CATEGORIES,
    FeedbackReport,
    answer_context,
    build_feedback_message,
    pull_issue_reply,
    record_reply,
    report_item,
    reported,
    waiting_report,
    watch,
)
from analyst.protocol import build_first_message, build_follow_up_message, escape_dollars
from analyst.render import headline, preview, render_answer, render_notes
from analyst.steps import describe
from analyst.threads import ThreadStore, ThreadSummary, earlier_turns
from analyst.turns import PendingTurn, observe, outcome, turn_messages, turn_steps

log = logging.getLogger("analyst")

ROOT = Path(__file__).parent
ASSETS = ROOT / "assets"
# Paradime branding: the Pragma UI kit's DinoAI sparkle, and the paradime.io logo
# with a wordmark that turns white in dark mode. Fonts and colors: .streamlit/config.toml.
ASSISTANT_AVATAR = str(ASSETS / "dinoai.svg")
PARADIME_LOGO = str(ASSETS / "paradime-logo.svg")
PARADIME_MARK = str(ASSETS / "paradime-mark.svg")
USER_AVATAR = ":material/person:"  # neutral, instead of the default red face
STATUS_LABELS = {
    "QUEUED": "Starting a DinoAI agent",
    "RUNNING": "DinoAI is analysing your data",
}
HISTORY_PATH = "history"
RECENT_KEY = "recent_thread"
RECENT_IN_SIDEBAR = 8
REPORT_SENT, REPORT_ERROR = "report_sent", "report_error"  # from the dialog's send callback
WATCH_POLL_SECONDS = 3.0  # how often an open chat checks for the issue link of a report
PAGES: dict[str, Any] = {}  # filled by main(), so pages can link to each other


def setting(name: str, default: Any = None) -> Any:
    """Read from .streamlit/secrets.toml, else the environment.

    A name present in the secrets file wins even when blank, so a variable
    exported for the Paradime CLI cannot leak into the app's configuration.
    """
    try:
        in_secrets = name in st.secrets
    except Exception:  # no secrets file
        in_secrets = False
    value = st.secrets[name] if in_secrets else os.environ.get(name)
    return default if value in (None, "") else value


@st.cache_resource(show_spinner="Connecting to Paradime…")
def get_backend(
    mode: str,
    endpoint: str,
    secret: str,
    key: str,
    workspace: str,
    agent: str,
    model_family: str,
    speed: float,
    min_step: float,
) -> AgentBackend:
    if mode == "demo":
        return ReplayBackend(ROOT / "fixtures" / "replay", speed=speed, min_step=min_step)
    return ParadimeBackend(
        api_endpoint=endpoint,
        api_secret=secret,
        api_key=key,
        workspace_uid=workspace,
        agent=agent,
        model_family=model_family,
    )


def backend_from_settings() -> tuple[AgentBackend, str]:
    secret = setting("PARADIME_API_SECRET", "")
    mode = setting("ANALYST_MODE", "live" if secret else "demo")
    endpoint = setting("PARADIME_API_ENDPOINT", "")
    if mode == "live" and not (secret and endpoint):
        st.error(
            "Live mode needs `PARADIME_API_ENDPOINT` and `PARADIME_API_SECRET` in "
            "`.streamlit/secrets.toml` (see `secrets.toml.example`).",
            icon=":material/key:",
        )
        st.stop()
    try:
        backend = get_backend(
            mode,
            endpoint,
            secret,
            setting("PARADIME_API_KEY", ""),
            setting("PARADIME_WORKSPACE_UID", ""),
            setting("DINOAI_AGENT", ""),
            setting("DINOAI_MODEL_FAMILY", ""),
            float(setting("ANALYST_REPLAY_SPEED", 4.0)),
            float(setting("ANALYST_REPLAY_MIN_STEP", MIN_REPLAY_STEP_SECONDS)),
        )
    except ValueError as exc:  # the SDK validates the credential combination
        st.error(f"The Paradime credentials are incomplete: {exc}", icon=":material/key:")
        st.stop()
    return backend, mode


def starter_questions(backend: AgentBackend) -> list[str]:
    configured = setting("ANALYST_STARTER_QUESTIONS")
    if isinstance(configured, str):
        return [line.strip() for line in configured.splitlines() if line.strip()]
    if configured:
        return [str(q) for q in configured]
    return list(getattr(backend, "questions", []))


def reports_enabled(mode: str) -> bool:
    """Reports go through the stakeholder-analyst agent's squad: live mode needs that agent."""
    return mode == "demo" or bool(setting("DINOAI_AGENT"))


def feedback_route() -> tuple[str, str, str]:
    """The feedback agent, the Linear team key and the assignee's email."""
    return (
        setting("ANALYST_FEEDBACK_AGENT", "analytics-feedback"),
        setting("ANALYST_FEEDBACK_TEAM", "DNA"),
        setting("ANALYST_FEEDBACK_ASSIGNEE", ""),
    )


def thread_store() -> ThreadStore:
    return ThreadStore(Path(setting("ANALYST_THREADS_DIR", ROOT / ".threads")))


def reset_conversation() -> None:
    for name in ("history", "session_id", "pending", "color_registry", "watch_count"):
        st.session_state.pop(name, None)
    st.query_params.clear()


def switch_thread(session_id: str) -> None:
    """Open another saved thread in place; open_thread loads it on the rerun."""
    reset_conversation()
    st.query_params["thread"] = session_id
    st.session_state.opening = True  # show a loading state while it renders


def _open_recent() -> None:
    """A click in the recent list. Only a user's change calls this, never a rerun."""
    choice = st.session_state.get(RECENT_KEY)
    if choice and choice != st.session_state.get("session_id"):
        switch_thread(choice)


def save_thread() -> None:
    """Write the conversation to disk and keep its id in the URL."""
    session_id = st.session_state.get("session_id")
    if not session_id:
        return
    thread_store().save(
        session_id, st.session_state.history, st.session_state.pending, owner=current_user()
    )
    if st.query_params.get("thread") != session_id:
        st.query_params["thread"] = session_id


def current_user() -> str | None:
    """The signed-in viewer's email, when the app has Streamlit sign-in set up."""
    try:
        return st.user.email if st.user.is_logged_in else None
    except Exception:  # no auth configured
        return None


def thread_url(session_id: str) -> str:
    """The link that opens a thread in the chat, from whichever page asks."""
    base = (st.context.url or "").rstrip("/")
    if base.endswith(f"/{HISTORY_PATH}"):
        base = base[: -len(HISTORY_PATH) - 1]
    return f"{base}/?thread={session_id}"


def _ago(ts: float) -> str:
    seconds = max(0, int(time.time() - ts))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    return datetime.fromtimestamp(ts).strftime("%b %-d")


def open_thread(backend: AgentBackend) -> None:
    """Reopen the thread named in the URL after a reload, in a new tab or after a restart."""
    thread = st.query_params.get("thread")
    if not thread or thread == st.session_state.get("session_id"):
        return
    saved = thread_store().load(thread)
    if saved is not None:
        history, pending = saved
    else:
        # Not saved on this server: rebuild what the agent session still holds.
        try:
            with st.spinner("Opening the conversation…"):
                run = backend.get(thread, with_steps=False)
        except BackendError:
            st.query_params.clear()
            st.toast("That conversation link has expired.")
            return
        history = [_notice(f"Reopened DinoAI session `{thread}`. Earlier questions were not saved on this server.")]
        history += [_assistant_item(thread, answer=[m.content]) for m in run.messages if m.from_agent]
        pending = None
    st.session_state.update(history=history, pending=pending, session_id=thread)
    st.session_state.pop("color_registry", None)


def start_turn(backend: AgentBackend, question: str, history: list[dict[str, Any]]) -> PendingTurn:
    """Post the question: a follow-up in the current session, or a new session.

    ``history`` already ends with the question. If the current session cannot
    take a follow-up (for example, it expired), the new session gets a summary
    of the conversation so far, and the chat says what happened.
    """
    now = time.time()
    session_id = st.session_state.get("session_id")
    if session_id:
        try:
            before = backend.get(session_id)
            backend.send(session_id, build_follow_up_message(question))
            return PendingTurn(
                session_id,
                question,
                len(before.messages),
                now,
                first_turn=False,
                step_baseline=max((s.index for s in before.steps), default=-1),
            )
        except BackendError as exc:
            log.warning("Follow-up to %s failed, starting a new session: %s", session_id, exc)
            history.insert(
                len(history) - 1,
                _notice(
                    f"DinoAI session `{session_id}` could not take a follow-up ({str(exc).rstrip('.')}). "
                    "This question starts a new session, with a summary of the conversation so far."
                ),
            )
    message = build_first_message(question, setting("ANALYST_CONTEXT"), earlier_turns(history[:-1]))
    session_id, warning = backend.start(message)
    if warning:
        history.insert(len(history) - 1, _notice(f"Paradime: {warning}"))
    st.session_state.session_id = session_id
    return PendingTurn(session_id, question, 0, now, first_turn=True)


def _notice(text: str) -> dict[str, Any]:
    return {"role": "notice", "content": text}


def wait_for_turn(backend: AgentBackend, turn: PendingTurn, box: Any) -> dict[str, Any]:
    """Poll the run until the turn ends, showing the agent's progress in ``box``.

    The Paradime API has no push channel: messages arrive whole, so polling
    often is the only way to show them sooner. One poll is one ~0.3 s request.

    The box itself changes only when the turn ends. Each update of a status box
    sends its expanded state again, so updating its label every second closed
    the box again right after a reader opened it. The steps go into a list
    inside the box, and a live line under them shows the timer.
    """
    poll = float(setting("ANALYST_POLL_SECONDS", 1.0))
    what = "report" if turn.kind == "report" else "question"
    step_list = box.container()  # one line for each step, as they come
    live = box.empty()  # the current activity and the timer
    shown, failures = 0, 0
    lines: list[str] = []  # one per step, for APIs that expose the agent's steps
    shown_steps: set[int] = set()
    while True:
        try:
            snapshot = backend.get(turn.session_id, steps_after=turn.step_baseline)  # this turn's steps only
            failures = 0
        except BackendError as exc:
            failures += 1
            if failures >= 5:
                box.update(label="Lost contact with Paradime", state="error")
                return _assistant_item(turn.session_id, error=f"Could not read the agent run: {exc}")
            time.sleep(poll)
            continue
        now = time.time()
        done = observe(turn, snapshot, now)
        messages = turn_messages(turn, snapshot)
        for step in turn_steps(turn, snapshot):
            if step.index not in shown_steps:
                shown_steps.add(step.index)
                line = describe(step)
                if line:
                    lines.append(line)
                    step_list.markdown(f"- {escape_dollars(line)}")
        if not lines:  # no steps from this API: show the agent's own messages
            for message in messages[shown:]:
                step_list.markdown(preview(message.content))
        shown = len(messages)
        elapsed = _clock(now - turn.sent_at)
        if done:
            live.empty()
            total = len(snapshot.messages)
            callback = None
            if waiting_report(st.session_state.history) is not None:
                # The feedback agent's callback can wake the session during this turn.
                callback = pull_issue_reply(messages)
                if callback is not None:
                    kept = [m for m in snapshot.messages if m is not callback]
                    snapshot = dataclasses.replace(snapshot, messages=kept)
            result = outcome(turn, snapshot, now)
            box.update(
                label=f"Done in {elapsed}" if not result.error else "The agent did not finish",
                state="complete" if not result.error else "error",
            )
            if messages:
                agent = messages[-1].ts - turn.sent_at
                log.info(
                    "turn session=%s total=%.1fs agent=%.1fs app_delay=%.1fs",
                    turn.session_id, now - turn.sent_at, agent, now - turn.sent_at - agent,
                )
            item = _assistant_item(
                turn.session_id, result.answer, result.notes, result.error, now - turn.sent_at
            )
            item["steps"] = lines
            item["transcript"] = [turn.baseline, total]  # the messages this turn claimed
            item["kind"] = "ack" if turn.kind == "report" else "answer"
            if callback is not None:
                item["callback"] = callback.content
            return item
        pending_startup = [s.label for s in snapshot.startup if not s.done]
        if lines:
            label = f"{len(lines)} step{'s' if len(lines) != 1 else ''} so far"  # listed above
        elif snapshot.status == "QUEUED" and pending_startup:
            label = pending_startup[0]  # the pod's start-up checklist
        elif messages and not snapshot.terminal:
            label = headline(messages[-1].content)  # the agent's own progress line
        else:
            label = STATUS_LABELS.get(snapshot.status, "DinoAI is working")
        if snapshot.terminal:  # a follow-up that the run has not picked up, or is settling
            label = "Finishing up" if messages else f"Sending your {what} to DinoAI"
        live.caption(f"{escape_dollars(label)} · {elapsed}")
        time.sleep(poll)


def _assistant_item(
    session_id: str,
    answer: list[str] | None = None,
    notes: list[str] | None = None,
    error: str | None = None,
    seconds: float | None = None,
) -> dict[str, Any]:
    return {
        "role": "assistant",
        "answer": answer or [],
        "notes": notes or [],
        "error": error,
        "seconds": seconds,
        "session_id": session_id,
    }


def _clock(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}:{secs:02d}"


def render_history(
    history: list[dict[str, Any]], backend: AgentBackend | None = None, reports_on: bool = False
) -> None:
    for i, item in enumerate(history):
        if item["role"] == "notice":
            st.info(item["content"], icon=":material/forum:")
            continue
        if item["role"] == "user":
            with st.chat_message("user", avatar=USER_AVATAR):
                st.markdown(escape_dollars(item["content"]))
            continue
        if item["role"] == "report":
            render_report(item)
            continue
        with st.chat_message("assistant", avatar=ASSISTANT_AVATAR):
            if item["error"]:
                st.error(item["error"], icon=":material/error:")
            render_answer(item["answer"], key=f"m{i}")
            render_notes(item["notes"], item.get("steps"))
            verb = "Answered" if item.get("kind", "answer") == "answer" else "Replied"
            footer = [f"{verb} in {_clock(item['seconds'])}"] if item.get("seconds") else []
            footer.append(f"session `{item['session_id']}`")
            st.caption(" · ".join(footer))
            # Acknowledgements and issue links are not answers to rate or report.
            if item["answer"] and item.get("kind", "answer") == "answer":
                render_answer_actions(history, i, backend, reports_on)


def render_answer_actions(
    history: list[dict[str, Any]], i: int, backend: AgentBackend | None, reports_on: bool
) -> None:
    """Thumbs, and "Report a problem" (also offered after a thumbs down)."""
    item = history[i]
    # Keyed by session too: the same message position exists in every thread.
    key = f"{item['session_id']}-{i}"
    report = reported(history, i)
    busy = st.session_state.get("pending") is not None
    clicked = False
    with st.container(horizontal=True, vertical_alignment="center"):
        rating = st.feedback("thumbs", key=f"{key}-feedback", default=item.get("rating"))
        if report is not None:
            st.caption(f":material/flag: {_report_status(report)}")
        else:
            clicked = st.button(
                "Report a problem",
                key=f"{key}-report",
                type="tertiary",
                icon=":material/flag:",
                disabled=not reports_on or busy,
                help=(
                    "Tell the data team what is wrong. DinoAI files a Linear issue for it."
                    if reports_on
                    else "Needs the stakeholder-analyst agent: set DINOAI_AGENT (see agent/)."
                ),
            )
    thumbs_down = False
    if rating is not None and item.get("rating") != rating:
        item["rating"] = rating
        thumbs_down = rating == 0
        log.info("feedback session=%s rating=%s", item["session_id"], rating)
        save_thread()
    if (clicked or thumbs_down) and backend is not None and reports_on and report is None and not busy:
        report_dialog(backend, i)


def render_report(item: dict[str, Any]) -> None:
    """The reader's report, on their side of the chat."""
    with st.chat_message("user", avatar=":material/flag:"):
        st.markdown(f"**Reported a problem: {item['category']}**")
        if item["comment"]:
            st.markdown(escape_dollars(item["comment"]))
        st.caption(f"About the answer to: {escape_dollars(item['question'])} · {_report_status(item)}")


def _report_status(report: dict[str, Any]) -> str:
    issue = report.get("issue")
    return {
        "waiting": "Waiting for the Linear issue",
        "filed": f"Filed as [{issue['key']}]({issue['url']})" if issue else "Filed",
        "replied": "DinoAI replied below",
        "failed": "Not sent: DinoAI could not take the report",
        "expired": "No issue link arrived. Check the team's Linear issues",
    }.get(report.get("status", ""), "Reported")


@st.dialog("Report a problem", width="medium")
def report_dialog(backend: AgentBackend, index: int) -> None:
    if st.session_state.pop(REPORT_SENT, False):
        st.rerun()  # close the dialog: the chat shows the report and waits for DinoAI
    history = _open_answer(index)
    if history is None:
        st.info("This conversation is no longer open. Close this window.", icon=":material/info:")
        return
    question, _, _ = answer_context(history, index)
    _, team, assignee = feedback_route()
    key = _report_key(history, index)
    st.markdown(f"About the answer to: **{escape_dollars(question)}**")
    category = st.pills("What is wrong?", CATEGORIES, key=f"{key}-report-category")
    st.text_area(  # read by _send_clicked, from its key
        "What should the data team know?",
        key=f"{key}-report-comment",
        placeholder="For example: finance reports 1.2M for Q3, not 0.9M.",
        max_chars=2000,
    )
    owner = f" and assigns it to {assignee}" if assignee else ""
    st.caption(
        f"DinoAI files a Linear issue in the {team} team{owner}, "
        "with this answer and its SQL. DinoAI checks the answer first, so the issue link "
        "appears in this chat after a few minutes."
    )
    error = st.session_state.pop(REPORT_ERROR, None)
    if error:
        st.error(f"Could not send the report: {error}", icon=":material/error:")
    st.button(
        "Send report",
        key="send_report",
        type="primary",
        icon=":material/send:",
        disabled=not category,
        width="stretch",
        on_click=_send_clicked,
        args=(backend, index),
    )


def _open_answer(index: int) -> list[dict[str, Any]] | None:
    """The open conversation, if it still has the answer at ``index``.

    Dialogs and callbacks can run after the conversation was cleared or switched.
    """
    history = st.session_state.get("history")
    if not history or index >= len(history) or history[index].get("role") != "assistant":
        return None
    return history


def _report_key(history: list[dict[str, Any]], index: int) -> str:
    return f"{history[index]['session_id']}-{index}"


def _send_clicked(backend: AgentBackend, index: int) -> None:
    """The dialog's send button. A callback, so it runs before the dialog reruns."""
    history = _open_answer(index)
    if history is None or not st.session_state.get("session_id"):
        st.session_state[REPORT_ERROR] = "This conversation is no longer open."
        return
    key = _report_key(history, index)
    category = st.session_state.get(f"{key}-report-category")
    if not category:
        return
    try:
        send_report(backend, index, category, st.session_state.get(f"{key}-report-comment") or "")
    except BackendError as exc:
        st.session_state[REPORT_ERROR] = str(exc)
        return
    st.session_state[REPORT_SENT] = True


def send_report(backend: AgentBackend, index: int, category: str, comment: str) -> None:
    """Post the report to the session; chat_page then waits for the acknowledgement."""
    history = st.session_state.history
    session_id = st.session_state.session_id
    question, prose, visuals = answer_context(history, index)
    agent, team, assignee = feedback_route()
    message = build_feedback_message(
        FeedbackReport(
            category=category,
            comment=comment,
            question=question,
            answer=prose,
            visuals=visuals,
            session_id=session_id,
            conversation_url=thread_url(session_id),
            feedback_agent=agent,
            team_key=team,
            assignee_email=assignee,
            reporter=current_user() or "",
        )
    )
    now = time.time()
    before = backend.get(session_id)
    backend.send(session_id, message)
    history.append(report_item(category, comment, index, question, now))
    st.session_state.pending = PendingTurn(
        session_id,
        f"Reported a problem: {category}",
        len(before.messages),
        now,
        first_turn=False,
        step_baseline=max((s.index for s in before.steps), default=-1),
        kind="report",
    )
    log.info("report session=%s category=%s", session_id, category)
    save_thread()


@st.fragment(run_every=WATCH_POLL_SECONDS)
def issue_watcher(backend: AgentBackend) -> None:
    """Wait for the issue link that the feedback agent's callback makes the analyst post.

    A fragment's timed rerun runs only this function, not the page. If the
    conversation was cleared since the page last ran (New conversation, another
    thread, a reconnect after a restart), there is nothing to watch.
    """
    history = st.session_state.get("history")
    report = waiting_report(history) if history else None
    session_id = st.session_state.get("session_id")
    if report is None or not session_id or st.session_state.get("pending") is not None:
        return
    label = "The data team's agent is filing a Linear issue"
    try:
        snapshot = backend.get(session_id, with_steps=False)  # the watcher needs messages only
    except BackendError as exc:
        log.warning("Could not check %s for the issue link: %s", session_id, exc)
    else:
        result = watch(history, report, snapshot, time.time(), st.session_state.get("watch_count"))
        st.session_state.watch_count = len(snapshot.messages)
        if result.state == "done":
            add_issue_reply(history, result.reply, result.transcript)
        elif result.state == "expired":
            report["status"] = "expired"
        if result.state in ("done", "expired"):
            save_thread()
            st.rerun()
        if result.state == "posting":
            label = "DinoAI is posting the issue link"
    with st.chat_message("assistant", avatar=ASSISTANT_AVATAR):
        st.status(f"{label} · {_clock(time.time() - report['sent_at'])}", state="running")


def add_issue_reply(history: list[dict[str, Any]], contents: list[str], transcript: list[int] | None) -> None:
    """The analyst's reply to the feedback agent's callback, for the oldest waiting report."""
    report = waiting_report(history)
    if report is None or not contents:
        return
    record_reply(report, contents)
    item = _assistant_item(st.session_state.session_id, answer=contents[-1:], notes=contents[:-1])
    item["kind"] = "issue"
    if transcript:
        item["transcript"] = transcript
    history.append(item)
    log.info("report session=%s issue=%s", st.session_state.session_id, report.get("issue"))


def render_welcome(starters: list[str]) -> str | None:
    if not starters:
        return None
    st.markdown("#### Try one of these")
    clicked = None
    for i, question in enumerate(starters):
        if st.button(question, key=f"starter-{i}", icon=":material/arrow_outward:", width="stretch"):
            clicked = question
    return clicked


def render_sidebar(backend: AgentBackend, mode: str) -> None:
    with st.sidebar:
        st.button(
            "New conversation",
            key="new_conversation",
            icon=":material/add_comment:",
            on_click=reset_conversation,
            width="stretch",
            disabled=not st.session_state.history,
        )
        session_id = st.session_state.get("session_id")
        if session_id and st.session_state.history:
            render_share(session_id, st.session_state.history)
        render_recent(session_id)
        st.divider()
        st.caption(f"Backend: {backend.label}")
        if getattr(backend, "workspace", None):
            st.caption(f"Workspace: `{backend.workspace}`")
        if session_id:
            st.caption(f"Agent session: `{session_id}`")
        st.markdown(
            "**How it works**\n\n"
            "Your question goes to a DinoAI agent in Paradime. It finds the right dbt models, "
            "queries the warehouse, and replies with an answer and charts.\n\n"
            "Every chart has a **Data** tab with the rows behind it and a **SQL** tab with the query."
        )
        if mode == "demo":
            reason = (
                "`ANALYST_MODE` is set to `demo`. Unset it and restart the app to ask live questions."
                if setting("PARADIME_API_SECRET")
                else "Add Paradime credentials to `.streamlit/secrets.toml` to ask live questions."
            )
            st.info(f"Demo mode replays recorded DinoAI answers. {reason}", icon=":material/play_circle:")


def render_share(session_id: str, history: list[dict[str, Any]]) -> None:
    with st.popover("Share", icon=":material/ios_share:", width="stretch"):
        st.markdown("**Share this conversation**")
        st.caption(
            "Anyone with access to this app and the link can read the conversation "
            "and ask follow-ups in it."
        )
        st.code(thread_url(session_id), language=None, wrap_lines=True)
        st.download_button(
            "Download as Markdown",
            conversation_markdown(history, session_id),
            file_name=export_file_name(history),
            mime="text/markdown",
            on_click="ignore",
            icon=":material/download:",
            width="stretch",
        )


def render_recent(current: str | None) -> None:
    threads = thread_store().list(limit=RECENT_IN_SIDEBAR)
    if not threads:
        return
    recent = threads[:RECENT_IN_SIDEBAR]
    ids = [t.session_id for t in recent]
    titles = {t.session_id: _shorten(t.title, 60) for t in recent}
    # Keyed: without a key, Streamlit rebuilds a widget whose options, order or
    # captions changed ("3 min ago" changes every minute) as a new one, and a
    # click made on the old one is lost. Its value always follows the open
    # thread; only a user's click (on_change) switches threads.
    st.session_state[RECENT_KEY] = current if current in ids else None
    st.radio(
        "Recent conversations",
        ids,
        index=None,
        key=RECENT_KEY,
        format_func=titles.__getitem__,
        captions=[_thread_meta(t) for t in recent],
        on_change=_open_recent,
    )


def _thread_meta(thread: ThreadSummary) -> str:
    parts = [f"{thread.questions} question{'s' if thread.questions != 1 else ''}", _ago(thread.updated_at)]
    if thread.pending:
        parts.insert(0, "answering now")
    if thread.owner:
        parts.append(thread.owner)
    return " · ".join(parts)


def _shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " …"


def chat_page() -> None:
    title = setting("ANALYST_TITLE", "Ask your data")
    st.session_state.setdefault("history", [])
    st.session_state.setdefault("pending", None)

    backend, mode = backend_from_settings()
    st.session_state.pop(REPORT_SENT, None)  # a full run: the dialog that sent it is closed
    open_thread(backend)
    if st.session_state.get("session_id") and not st.query_params.get("thread"):
        # Back from another page: put the thread in the URL again, so it can be shared.
        st.query_params["thread"] = st.session_state.session_id
    render_sidebar(backend, mode)
    history: list[dict[str, Any]] = st.session_state.history
    pending: PendingTurn | None = st.session_state.pending

    st.title(title)
    st.badge("Powered by Paradime DinoAI", icon=":material/auto_awesome:", color="violet")
    st.caption("Ask a question in plain English. Answers come from your warehouse, with charts you can check.")

    starter = None
    welcome = st.empty()
    if not history and pending is None:
        with welcome.container():
            starter = render_welcome(starter_questions(backend))
    opening = st.session_state.pop("opening", False)
    with st.spinner("Opening the conversation…") if opening else nullcontext():
        render_history(history, backend, reports_enabled(mode))
    if pending is None and waiting_report(history) is not None:
        issue_watcher(backend)

    # The input is disabled while a turn runs ("disable" covers questions typed
    # into it, the flag covers starter clicks), so nothing typed meanwhile is lost.
    question = st.chat_input(
        "Ask about your data…",
        disabled=pending is not None or starter is not None,
        submit_mode="disable",
    ) or starter

    if question and pending is None:
        # Show the question and a status at once; the Paradime calls run behind them.
        welcome.empty()
        history.append({"role": "user", "content": question})
        with st.chat_message("user", avatar=USER_AVATAR):
            st.markdown(escape_dollars(question))
        with st.chat_message("assistant", avatar=ASSISTANT_AVATAR):
            box = st.status("DinoAI is working on your question", expanded=True)
            try:
                pending = st.session_state.pending = start_turn(backend, question, history)
            except BackendError as exc:
                session_id = st.session_state.get("session_id") or "none"
                history.append(_assistant_item(session_id, error=f"Could not start the DinoAI agent: {exc}"))
                save_thread()
                st.rerun()
            save_thread()
            finish_turn(backend, pending, box)
    elif pending is not None:  # a report, or a turn that a reload, a restart or a click interrupted
        label = "Passing your report to DinoAI" if pending.kind == "report" else "DinoAI is working on your question"
        with st.chat_message("assistant", avatar=ASSISTANT_AVATAR):
            finish_turn(backend, pending, st.status(label, expanded=True))


def finish_turn(backend: AgentBackend, pending: PendingTurn, box: Any) -> None:
    item = wait_for_turn(backend, pending, box)
    history = st.session_state.history
    callback = item.pop("callback", None)
    history.append(item)
    if pending.kind == "report" and item["error"]:
        report = next(i for i in reversed(history) if i.get("role") == "report")
        report["status"] = "failed"
    if callback is not None:
        add_issue_reply(history, [callback], None)
    st.session_state.pending = None
    save_thread()
    st.rerun()


def history_page() -> None:
    """Every conversation asked through this app, for the team running the trial."""
    st.set_page_config(layout="wide")  # the table needs the room
    st.title("Conversation history")
    st.caption(
        "Every conversation asked through this app, newest first. "
        "Anyone with access to the app can see them."
    )
    with st.spinner("Loading conversations…"):
        threads = thread_store().list()
    if not threads:
        st.info("No conversations yet. Ask a question on the Ask page.", icon=":material/forum:")
        return

    columns = st.columns(5)
    columns[0].metric("Conversations", f"{len(threads):,}", border=True)
    columns[1].metric("Questions", f"{sum(t.questions for t in threads):,}", border=True)
    columns[2].metric("Helpful", f"{sum(t.thumbs_up for t in threads):,}", border=True, help="Answers rated 👍")
    columns[3].metric("Not helpful", f"{sum(t.thumbs_down for t in threads):,}", border=True, help="Answers rated 👎")
    columns[4].metric(
        "Reports",
        f"{sum(t.reports for t in threads):,}",
        border=True,
        help="Problems that readers reported with an answer. DinoAI files each one as a Linear issue.",
    )

    rows = pd.DataFrame(
        [
            {
                "link": thread_url(t.session_id),
                # Aware datetimes: a naive one is read as UTC and lands hours off.
                "last_activity": datetime.fromtimestamp(t.updated_at, tz=timezone.utc),
                "title": t.title,
                "questions": t.questions,
                "status": _thread_status(t),
                "feedback": _feedback(t),
                "reports": _reports(t),
                "asked_by": t.owner or "",
            }
            for t in threads
        ]
    )
    if not rows["asked_by"].any():
        rows = rows.drop(columns="asked_by")  # nobody signed in: an empty column says nothing
    st.dataframe(
        rows,
        hide_index=True,
        width="stretch",
        column_config={
            "link": st.column_config.LinkColumn("", display_text="Open", width="small"),
            "last_activity": st.column_config.DatetimeColumn("Last activity", format="distance"),
            "title": st.column_config.TextColumn("Conversation", width="medium"),
            "questions": st.column_config.NumberColumn("Questions"),
            "status": st.column_config.TextColumn("Status"),
            "feedback": st.column_config.TextColumn("Feedback"),
            "reports": st.column_config.TextColumn("Reports", help="The Linear issues filed for reported problems"),
            "asked_by": st.column_config.TextColumn("Asked by"),
        },
    )


def _thread_status(thread: ThreadSummary) -> str:
    if thread.pending:
        return "Answering"
    if thread.failed and not thread.answered:
        return "Failed"
    if thread.failed:
        return f"Answered ({thread.failed} failed)"
    return "Answered"


def _reports(thread: ThreadSummary) -> str:
    if not thread.reports:
        return ""
    waiting = thread.reports - len(thread.issues)
    parts = list(thread.issues) + ([f"{waiting} waiting"] if waiting else [])
    return ", ".join(parts)


def _feedback(thread: ThreadSummary) -> str:
    parts = []
    if thread.thumbs_up:
        parts.append(f"👍 {thread.thumbs_up}")
    if thread.thumbs_down:
        parts.append(f"👎 {thread.thumbs_down}")
    return " · ".join(parts)


def main() -> None:
    st.set_page_config(
        page_title=setting("ANALYST_TITLE", "Ask your data"), page_icon=PARADIME_MARK, layout="centered"
    )
    st.logo(PARADIME_LOGO, size="large", link="https://www.paradime.io", icon_image=PARADIME_MARK)
    PAGES["chat"] = st.Page(chat_page, title="Ask", icon=":material/forum:", default=True)
    PAGES["history"] = st.Page(
        history_page, title="History", icon=":material/history:", url_path=HISTORY_PATH
    )
    st.navigation([PAGES["chat"], PAGES["history"]]).run()


main()
