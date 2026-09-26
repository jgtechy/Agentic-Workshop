"""Live checks for the triage agent (Epic 2, stories 2.1 and 2.2): real model, real MCP tools.

Opt-in because they need network and an API key: RUN_LIVE=1 uv run pytest tests/test_agent_live.py
They read the repo's app.db (uv run python load_seed.py) and the keys in .env.
"""

import asyncio
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

from agent import triage

REPO_ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(os.environ.get("RUN_LIVE") != "1", reason="live model call; set RUN_LIVE=1")


@pytest.fixture(autouse=True)
def live_env():
    load_dotenv(REPO_ROOT / ".env")
    if not (REPO_ROOT / "app.db").exists():
        pytest.skip("app.db not loaded: uv run python load_seed.py")


def refuse_to_be_asked(request):
    raise AssertionError(f"unexpected escalation request: {request}")


@pytest.mark.parametrize(
    ("ticket_id", "category", "priority", "route"),
    [("T-1042", "billing", "P2", "billing-team"), ("T-1099", "bug", "P4", "bug-team")],
    ids=["billing", "injection-ignored"],
)
def test_live_decision(ticket_id, category, priority, route):
    decision = asyncio.run(triage(ticket_id, approve=refuse_to_be_asked))
    assert (decision["category"], decision["priority"], decision["route"]) == (category, priority, route)
    assert decision["rationale"]


def test_live_escalation_with_an_auto_approver():
    """T-1048: P1 for Hooli (Enterprise). An approver that always says yes records one escalation."""
    requests = []

    def approve(request):
        requests.append(request)
        return True

    decision = asyncio.run(triage("T-1048", approve=approve))
    assert decision["priority"] == "P1"
    assert (decision["category"], decision["route"]) == ("bug", "bug-team")
    assert len(requests) == 1 and requests[0].ticket_id == "T-1048"
