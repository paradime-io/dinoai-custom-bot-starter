# Troubleshooting

Each entry gives the symptom, the cause, and the fix. Start with `python scripts/check_setup.py`: it finds most setup problems.

## The app

### The sidebar says "Demo replay", but I want live answers

The app runs in demo mode when `ANALYST_MODE` is `demo`, or when it finds no `PARADIME_API_SECRET`.

1. Make sure `.streamlit/secrets.toml` has `PARADIME_API_ENDPOINT` and `PARADIME_API_SECRET`.
2. Make sure `ANALYST_MODE` is not set to `demo`, in the secrets file or in your shell.
3. Restart the app.

### The app answers from a workspace I did not expect

A name in `.streamlit/secrets.toml` wins over the same name in your shell, even when its value is blank. A name that is not in the file comes from the environment. If your shell exports `PARADIME_API_SECRET` (for example, for the Paradime CLI) and the secrets file does not set it, the app uses the shell's key. Run `python scripts/check_setup.py`: it shows where each value comes from. The sidebar shows the backend and the workspace.

### `AttributeError` (for example `'PendingTurn' object has no attribute 'kind'`) after a code change

Streamlit runs `app.py` again on each rerun, but it does not always reload the modules that `app.py` imports. After you change a file in `analyst/`, restart the app. Saved conversations are on disk, so a restart loses nothing: a question that was in progress continues when you open its conversation again.

### An answer never arrives, but the agent finished in Paradime

The app polls the run only while the page that sent the question is open on that conversation. If the tab navigated away or reloaded at the wrong moment, the polling stopped. Open the conversation again (from **Recent conversations**, the **History** page or its `?thread=` link). The app reads the saved turn and picks up the answer.

### "Paradime's API firewall blocked the request"

Paradime's API firewall blocks a request whose body has a link to a local address, for example `http://localhost:8501/…` or `http://127.0.0.1/…`. The app removes the `http://` from such links before it sends a message (`analyst/sanitize.py`), so this error means the firewall matched something else in the message. Rephrase the message. If it continues, ask Paradime support to allow the app, and give them the time of the request.

### A follow-up starts a new conversation with a notice

The agent pod stops after a period without activity: up to 24 hours according to the Paradime docs, and 25 minutes on some runtimes. When Paradime refuses a follow-up, the app starts a new session and gives it a summary of the conversation so far. The notice in the chat says so. This is expected.

### Answers are slow

The first question in a conversation waits for the agent pod to start (about 17 seconds). See [How it works: Where the time goes](how-it-works.md#where-the-time-goes) for the options: a faster model family (`DINOAI_MODEL_FAMILY`), more context in `ANALYST_CONTEXT`, or a warm pod.

### The status box shows only a timer, not the agent's tool calls

Your Paradime environment does not serve the `steps` field of `dinoaiAgentRun` yet: `python scripts/check_setup.py` says so. Until it does, the app falls back to the agent's own progress messages, which the agent does not always post. When a Paradime release adds the field to your environment, restart the app: it then lists each tool call.

### An answer shows the raw JSON of a chart with a warning

The agent wrote a `viz` block that the parser cannot read, even after it repaired common JSON errors. The rest of the answer still shows. If it happens often, make the contract stricter in `analyst/protocol.py`, or pin a stronger model family.

## The agents

### "Agent not found", or the app ignores my agent

The API finds an agent by its slug first, then by its name. Use the slug: it never changes, and it is unique. An agent that you create in the builder gets a generated slug: the first letters of its name and a random suffix, for example `stakeholder-5029ee`. The agent builder shows it in the page URL: `…/agent/builder/edit/<slug>?uuid=…`. Put that slug in `DINOAI_AGENT` (and the feedback agent's slug in `ANALYST_FEEDBACK_AGENT`).

### The agent does not follow the response format

The app sends the format in the first message of each session. If the agent ignores it:

1. Make sure `DINOAI_AGENT` points to the analyst agent, not to the workspace's default agent.
2. Check that the agent's backstory does not ask for another output format.
3. Run `python scripts/smoke_test.py`: it checks the format of one answer.

## Report a problem

### The Report a problem button is disabled

In live mode, the button needs `DINOAI_AGENT`. The report goes through the analyst agent's squad, and the workspace's default agent has no squad. Set `DINOAI_AGENT` to the analyst's slug. The button is also disabled while an answer is in progress.

### The analyst says the issue "could not be filed": the feedback agent has no Linear tools

Open the feedback agent's session in Paradime. If its tools do not include `list_linear_issues` and `create_linear_issue`, the session could not see `LINEAR_API_KEY`. Paradime gives an agent the Linear tools only when the key is among the variables of the agent environment that its session runs in: the workspace's default variables, plus that environment's own. Since a Paradime release in October 2026, agent variables belong to agent environments, so a key that worked before can be missing after the release.

1. Add `LINEAR_API_KEY` on the **Agent** tab of the environment variables, for the agent environment that the feedback agent uses (see [setup.md, step 4](setup.md#step-4-connect-linear-for-the-agents)).
2. Run `python scripts/smoke_test.py --report`, or report a problem in the app again.

### The analyst acknowledges the report, but no issue link comes

Open the analyst's session in Paradime and look for a child session (the feedback agent's run). `scripts/smoke_test.py --report` prints its ID.

- **No child session.** The analyst could not start the feedback agent. Check that the analyst has the `invoke_agent` and `check_delegated_agents` tools, that its agents squad has the feedback agent, and that `ANALYST_FEEDBACK_AGENT` is the feedback agent's slug.
- **The child session failed at `create_linear_issue`** with "Missing Linear API key": set `LINEAR_API_KEY` in the workspace's agent environment variables (see [setup.md](setup.md)).
- **The child session filed the issue, but the analyst did not post it.** The callback reaches the analyst only while its pod is alive (at least 25 minutes after its last turn). Open the conversation again: if the analyst posted the link late, the app shows it.
- **The app stopped waiting after 15 minutes.** The report then says "No issue link arrived". Look in the team's Linear issues.

### The issue is created, but nobody is assigned

- `ANALYST_FEEDBACK_ASSIGNEE` is empty: the team triages the issue. This is the default.
- Your Paradime environment does not have assignee support in the agents' Linear tools yet. Before it, the tools cannot set an assignee.
- No Linear user has that email. The tool still files the issue, unassigned, and the analyst's reply says that the assignee was not found.

### The report goes to the wrong Linear team

Set `ANALYST_FEEDBACK_TEAM` to the team's key (the prefix of its issue IDs, for example `DNA` for `DNA-96`).

## Tests

### The tests use my live credentials

They do not. `tests/conftest.py` removes `PARADIME_*`, `DINOAI_*` and `ANALYST_*` from the environment and points Streamlit at an empty secrets file for each test. Streamlit copies a loaded `secrets.toml` into the environment, so without this, `AppTest` would read your real file. If you write a new `AppTest` test, create the app with the `_app()` helper in `tests/test_app.py`, which gives it its own settings.
