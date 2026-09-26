---
title: 'Human-gated escalation'
type: 'feature'
created: '2026-09-26'
status: 'done'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: 'b353c7aafd0f8fe3b879b63165f93c06eea510a9'
context: ['{project-root}/_bmad-output/specs/spec-epic-2/SPEC.md', '{project-root}/_bmad-output/implementation-artifacts/epic-2-context.md', '{project-root}/_bmad-output/specs/spec-epic-2/stories/1-the-triage-agent.md']
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The policy says a P1 ticket from an Enterprise customer is escalated to a person, but the agent has no `escalate_to_human` tool, so story 2.1 tells the model escalation is unavailable (SPEC CAP-5).

**Approach:** Add a local `escalate_to_human` tool, gated by LangChain's `HumanInTheLoopMiddleware` so every call pauses the run; `triage` resumes it with the answer from an approver, by default a yes/no prompt in the terminal.

## Boundaries & Constraints

**Always:** Every `escalate_to_human` call interrupts before it runs; it runs only on an explicit yes, and a no leaves the run to finish its decision without escalating. `triage(ticket_id)` still returns only the `TriageDecision` dict (Epic 3 validates it against the schema); it gains an optional approver argument, a callable that gets the pending escalation and returns yes or no, so Epic 3 can auto-approve and count without touching agent logic. The default approver is the terminal prompt: `y`/`yes` approves, `n`/`no` declines, anything else asks again, and end of input (Ctrl-D or a non-interactive stdin) declines. After the answer it prints one line on stderr, `Escalated <ticket_id> to a person.` or `Not escalated.`; stdout still carries only the JSON decision. The tool is refused (as a tool error, like 2.1's guard) unless `get_customer_history` has already succeeded for an Enterprise customer. A decision that is P1 for an Enterprise customer without an `escalate_to_human` call, or an approved escalation with a final priority other than P1, is a failed attempt under 2.1's retry-once rule. The approval resume happens inside the same `run_agent.py` span, so one run is one trace.

**Never:** No edits to `triage/`, `mcp/triage_server.py`, `TRIAGE_POLICY.md`, `seed/`, `eval/`, or `run_agent.py`'s MLflow lines. No escalation without a yes. No eval code. No new dependencies. Tests never need network, keys or a real terminal.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Approve | T-1048 (P1, Hooli Enterprise), answer yes | prompt shown; tool runs; decision `bug` / `P1` / `bug-team` | N/A |
| Decline | T-1048, answer no | prompt shown; tool does not run; same decision, not escalated | N/A |
| No trigger | T-1042 (P2) | no prompt, no escalation | N/A |
| Not Enterprise | model calls the tool for a non-Enterprise customer | refused before any prompt | tool error to model |
| Skipped escalation | P1 + Enterprise decision, tool never called | failed attempt, retried once | 2.1's one-line error on the second failure |
| Unattended | approver that always says yes | no terminal read; tool runs | N/A |

</frozen-after-approval>

## Code Map

- `agent.py` -- extend. `build_agent(..., extra_tools, middleware)` already switches the no-escalation prompt line off when a tool named `escalate_to_human` is passed. `tool_order_guard` / `check_tool_order` / `_tool_result` read earlier tool results from state: reuse them for the Enterprise check. `run_with_retry` owns attempts and the grounding check: add the escalation checks there. `triage()` builds model, MCP tools and agent.
- `langchain.agents.middleware.HumanInTheLoopMiddleware(interrupt_on={"escalate_to_human": {"allowed_decisions": ["approve", "reject"]}})` -- needs a checkpointer (`langgraph.checkpoint.memory.InMemorySaver`) and a `thread_id` config. A pause returns `__interrupt__` holding `action_requests`; resume with `Command(resume={"decisions": [{"type": "approve"} | {"type": "reject", "message": ...}]})`, one decision per request.
- `run_agent.py` -- `triage` runs inside `mlflow.start_span`; keep the resume inside that call. Only the call site may change.
- `tests/test_agent.py` -- scripted fake chat model and grounded-message helpers to reuse.
- `seed/customers.csv` -- Enterprise customers: C-05, C-66, C-73, C-77, C-88, C-91. P1 + Enterprise tickets in the eval set: T-1044, T-1048, T-1057.

## Tasks & Acceptance

**Execution:**
- [x] `agent.py` -- `escalate_to_human` tool (ticket id + reason; returns a confirmation), HITL middleware + checkpointer wired into `triage`, interrupt/resume loop driven by an approver (default terminal prompt), Enterprise refusal, and the two escalation checks in the retry loop -- CAP-5.
- [x] `tests/test_agent.py` -- offline tests for every I/O matrix row with a scripted model and stub approvers, plus the terminal approver's answer handling via a patched input -- no network, no real terminal.
- [x] `tests/test_agent_live.py` -- opt-in live case: T-1048 with an auto-approver records one escalation and returns `P1`.

**Acceptance Criteria:**
- Given `app.db` is loaded and a key is set, when `uv run python run_agent.py T-1048` runs and the person answers yes, then the run completes as escalated and prints the `P1` decision; answering no completes it without escalating.
- Given the same, when `run_agent.py T-1042` runs, then no prompt appears and the output matches story 2.1.
- Given no network and no keys, when `uv run pytest` runs, then all tests pass, including stories 1.1, 1.2 and 2.1.

## Implementation Notes

- Implemented by a subagent: `agent.py` (tool, HITL gate with `when` predicate reusing the Enterprise check, `invoke_with_approval`, `terminal_approver`, two retry checks), `tests/test_agent.py` (~36 new offline tests), `tests/test_agent_live.py` (T-1048 auto-approve case). `run_agent.py` unchanged.
- Beyond the spec: the guard also requires the escalated `ticket_id` to be one looked up with `get_ticket`; only a literal `True` from the approver approves; async approvers are awaited.
- Verification: `uv run pytest -q` 120 passed, 3 skipped (live). `RUN_LIVE=1 PROVIDER=groq` 3 passed. `run_agent.py T-1048` on Groq: y → `Escalated T-1048 to a person.`, n → `Not escalated.`, closed stdin → declined; decision `bug`/`P1`/`bug-team` each time. One trace per run. Gemini not run (free-tier quota).
- Review patches (triage rows 1, 5, 9, 14, 16): non-tty stdin declines without reading; `Approver` allows async, only `True` approves; reason labelled as model-written; single-interrupt resume; non-Enterprise P1 tests. Post-patch: `uv run pytest -q` 123 passed, 3 skipped. `echo y | run_agent.py T-1048` → declined, `Not escalated.`, `P1`. Groq live: T-1042 and T-1048 pass; T-1099 returned `P3` in 3 of 3 runs (the priority variance noted in story 2.1, not escalation). A real-terminal yes was not automated (pty simulation sent end of input first, which declined, as specified); the yes path is covered offline with a patched terminal.
- Known noise: MLflow's LangChain tracer logs `Error in MlflowLangchainTracer.on_interrupt/on_resume callback` on escalating runs; harmless.

## Spec Change Log

## Review Triage Log

| # | Layer | Finding | Verdict | Evidence | Route |
|---|---|---|---|---|---|
| 1 | blind | Piped stdin (`echo y \|`) approves an escalation | medium | `terminal_approver` reads non-tty input; the frozen rule and the chosen option say a non-interactive stdin declines. Escalation without a person present. | patch |
| 2 | blind+edge | A retry after an approved escalation asks again and may escalate twice | low | Real, but needs an approved attempt that then fails; the person is still asked each time. Design Notes accept re-asking. Fix carries state across attempts. | rejected |
| 3 | blind+edge | No limit of one escalation per run (sequential or parallel calls) | low | Model is told the outcome; live runs escalated once. Each call still needs a yes. Fix adds guards. | rejected |
| 4 | edge | Unbounded interrupt/resume loop after repeated declines | false | Every resume is a graph step; LangGraph's recursion limit ends the run, and `run_agent.py` prints one line. | rejected |
| 5 | blind | `Approver` alias says `bool` but async and non-`True` answers are handled | low | Epic 3 writes approvers against it; fix is a type-hint and docstring correction. | patch |
| 6 | blind+edge | Decline detected by matching `DECLINED_MESSAGE` text | low | Works with the locked LangChain version; only a future upgrade could break it. Fix restructures outcome tracking. | rejected |
| 7 | blind+edge | Escalation of another looked-up ticket counts for the triaged one | low | Needs an injected second `get_ticket`; the person sees the ticket id before approving. Fix threads the ticket id into the guard. | rejected |
| 8 | blind | Prompt tail has no explicit escalation step | low | Policy and tool docstring say it; live T-1048 escalated in 3 of 3 runs. | rejected |
| 9 | blind | Model-written `reason` shown to the person unlabelled | low | Reason can be shaped by untrusted ticket text; fix is a label in the prompt text. | patch |
| 10 | blind+edge | Ctrl-C at the prompt prints a traceback | false | Ctrl-C is an abort, not an answer; the spec defines Ctrl-D (end of input) as the decline. | rejected |
| 11 | blind | One-trace-per-run untested; MLflow tracer callback noise | low | Checked by hand in a live trace; the noise comes from MLflow's tracer, outside this story. | rejected |
| 12 | blind | Extra behaviour not recorded in the spec | low | Fix edits this spec. Recorded in Implementation Notes. | rejected |
| 13 | blind | Some tests check less than their names say | low | Stub-approver decline test is not expected to print; the non-tty gap is covered by #1. | rejected |
| 14 | verif | P1 for a non-Enterprise customer never goes through `run_with_retry` | low | Pre-verified: dropping the Enterprise condition keeps the suite green. | patch |
| 15 | verif | Several escalation requests in one pause untested | low | Pre-verified gap; needs parallel escalation calls, which the policy makes unlikely. Filed disposition: defer. | defer |
| 16 | verif | `len(interrupts) > 1` branch unreachable and untested | low | Installed HITL raises one interrupt holding every action request. Fix is a deletion. | patch |
| 17 | edge | `langgraph` imported but not declared | false | `langchain>=1.0` depends on `langgraph`, pinned in `uv.lock`; the spec forbids new dependencies. | rejected |
| 18 | edge | A custom approver prints no outcome line | false | The spec gives the line to the terminal prompt only; Epic 3 counts through its own approver. | rejected |
| 19 | edge | Extra blank lines on stderr at end of input | low | Cosmetic; the non-tty path changes with #1. | rejected |

## Design Notes

The approver keeps the gate testable and lets Epic 3 run unattended: `triage(ticket_id, approve=None)`; `approve(request) -> bool`, where `request` carries the tool call's ticket id and reason. A retry is a fresh attempt, so the person may be asked again; acceptable because retries are rare.

## Verification

**Commands:**
- `uv run pytest -q` -- expected: all pass, offline.
- `RUN_LIVE=1 PROVIDER=groq uv run pytest tests/test_agent_live.py` -- expected: pass.
- `PROVIDER=groq uv run python run_agent.py T-1048` -- expected: yes/no prompt; answer yes, then no, and compare.
