"""Tiny in-process LangGraph example so the LangGraph adapter has something to point at.

Use:
    mdk-eval run --target langgraph --graph-import-path examples.langgraph_example:graph \
        --dataset datasets/sample.jsonl --output ./results --no-judges

Skip if `langgraph` isn't installed.
"""
from __future__ import annotations

try:
    from langgraph.graph import END, StateGraph  # type: ignore
except Exception:  # pragma: no cover
    StateGraph = None  # type: ignore
    END = "END"

from typing import Any


def _intent(state: dict[str, Any]) -> dict[str, Any]:
    state.setdefault("__path__", []).append("intent_classifier")
    return state


def _responder(state: dict[str, Any]) -> dict[str, Any]:
    state.setdefault("__path__", []).append("responder")
    state["output"] = f"Echo: {state.get('prompt', '')}"
    return state


def _build():
    if StateGraph is None:  # pragma: no cover
        return None
    g = StateGraph(dict)
    g.add_node("intent", _intent)
    g.add_node("respond", _responder)
    g.set_entry_point("intent")
    g.add_edge("intent", "respond")
    g.add_edge("respond", END)
    return g.compile()


graph = _build()
