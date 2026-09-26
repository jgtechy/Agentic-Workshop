---
title: 'The eval run and the four code scorers'
type: 'feature'
created: '2026-09-26'
status: 'done'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: 'fb8793f03d7baba450aa44f6cd0f203a97b7d664'
context: ['{project-root}/_bmad-output/specs/spec-epic-3/SPEC.md', '{project-root}/_bmad-output/implementation-artifacts/epic-3-context.md']
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The agent from Epic 2 decides, but nothing measures how often it decides right, and its escalation gate would stop an unattended run at a yes/no prompt (SPEC CAP-1 to CAP-5, CAP-8).

**Approach:** Add `eval/run_eval.py`, which feeds all 20 rows of `eval/labelled_tickets.csv` to the Epic 2 agent through `mlflow.genai.evaluate`, with an approver that says yes to every escalation and counts it, and scores each ticket with four local scorers: `valid_schema`, `category_match`, `priority_match`, `tool_order`.

## Boundaries & Constraints

**Always:** `mlflow.genai.evaluate`, not a hand-rolled loop. Tracking URI `sqlite:///mlflow.db`, experiment `triage-agent`, one MLflow run per invocation. Inputs are `{"ticket_id"}`; expectations are `expected_category` and `expected_priority` from the CSV. Each ticket's prediction is one trace (`@mlflow.trace` on the predict function), so the escalation resume and every tool span sit inside it. The agent is called as-is: `triage(ticket_id, approve=<auto-approver>)`; the auto-approver returns `True` and counts calls (thread-safe), so the terminal is never read. A ticket whose `triage` raises is recorded as a failed prediction (an output holding the one-line error), so the run finishes and that ticket scores 0 on `valid_schema`, `category_match` and `priority_match`. Scorers are 0/1 and local: `valid_schema` uses Epic 1's `validate_decision`; `tool_order` is 1 only when a `get_ticket` span starts before a `get_customer_history` span in the ticket's trace. `uv run python eval/run_eval.py` prints the run id and the auto-approved escalation count. Tickets run one at a time unless `MLFLOW_GENAI_EVAL_MAX_WORKERS` is set, to stay under provider rate limits.

**Never:** No edits to `agent.py`, `run_agent.py`, `triage/`, `mcp/`, `seed/`, `TRIAGE_POLICY.md` or `eval/labelled_tickets.csv`. No `rationale_judge`, no summary of scorer means, no token count, no `eval/latest_report.json` (story 3.2). No new dependencies. Tests never need network or keys.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Correct decision | output `billing`/`P2`, labels `billing`/`P2` | `valid_schema`, `category_match`, `priority_match` = 1 | N/A |
| Wrong priority | output `P3`, label `P4` | `priority_match` = 0, others unaffected | N/A |
| Invalid output | output missing a field or the error output | `valid_schema` = 0; category/priority = 0 | N/A |
| Tool order right | trace: `get_ticket` then `get_customer_history` | `tool_order` = 1 | N/A |
| Tool order wrong or missing | history first, or a span absent | `tool_order` = 0 | N/A |
| Escalation | T-1048 raises a pause | auto-approved, counted, no terminal read | N/A |
| Agent failure | `triage` raises for one ticket | run completes; that ticket scores 0 | error kept in its output |
| Whole run | 20 CSV rows | one MLflow run in `triage-agent`, 20 rows scored | N/A |

</frozen-after-approval>

## Code Map

- `agent.py` -- `async triage(ticket_id, approve=None) -> dict`; `approve(EscalationRequest) -> bool | Awaitable[bool]`, only `True` approves; raises `TriageAgentError` (one line) after its own retry. The resume loop runs inside one `triage` call. Read only.
- `triage/schema.py` -- `validate_decision`, `TriageValidationError`. Read only.
- `run_agent.py` -- reference for MLflow setup (`set_tracking_uri`, `set_experiment`, `mlflow.langchain.autolog()`) and `load_dotenv()`. Read only.
- `eval/labelled_tickets.csv` -- columns `ticket_id, expected_category, expected_priority, expected_tools, judge_notes`; 20 rows. Read only.
- MLflow 3.16: `mlflow.genai.evaluate(data, scorers, predict_fn)` (async `predict_fn` accepted); `@mlflow.genai.scorers.scorer` functions take any of `inputs, outputs, expectations, trace`; trace spans have `name`, `span_type`, `start_time_ns`.
- `eval/` has no `__init__.py`; the script puts the repo root on `sys.path` to import `agent` and `triage`. Tests import it by path (as `tests/test_load_seed.py` loads `mcp/triage_server.py`).

## Tasks & Acceptance

**Execution:**
- [x] `eval/run_eval.py` -- dataset from the CSV, auto-approver with a counter, traced predict function that catches agent errors, the four scorers, `mlflow.genai.evaluate` in the `triage-agent` experiment, and a `__main__` that loads `.env`, sets up MLflow, runs, and prints the run id and escalation count -- CAP-1 to 5, 8.
- [x] `tests/test_run_eval.py` -- offline tests for every matrix row: scorers against hand-built outputs and traces, the auto-approver count, the failure path, and one `evaluate` run with a stubbed `triage` against a `tmp_path` tracking store -- no network.

