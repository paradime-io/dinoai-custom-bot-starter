# Build your own bot on the DinoAI agent harness

The app in this repository is one bot: a chat for business questions about warehouse data. This guide shows the pattern behind it, so that you can build another one, for example a support assistant, a data-quality triage bot, an internal tool, or a command-line helper.

## The building blocks

| Block | What it is | In this repository |
| - | - | - |
| A programmable agent | A DinoAI agent with a role, a goal, a backstory, a tool allowlist, and optionally an environment and a squad | `agent/*.yml` |
| An agent session | One conversation: an agent pod, a transcript and a status | One chat thread |
| The agents API | GraphQL: start a run, send a message, read a run | `analyst/backend.py` |
| A response contract | Instructions that make the agent's reply machine-readable | `analyst/protocol.py` |
| Turn tracking | Decides which messages answer which question | `analyst/turns.py` |
| A renderer | Shows the reply to the user | `analyst/render.py`, `analyst/charts.py` |
| A squad (optional) | Agents that hand work to other agents, with a callback | `agent/analytics-feedback.yml`, `analyst/feedback.py` |

Paradime runs the agent: the pod, the model, the tools, the warehouse connection, the dbt™ project. Your bot only sends messages and reads the results. It needs no warehouse credentials.

## Step 1. Define the agent

