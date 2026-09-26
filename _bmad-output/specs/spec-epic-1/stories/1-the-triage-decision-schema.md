---
title: 'The triage decision schema'
type: 'feature'
created: '2026-09-26'
status: 'done'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: '4eccada5e78f9ae0a6217025868f996a1c7ffea9'
context: ['{project-root}/_bmad-output/specs/spec-epic-1/SPEC.md', '{project-root}/TRIAGE_POLICY.md']
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Nothing defines what a triage decision is, so Epic 2's agent has no structured output to return and Epic 3's `valid_schema` scorer has nothing to check against (SPEC CAP-1).

**Approach:** Add one importable Pydantic schema for a decision (category, priority, route, rationale) plus a validator that accepts a dict or JSON text and raises one clear error naming every offending field.

## Boundaries & Constraints

**Always:** Allowed values are exactly: category `billing|bug|access|performance|how-to`; priority `P1|P2|P3|P4`; route `billing-team|bug-team|access-team|performance-team|how-to-team`; `rationale` non-empty after trimming. Unknown extra fields are rejected. Public interface is `triage.schema.TriageDecision`, `triage.schema.TriageValidationError` (a `ValueError` subclass) and `triage.schema.validate_decision(payload)`, the names Epics 2 and 3 import. Field descriptions are set so the model sees them as structured-output hints. Pure Python, no network, no API keys.

**Never:** No loader, agent, MCP or eval code (story 2 and later epics). No edits to `seed/`, `TRIAGE_POLICY.md`, `mcp/triage_server.py` or `run_agent.py`. No new dependencies: `pydantic` and `pytest` are already declared.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Valid dict | `{category: billing, priority: P2, route: billing-team, rationale: "Double charge puts money at stake."}` | Returns `TriageDecision` | N/A |
| Valid JSON text | same object as a JSON string | Returns `TriageDecision` | N/A |
| Unknown value | `priority: P0` (or bad category / route) | Rejected | `TriageValidationError` message names `priority` |
| Missing field | no `route` | Rejected | message names `route` |
| Empty rationale | `rationale: "   "` | Rejected | message names `rationale` |
| Extra field | adds `confidence: 0.9` | Rejected | message names `confidence` |
| Several problems | bad priority and missing route | Rejected | one message listing both fields |
| Route/category mismatch | `category: billing, route: bug-team` | Returns `TriageDecision` (pairing not enforced) | N/A |
| Multi-sentence rationale | `"Money is at stake. Customer is angry."` | Rejected | message names `rationale` |
| Not JSON / not an object | `"not json"`, `[1, 2]` | Rejected | `TriageValidationError` saying why |

**Decisions:**
- Route and category are validated independently; the schema does not enforce `TRIAGE_POLICY.md`'s category-to-route pairing. (User, 2026-09-26)
- `rationale` must be one sentence: blank text, and text where `.`, `!` or `?` is followed by whitespace and more text, is rejected. (User, 2026-09-26)

</frozen-after-approval>

## Code Map

- `pyproject.toml` -- already depends on `pydantic>=2.8`; pytest `testpaths = ["tests"]`. Read only.
- `TRIAGE_POLICY.md` -- source of the category, priority and route values. Read only.
- `triage/` -- does not exist; new package. `run_agent.py` imports `from agent import triage` (a module named `agent`), so the package name `triage` does not collide.
- `tests/` -- does not exist; new. Repo root is not importable from `tests/` by default, so add a `conftest.py` that puts the repo root on `sys.path`.
- Downstream importers (checkpoint branches `stage-3`/`stage-4`): `agent.py` imports `TriageDecision, TriageValidationError`; `eval/run_eval.py` imports `TriageDecision`. Keep these names.

## Tasks & Acceptance

**Execution:**
- [x] `triage/__init__.py` -- create empty -- makes `triage` a package.
- [x] `triage/schema.py` -- define `Literal` types for the three value sets, `TriageDecision(BaseModel)` with `extra="forbid"`, field descriptions and the rationale validator, `TriageValidationError(ValueError)`, and `validate_decision(payload: str | bytes | dict)` that parses JSON text, rejects non-objects, and turns Pydantic errors into one message listing each `field: problem` -- the single contract Epics 2 and 3 import.
- [x] `tests/conftest.py` -- add repo root to `sys.path` -- lets tests import `triage`.
- [x] `tests/test_schema.py` -- one test per I/O matrix row -- proves CAP-1.

