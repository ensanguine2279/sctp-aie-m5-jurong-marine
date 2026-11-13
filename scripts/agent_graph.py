#!/usr/bin/env python3
"""The agent: one question's turn as a LangGraph graph.

    START -> guard_in --(refused / asked back)--> END
                 |
                 v
               model --(answered / failed / too many rounds)--> guard_out -> END
                 ^  |  \__ (facts but no tool call: asked again, a tool required) -> model
                 |  v (tool calls)
               tools --(a tool asked the user something)--> guard_out

    guard_in   the guardrail on the question (injection, PDPA, out of scope), a year written
               short asked back, today's dates in the system prompt. Refused or asked back:
               straight to END, no model call
    model      one call to the model through the LiteLLM proxy, with the tool definitions;
               rounds counted, and past MAX_TOOL_ROUNDS it stops with an honest message. An
               answer with facts (a digit) but no tool call this turn is dropped and the
               model asked once more with tool_choice="required" (#1: every fact from a tool
               call made just now)
    tools      the tools the model picked, in order: the severity-scale guard (scale_guessed),
               the tool itself (agent_tools.run_tool, as agent_ro), period_check and
               completion_note. A tool that asks the user something ends the turn there
    guard_out  what the answer must say, added in code if the model left it out ("completed"
               read as CLOSED, dated when raised; "still open" or "now" on data that ends
               before today: as of that date), the guardrail on the answer (staff names,
               NRIC, phone, e-mail: replaced, in the history too) and the `turn` log line

This is the only engine (12 Nov 2026, set up as in production LangGraph apps). The nodes
hold the turn's logic; agent_app.py keeps the pieces they use (the prompt, the guards, the
date rules, the chat call) and `ask()`, a thin wrapper that runs this graph once for the
CLI, the eval suite and the tests. The API (agent_api.py) runs it for the page.

State and context, the LangGraph way:
- `AgentState` is what the graph carries and what a checkpointer would save: the
  conversation (`messages`, in the chat API's own format), the question, the round, the
  model's pending tool calls, the answer, and this turn's sources, notes and usage. Nodes
  never change the state they are given; they return what changed.
- `AgentContext` is what a run needs but must never be saved: the database connection, the
  model client, the logger, the round limit. It is passed per run (`context=`, LangGraph's
  runtime context API) and read in nodes as `runtime.context`.
- The graph is compiled once per process (`graph()`), not per question.

Usage
    uv add langgraph                             # personal repo
    pip install langgraph                        # team repo (it's in requirements-app.txt)
    python scripts/agent_app.py "what oil does CNC-03 take?"     # runs this graph
    python scripts/agent_graph.py mermaid        # the graph, as Mermaid, from LangGraph itself
"""
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parent))

from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.runtime import Runtime  # noqa: E402

import agent_app as app  # noqa: E402

NODES = ("guard_in", "model", "tools", "guard_out")


class AgentState(TypedDict, total=False):
    """What the graph carries between nodes. Every key is replaced, never merged, by the
    node that returns it (no reducers): nodes return the whole new value."""
    messages: list           # the conversation, chat-API dicts: system, user, assistant, tool
    question: str            # this turn's question, as typed
    route: str               # the next node, or "end"
    round: int               # model calls made this turn
    pending: list            # the tool calls the model just made (chat-API dicts)
    answer: Optional[str]    # this turn's answer, once there is one
    sources: list            # this turn's source records, every tool call's, in order
    tool_names: list         # the tools run this turn, in order
    notes: list              # this turn's tool notes (the caveats)
    usage: dict              # this turn's model calls, tokens and cost
    read_as_closed: bool     # "completed" was read as status CLOSED this turn
    retried: bool            # the model answered with facts and no tool call; asked again
    asked_user: bool         # this turn's answer is a tool's question back to the user
    step: dict               # what the node just did, for the log and the stream (no text)


@dataclass
class AgentContext:
    """What one run needs and the state must never hold (not saved, not copied)."""
    db: Any = None                          # agent_tools' read-only connection (agent_ro)
    client: Any = None                      # the OpenAI client pointed at the LiteLLM proxy
    model: str = app.MODEL
    logger: Any = app.log
    max_rounds: int = app.MAX_TOOL_ROUNDS
    complete_fn: Optional[Callable] = None  # the chat call; None = agent_app.complete


def _messages(state: AgentState) -> list:
    """A copy of the conversation to change: new list, new dicts (the state is never
    changed in place)."""
    return [dict(m) for m in state.get("messages") or []]


# ===================================================================== nodes ===

