---
id: SPEC-epic-1
companions: [../../../TRIAGE_POLICY.md, ../../../mcp/triage_server.py]
sources: [../../../INTENT.md]
---

> **Canonical contract.** This SPEC and the files in `companions:` are the complete, preservation-validated contract for what to build, test, and validate. Source documents listed in frontmatter are for traceability — consult them only if you need narrative rationale or prose color this contract intentionally omits.

# Epic 1: triage data and schema

## Why

Epics 2 and 3 have nothing to stand on yet: the agent needs a fixed shape for its decision, and the MCP tools need `app.db` to read from. Epic 1 lays that foundation for the workshop's triage agent — one decision schema that Epic 2's agent returns and Epic 3's eval checks, and a loader that turns `seed/` into the database `mcp/triage_server.py` already expects.

## Capabilities

- **CAP-1**
  - **intent:** Every triage decision is checked against one schema: a category, a priority, a route and a one-sentence rationale, with anything else rejected.
  - **success:** A decision with category in `billing`, `bug`, `access`, `performance`, `how-to`; priority in `P1`–`P4`; route in `billing-team`, `bug-team`, `access-team`, `performance-team`, `how-to-team`; and a rationale validates. An unknown category, priority or route, a missing field, or an empty rationale each fails with an error naming the offending field.

- **CAP-2**
  - **intent:** One command loads the seed data into a local SQLite database, repeatably.
  - **success:** `uv run python load_seed.py` creates `app.db` with tables `tickets` and `customers` whose columns match the CSV headers, holding 24 and 20 rows. A second run leaves identical contents. `get_ticket("T-1042")` from `mcp/triage_server.py` then returns customer `C-77`.

## Constraints

- Python 3.12 or newer, managed with uv; packages added with `uv add`.
- `seed/` is read-only; the loader only reads it.
- No network calls and no API keys anywhere in this epic.
- `mcp/triage_server.py` is unchanged and must keep working: it reads `app.db` at the repo root and queries `tickets(ticket_id, customer_id, created_at, text)` and `customers(customer_id, name, plan, open_tickets)`.
- `app.db` is never committed.
- The schema is importable from Python: Epic 2's agent returns it as structured output and Epic 3's eval checks against it.

## Non-goals

- The agent, the MCP tools, evals and any user interface.

## Success signal

- After `uv run python load_seed.py`, the unchanged MCP server answers `get_ticket("T-1042")` from `app.db`, and a decision of `billing` / `P2` / `billing-team` with a rationale validates while `billing` / `P0` is rejected with a clear error.

## Assumptions

- The schema is a Pydantic model (already a dependency), so Epic 2 can pass it straight to the agent as structured output.
- Unknown extra fields are rejected, reading "anything else is rejected" literally.
- `open_tickets` is stored as an integer, since the Enterprise rule compares it with 3; every other column is text.

## Open Questions

- Must the route match the category per `TRIAGE_POLICY.md` (so `billing` + `bug-team` is rejected)? `INTENT.md` lists the values independently.
- Is "one-sentence rationale" enforced beyond non-empty, e.g. rejecting multi-sentence text?
