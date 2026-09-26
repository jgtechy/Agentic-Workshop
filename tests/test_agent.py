"""Tests for the triage agent (Epic 2, stories 2.1 and 2.2). Offline: no network, no API keys, no terminal."""

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

    async def ainvoke(self, _inputs, _config=None):
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


# --- Human-gated escalation (story 2.2) --------------------------------------------------------

P1_BUG = {"category": "bug", "priority": "P1", "route": "bug-team", "rationale": "Nothing saves, so P1 applies."}
REASON = "P1 outage for an Enterprise customer."


class Approver:
    """A stub approver that records every request and gives a fixed answer."""

    def __init__(self, answer: bool):
        self.answer = answer
        self.requests: list[agent.EscalationRequest] = []

    def __call__(self, request):
        self.requests.append(request)
        return self.answer


def never_asked(_request):
    raise AssertionError("the approver must not be asked")


def escalating_run(mcp_server, ticket_id, customer_id, *script, approve=None):
    """Run triage-like: build_escalating_agent on MCP tools and a scripted model, then run_with_retry."""
    model = scripted(*script)

    async def go():
        graph = agent.build_escalating_agent(model, await agent.load_mcp_tools(mcp_server))
        return await run_with_retry(graph, ticket_id, approve)

    return run(go())


def lookups(ticket_id, customer_id, prefix):
    return [
        AIMessage("", tool_calls=[call("get_ticket", {"ticket_id": ticket_id}, f"{prefix}t")]),
        AIMessage("", tool_calls=[call("get_customer_history", {"customer_id": customer_id}, f"{prefix}h")]),
    ]


def escalate(ticket_id, prefix):
    return AIMessage("", tool_calls=[call("escalate_to_human", {"ticket_id": ticket_id, "reason": REASON}, f"{prefix}e")])


def decide(decision, prefix):
    return AIMessage("", tool_calls=[call("TriageDecision", decision, f"{prefix}d")])


def spy_escalations(monkeypatch):
    """Record every real run of the escalate_to_human tool body."""
    ran = []
    original = agent.escalate_to_human.func

    def spy(ticket_id, reason):
        ran.append(ticket_id)
        return original(ticket_id, reason)

    monkeypatch.setattr(agent.escalate_to_human, "func", spy)
    return ran


def test_approve_runs_the_tool(mcp_server, monkeypatch):
    ran = spy_escalations(monkeypatch)
    approver = Approver(True)
    decision = escalating_run(
        mcp_server, "T-1048", "C-05",
        *lookups("T-1048", "C-05", "a"), escalate("T-1048", "a"), decide(P1_BUG, "a"),
        approve=approver,
    )
    assert decision == P1_BUG
    assert approver.requests == [agent.EscalationRequest("T-1048", REASON)]
    assert ran == ["T-1048"]


def test_decline_does_not_run_the_tool(mcp_server, monkeypatch):
    ran = spy_escalations(monkeypatch)
    approver = Approver(False)
    decision = escalating_run(
        mcp_server, "T-1048", "C-05",
        *lookups("T-1048", "C-05", "a"), escalate("T-1048", "a"), decide(P1_BUG, "a"),
        approve=approver,
    )
    assert decision == P1_BUG
    assert len(approver.requests) == 1
    assert ran == []


def test_decline_tells_the_model_it_was_not_escalated(mcp_server):
    model = scripted(*lookups("T-1048", "C-05", "a"), escalate("T-1048", "a"), decide(P1_BUG, "a"))

    async def go():
        graph = agent.build_escalating_agent(model, await agent.load_mcp_tools(mcp_server))
        return await agent.invoke_with_approval(graph, "T-1048", Approver(False))

    result = run(go())
    escalation = [m for m in result["messages"] if isinstance(m, ToolMessage) and m.name == "escalate_to_human"]
    assert len(escalation) == 1 and escalation[0].status == "error"
    assert agent.DECLINED_MESSAGE in escalation[0].content
    assert agent.escalation_outcome(result["messages"]) == (True, False)