def guard_in(state: AgentState, runtime: Runtime[AgentContext]) -> AgentState:
    """Everything a question goes through before the model. Also starts the turn: this
    turn's sources, notes, usage and round begin empty here, whatever an earlier turn
    left in the state."""
    ctx, q = runtime.context, state.get("question") or ""
    lg, msgs = ctx.logger, _messages(state)
    turn = {"round": 0, "pending": [], "answer": None, "sources": [], "tool_names": [],
            "notes": [], "usage": app._new_usage(), "read_as_closed": False,
            "retried": False, "asked_user": False}
    refused = {"node": "guard_in", "passed": False, "answered_without_model": True}

    if not app.guardrail.names_loaded():       # fail closed: no name list, no answers
        app._emit(lg, "guard_not_ready")
        app._emit(lg, "turn", question="[withheld: guard not ready]", tools_called=[],
                  n_sources=0, refused="not_ready")
        return {**turn, "messages": msgs, "answer": app.guardrail.MESSAGES["not_ready"],
                "route": "end", "step": refused}
    verdict = app.guardrail.check_input(q)
    if not verdict.allowed:                    # only a placeholder goes into the history
        msgs.append({"role": "user",
                     "content": f"[question withheld by the guardrail: {verdict.rule}]"})
        msgs.append({"role": "assistant", "content": verdict.message})
        app._emit(lg, "input_refused", rule=verdict.rule, reason=verdict.reason)
        app._emit(lg, "turn", question=app._loggable(q, verdict.rule), tools_called=[],
                  n_sources=0, refused=verdict.rule)
        return {**turn, "messages": msgs, "answer": verdict.message, "route": "end",
                "step": refused}

    short = app.incomplete_year(q)             # "nov 202": asked, never guessed
    if short:
        reply = app.year_question(short, app.data_dates(ctx.db))
        msgs.append({"role": "user", "content": q})
        msgs.append({"role": "assistant", "content": reply})
        app._emit(lg, "year_unclear", written=short[0])
        app._emit(lg, "turn", question=q[:200], tools_called=[], n_sources=0,
                  model_calls=0, cost_usd=0.0)
        return {**turn, "messages": msgs, "answer": reply, "route": "end", "step": refused}

    if ctx.db is not None and msgs and msgs[0].get("role") == "system":
        dates = app.data_dates(ctx.db)         # what "last month" means (#5), in code
        if dates is None:
            app._emit(lg, "dates_unknown")
        msgs[0]["content"] = app.SYSTEM_PROMPT + app.dates_note(dates)
    msgs.append({"role": "user", "content": q})
    return {**turn, "messages": msgs, "route": "model",
            "step": {"node": "guard_in", "passed": True}}


def model(state: AgentState, runtime: Runtime[AgentContext]) -> AgentState:
    """One model call. The reply is appended to the conversation, logged (chat_call) and
    added to this turn's usage; tool calls go to `tools`, a plain answer to `guard_out`."""
    ctx = runtime.context
    n = int(state.get("round") or 0) + 1
    if n > ctx.max_rounds:                     # stops a loop, never a crash (#4)
        app._emit(ctx.logger, "max_rounds_exceeded", rounds=ctx.max_rounds)
        return {"answer": app.ROUNDS_EXHAUSTED, "route": "guard_out",
                "step": {"node": "model", "round": n - 1, "stopped": "max_rounds"}}
    complete_fn = ctx.complete_fn or app.complete   # looked up per call, so a test's
    msgs = _messages(state)                         # monkeypatched app.complete is used
    # Asked again after answering with facts and no tool call: this time it must call one.
    forced = bool(state.get("retried")) and not state.get("tool_names")
    t0 = time.monotonic()
    try:
        if forced:
            resp = complete_fn(ctx.client, ctx.model, msgs, app.at.tool_schemas(),
                               tool_choice="required")
        else:
            resp = complete_fn(ctx.client, ctx.model, msgs, app.at.tool_schemas())
    except app.ChatError as e:                 # budget cap, network, timeout: said plainly
        app._emit(ctx.logger, "chat_error", round=n, model=ctx.model, error=str(e))
        return {"answer": app.CHAT_FAILED, "route": "guard_out", "round": n,
                "step": {"node": "model", "round": n, "failed": True}}

    msg = resp.choices[0].message
    said = app._assistant_message(msg)
    calls = said.get("tool_calls") or []
    cost = getattr(resp, "cost_usd", None)
    u = getattr(resp, "usage", None)
    pt, ct = getattr(u, "prompt_tokens", None), getattr(u, "completion_tokens", None)
    app._emit(ctx.logger, "chat_call", round=n, model=ctx.model, prompt_tokens=pt,
              completion_tokens=ct, tool_calls=[c["function"]["name"] for c in calls],
              cost_usd=cost, ms=round((time.monotonic() - t0) * 1000, 1),
              **({"tool_choice": "required"} if forced else {}))
    usage = dict(state.get("usage") or app._new_usage())
    usage["model_calls"] += 1
    usage["prompt_tokens"] += pt or 0
    usage["completion_tokens"] += ct or 0
    if cost is None:
        usage["cost_complete"] = False         # a call the proxy didn't price
    else:
        usage["cost_usd"] += cost
    step = {"node": "model", "round": n, "tool_calls": [c["function"]["name"] for c in calls]}
    if forced:
        step["forced"] = True
    if not calls and not state.get("tool_names") and app.states_facts(msg.content):
        if not state.get("retried"):           # facts, but no tool this turn (#1): its
            app._emit(ctx.logger, "ungrounded_answer_retried", round=n)   # answer is dropped
            return {"usage": usage, "round": n, "retried": True, "route": "model",
                    "step": {**step, "retry": "facts without a tool call"}}
        app._emit(ctx.logger, "ungrounded_answer", round=n)   # still none: kept, flagged
    msgs.append(said)
    if not calls:
        return {"messages": msgs, "usage": usage, "round": n, "answer": msg.content,
                "pending": [], "route": "guard_out", "step": step}
    return {"messages": msgs, "usage": usage, "round": n, "pending": calls, "route": "tools",
            "step": step}


