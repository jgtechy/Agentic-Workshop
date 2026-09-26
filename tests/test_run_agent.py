"""Tests for run_agent.py's one-line error exits (Epic 2, story 2.1). Offline, no keys."""

import sys

import pytest

import agent
import run_agent


@pytest.fixture
def cli(monkeypatch, tmp_path):
    """run_agent.main() for T-1042 with empty keys and mlflow.db written under tmp_path."""
    monkeypatch.setattr(sys, "argv", ["run_agent.py", "T-1042"])
    for name in ("GEMINI_API_KEY", "GROQ_API_KEY", "PROVIDER", "MODEL"):
        monkeypatch.setenv(name, "")  # set, so load_dotenv does not fill them from .env
    monkeypatch.chdir(tmp_path)
    return run_agent.main


def exit_message(main) -> str:
    with pytest.raises(SystemExit) as info:
        main()
    message = str(info.value.code)
    assert "\n" not in message
    assert message.startswith("Error:")
    return message


def test_missing_key_exits_with_one_line(cli):
    assert "GEMINI_API_KEY" in exit_message(cli)


def test_unexpected_error_exits_with_type_and_first_line(cli, monkeypatch):
    async def boom(_ticket_id):
        raise RuntimeError("provider unavailable\nsecond line of detail")

    monkeypatch.setattr(agent, "triage", boom)
    assert exit_message(cli) == "Error: RuntimeError: provider unavailable"