**Acceptance Criteria:**
- Given the repo after this story, when `uv run pytest` runs, then all schema tests pass and nothing needs a network connection or API key.
- Given `TriageDecision`, when `TriageDecision.model_json_schema()` is called, then category, priority and route appear as enums with the exact allowed values, and every field has a description.

## Implementation Notes

- Implemented inline (no subagent). Files: `triage/__init__.py` (empty), `triage/schema.py`, `tests/conftest.py`, `tests/test_schema.py`.
- `validate_decision` also accepts `bytes`; the rationale validator returns the trimmed text.
- Matrix audit: all 10 rows covered by passing tests; the JSON-schema AC is `test_json_schema_lists_allowed_values_and_descriptions`. `uv run pytest -v`: 16 passed.
- Review patches: catch any `ValueError` from `json.loads`; `frozen=True`; strip Pydantic's "Value error, " prefix; tests for bad UTF-8, `None`, oversized int, exact error reasons, trimming, immutability and message format. `uv run pytest -q`: 22 passed.

## Spec Change Log

## Review Triage Log

| # | Layer | Finding | Verdict | Evidence | Route |
|---|---|---|---|---|---|
| 1 | edge | Abbreviations ("e.g. outage") rejected as two sentences | false | Behaviour is the frozen decision; user chose it knowing it trips on abbreviations. Fix would edit the spec. | rejected |
| 2 | edge | Newline-only or non-ASCII sentence breaks pass | false | Frozen decision defines a break as `.`/`!`/`?` + whitespace + text; code matches it. | rejected |
| 3 | edge | Deeply nested JSON escapes as RecursionError | false | Ran `validate_decision('[' * 100000)`: raised `TriageValidationError`. | rejected |
| 4 | edge | 5000-digit integer escapes as plain ValueError | low | Reproduced: `ValueError` escaped. Direct fix: catch `ValueError`. | patch |
| 5 | edge | `bytearray` / non-dict Mapping rejected | low | Real, but no caller passes them and the fix adds branches. | rejected |
| 6 | edge | Fields can be reassigned after validation | low | Reproduced `decision.priority = "P0"`. One-word fix `frozen=True`. | patch |
| 7 | edge | Punctuation-only rationale (".") passes | low | Real, unlikely from a model, fix adds a guard. | rejected |
| 8 | edge | Some errors escape or lack field names | low | Escape case is #4; JSON/object errors are matrix-specified "saying why", not field errors. | patch (with #4) |
| 9 | verif | Invalid-UTF-8 bytes path untested | low | Pre-verified gap. | patch |
| 10 | verif | Rationale trimming untested | low | Pre-verified gap. | patch |
| 11 | verif | JSON vs object error reasons not distinguished in test | low | Pre-verified gap. | patch |
| 12 | blind | `TriageDecision` instance rejected by `validate_decision` | low | Reproduced; but stage-3/4 consumers use `TriageDecision` directly (`ToolStrategy`, `model_validate`), not `validate_decision`. Fix widens the interface. | rejected |
| 13 | blind | Duplicate JSON keys silently accepted | low | Real, unlikely, fix adds a parse hook. | rejected |
| 14 | blind | No rationale length limit | low | Not in the intent; fix edits the spec. Raise via `/bmad-spec` if wanted. | rejected |
| 15 | blind | Abbreviation rejection undocumented | false | Same as #1; matrix row + `test_multi_sentence_rationale` document the rule. | rejected |
| 16 | blind | Not-JSON/not-object test too loose | low | Same root cause as #11. | patch (with #11) |
| 17 | blind | Bytes error path / None untested | low | Same as #9; added `None` case too. | patch (with #9) |
| 18 | blind | Trim and trailing newline untested | low | Same as #10; test uses a trailing newline. | patch (with #10) |
| 19 | blind | Errors carry Pydantic's "Value error, " prefix | low | Reproduced `rationale: Value error, rationale must not be empty`. Direct fix: strip prefix; exact-format test added. | patch |

## Verification

**Commands:**
- `uv run pytest -q` -- expected: all tests pass.
- `uv run python -c "from triage.schema import validate_decision; print(validate_decision({'category':'billing','priority':'P2','route':'billing-team','rationale':'Money is at stake.'}))"` -- expected: prints the decision.