def tools(state: AgentState, runtime: Runtime[AgentContext]) -> AgentState:
    """Run every tool call the model just made, in order, one tool message each. A tool
    that asks for a clarification (CLARIFY_CODES) ends the turn with its question: the
    model never answers over it (trap C1)."""
    ctx, lg, n = runtime.context, runtime.context.logger, int(state.get("round") or 0)
    msgs = _messages(state)
    sources = list(state.get("sources") or [])
    names = list(state.get("tool_names") or [])
    notes = list(state.get("notes") or [])
    ran, clarify, closed = [], None, bool(state.get("read_as_closed"))
    for call in state.get("pending") or []:
        t1 = time.monotonic()
        name, arguments = call["function"]["name"], call["function"]["arguments"]
        if app.scale_guessed(name, arguments, msgs):
            app._emit(lg, "scale_guess_blocked", round=n, tool=name,
                      arguments=str(arguments)[:500])
            arguments = app._without_scale(arguments)
        result = app.at.run_tool(ctx.db, name, arguments)
        err = getattr(result, "error", None)
        app._emit(lg, "tool_call", round=n, tool=name, arguments=str(arguments)[:500],
                  ok=getattr(result, "ok", None),
                  error_code=err.get("code") if isinstance(err, dict) else None,
                  n_sources=len(result.sources),
                  ms=round((time.monotonic() - t1) * 1000, 1))
        names.append(name)
        ran.append(name)
        if clarify is None and isinstance(err, dict) and err.get("code") in app.CLARIFY_CODES:
            clarify = err.get("message") or "Could you clarify your question?"
        content, extra_sources, extra_notes = app.period_check(
            ctx.db, name, arguments, result, lg, n)
        done_note = app.completion_note(name, msgs)
        if done_note:                          # "completed" read as CLOSED, said aloud
            content = app._with_note(content, done_note)
            extra_notes = list(extra_notes) + [done_note]
            closed = True
            app._emit(lg, "completed_read_as_closed", round=n, tool=name)
        sources.extend(result.sources)
        sources.extend(extra_sources)
        notes.extend(getattr(result, "notes", None) or [])
        notes.extend(extra_notes)
        msgs.append({"role": "tool", "tool_call_id": call["id"], "content": content})
    out = {"messages": msgs, "sources": sources, "tool_names": names, "notes": notes,
           "pending": [], "read_as_closed": closed,
           "step": {"node": "tools", "round": n, "tools": ran}}
    if clarify:
        app._emit(lg, "clarification_returned", round=n)
        msgs.append({"role": "assistant", "content": clarify})
        return {**out, "answer": clarify, "route": "guard_out", "read_as_closed": False,
                "asked_user": True,
                "step": {**out["step"], "asked_user": True}}   # a question back: no caveat
    return {**out, "route": "model"}


