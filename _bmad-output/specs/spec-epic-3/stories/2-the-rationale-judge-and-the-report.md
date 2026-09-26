---
title: 'The rationale judge and the report'
type: 'feature'
created: '2026-09-26'
status: 'done'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: '775777dfca3ffd5f64df0f283160c4658664eee9'
context: ['{project-root}/_bmad-output/specs/spec-epic-3/SPEC.md', '{project-root}/_bmad-output/implementation-artifacts/epic-3-context.md', '{project-root}/_bmad-output/specs/spec-epic-3/stories/1-the-eval-run-and-the-four-code-scorers.md']
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The eval scores decisions four ways in code, but nothing judges whether each rationale is sound, and the numbers (scorer means, tokens, escalations) are visible only in the MLflow UI (SPEC CAP-6, CAP-7, CAP-8).

**Approach:** Add a `rationale_judge` scorer to `eval/run_eval.py` that asks a Groq model, through `ChatGroq`, whether each ticket's rationale is sound given that ticket's `judge_notes`, then print a summary of the run and write the same numbers to `eval/latest_report.json`.

## Boundaries & Constraints

**Always:** The judge uses `ChatGroq` with `JUDGE_MODEL` (default `openai/gpt-oss-120b`) and `GROQ_API_KEY`, whatever `PROVIDER` says, and never reads `GEMINI_API_KEY`. For each ticket it returns `pass` or `fail` with a one-line reason. It sees the decision (category, priority, rationale) and the ticket's `judge_notes`, not the ticket text, which is untrusted. A failed prediction is judged `fail` without calling Groq. A judge call that errors is `fail` with a reason starting `Judge error:`, so every ticket gets a verdict. `GROQ_API_KEY` missing stops the script before any ticket runs, with a one-line error. The report holds: run id, the mean of each of the five scorers (`rationale_judge` as its pass rate), the agent's input, output and total tokens summed from the run's prediction traces (judge tokens excluded), the auto-approved escalation count, and failed predictions out of total. The script prints those numbers and writes the same values to `eval/latest_report.json` (already git-ignored). Story 3.1's behaviour (unattended run, exit 1 when every prediction failed) stays.

**Never:** No edits to `agent.py`, `run_agent.py`, `triage/`, `mcp/`, `seed/`, `TRIAGE_POLICY.md` or `eval/labelled_tickets.csv`. No new dependencies. No other new files outside MLflow's store. Tests never need network or keys.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Sound rationale | decision + notes, judge says pass | `rationale_judge` = pass with a reason | N/A |
| Unsound rationale | judge says fail | fail with a reason | N/A |
| Failed prediction | output is the error | fail, Groq not called | N/A |
| Judge error | Groq call raises | fail, reason `Judge error: ...` | run continues |
| Provider switch | `PROVIDER` unset (Gemini agent) | judge still uses `ChatGroq` and `GROQ_API_KEY` | N/A |
| No Groq key | `GROQ_API_KEY` empty | nothing runs | one-line error naming `GROQ_API_KEY` |
| Report | finished run | printed numbers equal `eval/latest_report.json` | N/A |
| Tokens | judge calls traced too | total counts agent traces only | N/A |

</frozen-after-approval>

## Code Map

- `eval/run_eval.py` -- extend. Story 3.1 gives `load_dataset`, `AutoApprover`/`Counter`, `make_predict_fn` (predict traced as `triage`, error output `{"error": ...}`), four `@scorer`s, `run_eval(data, approver, triage_fn, failures)`, `main()`. `load_dataset` must also carry `judge_notes` into expectations.
- `eval/labelled_tickets.csv` -- `judge_notes` column. Read only.
- MLflow 3.16: a scorer may return `mlflow.entities.Feedback(value=..., rationale=...)`. MLflow only averages bool, number or `yes`/`no`, so compute the `rationale_judge` pass rate from the run's assessments. Trace token totals: `trace.info.token_usage` (`input_tokens`, `output_tokens`, `total_tokens`). `mlflow.search_traces(locations=[experiment_id], run_id=..., return_type="list")`.
- `langchain_groq.ChatGroq(model=..., api_key=...)` with `with_structured_output` for the pass/fail verdict.
- `tests/test_run_eval.py` -- the `cli` fixture points `TRACKING_URI` at `tmp_path` (MLflow caches one store per URI); reuse it.

## Tasks & Acceptance

**Execution:**
- [x] `eval/run_eval.py` -- `rationale_judge` scorer with an injectable judge model, Groq key check, report builder (scorer means from assessments, agent tokens from prediction traces, escalations, failures), printed summary and `eval/latest_report.json` -- CAP-6, 7, 8.
- [x] `tests/test_run_eval.py` -- offline tests for every matrix row with a fake judge model; the report test runs `main()` in `tmp_path` and compares stdout to the JSON -- no network.

**Acceptance Criteria:**
- Given `app.db` is loaded and `PROVIDER=groq` with a Groq key, when `uv run python eval/run_eval.py` runs, then it finishes unattended, prints five scorer means, agent tokens, the escalation count and failed predictions, and `eval/latest_report.json` holds the same numbers.
- Given no network and no keys, when `uv run pytest` runs, then all tests pass, including Epics 1 and 2 and story 3.1.

## Implementation Notes

