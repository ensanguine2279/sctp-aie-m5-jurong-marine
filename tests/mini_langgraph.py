"""A stand-in for the small part of LangGraph that agent_graph.py uses, so the graph's tests
run where langgraph isn't installed (the sandbox it was written in; a fresh checkout before
`uv add langgraph`). Where langgraph IS installed, the tests use the real one: install()
does nothing then.

Covered, as LangGraph does it: StateGraph(schema) with add_node, add_edge,
add_conditional_edges (a path function and a path map), START and END; compile();
stream(input, config, stream_mode="updates"), yielding {node: its update} after each node;
invoke(); the state merged key by key (a later value replaces an earlier one, the default
for a key with no reducer); an update with a key the schema doesn't have refused; a run
longer than config["recursion_limit"] steps stopped with GraphRecursionError; and
get_graph().draw_mermaid().
"""
from __future__ import annotations

import importlib.machinery
import sys
import types
import typing

START, END = "__start__", "__end__"


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
    def __init__(self, keys, nodes, edges, branches):
        self._keys, self._nodes, self._edges, self._branches = keys, nodes, edges, branches

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

    def stream(self, input, config=None, *, stream_mode="updates"):
        if stream_mode != "updates":
            raise NotImplementedError("only stream_mode='updates' is stood in for")
        limit = int((config or {}).get("recursion_limit", 25))
        state = dict(input or {})
        node, steps = self._next(START, state), 0
        while node != END:
            steps += 1
            if steps > limit:
                raise GraphRecursionError(f"Recursion limit of {limit} reached")
            update = self._nodes[node](dict(state)) or {}
            bad = set(update) - self._keys
            if bad:
                raise InvalidUpdateError(f"{node} returned keys not in the state: {sorted(bad)}")
            state.update(update)
            yield {node: update}
            node = self._next(node, state)

    def invoke(self, input, config=None):
        state = dict(input or {})
        for update in self.stream(input, config):
            for change in update.values():
                state.update(change)
        return state

    def get_graph(self):
        edges = [(a, b, None) for a, b in self._edges.items()]
        for a, (_, mapping) in self._branches.items():
            edges += [(a, b, k) for k, b in mapping.items()]
        return _Drawable(edges)


class StateGraph:
    def __init__(self, schema):
        self._keys = set(typing.get_type_hints(schema))
        self._nodes, self._edges, self._branches = {}, {}, {}

    def add_node(self, name, fn):
        if name in self._nodes or name in (START, END):
            raise ValueError(f"node {name!r} already exists or is reserved")
        self._nodes[name] = fn

    def add_edge(self, a, b):
        self._edges[a] = b

    def add_conditional_edges(self, source, path, path_map):
        self._branches[source] = (path, dict(path_map))

    def compile(self):
        for a, b in self._edges.items():
            for n in (a, b):
                if n not in (START, END) and n not in self._nodes:
                    raise ValueError(f"edge to unknown node {n!r}")
        for a, (_, mapping) in self._branches.items():
            for n in mapping.values():
                if n != END and n not in self._nodes:
                    raise ValueError(f"branch to unknown node {n!r}")
        return _Compiled(self._keys, self._nodes, dict(self._edges), dict(self._branches))


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
    errors = types.ModuleType("langgraph.errors")
    errors.GraphRecursionError, errors.InvalidUpdateError = GraphRecursionError, InvalidUpdateError
    pkg.graph, pkg.errors = graph, errors
    sys.modules.update({"langgraph": pkg, "langgraph.graph": graph, "langgraph.errors": errors})
    return True