def test_no_trigger_no_prompt(mcp_server, monkeypatch):
    ran = spy_escalations(monkeypatch)
    decision = escalating_run(
        mcp_server, "T-1042", "C-77",
        *lookups("T-1042", "C-77", "a"), decide(GOOD, "a"),
        approve=never_asked,
    )
    assert decision == GOOD and ran == []


def test_not_enterprise_is_refused_before_any_prompt(mcp_server, monkeypatch):
    ran = spy_escalations(monkeypatch)
    # T-1043 belongs to C-12 (Acme, Team plan)
    p1 = {"category": "bug", "priority": "P1", "route": "bug-team", "rationale": "Export is broken, so P1 applies."}
    model = scripted(*lookups("T-1043", "C-12", "a"), escalate("T-1043", "a"), decide(p1, "a"))

    async def go():
        graph = agent.build_escalating_agent(model, await agent.load_mcp_tools(mcp_server))
        return await agent.invoke_with_approval(graph, "T-1043", never_asked)

    result = run(go())
    refused = [m for m in result["messages"] if isinstance(m, ToolMessage) and m.name == "escalate_to_human"]
    assert len(refused) == 1 and refused[0].status == "error" and "Enterprise" in refused[0].content
    assert ran == []
    assert result["structured_response"].priority == "P1"


def test_escalation_before_customer_lookup_is_refused(mcp_server, monkeypatch):
    ran = spy_escalations(monkeypatch)
    model = scripted(
        AIMessage("", tool_calls=[call("get_ticket", {"ticket_id": "T-1048"}, "t")]),
        escalate("T-1048", "x"),
        AIMessage("", tool_calls=[call("get_customer_history", {"customer_id": "C-05"}, "h")]),
        escalate("T-1048", "y"),
        decide(P1_BUG, "d"),
    )
    approver = Approver(True)

    async def go():
        graph = agent.build_escalating_agent(model, await agent.load_mcp_tools(mcp_server))
        return await run_with_retry(graph, "T-1048", approver)

    assert run(go()) == P1_BUG
    assert len(approver.requests) == 1 and ran == ["T-1048"]


def test_skipped_escalation_is_retried_once(mcp_server):
    approver = Approver(True)
    decision = escalating_run(
        mcp_server, "T-1048", "C-05",
        *lookups("T-1048", "C-05", "a"), decide(P1_BUG, "a"),  # attempt 1: skips the escalation
        *lookups("T-1048", "C-05", "b"), escalate("T-1048", "b"), decide(P1_BUG, "b"),
        approve=approver,
    )
    assert decision == P1_BUG and len(approver.requests) == 1


def test_skipped_escalation_twice_stops_with_a_clear_error(mcp_server):
    with pytest.raises(TriageAgentError, match="P1 for an Enterprise customer but escalate_to_human was not called") as info:
        escalating_run(
            mcp_server, "T-1048", "C-05",
            *lookups("T-1048", "C-05", "a"), decide(P1_BUG, "a"),
            *lookups("T-1048", "C-05", "b"), decide(P1_BUG, "b"),
            approve=never_asked,
        )
    assert "\n" not in str(info.value)


def test_approved_escalation_with_a_non_p1_decision_fails(mcp_server):
    p2 = {**P1_BUG, "priority": "P2"}
    with pytest.raises(TriageAgentError, match="escalated but the final priority is P2"):
        escalating_run(
            mcp_server, "T-1048", "C-05",
            *lookups("T-1048", "C-05", "a"), escalate("T-1048", "a"), decide(p2, "a"),
            *lookups("T-1048", "C-05", "b"), escalate("T-1048", "b"), decide(p2, "b"),
            approve=Approver(True),
        )


def test_declined_escalation_with_a_non_p1_decision_is_accepted(mcp_server):
    p2 = {**P1_BUG, "priority": "P2"}
    decision = escalating_run(
        mcp_server, "T-1048", "C-05",
        *lookups("T-1048", "C-05", "a"), escalate("T-1048", "a"), decide(p2, "a"),
        approve=Approver(False),
    )
    assert decision == p2


