"""Graphviz DOT export."""

from __future__ import annotations

from collections.abc import Iterable

from eventlineage.graph.model import Graph, NodeKind, kind_values
from eventlineage.models.common import DependencyKind

NODE_STYLE: dict[NodeKind, dict[str, str]] = {
    NodeKind.SERVICE: {
        "shape": "box",
        "style": "rounded,filled",
        "fillcolor": "#e8f0fe",
        "color": "#1a73e8",
    },
    NodeKind.EVENT: {
        "shape": "ellipse",
        "style": "filled",
        "fillcolor": "#fff4e5",
        "color": "#e8710a",
    },
    NodeKind.TOPIC: {
        "shape": "cylinder",
        "style": "filled",
        "fillcolor": "#e6f4ea",
        "color": "#137333",
    },
}

EDGE_STYLE: dict[DependencyKind, dict[str, str]] = {
    DependencyKind.PRODUCES: {"color": "#1a73e8", "label": "produces"},
    DependencyKind.CONSUMES: {"color": "#5f6368", "label": "consumes"},
    DependencyKind.PUBLISHES_TO: {"color": "#1a73e8", "label": "publishes", "style": "dashed"},
    DependencyKind.SUBSCRIBES_TO: {"color": "#5f6368", "label": "subscribes", "style": "dashed"},
    DependencyKind.ROUTES_VIA: {"color": "#137333", "label": "routes", "style": "dotted"},
    DependencyKind.TRIGGERS: {"color": "#d93025", "label": "triggers"},
    DependencyKind.DERIVED_FROM: {"color": "#d93025", "label": "derived", "style": "dashed"},
    DependencyKind.DEPENDS_ON: {"color": "#9334e6", "label": "depends on"},
}


def _quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    return f'"{escaped}"'


def render_dot(
    graph: Graph,
    *,
    title: str | None = None,
    node_kinds: Iterable[NodeKind] | None = None,
    edge_kinds: Iterable[DependencyKind] | None = None,
    max_nodes: int | None = None,
) -> str:
    """Render a graph as Graphviz DOT.

    ``max_nodes`` truncates large graphs deterministically (by node id) so a
    10k-node architecture still produces something a human can open.
    """
    allowed_nodes = None if node_kinds is None else set(node_kinds)
    allowed_edges = kind_values(edge_kinds)
    nodes = [node for node in graph.nodes if allowed_nodes is None or node.kind in allowed_nodes]
    if max_nodes is not None and len(nodes) > max_nodes:
        nodes = nodes[:max_nodes]
    keep = {node.id for node in nodes}
    index = {node.id: position for position, node in enumerate(nodes)}

    lines = ["digraph eventlineage {"]
    if title:
        lines.append(f"  label={_quote(title)};")
        lines.append("  labelloc=t;")
    lines.append("  rankdir=LR;")
    lines.append('  node [fontname="Helvetica",fontsize=10];')
    lines.append('  edge [fontname="Helvetica",fontsize=8];')
    for node in nodes:
        style = NODE_STYLE.get(node.kind, {})
        attributes = ", ".join(f"{key}={_quote(value)}" for key, value in style.items())
        lines.append(f"  n{index[node.id]} [label={_quote(node.label)}, {attributes}];")
    for edge in graph.edges:
        source = graph.node(edge.source)
        target = graph.node(edge.target)
        if source.id not in keep or target.id not in keep:
            continue
        if allowed_edges is not None and edge.kind.value not in allowed_edges:
            continue
        style = dict(EDGE_STYLE.get(edge.kind, {}))
        if edge.inferred:
            style["style"] = "dashed"
            style.setdefault("label", edge.kind.value)
            style["label"] = f"{style['label']} (inferred)"
        attributes = ", ".join(f"{key}={_quote(value)}" for key, value in style.items())
        lines.append(f"  n{index[source.id]} -> n{index[target.id]} [{attributes}];")
    lines.append("}")
    return "\n".join(lines) + "\n"


__all__ = ["EDGE_STYLE", "NODE_STYLE", "render_dot"]
