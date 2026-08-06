"""Importable example graph factories for install_checker and smoke tests.

These live inside the ``wtb.sdk`` package so that Ray actors (which cannot
resolve ``__main__``) can import them by module path.
"""

from __future__ import annotations

from typing import Any


def create_linear_graph():
    """3-node linear graph: A -> B -> C."""
    from typing import TypedDict

    from langgraph.graph import END, StateGraph

    class St(TypedDict):
        messages: list
        count: int
        result: str
        _variant_config: dict

    def node_a(state: dict[str, Any]) -> dict:
        return {
            "messages": state.get("messages", []) + ["A"],
            "count": state.get("count", 0) + 1,
        }

    def node_b(state: dict[str, Any]) -> dict:
        variant = (state.get("_variant_config") or {}).get("node_b")
        marker = f"B:{variant}" if variant and variant != "default" else "B"
        return {
            "messages": state.get("messages", []) + [marker],
            "count": state.get("count", 0) + 1,
        }

    def node_c(state: dict[str, Any]) -> dict:
        msgs = state.get("messages", []) + ["C"]
        return {
            "messages": msgs,
            "count": state.get("count", 0) + 1,
            "result": ",".join(msgs),
        }

    g = StateGraph(St)
    g.add_node("node_a", node_a)
    g.add_node("node_b", node_b)
    g.add_node("node_c", node_c)
    g.add_edge("__start__", "node_a")
    g.add_edge("node_a", "node_b")
    g.add_edge("node_b", "node_c")
    g.add_edge("node_c", END)
    return g
