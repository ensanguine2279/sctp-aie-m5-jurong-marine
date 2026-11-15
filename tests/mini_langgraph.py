"""A stand-in for the small part of LangGraph that agent_graph.py uses, so the graph's tests
run where langgraph isn't installed (the sandbox it was written in; a fresh checkout before
`uv add langgraph`). Where langgraph IS installed, the tests use the real one: install()
does nothing then.

Covered, as LangGraph does it: StateGraph(schema, context_schema=...) with add_node,
add_edge, add_conditional_edges (a path function and a path map), START and END;
compile(checkpointer=None); stream(input, config, context=..., stream_mode=...) with
"updates" ({node: its update} after each node), "values" (the whole state, first the input,
then after each node) or a list of modes (then (mode, chunk) pairs); invoke(); a node given
`runtime` (langgraph.runtime.Runtime, its .context the run's context) when it has a
parameter of that name; the state merged key by key (a later value replaces an earlier one,
the default for a key with no reducer); an update with a key the schema doesn't have
refused; a run longer than config["recursion_limit"] steps stopped with
GraphRecursionError; get_graph().draw_mermaid(); and a checkpointer: compile(checkpointer=
InMemorySaver()) with config {"configurable": {"thread_id": ...}} starts a run from the
thread's saved state with the input merged in, saves the final state when the run finishes
(as durability="exit"; a run that raises saves nothing here: the real LangGraph's behaviour
then isn't documented, and the agent doesn't rely on either), get_state(config).values, and
delete_thread(thread_id).
"""
from __future__ import annotations

import importlib.machinery
import inspect
import sys
import types
import typing
from dataclasses import dataclass

START, END = "__start__", "__end__"


@dataclass
class Runtime:
    """langgraph.runtime.Runtime: what a node gets as `runtime`. Subscriptable for type
    hints (Runtime[AgentContext]), as the real one is generic."""
    context: object = None

    def __class_getitem__(cls, item):
        return cls


class InMemorySaver:
    """langgraph.checkpoint.memory.InMemorySaver, as far as the agent uses it."""

    def __init__(self):
        self.storage: dict = {}

    def delete_thread(self, thread_id):
        self.storage.pop(thread_id, None)


class _State:
    def __init__(self, values):
        self.values = values


class GraphRecursionError(RecursionError):
    pass


class InvalidUpdateError(ValueError):
    pass


class _Drawable:
    def __init__(self, edges):
        self._edges = edges

    def draw_mermaid(self) -> str:
        lines = ["graph TD;"]
        for a, b, label in self._edges:
            arrow = f" -. {label} .-> " if label else " --> "
            lines.append(f"\t{a}{arrow}{b};")
        return "\n".join(lines) + "\n"


class _Compiled:
    def __init__(self, keys, nodes, edges, branches, checkpointer=None):
        self._keys, self._nodes, self._edges, self._branches = keys, nodes, edges, branches
        self.checkpointer = checkpointer

    @staticmethod
    def _call(fn, state, runtime):
        if "runtime" in inspect.signature(fn).parameters:
            return fn(state, runtime=runtime)
        return fn(state)

    def _next(self, node, state):
        if node in self._branches:
            path, mapping = self._branches[node]
            key = path(state)
            if key not in mapping:
                raise ValueError(f"{node}: route {key!r} is not in its path map")
            return mapping[key]
        nxt = self._edges.get(node)
        if nxt is None:
            raise ValueError(f"{node} has no way out")
        return nxt

    def _thread(self, config):
        if self.checkpointer is None:
            return None
        return ((config or {}).get("configurable") or {}).get("thread_id")

    def get_state(self, config):
        thread = self._thread(config)
        if thread is None:
            raise ValueError("No checkpointer set")
        return _State(dict(self.checkpointer.storage.get(thread) or {}))

    def stream(self, input, config=None, *, context=None, stream_mode="updates",
               durability=None):
        if durability not in (None, "exit", "async", "sync"):
            raise ValueError(f"durability {durability!r}")
        modes = [stream_mode] if isinstance(stream_mode, str) else list(stream_mode)
        bad = set(modes) - {"updates", "values"}
        if bad:
            raise NotImplementedError(f"stream modes not stood in for: {sorted(bad)}")
        pair = not isinstance(stream_mode, str)

        def out(mode, chunk):
            return (mode, chunk) if pair else chunk

        limit = int((config or {}).get("recursion_limit", 25))
        runtime = Runtime(context=context)
        thread = self._thread(config)
        state = dict(self.checkpointer.storage.get(thread) or {}) if thread else {}
        state.update(input or {})
        if "values" in modes:
            yield out("values", dict(state))
        node, steps = self._next(START, state), 0
        while node != END:
            steps += 1
            if steps > limit:
                raise GraphRecursionError(f"Recursion limit of {limit} reached")
            update = self._call(self._nodes[node], dict(state), runtime) or {}
            bad = set(update) - self._keys
            if bad:
                raise InvalidUpdateError(f"{node} returned keys not in the state: {sorted(bad)}")
            state.update(update)
            if "updates" in modes:
                yield out("updates", {node: update})
            if "values" in modes:
                yield out("values", dict(state))
            node = self._next(node, state)
        if thread is not None:
            self.checkpointer.storage[thread] = dict(state)    # saved when the run finishes

    def invoke(self, input, config=None, *, context=None):
        state = dict(input or {})
        for chunk in self.stream(input, config, context=context, stream_mode="values"):
            state = chunk
        return state

    def get_graph(self):
        edges = [(a, b, None) for a, b in self._edges.items()]
        for a, (_, mapping) in self._branches.items():
            edges += [(a, b, k) for k, b in mapping.items()]
        return _Drawable(edges)