**Acceptance Criteria:**
- Given `app.db` is loaded and `PROVIDER=groq` with a Groq key, when `uv run python eval/run_eval.py` runs, then it finishes with no person present, logs one run with 20 scored rows and the four scorers, and prints the run id and an escalation count of at least 1.
- Given no network and no keys, when `uv run pytest` runs, then all tests pass, including Epics 1 and 2.

## Implementation Notes

- `run_eval()` sets `MLFLOW_GENAI_EVAL_MAX_WORKERS=1` and `MLFLOW_GENAI_EVAL_SKIP_TRACE_VALIDATION=true` only when unset. The skip stops MLflow's pre-flight check from calling the agent an extra time on the first ticket (an extra model call and a possible extra approval); the predict function is already traced, so the check has nothing to add.
- The predict function is sync (`asyncio.run(triage(...))`) under `@mlflow.trace(name="triage", span_type="AGENT")`, so the whole agent run, including escalation resumes, is one trace.
- `category_match` and `priority_match` score 0 unless the output passes `validate_decision` (matrix row "Invalid output").
- `tool_order` compares the first `get_ticket` span start with the first `get_customer_history` span start.
- Review patches (triage rows 1, 3, 9, 14): failed-prediction counter; `main()` prints `Failed predictions: <n> of <total>` and exits 1 when every prediction failed; `main()` test; worker-default tests; env vars restored after tests. The `main()` test points `TRACKING_URI` at its own `tmp_path` file, because MLflow caches one store per URI string and the relative URI reused an earlier test's store; a separate test pins the real constants.
- Post-patch: `uv run pytest -q` 143 passed, 3 skipped (twice). Live `PROVIDER=groq uv run python eval/run_eval.py </dev/null`: run `45ca7461698142b5ab1cdf896bd2cd9e`, 3 auto-approved escalations, 0 of 20 failed, exit 0, about 2.5 minutes.

## Spec Change Log

## Review Triage Log

| # | Layer | Finding | Verdict | Evidence | Route |
|---|---|---|---|---|---|
| 1 | blind+edge | A run where every prediction failed (missing key, quota, no `app.db`) prints a run id and exits 0 | medium | Every exception becomes `{"error": ...}`; nothing counts failures. Likely: Gemini's free tier (20 requests/day) cannot finish one eval. | patch |
| 2 | blind+edge | Escalation count counts approvals, so a retried ticket counts twice | false | An approved retry runs `escalate_to_human` again, so two escalations really happened; the count is truthful. | rejected |
| 3 | blind+edge+verif | `run_eval()` leaves `MLFLOW_GENAI_EVAL_*` set; the test's `delenv(raising=False)` cannot undo it | low | Leaks into later tests in the session; fix is test-side registration via `monkeypatch`. | patch (with #14) |
| 4 | blind | Test leaves the MLflow experiment and autolog state behind | low | No later test depends on it; fix adds teardown. | rejected |
| 5 | blind | `sqlite:///mlflow.db` is relative to the working directory | false | Same URI as `run_agent.py` and AGENTS.md; commands run from the repo root. | rejected |
| 6 | blind | `tool_order` only tested against fake span names | false | Verification layer ran the real agent, MCP tools and scripted model: real `get_ticket`/`get_customer_history` TOOL spans, all scorers 1. | rejected |
| 7 | blind | CSV `expected_tools` ignored | false | SPEC CAP-5 defines `tool_order` as the fixed `get_ticket` → `get_customer_history` order. | rejected |
| 8 | blind+edge | `load_dataset` does not validate rows, blanks or count | low | CSV is read-only with 20 complete rows. | rejected |
| 9 | blind+verif | `main()` (tracking store, experiment, printed lines) untested | low | Pre-verified: renaming the experiment or printing the wrong count passes the suite. | patch |
| 10 | blind | End-to-end test assumes the root span is first | low | Holds for MLflow's span ordering; cosmetic. | rejected |
| 11 | blind+edge | Lazy `agent` import in the error path could raise | false | With the default triage, `agent` was already imported to build `predict`; stubs run in a repo where it imports. | rejected |
| 12 | blind | Test module registered as `run_eval` | low | Naming only. | rejected |
| 13 | edge | Error text dropped when the message starts with a blank line | low | Unlikely; the type name remains. | rejected |
| 14 | verif | The one-worker default is never checked | low | Pre-verified: deleting the `setdefault` keeps the suite green. | patch |
| 15 | edge | `asyncio.run` fails if the worker thread has a running loop | false | MLflow runs `predict_fn` in a thread pool with no loop; the live run completed 20 tickets. | rejected |
| 16 | edge | `CancelledError` escapes `predict` | low | Nothing cancels `triage` in the eval. | rejected |
| 17 | edge | Tool spans from a failed first attempt count toward `tool_order` | false | The guard refuses an out-of-order call before it runs, so it leaves no tool span; any spans present are in order. | rejected |

## Verification

**Commands:**
- `uv run pytest -q` -- expected: all pass, offline.
- `PROVIDER=groq uv run python eval/run_eval.py` -- expected: completes unattended; prints run id and escalation count.
- Open the run in the MLflow UI (`triage-agent` → Evaluations) -- expected: 20 rows, four scorer columns, one trace per row.
