"""Evaluate the triage agent over every labelled ticket (Epic 3, story 3.1).

Usage: uv run python eval/run_eval.py

Runs the Epic 2 agent on each row of eval/labelled_tickets.csv through
`mlflow.genai.evaluate`, approving every escalation automatically, and scores each ticket
with four local 0/1 scorers: valid_schema, category_match, priority_match and tool_order.
Logs one MLflow run to the `triage-agent` experiment in sqlite:///mlflow.db and prints the
run id and how many escalations were auto-approved.
"""

import asyncio
import csv
import os
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import mlflow
from mlflow.genai.scorers import scorer

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from triage.schema import TriageValidationError, validate_decision  # noqa: E402

LABELS_PATH = REPO_ROOT / "eval" / "labelled_tickets.csv"
TRACKING_URI = "sqlite:///mlflow.db"
EXPERIMENT = "triage-agent"
MAX_WORKERS_VAR = "MLFLOW_GENAI_EVAL_MAX_WORKERS"
# MLflow's pre-flight check would call the agent an extra time on the first ticket (a model
# call and, for an escalating ticket, an extra approval). The predict function is already
# traced, so the check has nothing to find.
SKIP_TRACE_CHECK_VAR = "MLFLOW_GENAI_EVAL_SKIP_TRACE_VALIDATION"

TICKET_SPAN = "get_ticket"
HISTORY_SPAN = "get_customer_history"


# --- Dataset ---------------------------------------------------------------------------------


def load_dataset(path: Path = LABELS_PATH) -> list[dict]:
    """One eval row per CSV row: inputs {ticket_id}, expectations {expected_category, expected_priority}."""
    with Path(path).open(newline="", encoding="utf-8") as handle:
        return [
            {
                "inputs": {"ticket_id": row["ticket_id"].strip()},
                "expectations": {
                    "expected_category": row["expected_category"].strip(),
                    "expected_priority": row["expected_priority"].strip(),
                },
            }
            for row in csv.DictReader(handle)
        ]


# --- Unattended escalation -------------------------------------------------------------------


class Counter:
    """A thread-safe counter."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._count = 0

    def increment(self) -> None:
        with self._lock:
            self._count += 1

    @property
    def count(self) -> int:
        with self._lock:
            return self._count


class AutoApprover(Counter):
    """Approves every escalation without reading the terminal, and counts them (thread-safe)."""

    def __call__(self, request: Any) -> bool:
        self.increment()
        return True


# --- Prediction ------------------------------------------------------------------------------


def _one_line_error(error: Exception) -> str:
    from agent import TriageAgentError

    if isinstance(error, TriageAgentError):
        return " ".join(str(error).split()) or type(error).__name__
    first_line = next(iter(str(error).splitlines()), "").strip()
    return f"{type(error).__name__}: {first_line}" if first_line else type(error).__name__


def make_predict_fn(
    approve: Callable[[Any], bool], triage_fn: Callable | None = None, failures: Counter | None = None
) -> Callable:
    """A traced predict function: one trace per ticket, holding every agent call and tool span.

    A ticket whose triage raises returns {"error": <one line>} so the run still finishes,
    and is counted in `failures`."""
    if triage_fn is None:
        from agent import triage as triage_fn

    @mlflow.trace(name="triage", span_type="AGENT")
    def predict(ticket_id: str) -> dict:
        try:
            return asyncio.run(triage_fn(ticket_id, approve=approve))
        except Exception as error:  # a failed prediction is scored, not fatal
            if failures is not None:
                failures.increment()
            return {"error": _one_line_error(error)}

    return predict


# --- Scorers ---------------------------------------------------------------------------------


def _valid(outputs: Any) -> dict | None:
    """The validated decision as a dict, or None when the output breaks the schema."""
    try:
        return validate_decision(outputs).model_dump()
    except TriageValidationError:
        return None


@scorer
def valid_schema(outputs) -> int:
    """1 when the output validates against the Epic 1 triage-decision schema."""
    return int(_valid(outputs) is not None)


@scorer
def category_match(outputs, expectations) -> int:
    """1 when a valid output's category equals expected_category."""
    decision = _valid(outputs)
    return int(decision is not None and decision["category"] == (expectations or {}).get("expected_category"))


@scorer
def priority_match(outputs, expectations) -> int:
    """1 when a valid output's priority equals expected_priority."""
    decision = _valid(outputs)
    return int(decision is not None and decision["priority"] == (expectations or {}).get("expected_priority"))


def _first_start(spans, name: str) -> int | None:
    starts = [span.start_time_ns for span in spans if span.name == name and span.start_time_ns is not None]
    return min(starts) if starts else None


@scorer
def tool_order(trace) -> int:
    """1 when the trace's first get_ticket span starts before its first get_customer_history span."""
    spans = getattr(getattr(trace, "data", None), "spans", None) or []
    ticket, history = _first_start(spans, TICKET_SPAN), _first_start(spans, HISTORY_SPAN)
    return int(ticket is not None and history is not None and ticket < history)


SCORERS = [valid_schema, category_match, priority_match, tool_order]


# --- Run -------------------------------------------------------------------------------------


def run_eval(
    data: list[dict],
    approver: AutoApprover,
    triage_fn: Callable | None = None,
    failures: Counter | None = None,
):
    """Evaluate in the current tracking store and experiment; returns MLflow's EvaluationResult."""
    os.environ.setdefault(MAX_WORKERS_VAR, "1")  # one ticket at a time unless the person says otherwise
    os.environ.setdefault(SKIP_TRACE_CHECK_VAR, "true")
    return mlflow.genai.evaluate(
        data=data,
        scorers=SCORERS,
        predict_fn=make_predict_fn(approver, triage_fn, failures),
    )


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
    mlflow.set_tracking_uri(TRACKING_URI)
    mlflow.set_experiment(EXPERIMENT)
    mlflow.langchain.autolog()

    approver, failures = AutoApprover(), Counter()
    data = load_dataset()
    result = run_eval(data, approver, failures=failures)
    print(f"Run id: {result.run_id}")
    print(f"Auto-approved escalations: {approver.count}")
    print(f"Failed predictions: {failures.count} of {len(data)}")
    if data and failures.count == len(data):
        raise SystemExit(1)  # nothing was triaged: not a finished eval


if __name__ == "__main__":
    main()
