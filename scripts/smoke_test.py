"""Run one live question through the stakeholder agent, end to end.

    python scripts/smoke_test.py                  # one question: about 1 to 2 minutes
    python scripts/smoke_test.py --report         # then report a problem: creates a REAL Linear issue
    python scripts/smoke_test.py --question "How many races did Red Bull win in 2023?"

The question goes to DINOAI_AGENT with the app's own opening message, and the
answer must follow the app's response format: Markdown with at least one
```viz block that the app can draw. With --report, the script then sends a
"Report a problem" for that answer, like the app's dialog, and waits for the
analyst's acknowledgement and the Linear issue link from the feedback agent.
Close that issue in Linear afterwards.

Exit code 0 when every step passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import sys
import time

from common import live_backend, load_secrets, setting

from analyst.backend import BackendError, RunSnapshot
from analyst.feedback import FeedbackReport, answer_context, build_feedback_message, find_issue
from analyst.protocol import VizError, VizSegment, build_first_message, parse_message
from analyst.steps import describe
from analyst.turns import PendingTurn, observe, outcome, turn_steps

DEFAULT_QUESTION = "How many races did each constructor win in 2023? Show it as a bar chart."
POLL_SECONDS = 2.0


def wait(backend, turn: PendingTurn, timeout: float) -> RunSnapshot:
    """Poll until the turn ends, printing the agent's steps as they come."""
    seen: set[int] = set()
    while True:
        snapshot = backend.get(turn.session_id)
        done = observe(turn, snapshot, time.time())
        for step in turn_steps(turn, snapshot):
            if step.index not in seen:
                seen.add(step.index)
                line = describe(step)
                if line:
                    print(f"    {time.time() - turn.sent_at:5.0f}s  {line[:100]}")
        if done or time.time() - turn.sent_at > timeout:
            return snapshot
        time.sleep(POLL_SECONDS)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--question", default=DEFAULT_QUESTION)
    parser.add_argument("--report", action="store_true", help="also test Report a problem (creates a Linear issue)")
    parser.add_argument("--timeout", type=float, default=600, help="seconds to wait for each step")
    args = parser.parse_args()

    secrets = load_secrets()
    if not setting(secrets, "DINOAI_AGENT"):
        print("FAIL  DINOAI_AGENT is not set. Run scripts/check_setup.py first.")
        return 1
    try:
        backend = live_backend(secrets)
    except ValueError as exc:
        print(f"FAIL  The credentials are incomplete: {exc}")
        return 1

    # 1. The question, in a new session, with the app's response contract.
    print(f"1. Question to {backend.agent}: {args.question}")
    try:
        session_id, warning = backend.start(build_first_message(args.question, setting(secrets, "ANALYST_CONTEXT")))
    except BackendError as exc:
        print(f"FAIL  Paradime did not start the agent: {exc}")
        return 1
    print(f"   DinoAI session {session_id}{f' (Paradime: {warning})' if warning else ''}")
    turn = PendingTurn(session_id, args.question, 0, time.time(), first_turn=True)
    snapshot = wait(backend, turn, args.timeout)
    result = outcome(turn, snapshot, time.time())
    if result.error:
        print(f"FAIL  {result.error}")
        return 1
    segments = [s for content in result.answer for s in parse_message(content)]
    visuals = [s for s in segments if isinstance(s, VizSegment)]
    broken = [s for s in segments if isinstance(s, VizError)]
    print(f"   Answered in {time.time() - turn.sent_at:.0f}s: {len(visuals)} visual(s), {len(broken)} unreadable")
    for visual in visuals:
        print(f"     - {visual.spec.get('type')}: {visual.spec.get('title') or '(no title)'}")
    if not visuals or broken:
        print("FAIL  The answer does not follow the response format (see docs/troubleshooting.md).")
        return 1
    print("PASS  The agent answered in the app's format.")
    print(f"      Open it in the app: http://localhost:8501/?thread={session_id}")

    if not args.report:
        return 0

    # 2. Report a problem with that answer, as the app's dialog does.
    agent = setting(secrets, "ANALYST_FEEDBACK_AGENT", "analytics-feedback")
    print(f"\n2. Report a problem, through {backend.agent} to {agent}. This creates a REAL Linear issue.")
    history = [{"role": "user", "content": args.question}, {"role": "assistant", "answer": result.answer}]
    question, prose, visual_specs = answer_context(history, 1)
    message = build_feedback_message(
        FeedbackReport(
            category="Other",
            comment="Smoke test of the Report a problem flow (scripts/smoke_test.py). Close this issue.",
            question=question,
            answer=prose,
            visuals=visual_specs,
            session_id=session_id,
            conversation_url="",
            feedback_agent=agent,
            team_key=setting(secrets, "ANALYST_FEEDBACK_TEAM", "DNA"),
            assignee_email=setting(secrets, "ANALYST_FEEDBACK_ASSIGNEE", ""),
            reporter="smoke test",
        )
    )
    before = backend.get(session_id)
    sent_at = time.time()
    backend.send(session_id, message)
    ack_turn = PendingTurn(
        session_id, "Smoke test report", len(before.messages), sent_at, first_turn=False,
        step_baseline=max((s.index for s in before.steps), default=-1), kind="report",
    )
    snapshot = wait(backend, ack_turn, args.timeout)
    ack = outcome(ack_turn, snapshot, time.time())
    if ack.error:
        print(f"FAIL  The analyst did not acknowledge the report: {ack.error}")
        return 1
    print(f"   Acknowledged after {time.time() - sent_at:.0f}s: {ack.answer[-1][:120]}")

    # 3. The feedback agent files the issue and calls back; the analyst posts the link.
    print("3. Waiting for the Linear issue link (usually a few minutes)...")
    while time.time() - sent_at < args.timeout:
        snapshot = backend.get(session_id)
        for item in snapshot.messages[len(before.messages):]:
            issue = find_issue(item.content)
            if issue:
                print(f"   After {time.time() - sent_at:.0f}s: {item.content[:160]}")
                if snapshot.child_session_ids:
                    print(f"   Feedback agent session(s): {', '.join(snapshot.child_session_ids)}")
                print(f"PASS  Filed {issue['key']}: {issue['url']}")
                print("      Close this issue in Linear: it was a test.")
                return 0
        time.sleep(POLL_SECONDS * 2)
    print(f"FAIL  No issue link after {args.timeout:.0f}s. Read the feedback agent's session in Paradime.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
