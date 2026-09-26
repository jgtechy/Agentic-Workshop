---
title: 'The seed loader'
type: 'feature'
created: '2026-09-26'
status: 'done'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: 'b832234b4f212f132988d58d9f6d900c7a129aab'
context: ['{project-root}/_bmad-output/specs/spec-epic-1/SPEC.md', '{project-root}/mcp/triage_server.py']
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** `mcp/triage_server.py` reads `app.db`, but nothing creates it, so the MCP tools fail with "app.db not found" and Epic 2's agent has no data (SPEC CAP-2).

**Approach:** Add `load_seed.py` that reads `seed/tickets.csv` and `seed/customers.csv` and rebuilds the `tickets` and `customers` tables in `app.db` at the repo root, in one transaction, so every run leaves the same contents.

## Boundaries & Constraints

**Always:** Columns are exactly the CSV headers, in header order: `tickets(ticket_id, customer_id, created_at, text)` and `customers(customer_id, name, plan, open_tickets)`. `open_tickets` is `INTEGER`; every other column is `TEXT`. `ticket_id` and `customer_id` are each table's primary key. Each run replaces both tables (drop, create, insert) inside one transaction, so a rerun gives identical contents and a failed run leaves the previous `app.db` untouched. `uv run python load_seed.py` with no arguments writes `app.db` at the repo root and prints the row counts. Pure standard library (`csv`, `sqlite3`), no network, no API keys.

**Never:** No edits to `seed/`, `mcp/triage_server.py`, `run_agent.py`, `TRIAGE_POLICY.md` or `triage/`. No agent, MCP tool, or eval code. No new dependencies. Never commit `app.db` (already in `.gitignore`). Other tables in an existing `app.db` are left alone.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Fresh load | no `app.db` | `app.db` created; 24 tickets, 20 customers | N/A |
| Rerun | `app.db` from a previous run | Same rows as the first run, no duplicates | N/A |
| Stale data | `app.db` whose `tickets` holds an extra row | Extra row gone; contents equal the CSVs | N/A |
| Types | customer `C-05` | `open_tickets` is the integer `4` | N/A |
| Text fidelity | ticket text with commas or quotes | Stored exactly as `csv` reads it | N/A |
| MCP read | after a load | `get_ticket("T-1042")` returns `customer_id` `C-77`; `get_customer_history("C-77")` includes `T-1042` | N/A |
| Missing seed file | `customers.csv` absent | Nothing written; existing `app.db` unchanged | Clear error naming the missing file |
| Duplicate id | two rows with one `ticket_id` | Nothing written; existing `app.db` unchanged | Error from the primary key, naming the table |

</frozen-after-approval>

## Code Map

- `seed/tickets.csv` -- 24 rows, header `ticket_id,customer_id,created_at,text`; one text is quoted and contains a comma. Read only.
- `seed/customers.csv` -- 20 rows, header `customer_id,name,plan,open_tickets`; `open_tickets` is 0–4. Every ticket's `customer_id` exists here. Read only.
- `mcp/triage_server.py` -- `DB_PATH` = repo root `app.db`; `_query` selects the exact column names above; `get_ticket` and `get_customer_history` are plain functions decorated by `FastMCP.tool()`, callable directly in tests. Read only. Load it in tests with `importlib.util.spec_from_file_location`, because the local `mcp/` folder has no `__init__.py` and must not shadow the installed `mcp` package; point it at a temp DB by setting the module's `DB_PATH`.
- `tests/conftest.py` -- already puts the repo root on `sys.path`, so tests can `import load_seed`.
- `triage/schema.py` -- story 1, unrelated; do not touch.
- `.gitignore` -- already ignores `app.db`.

## Tasks & Acceptance

**Execution:**
- [x] `load_seed.py` -- `load_seed(db_path: Path = <repo>/app.db, seed_dir: Path = <repo>/seed) -> dict[str, int]` that reads both CSVs with `csv.DictReader` (`newline=""`, UTF-8), then in one transaction drops, creates and fills both tables, and returns row counts per table; a `__main__` block calls it and prints the counts -- the one command CAP-2 names; parameters keep tests off the real `app.db`.
- [x] `tests/test_load_seed.py` -- one test per I/O matrix row, each using a `tmp_path` DB (and a `tmp_path` seed copy for the error rows) -- proves CAP-2 without touching the repo's `app.db` or `seed/`.

