#!/usr/bin/env python3
"""The agent's turn as a LangGraph graph: the same steps as agent_app's loop, as nodes.

    START -> guard_in --(refused / asked back)--> END
                 |
                 v
               model --(answered / failed / too many rounds)--> guard_out -> END
                 ^  |
                 |  v (tool calls)
               tools --(a tool asked the user something)--> guard_out

Each node calls one function from agent_app.py (11 Nov 2026), so the graph adds no agent
logic and the two engines can't drift apart:

    guard_in   agent_app.before_model   guardrail on the question, the short-year question,
                                        today's dates in the system prompt
    model      agent_app.model_round    one model call (rounds counted; past
                                        MAX_TOOL_ROUNDS it stops, as the loop does)
    tools      agent_app.tool_round     the tools the model picked, with the scale guard,
                                        period_check and completion_note
    guard_out  agent_app.after_model    guardrail on the answer, the `turn` log line

What the graph adds: each step is a node you can see (diagram(), the `graph_step` log
event, the API's /ask/stream), and a place to add a step later (an LLM judge, a human
approval) without touching the loop.

What it keeps out of the graph state, on purpose: the conversation, the database
connection, the model client and the logger live in a per-question `Turn` the nodes share.
The state holds only where to go next, the round and the step just taken. So the turn's
messages are never copied or merged by LangGraph, ask()'s all-or-nothing copy stays the one
copy, and sessions.py still saves conversations, as it does for the loop. A LangGraph
checkpointer could replace sessions.py later; it isn't needed for that today.

Picked by AGENT_ENGINE=graph (agent_app.ask() dispatches; the page, the CLI, the eval suite
and the API all follow it). Without langgraph installed, ask() runs the loop and logs
`engine_unavailable`.

Usage
    uv add langgraph                             # personal repo
    pip install langgraph                        # team repo (add to requirements-app.txt)
    AGENT_ENGINE=graph python scripts/agent_app.py "what oil does CNC-03 take?"
    python scripts/agent_graph.py mermaid        # the graph, as Mermaid, from LangGraph itself
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_app as app  # noqa: E402

NODES = ("guard_in", "model", "tools", "guard_out")


class TurnState(TypedDict, total=False):
    """What LangGraph carries between nodes: routing only (see the module's docstring)."""
    route: str              # the next node, or "end"
    round: int              # model calls made so far this turn
    step: dict              # what the node just did, for on_step / the log (no text)


@dataclass
class Turn:
    """One question's working set, shared by the nodes."""
    db: object
    client: object
    messages: list
    question: str
    model: str
    logger: object
    tally: dict
    max_rounds: int = app.MAX_TOOL_ROUNDS
    complete_fn: Callable | None = None
    notes_out: list | None = None
    raw_sources: list | None = None
    sources: list = field(default_factory=list)
    tool_names: list = field(default_factory=list)
    calls: list = field(default_factory=list)   # the tool calls the model just made
    answer: str | None = None
    result: tuple | None = None          # (answer as shown, source labels) once finished


# ===================================================================== nodes ===

def guard_in(turn: Turn, state: TurnState) -> TurnState:
    early = app.before_model(turn.db, turn.messages, turn.question, logger=turn.logger)
    if early is None:
        return {"route": "model", "round": 0, "step": {"node": "guard_in", "passed": True}}
    turn.result = (early, [])
    return {"route": "end", "round": 0,
            "step": {"node": "guard_in", "passed": False, "answered_without_model": True}}


def model(turn: Turn, state: TurnState) -> TurnState:
    n = int(state.get("round") or 0) + 1
    if n > turn.max_rounds:
        turn.answer = app.rounds_exhausted(turn.logger, turn.max_rounds)
        return {"route": "guard_out", "round": n - 1,
                "step": {"node": "model", "round": n - 1, "stopped": "max_rounds"}}
    msg = app.model_round(turn.client, turn.messages, n, model=turn.model,
                          complete_fn=turn.complete_fn, logger=turn.logger, tally=turn.tally)
    if msg is None:
        turn.answer = app.CHAT_FAILED
        return {"route": "guard_out", "round": n,
                "step": {"node": "model", "round": n, "failed": True}}
    calls = getattr(msg, "tool_calls", None) or []
    if not calls:
        turn.answer = msg.content
        return {"route": "guard_out", "round": n,
                "step": {"node": "model", "round": n, "tool_calls": []}}
    turn.calls = calls
    return {"route": "tools", "round": n,
            "step": {"node": "model", "round": n,
                     "tool_calls": [c.function.name for c in calls]}}


def tools(turn: Turn, state: TurnState) -> TurnState:
    n = int(state.get("round") or 0)
    before = len(turn.tool_names)
    clarify = app.tool_round(turn.db, turn.calls, turn.messages, n, logger=turn.logger,
                             sources=turn.sources, tool_names=turn.tool_names,
                             notes_out=turn.notes_out)
    step = {"node": "tools", "round": n, "tools": turn.tool_names[before:]}
    if clarify:
        turn.answer = clarify
        return {"route": "guard_out", "step": {**step, "asked_user": True}}
    return {"route": "model", "step": step}


def guard_out(turn: Turn, state: TurnState) -> TurnState:
    shown, labels = app.after_model(turn.answer, turn.sources, turn.tool_names, turn.messages,
                                    turn.question, logger=turn.logger, tally=turn.tally,
                                    raw_sources=turn.raw_sources)
    turn.result = (shown, labels)
    return {"route": "end",
            "step": {"node": "guard_out", "answer_replaced": shown != turn.answer,
                     "sources": len(labels), "model_calls": turn.tally.get("model_calls", 0)}}


def _route(state: TurnState) -> str:
    return state.get("route") or "end"


# ===================================================================== graph ===

def build(turn: Turn | None = None):
    """The compiled graph for one question. Without a Turn (diagram()), the nodes are
    never run, so any stand-in will do."""
    from langgraph.graph import END, START, StateGraph

    t = turn
    g = StateGraph(TurnState)
    g.add_node("guard_in", lambda s: guard_in(t, s))
    g.add_node("model", lambda s: model(t, s))
    g.add_node("tools", lambda s: tools(t, s))
    g.add_node("guard_out", lambda s: guard_out(t, s))
    g.add_edge(START, "guard_in")
    g.add_conditional_edges("guard_in", _route, {"model": "model", "end": END})
    g.add_conditional_edges("model", _route, {"tools": "tools", "guard_out": "guard_out"})
    g.add_conditional_edges("tools", _route, {"model": "model", "guard_out": "guard_out"})
    g.add_edge("guard_out", END)
    return g.compile()


def diagram() -> str:
    """The graph as Mermaid, drawn by LangGraph from the compiled graph (so it can't
    disagree with the code)."""
    return build().get_graph().draw_mermaid()


def recursion_limit(max_rounds: int) -> int:
    """LangGraph stops a run after this many steps. A turn takes at most guard_in, then
    model + tools per round, one more model (which stops it) and guard_out: 2 * rounds + 3.
    Doubled, so the graph's own guard never fires before the round count does."""
    return 4 * max_rounds + 10


def run(db, client, messages, question, *, model=app.MODEL, logger=app.log, raw_sources=None,
        notes=None, usage=None, on_step=None, complete_fn=None,
        max_rounds=app.MAX_TOOL_ROUNDS) -> tuple[str, list[str]]:
    """One question through the graph, on `messages` (ask() passes its working copy).
    Returns (answer, source labels), the same as the loop's _ask(). on_step is called with
    each node's step dict as the node finishes; an error in it is logged, never raised."""
    turn = Turn(db=db, client=client, messages=messages, question=question, model=model,
                logger=logger, tally=app.start_usage(usage), max_rounds=max_rounds,
                complete_fn=complete_fn, notes_out=notes, raw_sources=raw_sources)
    graph = build(turn)
    for update in graph.stream({"round": 0}, {"recursion_limit": recursion_limit(max_rounds)},
                               stream_mode="updates"):
        for node, change in (update or {}).items():
            step = (change or {}).get("step") or {"node": node}
            app._emit(logger, "graph_step", **step)
            if on_step is not None:
                try:
                    on_step(dict(step))
                except Exception as e:           # a caller's callback never breaks a turn
                    app._emit(logger, "on_step_error", error=type(e).__name__)
    if turn.result is None:                      # can't happen: every path ends in a node
        raise RuntimeError("the graph finished without an answer")   # that sets it
    return turn.result


def main() -> None:
    if sys.argv[1:] == ["mermaid"]:
        try:
            print(diagram())
        except ImportError:
            sys.exit("langgraph isn't installed: uv add langgraph (or pip install langgraph).")
        return
    print(__doc__)


if __name__ == "__main__":
    main()