def test_unattended_approver_never_reads_the_terminal(mcp_server, monkeypatch):
    def no_terminal(*_args):
        raise AssertionError("terminal read")

    monkeypatch.setattr("builtins.input", no_terminal)
    ran = spy_escalations(monkeypatch)
    decision = escalating_run(
        mcp_server, "T-1048", "C-05",
        *lookups("T-1048", "C-05", "a"), escalate("T-1048", "a"), decide(P1_BUG, "a"),
        approve=lambda _request: True,
    )
    assert decision == P1_BUG and ran == ["T-1048"]


def test_async_approver_is_awaited(mcp_server, monkeypatch):
    ran = spy_escalations(monkeypatch)

    async def approve(_request):
        return True

    escalating_run(
        mcp_server, "T-1048", "C-05",
        *lookups("T-1048", "C-05", "a"), escalate("T-1048", "a"), decide(P1_BUG, "a"),
        approve=approve,
    )
    assert ran == ["T-1048"]


@pytest.mark.parametrize("answer", ["yes", 1, None, "y"])
def test_only_an_explicit_true_counts_as_yes(mcp_server, monkeypatch, answer):
    ran = spy_escalations(monkeypatch)
    escalating_run(
        mcp_server, "T-1048", "C-05",
        *lookups("T-1048", "C-05", "a"), escalate("T-1048", "a"), decide(P1_BUG, "a"),
        approve=lambda _request: answer,
    )
    assert ran == []