def guard_out(state: AgentState, runtime: Runtime[AgentContext]) -> AgentState:
    """What the answer must say, in code, then the guardrail on the answer, then the turn's
    log line. When "completed" was read as CLOSED this turn and the answer doesn't say so
    (and that dates are when work orders were raised), a fixed sentence is added
    (`caveat_added`). An answer with personal data is replaced, in the conversation too, so
    the next turn's prompt never carries it."""
    ctx, lg = runtime.context, runtime.context.logger
    answer, msgs = state.get("answer"), _messages(state)
    model_answered = not state.get("asked_user") and answer not in (app.CHAT_FAILED,
                                                                    app.ROUNDS_EXHAUSTED)
    caveats = []
    if model_answered and state.get("read_as_closed"):
        caveats.append(("completed_read_as_closed", app.completion_said))
    if model_answered and ctx.db is not None:  # "still open" on data that ends in June
        caveats.append(("data_end", lambda a: app.as_of_said(
            a, state.get("question"), app.data_dates(ctx.db))))
    for name, add in caveats:
        said = add(answer)
        if said != answer:
            app._emit(lg, "caveat_added", caveat=name)
            for m in reversed(msgs):           # the history gets the same answer
                if m.get("role") == "assistant" and m.get("content") == answer:
                    m["content"] = said
                    break
            answer = said
    usage = state.get("usage") or app._new_usage()
    sources, names = state.get("sources") or [], state.get("tool_names") or []
    shown = answer
    out = app.guardrail.check_output(answer or "")
    if not out.allowed:
        app._emit(lg, "output_blocked", rule=out.rule, reason=out.reason)   # never the text
        shown = out.message
        for m in reversed(msgs):               # the leaked text leaves the history too
            if m.get("role") == "assistant" and m.get("content"):
                m["content"] = shown
                break
    app._emit(lg, "turn", question=(state.get("question") or "")[:200], tools_called=names,
              n_sources=len(sources), model_calls=usage["model_calls"],
              cost_usd=round(usage["cost_usd"], 6) if usage["cost_complete"] else None)
    return {"messages": msgs, "answer": shown, "route": "end",
            "step": {"node": "guard_out", "answer_replaced": shown != answer,
                     "sources": len(app.format_sources(sources)),
                     "model_calls": usage["model_calls"]}}


def _route(state: AgentState) -> str:
    return state.get("route") or "end"


# ===================================================================== graph ===

def build(checkpointer=None):
    """The compiled graph. Compile once and reuse it (graph()); a checkpointer, if given,
    saves AgentState per conversation (the API's, from phase 2)."""
    g = StateGraph(AgentState, context_schema=AgentContext)
    g.add_node("guard_in", guard_in)
    g.add_node("model", model)
    g.add_node("tools", tools)
    g.add_node("guard_out", guard_out)
    g.add_edge(START, "guard_in")
    g.add_conditional_edges("guard_in", _route, {"model": "model", "end": END})
    g.add_conditional_edges("model", _route, {"tools": "tools", "guard_out": "guard_out",
                                              "model": "model"})   # asked again: no tool used
    g.add_conditional_edges("tools", _route, {"model": "model", "guard_out": "guard_out"})
    g.add_edge("guard_out", END)
    return g.compile(checkpointer=checkpointer)


_GRAPH = None


def graph():
    """The graph without a checkpointer, compiled on first use and reused."""
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build()
    return _GRAPH


def diagram() -> str:
    """The graph as Mermaid, drawn by LangGraph from the compiled graph (so it can't
    disagree with the code)."""
    return graph().get_graph().draw_mermaid()


def recursion_limit(max_rounds: int) -> int:
    """LangGraph stops a run after this many steps. A turn takes at most guard_in, then
    model + tools per round, one more model (which stops it) and guard_out: 2 * rounds + 3.
    Doubled, so the graph's own guard never fires before the round count does."""
    return 4 * max_rounds + 10


def run(messages: list, question: str, context: AgentContext, *, on_step=None,
        compiled=None, config: dict | None = None) -> dict:
    """One question through the graph. Returns the final AgentState. on_step is called
    with each node's step (no question or answer text) as the node finishes; an error in
    it is logged, never raised. `compiled` and `config` let a caller run a graph built with
    a checkpointer, on its own thread."""
    g = compiled or graph()
    cfg = {"recursion_limit": recursion_limit(context.max_rounds), **(config or {})}
    final = None
    for mode, chunk in g.stream({"messages": messages, "question": question}, cfg,
                                context=context, stream_mode=["updates", "values"]):
        if mode == "values":
            final = chunk
            continue
        for node, change in (chunk or {}).items():
            step = (change or {}).get("step") or {"node": node}
            app._emit(context.logger, "graph_step", **step)
            if on_step is not None:
                try:
                    on_step(dict(step))
                except Exception as e:           # a caller's callback never breaks a turn
                    app._emit(context.logger, "on_step_error", error=type(e).__name__)
    if not final or final.get("route") != "end":  # every path ends with route "end"
        raise RuntimeError("the graph finished without an answer")
    return final


def main() -> None:
    if sys.argv[1:] == ["mermaid"]:
        print(diagram())
        return
    print(__doc__)


if __name__ == "__main__":
    main()