**Acceptance Criteria:**
- Given a clean checkout, when `uv run python load_seed.py` runs, then `app.db` exists at the repo root with `tickets` (24 rows) and `customers` (20 rows), and the command prints both counts.
- Given the repo after this story, when `uv run pytest` runs, then all tests pass, including story 1's, with no network connection or API key.

## Implementation Notes

- Implemented by a subagent. Files: `load_seed.py`, `tests/test_load_seed.py`.
- Beyond the spec: exact header check and integer check on `open_tickets` before the DB opens; a failed first load deletes the empty `app.db` it created.
- Connection uses `autocommit=False` with explicit commit/rollback instead of `with sqlite3.connect(...)`, so the drops roll back too.
- Review patches: tests for fresh-DB failure cleanup, default path matching `mcp/triage_server.py`, bad header and non-integer `open_tickets`. `uv run pytest -q`: 41 passed; `uv run python load_seed.py` twice: 24 tickets, 20 customers both times.

## Spec Change Log

## Review Triage Log

| # | Layer | Finding | Verdict | Evidence | Route |
|---|---|---|---|---|---|
| 1 | verif | Fresh-DB failure cleanup (`unlink`) untested | low | Pre-verified: replacing it with `pass` keeps all tests green. | patch |
| 2 | verif | `DEFAULT_DB_PATH` / CLI never exercised | low | Pre-verified: a wrong default passes every test; the MCP server would read a different file. | patch |
| 3 | verif | Header and integer error paths untested | low | Pre-verified gap; test-only fix is direct. | patch |
| 4 | blind+edge | Short or long CSV rows load NULLs or truncate silently | low | Real in `csv.DictReader`, but `seed/` is read-only and every row has 4 fields; fix adds guards. | rejected |
| 5 | blind | Untested error branches | low | Same as #3. | patch (with #3) |
| 6 | blind | Duplicate-id test `match="tickets"` does not pin the wrapper | false | Matrix only needs the error to name the table; sqlite's own message does. | rejected |
| 7 | blind | Fresh-DB IntegrityError path untested | low | Same as #1. | patch (with #1) |
| 8 | blind | CLI acceptance criterion untested | low | Same as #2. | patch (with #2) |
| 9 | blind | Story file untracked; notes empty | false | Story file is committed with the code; notes are filled at presentation. Fix would edit this spec. | rejected |
| 10 | blind+edge | `rollback()`/`close()`/`unlink` raising masks the original error | low | Needs a failing rollback or locked file on a local DB; unlikely; fix adds structure. | rejected |
| 11 | blind | Missing DB parent dir / BOM in CSV give unclear errors | low | Default paths always exist; seed has no BOM (read-only). Unlikely; fix adds handling. | rejected |
| 12 | blind | Returned counts come from parsed rows, not the DB | false | Every parsed row is inserted or the whole load fails; counts cannot differ. | rejected |
| 13 | edge | Negative or padded `open_tickets` accepted | low | Seed values are 0–4; unlikely; fix adds a guard. | rejected |
| 14 | edge | Blank or padded primary keys accepted | low | Profiled seed: no blanks or whitespace; unlikely; fix adds a guard. | rejected |
| 15 | edge | Orphan tickets (unknown `customer_id`) accepted | low | Profiled seed: no orphans; not in intent; fix adds a check. | rejected |
| 16 | edge | `exists()`/`connect()` race could unlink another process's DB | low | Needs a concurrent creator of `app.db`; unlikely; fix adds complexity. | rejected |

## Design Notes

Idempotency comes from rebuild, not upsert: drop and recreate both tables inside a single `with sqlite3.connect(...)` transaction. Read and validate both CSVs before opening the connection, so a missing file fails before anything is written. SQLite rolls back the whole transaction on a primary-key violation, which keeps the previous `app.db` intact. Caveat: in its default mode Python's `sqlite3` only opens a transaction before DML, so a `DROP TABLE` would autocommit on its own. Open the connection with `autocommit=False` (Python 3.12+) or issue an explicit `BEGIN`, so the drops roll back too.

## Verification

**Commands:**
- `uv run pytest -q` -- expected: all tests pass.
- `uv run python load_seed.py` -- expected: prints 24 tickets and 20 customers.
- `uv run python load_seed.py && sqlite3 app.db "SELECT count(*) FROM tickets; SELECT count(*) FROM customers;"` run twice -- expected: `24` and `20` both times.
- `git status --short` -- expected: `app.db` not listed.