def test_default_approver_is_the_terminal(mcp_server, monkeypatch, capsys):
    answers = iter(["y"])
    monkeypatch.setattr(agent, "_stdin_is_terminal", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    ran = spy_escalations(monkeypatch)
    escalating_run(
        mcp_server, "T-1048", "C-05",
        *lookups("T-1048", "C-05", "a"), escalate("T-1048", "a"), decide(P1_BUG, "a"),
    )
    assert ran == ["T-1048"]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Escalated T-1048 to a person." in captured.err


def test_triage_passes_the_approver_through(mcp_server, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    model = scripted(*lookups("T-1048", "C-05", "a"), escalate("T-1048", "a"), decide(P1_BUG, "a"))
    monkeypatch.setattr(agent, "build_model", lambda: model)
    real_load = agent.load_mcp_tools
    monkeypatch.setattr(agent, "load_mcp_tools", lambda: real_load(mcp_server))
    approver = Approver(True)
    assert run(triage("T-1048", approve=approver)) == P1_BUG
    assert len(approver.requests) == 1


# --- The terminal approver ---------------------------------------------------------------------


def ask_terminal(monkeypatch, capsys, *answers):
    """terminal_approver on an interactive terminal whose person types `answers` (fake input echoes no newline)."""
    monkeypatch.setattr(agent, "_stdin_is_terminal", lambda: True)
    replies = iter(answers)

    def fake_input(*_args):
        reply = next(replies)
        if reply is EOFError:
            raise EOFError
        return reply

    monkeypatch.setattr("builtins.input", fake_input)
    approved = agent.terminal_approver(agent.EscalationRequest("T-1048", "Outage.\x1b[2J"))
    return approved, capsys.readouterr()


@pytest.mark.parametrize("answer", ["y", "yes", " YES ", "Y"])
def test_terminal_yes(monkeypatch, capsys, answer):
    approved, out = ask_terminal(monkeypatch, capsys, answer)
    assert approved is True
    assert out.out == ""
    assert out.err.rstrip().endswith("Escalated T-1048 to a person.")


@pytest.mark.parametrize("answer", ["n", "no", "NO"])
def test_terminal_no(monkeypatch, capsys, answer):
    approved, out = ask_terminal(monkeypatch, capsys, answer)
    assert approved is False
    assert out.out == ""
    assert out.err.rstrip().endswith("Not escalated.")


def test_terminal_asks_again_on_other_answers(monkeypatch, capsys):
    approved, out = ask_terminal(monkeypatch, capsys, "", "maybe", "yep", "yes")
    assert approved is True
    assert out.err.count("[y/n]") == 4


def test_terminal_end_of_input_declines(monkeypatch, capsys):
    approved, out = ask_terminal(monkeypatch, capsys, "maybe", EOFError)
    assert approved is False
    assert out.err.rstrip().endswith("Not escalated.")


def test_terminal_prompt_shows_ticket_and_reason_without_control_characters(monkeypatch, capsys):
    _, out = ask_terminal(monkeypatch, capsys, "n")
    assert "T-1048" in out.err and "Agent's reason" in out.err and "Outage." in out.err
    assert "\x1b" not in out.err


# --- Escalation guard and checks, unit level -------------------------------------------------


def history_for(customer_id, plan, call_id="h1"):
    body = {"customer_id": customer_id, "name": "X", "plan": plan, "open_tickets": 0, "ticket_ids": []}
    return ToolMessage(content=json.dumps(body), name="get_customer_history", tool_call_id=call_id)


def escalate_call(ticket_id="T-1042"):
    return {"name": "escalate_to_human", "args": {"ticket_id": ticket_id, "reason": "r"}, "id": "e1"}


def test_escalation_guard_accepts_enterprise():
    assert check_tool_order(escalate_call(), [ticket_result("C-77"), history_for("C-77", "Enterprise")]) is None


@pytest.mark.parametrize(
    ("messages", "fragment"),
    [
        ([], "get_ticket"),
        ([ticket_result("C-77")], "get_customer_history"),
        ([ticket_result("C-77"), history_for("C-77", "Team")], "Enterprise"),
        ([ticket_result("C-77"), history_for("C-31", "Enterprise")], "get_customer_history"),
    ],
)
def test_escalation_guard_refuses(messages, fragment):
    reason = check_tool_order(escalate_call(), messages)
    assert reason and fragment in reason


def test_escalation_guard_refuses_another_ticket():
    reason = check_tool_order(escalate_call("T-2000"), [ticket_result("C-77"), history_for("C-77", "Enterprise")])
    assert reason and "T-2000" in reason


def escalation_message(status="success", content="Escalated T-1042 to a person."):
    return ToolMessage(content=content, name="escalate_to_human", tool_call_id="e1", status=status)


def test_stub_agent_p1_enterprise_with_escalation_is_accepted():
    decision = {**GOOD, "priority": "P1"}
    messages = [ticket_result("C-77"), history_result("C-77"), escalation_message()]
    stub = StubAgent({"structured_response": TriageDecision(**decision), "messages": messages})
    assert run(run_with_retry(stub, "T-1042", never_asked)) == decision


def test_refused_escalation_does_not_count_as_requested():
    messages = [escalation_message("error", "Refused: only tickets from Enterprise customers are escalated")]
    assert agent.escalation_outcome(messages) == (False, False)


def test_terminal_non_tty_stdin_declines_without_reading(monkeypatch, capsys):
    monkeypatch.setattr(agent, "_stdin_is_terminal", lambda: False)

    def no_read(*_args):
        raise AssertionError("input() called on a non-interactive stdin")

    monkeypatch.setattr("builtins.input", no_read)
    approved = agent.terminal_approver(agent.EscalationRequest("T-1048", "Outage."))
    out = capsys.readouterr()
    assert approved is False
    assert out.out == ""
    assert "T-1048" in out.err and "Agent's reason" in out.err
    assert out.err.strip().splitlines()[-1] == "Not escalated."


# --- P1 for a non-Enterprise customer goes through the retry loop -------------------------------

T1043_P1 = {"category": "bug", "priority": "P1", "route": "bug-team", "rationale": "Export is broken, so P1 applies."}


def test_non_enterprise_p1_with_refused_escalation_is_accepted_first_time(mcp_server, monkeypatch):
    ran = spy_escalations(monkeypatch)
    # T-1043 belongs to C-12 (Acme, Team plan); the model tries to escalate and is refused.
    decision = escalating_run(
        mcp_server, "T-1043", "C-12",
        *lookups("T-1043", "C-12", "a"), escalate("T-1043", "a"), decide(T1043_P1, "a"),
        approve=never_asked,
    )
    assert decision == T1043_P1 and ran == []


def test_non_enterprise_p1_without_escalation_is_accepted_first_time(mcp_server):
    # Only one attempt is scripted: a retry would exhaust the fake model and fail.
    decision = escalating_run(
        mcp_server, "T-1043", "C-12",
        *lookups("T-1043", "C-12", "a"), decide(T1043_P1, "a"),
        approve=never_asked,
    )
    assert decision == T1043_P1
