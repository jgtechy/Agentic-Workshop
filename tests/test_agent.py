"""Tests for the triage agent (Epic 2, story 2.1). Offline: no network, no API keys."""

import asyncio
import json
import shutil
from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from pydantic import ValidationError

import agent
import load_seed
from agent import TriageAgentError, build_agent, build_model, check_tool_order, run_with_retry, triage
from triage.schema import TriageDecision, validate_decision

REPO_ROOT = Path(__file__).resolve().parent.parent

GOOD = {"category": "billing", "priority": "P2", "route": "billing-team", "rationale": "Money is at stake, so P2 applies."}
KEY_VARS = ("PROVIDER", "MODEL", "GEMINI_API_KEY", "GROQ_API_KEY")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in KEY_VARS:
        monkeypatch.delenv(name, raising=False)


def run(coro):
    return asyncio.run(coro)


# --- Provider switch and missing keys -------------------------------------------------------


def test_default_provider_is_gemini(monkeypatch):
    from langchain_google_genai import ChatGoogleGenerativeAI

    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    model = build_model()
    assert isinstance(model, ChatGoogleGenerativeAI)
    assert model.model.endswith("gemini-3.8-flash")
    assert model.google_api_key.get_secret_value() == "test-gemini-key"


def test_groq_provider(monkeypatch):
    from langchain_groq import ChatGroq

    monkeypatch.setenv("PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    model = build_model()
    assert isinstance(model, ChatGroq)
    assert model.model_name == "openai/gpt-oss-120b"
    assert model.groq_api_key.get_secret_value() == "test-groq-key"


def test_model_override(monkeypatch):
    monkeypatch.setenv("PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "k")
    monkeypatch.setenv("MODEL", "llama-test")
    assert build_model().model_name == "llama-test"


@pytest.mark.parametrize(("provider", "var"), [(None, "GEMINI_API_KEY"), ("groq", "GROQ_API_KEY")])
def test_missing_key_names_the_variable(monkeypatch, provider, var):
    if provider:
        monkeypatch.setenv("PROVIDER", provider)
    with pytest.raises(TriageAgentError, match=var):
        build_model()


def test_missing_key_stops_before_any_tool_or_model(monkeypatch):
    async def must_not_load(*_args, **_kwargs):
        raise AssertionError("tools loaded without a key")

    monkeypatch.setattr(agent, "load_mcp_tools", must_not_load)
    with pytest.raises(TriageAgentError, match="GEMINI_API_KEY"):
        run(triage("T-1042"))


def test_other_provider_key_does_not_count(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k")
    with pytest.raises(TriageAgentError, match="GEMINI_API_KEY"):
        build_model()


def test_unknown_provider(monkeypatch):
    monkeypatch.setenv("PROVIDER", "openai")
    with pytest.raises(TriageAgentError, match="PROVIDER"):
        build_model()


def test_error_message_never_contains_a_key(monkeypatch):
    monkeypatch.setenv("PROVIDER", "groq")
    monkeypatch.setenv("GEMINI_API_KEY", "secret-gemini-value")
    with pytest.raises(TriageAgentError) as info:
        build_model()
    assert "secret-gemini-value" not in str(info.value)


# --- Tool-order guard --------------------------------------------------------------------


def ticket_result(customer_id="C-77", call_id="t1", status="success"):
    body = {"ticket_id": "T-1042", "customer_id": customer_id, "created_at": "x", "text": "y"}
    return ToolMessage(
        content=[{"type": "text", "text": json.dumps(body)}], name="get_ticket", tool_call_id=call_id, status=status
    )


def history_call(customer_id):
    return {"name": "get_customer_history", "args": {"customer_id": customer_id}, "id": "h1"}


def test_guard_allows_get_ticket_any_time():
    assert check_tool_order({"name": "get_ticket", "args": {"ticket_id": "T-1"}, "id": "1"}, []) is None


def test_guard_refuses_history_before_ticket():
    reason = check_tool_order(history_call("C-77"), [HumanMessage("Triage ticket T-1042.")])
    assert reason and "get_ticket" in reason and "customer_id" in reason


def test_guard_refuses_a_different_customer_id():
    reason = check_tool_order(history_call("C-31"), [ticket_result("C-77")])
    assert reason and "C-77" in reason


def test_guard_accepts_the_returned_customer_id():
    assert check_tool_order(history_call("C-77"), [ticket_result("C-77")]) is None


@pytest.mark.parametrize("args", [{"customer_id": ["C-77"]}, {"customer_id": {"id": "C-77"}}, {"customer_id": 77}, ["C-77"], None, {}])
def test_guard_refuses_non_string_or_missing_ids(args):
    call = {"name": "get_customer_history", "args": args, "id": "h1"}
    assert check_tool_order(call, [ticket_result("C-77")]) is not None


def test_guard_ignores_failed_get_ticket():
    error = ToolMessage(content="Error executing tool get_ticket: No ticket", name="get_ticket", tool_call_id="t", status="error")
    assert check_tool_order(history_call("C-77"), [error]) is not None


# --- Retry once, fail twice ----------------------------------------------------------------


class StubAgent:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    async def ainvoke(self, _inputs):
        self.calls += 1
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def history_result(customer_id="C-77", call_id="h1"):
    body = {"customer_id": customer_id, "name": "Northwind", "plan": "Enterprise", "open_tickets": 2, "ticket_ids": []}
    return ToolMessage(content=[{"type": "text", "text": json.dumps(body)}], name="get_customer_history", tool_call_id=call_id)


def grounded_result(messages=None):
    if messages is None:
        messages = [ticket_result("C-77"), history_result("C-77")]
    return {"structured_response": TriageDecision(**GOOD), "messages": messages}


def schema_error():
    from langchain.agents.structured_output import StructuredOutputValidationError

    try:
        TriageDecision.model_validate({**GOOD, "priority": "P9"})
    except ValidationError as error:
        return StructuredOutputValidationError("TriageDecision", error, AIMessage(""))


def test_first_attempt_succeeds():
    stub = StubAgent(grounded_result())
    assert run(run_with_retry(stub, "T-1042")) == GOOD
    assert stub.calls == 1


def test_retry_once_after_a_schema_failure():
    stub = StubAgent(schema_error(), grounded_result())
    decision = run(run_with_retry(stub, "T-1042"))
    assert decision == GOOD and stub.calls == 2
    validate_decision(decision)


def test_retry_once_after_missing_structured_output():
    stub = StubAgent({"messages": []}, grounded_result())
    assert run(run_with_retry(stub, "T-1042")) == GOOD


def test_fail_twice_stops_with_a_clear_error():
    stub = StubAgent(schema_error(), schema_error())
    with pytest.raises(TriageAgentError, match="schema twice.*priority") as info:
        run(run_with_retry(stub, "T-1042"))
    assert "\n" not in str(info.value)
    assert stub.calls == 2


def test_ungrounded_once_then_grounded_is_accepted():
    stub = StubAgent(grounded_result([]), grounded_result())
    assert run(run_with_retry(stub, "T-1042")) == GOOD
    assert stub.calls == 2


@pytest.mark.parametrize(
    "messages",
    [
        [],  # no lookups at all
        [ticket_result("C-77")],  # no customer lookup
        [ticket_result("C-31"), history_result("C-77")],  # history for another customer
        [ticket_result("C-77", status="error"), history_result("C-77")],  # failed ticket lookup
    ],
)
def test_ungrounded_twice_stops_with_a_clear_error(messages):
    stub = StubAgent(grounded_result(messages), grounded_result(messages))
    with pytest.raises(TriageAgentError, match="not grounded in lookups of ticket T-1042") as info:
        run(run_with_retry(stub, "T-1042"))
    assert "\n" not in str(info.value)
    assert stub.calls == 2


def test_lookup_of_a_different_ticket_is_not_grounded():
    # ticket_result's body is ticket T-1042, so triaging T-2000 is not grounded by it
    stub = StubAgent(grounded_result(), grounded_result())
    with pytest.raises(TriageAgentError, match="T-2000"):
        run(run_with_retry(stub, "T-2000"))


def test_other_errors_are_not_retried():
    stub = StubAgent(RuntimeError("network down"), grounded_result())
    with pytest.raises(RuntimeError):
        run(run_with_retry(stub, "T-1042"))
    assert stub.calls == 1


# --- MCP tools over stdio against a tmp_path DB -----------------------------------------------


@pytest.fixture
def mcp_server(tmp_path) -> Path:
    """A copy of the MCP server whose app.db (parent.parent / app.db) is a fresh tmp_path DB."""
    server = tmp_path / "mcp" / "triage_server.py"
    server.parent.mkdir()
    shutil.copy(REPO_ROOT / "mcp" / "triage_server.py", server)
    load_seed.load_seed(tmp_path / "app.db")
    return server


def test_mcp_tools_load_over_stdio(mcp_server):
    async def go():
        tools = {t.name: t for t in await agent.load_mcp_tools(mcp_server)}
        assert set(tools) == {"get_ticket", "get_customer_history"}
        ticket = await tools["get_ticket"].ainvoke(
            {"type": "tool_call", "id": "1", "name": "get_ticket", "args": {"ticket_id": "T-1042"}}
        )
        return ticket

    ticket = run(go())
    assert agent.ticket_customer_ids([ticket]) == {"C-77"}


# --- One full run with a scripted fake chat model ---------------------------------------------


class ScriptedChatModel(GenericFakeChatModel):
    """Replays scripted AI messages; tool binding is a no-op."""

    def bind_tools(self, tools, **kwargs):
        return self


def scripted(*messages):
    return ScriptedChatModel(messages=iter(messages))


def call(name, args, call_id):
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def test_full_triage_run_with_a_fake_model(mcp_server, monkeypatch):
    model = scripted(
        AIMessage("", tool_calls=[call("get_customer_history", {"customer_id": "C-77"}, "c0")]),
        AIMessage("", tool_calls=[call("get_ticket", {"ticket_id": "T-1042"}, "c1")]),
        AIMessage("", tool_calls=[call("get_customer_history", {"customer_id": "C-77"}, "c2")]),
        AIMessage("", tool_calls=[call("TriageDecision", GOOD, "c3")]),
    )

    async def go():
        tools = await agent.load_mcp_tools(mcp_server)
        graph = build_agent(model, tools)
        result = await graph.ainvoke({"messages": [{"role": "user", "content": "Triage ticket T-1042."}]})
        grounded_model = scripted(
            AIMessage("", tool_calls=[call("get_ticket", {"ticket_id": "T-1042"}, "d1")]),
            AIMessage("", tool_calls=[call("get_customer_history", {"customer_id": "C-77"}, "d2")]),
            AIMessage("", tool_calls=[call("TriageDecision", GOOD, "d3")]),
        )
        return result, await run_with_retry(build_agent(grounded_model, tools), "T-1042")

    result, decision = run(go())
    tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    refused, ticket, history = tool_messages[:3]
    assert refused.status == "error" and "get_ticket" in refused.content
    assert ticket.name == "get_ticket" and ticket.status != "error"
    assert history.name == "get_customer_history" and history.status != "error"
    assert "Northwind" in str(history.content)
    assert result["structured_response"] == TriageDecision(**GOOD)
    assert decision == GOOD


def test_full_run_without_lookups_is_not_grounded(mcp_server):
    model = scripted(
        AIMessage("", tool_calls=[call("TriageDecision", GOOD, "a")]),
        AIMessage("", tool_calls=[call("TriageDecision", GOOD, "b")]),
    )

    async def go():
        return await run_with_retry(build_agent(model, await agent.load_mcp_tools(mcp_server)), "T-1042")

    with pytest.raises(TriageAgentError, match="not grounded"):
        run(go())


def test_full_run_bad_output_twice_raises(mcp_server):
    bad = {**GOOD, "rationale": "One. Two."}
    model = scripted(
        AIMessage("", tool_calls=[call("TriageDecision", bad, "a")]),
        AIMessage("", tool_calls=[call("TriageDecision", bad, "b")]),
    )

    async def go():
        return await run_with_retry(build_agent(model, await agent.load_mcp_tools(mcp_server)), "T-1042")

    with pytest.raises(TriageAgentError, match="single sentence"):
        run(go())


def test_system_prompt_is_the_policy_plus_data_warning():
    prompt = agent.system_prompt()
    assert prompt.startswith((REPO_ROOT / "TRIAGE_POLICY.md").read_text(encoding="utf-8"))
    assert "untrusted data" in prompt


def _system_prompt_for(extra_tools) -> str:
    """The system prompt a built agent actually sends, captured during one scripted run."""
    seen = []

    class Capture(AgentMiddleware):
        async def awrap_model_call(self, request, handler):
            seen.append(request.system_prompt)
            return await handler(request)

    model = scripted(AIMessage("", tool_calls=[call("TriageDecision", GOOD, "x")]))
    graph = build_agent(model, [], extra_tools=extra_tools, middleware=[Capture()])
    run(graph.ainvoke({"messages": [{"role": "user", "content": "Triage ticket T-1042."}]}))
    return seen[0]


def test_prompt_says_escalation_is_unavailable_without_the_tool():
    prompt = _system_prompt_for([])
    assert agent.NO_ESCALATION_LINE.strip() in prompt


def test_prompt_keeps_escalation_when_the_tool_is_present():
    @tool
    def escalate_to_human(reason: str) -> str:
        """Stub escalation tool."""
        return "ok"

    prompt = _system_prompt_for([escalate_to_human])
    assert agent.NO_ESCALATION_LINE.strip() not in prompt
    assert prompt.startswith((REPO_ROOT / "TRIAGE_POLICY.md").read_text(encoding="utf-8"))


def test_extra_tools_and_middleware_are_accepted():
    @tool
    def escalate_stub(reason: str) -> str:
        """Stub."""
        return "ok"

    fired = []

    class Marker(AgentMiddleware):
        async def abefore_model(self, state, runtime):
            fired.append("before_model")

    graph = build_agent(
        scripted(AIMessage("", tool_calls=[call("TriageDecision", GOOD, "x")])),
        [],
        extra_tools=[escalate_stub],
        middleware=[Marker()],
    )
    assert "escalate_stub" in graph.nodes["tools"].bound.tools_by_name
    result = run(graph.ainvoke({"messages": [{"role": "user", "content": "Triage ticket T-1042."}]}))
    assert fired == ["before_model"]
    assert result["structured_response"] == TriageDecision(**GOOD)
