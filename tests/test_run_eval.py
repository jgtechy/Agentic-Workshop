"""Tests for the eval harness (Epic 3, story 3.1). Offline: no network, no keys."""

import importlib.util
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import mlflow
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_EVAL_PATH = REPO_ROOT / "eval" / "run_eval.py"

_spec = importlib.util.spec_from_file_location("run_eval", RUN_EVAL_PATH)
run_eval = importlib.util.module_from_spec(_spec)
sys.modules["run_eval"] = run_eval
_spec.loader.exec_module(run_eval)

GOOD = {"category": "billing", "priority": "P2", "route": "billing-team", "rationale": "Double charge is P2."}
LABELS = {"expected_category": "billing", "expected_priority": "P2"}


def score(scorer, **kwargs):
    value = scorer(**kwargs)
    return getattr(value, "value", value)


def span(name, start):
    return SimpleNamespace(name=name, start_time_ns=start)


def trace_of(*spans):
    return SimpleNamespace(data=SimpleNamespace(spans=list(spans)))


# --- Dataset ---------------------------------------------------------------------------------


def test_dataset_has_every_csv_row_with_inputs_and_expectations():
    data = run_eval.load_dataset()
    assert len(data) == 20
    first = data[0]
    assert first == {
        "inputs": {"ticket_id": "T-1042"},
        "expectations": {"expected_category": "billing", "expected_priority": "P2"},
    }
    assert all(set(row["inputs"]) == {"ticket_id"} for row in data)


# --- Scorers ---------------------------------------------------------------------------------


def test_correct_decision_scores_one_on_schema_category_priority():
    assert score(run_eval.valid_schema, outputs=GOOD) == 1
    assert score(run_eval.category_match, outputs=GOOD, expectations=LABELS) == 1
    assert score(run_eval.priority_match, outputs=GOOD, expectations=LABELS) == 1


def test_wrong_priority_only_fails_priority_match():
    output = {**GOOD, "priority": "P3"}
    labels = {**LABELS, "expected_priority": "P4"}
    assert score(run_eval.priority_match, outputs=output, expectations=labels) == 0
    assert score(run_eval.valid_schema, outputs=output) == 1
    assert score(run_eval.category_match, outputs=output, expectations=labels) == 1


@pytest.mark.parametrize(
    "output",
    [
        {k: v for k, v in GOOD.items() if k != "rationale"},  # missing a field
        {"error": "Decision failed the triage schema twice: boom"},  # the failure output
        None,
    ],
)
def test_invalid_output_scores_zero(output):
    assert score(run_eval.valid_schema, outputs=output) == 0
    assert score(run_eval.category_match, outputs=output, expectations=LABELS) == 0
    assert score(run_eval.priority_match, outputs=output, expectations=LABELS) == 0


def test_tool_order_right():
    trace = trace_of(span("triage", 0), span("get_ticket", 10), span("get_customer_history", 20))
    assert score(run_eval.tool_order, trace=trace) == 1


@pytest.mark.parametrize(
    "spans",
    [
        [span("get_customer_history", 10), span("get_ticket", 20)],  # history first
        [span("get_ticket", 10)],  # history absent
        [span("get_customer_history", 10)],  # ticket absent
        [],
    ],
)
def test_tool_order_wrong_or_missing(spans):
    assert score(run_eval.tool_order, trace=trace_of(*spans)) == 0


def test_tool_order_without_trace_is_zero():
    assert score(run_eval.tool_order, trace=None) == 0


# --- Auto-approver and predict function ------------------------------------------------------


def test_auto_approver_approves_and_counts_across_threads():
    approver = run_eval.AutoApprover()
    threads = [threading.Thread(target=lambda: [approver(object()) for _ in range(100)]) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert approver.count == 800
    assert approver(object()) is True


def test_escalation_is_auto_approved_without_reading_the_terminal(monkeypatch):
    def no_input(*_):
        raise AssertionError("the terminal must not be read")

    monkeypatch.setattr("builtins.input", no_input)
    approver = run_eval.AutoApprover()

    async def escalating_triage(ticket_id, approve=None):
        assert approve(SimpleNamespace(ticket_id=ticket_id, reason="Enterprise P1")) is True
        return GOOD

    predict = run_eval.make_predict_fn(approver, escalating_triage)
    assert predict("T-1048") == GOOD
    assert approver.count == 1


def test_agent_failure_becomes_a_one_line_error_output():
    from agent import TriageAgentError

    async def failing_triage(ticket_id, approve=None):
        raise TriageAgentError("Decision failed the triage schema twice: bad")

    async def crashing_triage(ticket_id, approve=None):
        raise ValueError("first line\nsecond line")

    approver = run_eval.AutoApprover()
    assert run_eval.make_predict_fn(approver, failing_triage)("T-1") == {
        "error": "Decision failed the triage schema twice: bad"
    }
    assert run_eval.make_predict_fn(approver, crashing_triage)("T-1") == {"error": "ValueError: first line"}


EVAL_ENV = ("MLFLOW_GENAI_EVAL_MAX_WORKERS", "MLFLOW_GENAI_EVAL_SKIP_TRACE_VALIDATION")


def unset_eval_env(monkeypatch):
    """Unset the variables run_eval() defaults, registered so monkeypatch restores them afterwards."""
    for name in EVAL_ENV:
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)


def test_run_eval_keeps_a_preset_worker_count(monkeypatch):
    unset_eval_env(monkeypatch)
    monkeypatch.setenv("MLFLOW_GENAI_EVAL_MAX_WORKERS", "4")
    monkeypatch.setattr(mlflow.genai, "evaluate", lambda **kwargs: None)
    run_eval.run_eval([], run_eval.AutoApprover(), triage_fn=lambda *a, **k: None)
    assert os.environ["MLFLOW_GENAI_EVAL_MAX_WORKERS"] == "4"
    assert os.environ["MLFLOW_GENAI_EVAL_SKIP_TRACE_VALIDATION"] == "true"


