# Epic 3 Context: Measure the agent

<!-- Compiled from planning artifacts. Edit freely. Regenerate with compile-epic-context if planning docs change. -->

## Goal

Epic 2 delivered a triage agent that decides, but nothing yet says how well it decides or what it costs. Epic 3 closes the loop with one command, `uv run python eval/run_eval.py`. It runs the existing agent over the 20 hand-labelled tickets in `eval/labelled_tickets.csv` through `mlflow.genai.evaluate`. Each run gets four code scorers and one independent Groq LLM judge. The command then reports the scorer means, the agent's total token spend and how many escalations were auto-approved, so attendees leave with a measured baseline. No separate planning artifacts (PRD, architecture, UX) exist, so this context comes from the epic spec, its companions and `AGENTS.md`.

## Stories

- Story 3.1: The eval run and the four code scorers
- Story 3.2: The rationale judge and the report

## Requirements & Constraints

- **One pass, one run.** The eval builds inputs from all 20 CSV rows and logs exactly one MLflow run to `sqlite:///mlflow.db` under the `triage-agent` experiment. It must finish with no person present.
- **Five scorers, each applied to every ticket:**
  - `valid_schema`: 1 if the output validates against the Epic 1 triage-decision schema, else 0.
  - `category_match` / `priority_match`: 1 if the output equals `expected_category` / `expected_priority`, else 0.
  - `tool_order`: 1 if the ticket's trace shows a `get_ticket` span starting before the `get_customer_history` span, else 0.
  - `rationale_judge`: `pass` or `fail` plus a one-line reason, judged against the row's `judge_notes`. For the reported mean, `pass` counts as 1 and `fail` as 0, so the mean is the pass rate.
- **Judge isolation.** `rationale_judge` always uses `ChatGroq` with `JUDGE_MODEL` (default `openai/gpt-oss-120b`) and `GROQ_API_KEY`, whatever the agent's `PROVIDER` is. It never reads `GEMINI_API_KEY`, so it never uses up the agent's Gemini quota.
- **Local scoring.** The four code scorers work only on schema, label and trace data. The only network calls allowed are the agent's model calls and the judge's Groq call.
- **Unattended escalation (eval only).** Every escalation raised during the eval is approved automatically. T-1044, T-1048 and T-1057 are the expected cases. The run never blocks on terminal input. The count of auto-approved escalations appears in the printed output and in the report. A normal `run_agent.py` run must still pause for a person's yes or no.
- **Report.** The script prints the mean of each of the five scorers and the agent's total tokens, read from the MLflow traces. It writes the same numbers, plus the escalation count, to `eval/latest_report.json`. That is the only new file this epic writes outside MLflow's own store.
- **Read-only:** `eval/labelled_tickets.csv`, `TRIAGE_POLICY.md`, `seed/`, and the Epic 2 agent's decision logic, prompts and policy handling. The eval calls the agent as it is.
- **Non-goals:** dashboards, CI, hosting, and tuning the agent to raise its score.

## Technical Decisions

- **Harness:** use `mlflow.genai.evaluate` with MLflow scorers. Do not write your own scoring loop. Use the tracking URI `sqlite:///mlflow.db` and the experiment `triage-agent`. Do not use LangSmith or Databricks.
- **Agent contract (built):** `agent.triage(ticket_id, approve=None)` is an **async** function that returns the decision as a plain dict with keys `category`, `priority`, `route` and `rationale`. After one internal retry it raises `TriageAgentError`, whose message is one line and safe to print. `approve` is a callback that takes an `EscalationRequest(ticket_id, reason)` and may be sync or async. Only a literal `True` approves. When `approve` is `None`, the agent falls back to the terminal prompt. Auto-approval therefore means passing an approver that returns `True` and counts its calls. It does not mean changing `agent.py`.
- **Schema (built):** `triage.schema.validate_decision(payload)` accepts a dict or JSON text. It returns a `TriageDecision` or raises `TriageValidationError`. Base `valid_schema` on this function.
- **One trace per ticket:** wrap each ticket's predict function in a single MLflow trace, for example with `mlflow.trace`. An approved escalation resumes the agent in a second invoke. Without a wrapping trace, that second invoke is autologged as a separate trace that `tool_order` can't see. `run_agent.py` shows the setup: tracking URI, experiment, `mlflow.langchain.autolog()`, and `load_dotenv()` for keys.
- **Token totals:** read the agent's token usage from the run's MLflow traces, not from a separate counter.
- **Secrets:** never print an API key. Never commit `.env` or `mlflow.db`.
- **Dependencies:** add packages with `uv add`, never pip.

## Cross-Story Dependencies

- Story 3.2 builds on 3.1. It adds the fifth scorer to the same `mlflow.genai.evaluate` call and reports on the run 3.1 produces, including the escalation count 3.1's auto-approver collects.
- Epic 3 depends on Epic 1's schema (`triage/schema.py`) and Epic 2's agent (`agent.py`), the MCP tools in `mcp/triage_server.py` and a loaded `app.db` (`uv run python load_seed.py`). Epic 3 consumes them and does not change them. If one of them needs to change, raise it rather than editing it.