- `make_rationale_judge(judge_model)` builds the scorer around any object with `invoke(messages)` returning a `JudgeVerdict` (or a dict); `make_judge_model()` is `ChatGroq(model=JUDGE_MODEL or openai/gpt-oss-120b, api_key=GROQ_API_KEY).with_structured_output(JudgeVerdict)` and reads no other variable. The prompt holds category, priority, rationale and `judge_notes` only, and tells the judge the block is data.
- A non-valid output (the failed prediction) is `fail` with `Failed prediction: <error>`, no call. Any judge exception, or an answer that is not pass/fail, is `fail` with `Judge error: <type>: <first line>`.
- `run_eval(..., judge_model=None)` adds the fifth scorer only when given a judge, so 3.1's tests keep their four-scorer run.
- `build_report` reads the run's traces with `search_traces(locations=[experiment_id], run_id=...)` and keeps only prediction traces: root span `triage` and no `mlflow.trace.sourceScorer` tag. The live run showed a judge `RunnableSequence` trace (479 tokens) linked to the eval run, so the root-span filter is load-bearing. Means are rounded to 4 places; the printed summary is built from the same dict that is written to `eval/latest_report.json`.
- `main()` exits with `Error: GROQ_API_KEY is not set; ...` right after `load_dotenv`, before MLflow setup or any ticket.
- Live `PROVIDER=groq uv run python eval/run_eval.py </dev/null`: run `ffdc748070c84586ae574d40b5d13680`, exit 0; means valid_schema/category/priority 0.9, tool_order 1.0, rationale_judge 0.8; agent tokens 61088/7937/69025; 3 auto-approved escalations; 2 of 20 failed predictions (Groq 400s from the agent on T-1045, T-1048). One judge call (T-1057) failed with a Groq tool-call validation 400 and was recorded as `Judge error:`, as specified. The printed numbers match `eval/latest_report.json`.
- `uv run pytest -q`: 152 passed, 3 skipped.
- Review patches (triage rows 1, 2, 11): `judge_errors` in the report and summary (verdict stays `fail`); `<`/`>` escaped in every value put into the judge prompt; direct `build_report` test with an untagged judge root span.
- Post-patch: `uv run pytest -q` 154 passed, 3 skipped (twice). Live with defaults: Groq's daily token limit for `openai/gpt-oss-120b` (200k TPD) was exhausted, so all 20 predictions failed with 429 and the script exited 1 as designed. Live with `MODEL=openai/gpt-oss-20b JUDGE_MODEL=openai/gpt-oss-20b`: run `3a692f3c752b4e7f8b26b9e1cf00c6e0`, means 0.9/0.9/0.9/0.9, `rationale_judge` 0.85, 0 judge errors, 65,420 agent tokens, 3 escalations, 2 of 20 failed, exit 0; the printed numbers match `eval/latest_report.json`.

## Spec Change Log

## Review Triage Log

| # | Layer | Finding | Verdict | Evidence | Route |
|---|---|---|---|---|---|
| 1 | blind | Judge errors count as plain fails, so a Groq outage looks like 20 unsound rationales | medium | The spec makes an error a `fail`; nothing counts them. The live run already had one (T-1057), and rate limits (429) hit during development. Fix keeps the verdict and adds a count. | patch |
| 2 | blind+edge+verif | The rationale is interpolated between `<decision>` tags unescaped, so text containing `</decision>` or `</notes>` escapes the data block | low | The rationale is agent output that can echo untrusted ticket text; the fix is a direct escape. | patch |
| 3 | blind | Judge uses tool-calling structured output; `json_schema` might avoid Groq 400s | low | One judge error in 20; could not be compared live (rate limited). Fix changes the call mode or adds a retry. | rejected |
| 4 | blind | No test for a non-error invalid output or for empty `judge_notes` | low | Both branches are simple; the CSV has notes on every row. | rejected |
| 5 | blind+edge | Means silently use fewer rows if a scorer or a trace is missing | low | The five scorers never raise (the judge catches everything); the live runs had 20 of 20. | rejected |
| 6 | blind | Run id is lost if `build_report` raises | low | Needs `search_traces` to fail on a local sqlite store. | rejected |
| 7 | blind | Report lacks provider, models and a timestamp | low | Not required by the spec; the run id links to MLflow, which records the rest. | rejected |
| 8 | blind | The "no ticket text" test is weak | low | The scorer never receives ticket text by construction. | rejected |
| 9 | blind | "No other new files" contradicts writing the report | false | "Other" means besides the report, as SPEC constraints say. | rejected |
| 10 | blind | `yes`/`no` values are dropped from the means | false | None of the five scorers returns `yes`/`no`. | rejected |
| 11 | verif | The root-span half of the token filter is untested for an untagged judge trace | low | Pre-verified: removing it keeps the suite green, and the live run had a 479-token untagged judge trace. | patch |
| 12 | edge | Invalid (`valid=False`) assessments counted | false | A fresh eval run has no overrides. | rejected |
| 13 | edge | `search_traces` without flush may miss traces | low | `evaluate` returns after logging; the live report had all 20. | rejected |
| 14 | edge | Empty `judge_notes` gets judged anyway | low | Read-only CSV has notes on every row. | rejected |
| 15 | edge | `load_dataset` fails on a CSV without `judge_notes` | low | The labels file is fixed and read-only. | rejected |
| 16 | edge | Report write failure prints a traceback | low | `eval/` always exists in the repo. | rejected |

## Verification

**Commands:**
- `uv run pytest -q` -- expected: all pass, offline.
- `PROVIDER=groq uv run python eval/run_eval.py` -- expected: summary printed; `cat eval/latest_report.json` matches it.