# --- One evaluate run against a tmp tracking store -------------------------------------------


def test_evaluate_run_scores_every_row_in_one_run(tmp_path, monkeypatch):
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    monkeypatch.setenv("DO_NOT_TRACK", "true")
    unset_eval_env(monkeypatch)
    previous_uri = mlflow.get_tracking_uri()
    mlflow.set_tracking_uri(f"sqlite:///{tmp_path / 'mlflow.db'}")
    try:
        experiment = mlflow.set_experiment("triage-agent")
        data = run_eval.load_dataset()
        calls = []

        async def stub_triage(ticket_id, approve=None):
            calls.append(ticket_id)
            if ticket_id == "T-1043":
                raise RuntimeError("model unavailable")
            with mlflow.start_span(name="get_ticket", span_type="TOOL"):
                pass
            with mlflow.start_span(name="get_customer_history", span_type="TOOL"):
                pass
            if ticket_id == "T-1048":
                approve(SimpleNamespace(ticket_id=ticket_id, reason="Enterprise P1"))
            row = next(r for r in data if r["inputs"]["ticket_id"] == ticket_id)
            category = row["expectations"]["expected_category"]
            return {
                "category": category,
                "priority": row["expectations"]["expected_priority"],
                "route": f"{category}-team",
                "rationale": "Stubbed decision.",
            }

        approver, failures = run_eval.AutoApprover(), run_eval.Counter()
        result = run_eval.run_eval(data, approver, stub_triage, failures)

        assert approver.count == 1
        assert failures.count == 1
        assert os.environ["MLFLOW_GENAI_EVAL_MAX_WORKERS"] == "1"
        assert sorted(calls) == sorted(r["inputs"]["ticket_id"] for r in data)  # no extra pre-flight call
        runs = mlflow.search_runs(experiment_ids=[experiment.experiment_id], output_format="list")
        assert [run.info.run_id for run in runs] == [result.run_id]

        traces = mlflow.search_traces(run_id=result.run_id, return_type="list")
        assert len(traces) == 20
        scores: dict[str, dict[str, float]] = {}
        for trace in traces:
            ticket = trace.data.spans[0].inputs["ticket_id"]
            scores[ticket] = {
                a.name: a.value for a in trace.info.assessments if a.name in {s.name for s in run_eval.SCORERS}
            }
        assert all(set(s) == {"valid_schema", "category_match", "priority_match", "tool_order"} for s in scores.values())
        failed = scores.pop("T-1043")
        assert failed == {"valid_schema": 0, "category_match": 0, "priority_match": 0, "tool_order": 0}
        assert all(v == 1 for s in scores.values() for v in s.values())
    finally:
        mlflow.set_tracking_uri(previous_uri)


# --- main() ----------------------------------------------------------------------------------


@pytest.fixture
def cli(monkeypatch, tmp_path):
    """run_eval.main() on two tickets, with empty keys and mlflow.db written under tmp_path."""
    import agent

    for name in ("GEMINI_API_KEY", "GROQ_API_KEY", "PROVIDER", "MODEL"):
        monkeypatch.setenv(name, "")  # set, so load_dotenv does not fill them from .env
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    monkeypatch.setenv("DO_NOT_TRACK", "true")
    unset_eval_env(monkeypatch)
    rows = run_eval.load_dataset()[:2]
    monkeypatch.setattr(run_eval, "load_dataset", lambda: rows)
    monkeypatch.chdir(tmp_path)
    # MLflow caches one store per URI string, so the relative sqlite:///mlflow.db would reuse the
    # store an earlier test opened in its own directory. Point at this test's file explicitly.
    monkeypatch.setattr(run_eval, "TRACKING_URI", f"sqlite:///{tmp_path / 'mlflow.db'}")
    previous_uri = mlflow.get_tracking_uri()

    def use(stub):
        monkeypatch.setattr(agent, "triage", stub)
        return run_eval.main

    yield use
    mlflow.langchain.autolog(disable=True)
    mlflow.set_tracking_uri(previous_uri)


def test_tracking_store_and_experiment_constants():
    assert run_eval.TRACKING_URI == "sqlite:///mlflow.db"
    assert run_eval.EXPERIMENT == "triage-agent"


def test_main_logs_to_triage_agent_and_prints_counts(cli, tmp_path, capsys):
    async def approving_triage(ticket_id, approve=None):
        if ticket_id == "T-1042":
            approve(SimpleNamespace(ticket_id=ticket_id, reason="Enterprise P1"))
        return GOOD

    cli(approving_triage)()

    out = capsys.readouterr().out
    assert "Auto-approved escalations: 1" in out
    assert "Failed predictions: 0 of 2" in out
    mlflow.set_tracking_uri(f"sqlite:///{tmp_path / 'mlflow.db'}")
    experiment = mlflow.get_experiment_by_name("triage-agent")
    assert experiment is not None
    runs = mlflow.search_runs(experiment_ids=[experiment.experiment_id], output_format="list")
    assert len(runs) == 1
    assert f"Run id: {runs[0].info.run_id}" in out


def test_main_exits_1_when_every_prediction_fails(cli, capsys):
    async def failing_triage(ticket_id, approve=None):
        raise RuntimeError("GROQ_API_KEY is not set")

    with pytest.raises(SystemExit) as info:
        cli(failing_triage)()

    assert info.value.code == 1
    out = capsys.readouterr().out
    assert "Run id: " in out
    assert "Failed predictions: 2 of 2" in out