Write the agent as YAML (`.dinoai/agents/<name>.yml` on the default branch of the workspace's repository) or in the agent builder (see [setup.md, step 3](setup.md#step-3-create-the-two-agents)).

```yaml
name: support-triage          # must match the file name
version: 1
role: >
  Support engineer who triages data questions from the support queue.
goal: >
  Answer from the governed marts, or say clearly that the data cannot answer.
backstory: >
  You read the dbt project before you query. You never change code or data.
tools:
  mode: allowlist             # without a tools block, the agent gets every tool
  list:
    - run_sql_query
    - get_mart_models
    - get_node_details
# environment: support-readonly   # an Agent environment with a read-only warehouse role
# model_family: <slug>            # a faster or stronger model preset
# agents_squad: [other-agent]     # agents this one may start with invoke_agent
```

Rules that worked for us:

- **Allow only the tools the job needs.** A bot that answers questions needs no write tools. The docs recommend an allowlist for every agent that runs unattended.
- **Use a read-only environment** for any agent that runs SQL from user input.
- **Keep the output format out of the definition.** Put "who you are" and "what you may use" in the agent. Put "how to format the reply" in the bot's first message (step 3). Then the format and its parser change together, in one place, with a version number.
- **Refer to the agent by its slug.** The API finds an agent by slug first, then by name. The slug never changes; a name can.

## Step 2. Call the API

### With the Python SDK

```bash
pip install "paradime-io>=6.8"
```

```python
import time
from paradime import Paradime

client = Paradime(
    api_endpoint="https://api.paradime.io/api/v1/<company_token>/graphql",
    api_secret="prdm_cmp_...",          # an account key with the DinoAI agent API capability
    workspace_uid="<workspace uid>",
)
agents = client.dinoai_agents

# Start a session. The message is appended to the agent's role, goal and backstory.
started = agents.trigger_run(agent="support-triage", message="How many tickets did we close last week?")
session_id = started.agent_session_id

# Read the run until it finishes. A run is QUEUED while its pod starts, then RUNNING.
# Each read also returns the agent's new steps: its tool calls and its notes.
seen = -1
while True:
    run = agents.get_run(
        agent_session_id=session_id, include_steps=True, include_tool_io=True, max_chars=200, steps_after=seen
    )
    for step in run.steps or []:
        print(step.role, step.tool_name or "", (step.tool_input or step.content or "")[:80])
        seen = max(seen, step.index)
    if run.status.value in ("COMPLETED", "FAILED", "EXPIRED", "STOPPED"):
        break
    time.sleep(2)
answer = [m.content for m in run.messages if m.role == "agent"][-1]

# A follow-up goes to the same session, so the agent keeps the context.
agents.send_message(agent_session_id=session_id, message="And the week before?")
```

For a one-shot bot (a script, a scheduled job), `agents.trigger_run_and_wait(agent=..., message=..., timeout=600, poll_interval=5)` does the waiting for you, and raises `DinoaiAgentRunFailedException` if the run fails.

### With GraphQL, from any language

Every request is a `POST` to the endpoint, with these headers:

```text
Content-Type: application/json
Authorization: Bearer <account key>
X-Paradime-Workspace: <workspace uid>
```

```graphql
mutation Start($agent: String, $message: String) {
  triggerDinoaiAgentRun(agent: $agent, message: $message) { ok agentSessionId status warning }
}

mutation FollowUp($id: String!, $message: String!) {
  sendDinoaiAgentMessage(agentSessionId: $id, message: $message) { ok status }
}

query Read($id: String!) {
  dinoaiAgentRun(agentSessionId: $id) {
    status
    messages { ts role content }
    childSessionIds
  }
}
```

`triggerDinoaiAgentRun` also takes `modelFamily` (a model preset for this run; an unknown slug falls back to the default and returns a `warning`), `baseBranch`, and `slack: {channel, threadTs}` to post the run into a Slack thread.

What the API gives you, and what it does not:

- **Messages arrive complete.** There is no streaming and no subscription: read the run in a loop. One read takes about 0.3 seconds, so a 1-second loop is fine.
- **The transcript has the agent's messages, not yours.** Keep your own record of what you sent.
- **Tool calls are in the run's steps.** `get_run(agent_session_id=..., include_steps=True, steps_after=...)` (paradime-io 6.8 or later) returns each step of the agent: its role (`USER`, `AGENT`, `TOOL`), the tool's name, and, with `include_tool_io=True`, the tool's input and output, capped by `max_chars`. Use `steps_after` with the highest index you have seen, to get only new steps. A Paradime environment that does not have the field yet refuses the query: read the plain run there, and ask the agent to post a short line before each tool call.
- **A busy session queues messages.** A message sent while the agent works is picked up when the current turn finishes.
- **API runs are unattended.** The agent does not stop to ask questions or wait in plan mode.

## Step 3. Make the reply machine-readable

The API returns Markdown text. If your bot needs structure (a chart, a ticket, a decision), define a contract and send it in the first message of each session:

````text
Reply in Markdown. Put each chart in a fenced block with the tag `viz` that holds
one JSON object: {"type": "bar" | "line" | ..., "x": ..., "y": ..., "columns": [...], "rows": [[...]]}.
Write one final message.
````

Then parse the reply, and be lenient: repair common JSON errors, and show the raw text when a block cannot be read, instead of failing. `analyst/protocol.py` is a complete example, with a version number that you increase when the contract changes. On follow-ups, a one-line reminder of the format is enough: the session keeps the first message.

## Step 4. Track turns

A session is one growing transcript plus a status. To find the answer to the message you just sent:

1. Before you send, count the messages in the transcript (the baseline).
2. After you send, read the run in a loop. The answer is in the messages after the baseline.
3. Decide when the turn is over. For a first message, wait until the status is no longer `QUEUED` or `RUNNING`. For a follow-up, the status can still show the previous turn's `COMPLETED` right after you send. So wait until you have seen the run active again, or until the new messages have stopped for a few seconds.
4. Take the last message as the answer. Earlier messages are progress notes.
5. Save the pending turn (session ID, baseline, time sent). If your process restarts, read the run again and continue.

`analyst/turns.py` implements these rules in about 100 lines, and `tests/test_turns.py` tests them.

## Step 5. Optional: a squad with a callback

An agent can hand work to another agent. In this repository, the analyst hands a reader's report to a feedback agent, which files a Linear issue. The pattern:

1. The parent agent has `invoke_agent` (and `check_delegated_agents`) in its tools, and the child in `agents_squad`.
2. The parent calls `invoke_agent` with `callback_session_id` set to its own session ID. The child runs in its own pod, with its own session (it appears in the parent's `childSessionIds`).
3. The child calls `notify_parent_session` with its result. Put `notify_parent_session` in the child's allowlist.
4. The parent wakes up with the child's result as its next input, and replies.

The reply in step 4 answers no message from your bot. So watch the session for messages that arrive after your last turn ended, and show them. `analyst/feedback.py` does this, and also handles a callback that lands in the middle of the next question.

Tell the parent its own session ID in your message: it needs it for `callback_session_id`.

## Step 6. Before real users

- **Authentication.** Put your bot behind sign-in. The API key must stay on your server.
- **Least privilege.** A tool allowlist for every agent, and a read-only warehouse role for agents that run SQL.
- **Budget.** Set a monthly cap for background agents, and per agent if needed (Settings → Budget & Consumption → DinoAI Credits → Workspace).
- **No local links in messages.** Paradime's API firewall blocks a request body that has a link to a local address, for example `http://localhost:8501/…`. Remove the scheme, or leave such links out (`analyst/sanitize.py`).
- **Session lifetime.** The docs say an agent pod stays alive for up to 24 hours after the last message. Some runtimes use shorter limits. Plan for a follow-up that Paradime refuses: start a new session with a summary of the conversation.
- **Keep the session ID** with each conversation. With it, you can open the session in Paradime and read every tool call the agent made.

## Other ways to reach users

You do not always need your own frontend:

- **Slack.** Set `slack.channel` in the agent's YAML (or **Output** in the builder), and the agent posts its run into a Slack thread. Or pass `slack: {channel, threadTs}` when you start the run.
- **The command line.** `paradime dinoai` in the Paradime CLI starts a run and follows it in the terminal.
- **Schedules.** Bolt schedules can start an agent; see the programmable agents guides in the Paradime docs.

The pattern in this guide applies when you need your own interface: custom rendering, your own sign-in, or an app that your users already open.
