"""LangGraph wiring for the investigation pipeline.

    plan -> promql -> reflect(promql) --retry--> promql
                             |
                             ok
                             v
                          decide --sufficient--------------> report -> END
                             |
                      need_node_access
                             v
                   node_investigate -> reflect(node) --retry--> node_investigate
                                              |
                                              ok --------------> report

An explicit graph is the point: retry limits, the reflection loops and the escalation to node
access are all visible edges rather than emergent chat behaviour.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from tsagent.graph.deps import GraphDeps
from tsagent.graph.nodes.decide import decide_node
from tsagent.graph.nodes.node_ssh import node_investigate_node
from tsagent.graph.nodes.planner import planner_node
from tsagent.graph.nodes.promql import promql_node
from tsagent.graph.nodes.reflect import make_reflection_node, reflection_route
from tsagent.graph.nodes.report import report_node
from tsagent.graph.state import IncidentState


def build_graph(deps: GraphDeps, checkpointer=None):
    """Compile the investigation graph against ``deps``."""

    async def plan(state: IncidentState) -> dict:
        return await planner_node(state, deps)

    async def promql(state: IncidentState) -> dict:
        return await promql_node(state, deps)

    async def decide(state: IncidentState) -> dict:
        return await decide_node(state, deps)

    async def node_investigate(state: IncidentState) -> dict:
        return await node_investigate_node(state, deps)

    async def report(state: IncidentState) -> dict:
        return await report_node(state, deps)

    graph = StateGraph(IncidentState)
    graph.add_node("plan", plan)
    graph.add_node("promql", promql)
    graph.add_node("reflect_promql", make_reflection_node("promql", deps))
    graph.add_node("decide", decide)
    graph.add_node("node_investigate", node_investigate)
    graph.add_node("reflect_node", make_reflection_node("node", deps))
    graph.add_node("report", report)

    graph.add_edge(START, "plan")
    graph.add_edge("plan", "promql")
    graph.add_edge("promql", "reflect_promql")
    graph.add_conditional_edges(
        "reflect_promql",
        reflection_route("promql", deps),
        {"retry": "promql", "ok": "decide"},
    )
    graph.add_conditional_edges(
        "decide",
        _decision_route,
        {"sufficient": "report", "need_node_access": "node_investigate"},
    )
    graph.add_edge("node_investigate", "reflect_node")
    graph.add_conditional_edges(
        "reflect_node",
        reflection_route("node", deps),
        {"retry": "node_investigate", "ok": "report"},
    )
    graph.add_edge("report", END)

    return graph.compile(checkpointer=checkpointer)


def _decision_route(state: IncidentState) -> str:
    return (
        "need_node_access"
        if state.get("decision") == "need_node_access"
        else "sufficient"
    )
