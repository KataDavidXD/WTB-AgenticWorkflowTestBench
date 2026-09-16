"""Real, deterministic file workflows for local-console acceptance (no LLM)."""
import operator
import time
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from wtb.sdk.workflow_project import WorkflowProject


class State(TypedDict, total=False):
    text: str
    repeat: int
    count: int
    delay: float
    fail: bool
    notes: Annotated[list[str], operator.add]
    _output_files: dict[str, str]


def prepare(state: State):
    time.sleep(min(max(float(state.get("delay", 0.2)), 0), 5))
    return {"count": 0, "_output_files": {"report.txt": "draft:" + state.get("text", "WTB")}}


def transform(state: State):
    time.sleep(min(max(float(state.get("delay", 0.2)), 0), 5))
    if state.get("fail"):
        raise ValueError("Requested failure in real transform node")
    count = state.get("count", 0) + 1
    return {"count": count, "_output_files": {"report.txt": f"pass {count}:" + state.get("text", "WTB")}}


def uppercase(state: State):
    result = transform(state)
    result["_output_files"]["report.txt"] = result["_output_files"]["report.txt"].upper()
    return result


def finish(state: State):
    return {"_output_files": {"report.txt": "final:" + state["_output_files"]["report.txt"]}}


def route(state: State):
    return "transform" if state["count"] < min(int(state.get("repeat", 1)), 10) else "finish"


def graph():
    g = StateGraph(State)
    g.add_node("prepare", prepare)
    g.add_node("transform", transform)
    g.add_node("finish", finish)
    g.add_edge(START, "prepare")
    g.add_edge("prepare", "transform")
    g.add_conditional_edges("transform", route, {"transform": "transform", "finish": "finish"})
    g.add_edge("finish", END)
    return g


def left(state: State):
    time.sleep(min(max(float(state.get("delay", 0.2)), 0), 5))
    return {"notes": ["left"]}


def right(state: State):
    time.sleep(min(max(float(state.get("delay", 0.2)), 0), 5))
    return {"notes": ["right"]}


def parallel_graph():
    g = StateGraph(State)
    for name, fn in [("prepare", prepare), ("left", left), ("right", right), ("finish", finish)]:
        g.add_node(name, fn)
    g.add_edge(START, "prepare")
    g.add_edge("prepare", "left")
    g.add_edge("prepare", "right")
    g.add_edge(["left", "right"], "finish")
    g.add_edge("finish", END)
    return g


def create_project():
    project = WorkflowProject(name="file-workflow", graph_factory=graph, description="真实文件输出、条件循环、节点替换及并行分支")
    project.register_variant("transform", "uppercase", uppercase, "大写输出")
    project.register_workflow_variant("parallel", parallel_graph, "两个节点并行后汇合")
    return project