class StateGraph:
    def __init__(self, schema, context_schema=None):
        self._keys = set(typing.get_type_hints(schema))
        self.context_schema = context_schema
        self._nodes, self._edges, self._branches = {}, {}, {}

    def add_node(self, name, fn):
        if name in self._nodes or name in (START, END):
            raise ValueError(f"node {name!r} already exists or is reserved")
        self._nodes[name] = fn

    def add_edge(self, a, b):
        self._edges[a] = b

    def add_conditional_edges(self, source, path, path_map):
        self._branches[source] = (path, dict(path_map))

    def compile(self, checkpointer=None):
        for a, b in self._edges.items():
            for n in (a, b):
                if n not in (START, END) and n not in self._nodes:
                    raise ValueError(f"edge to unknown node {n!r}")
        for a, (_, mapping) in self._branches.items():
            for n in mapping.values():
                if n != END and n not in self._nodes:
                    raise ValueError(f"branch to unknown node {n!r}")
        return _Compiled(self._keys, self._nodes, dict(self._edges), dict(self._branches),
                         checkpointer)


def install() -> bool:
    """Put this stand-in in sys.modules as `langgraph` and `langgraph.graph`, unless the
    real langgraph can be imported. True when the stand-in is in use."""
    try:
        import langgraph.graph  # noqa: F401
        return getattr(sys.modules["langgraph"], "STAND_IN", False)
    except ImportError:
        pass
    pkg = types.ModuleType("langgraph")
    pkg.__spec__ = importlib.machinery.ModuleSpec("langgraph", None, is_package=True)
    pkg.__path__ = []
    pkg.STAND_IN = True
    graph = types.ModuleType("langgraph.graph")
    graph.__spec__ = importlib.machinery.ModuleSpec("langgraph.graph", None)
    graph.StateGraph, graph.START, graph.END = StateGraph, START, END
    runtime = types.ModuleType("langgraph.runtime")
    runtime.__spec__ = importlib.machinery.ModuleSpec("langgraph.runtime", None)
    runtime.Runtime = Runtime
    ckpt = types.ModuleType("langgraph.checkpoint")
    ckpt.__spec__ = importlib.machinery.ModuleSpec("langgraph.checkpoint", None, is_package=True)
    ckpt.__path__ = []
    memory = types.ModuleType("langgraph.checkpoint.memory")
    memory.__spec__ = importlib.machinery.ModuleSpec("langgraph.checkpoint.memory", None)
    memory.InMemorySaver = InMemorySaver
    ckpt.memory = memory
    errors = types.ModuleType("langgraph.errors")
    errors.GraphRecursionError, errors.InvalidUpdateError = GraphRecursionError, InvalidUpdateError
    pkg.graph, pkg.errors, pkg.runtime, pkg.checkpoint = graph, errors, runtime, ckpt
    sys.modules.update({"langgraph": pkg, "langgraph.graph": graph, "langgraph.errors": errors,
                        "langgraph.runtime": runtime, "langgraph.checkpoint": ckpt,
                        "langgraph.checkpoint.memory": memory})
    return True