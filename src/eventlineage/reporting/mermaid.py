"""Mermaid flowchart export (renders in GitHub, GitLab and most wikis)."""

from __future__ import annotations

from collections.abc import Iterable

from eventlineage.graph.model import Graph, NodeKind, kind_values
from eventlineage.models.common import DependencyKind

CLASS_DEFS: dict[NodeKind, str] = {
    NodeKind.SERVICE: "classDef service fill:#e8f0fe,stroke:#1a73e8,stroke-width:1px;",
    NodeKind.EVENT: "classDef event fill:#fff4e5,stroke:#e8710a,stroke-width:1px;",
    NodeKind.TOPIC: "classDef topic fill:#e6f4ea,stroke:#137333,stroke-width:1px;",
}

EDGE_LABELS: dict[str, str] = {
    DependencyKind.PRODUCES.value: "produces",
    DependencyKind.CONSUMES.value: "consumes",
    DependencyKind.PUBLISHES_TO.value: "publishes",
    DependencyKind.SUBSCRIBES_TO.value: "subscribes",
    DependencyKind.ROUTES_VIA.value: "routes via",
    DependencyKind.TRIGGERS.value: "triggers",
    DependencyKind.DERIVED_FROM.value: "derived from",
    DependencyKind.DEPENDS_ON.value: "depends on",
}


def _escape(value: str) -> str:
    return value.replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def render_mermaid(
    graph: Graph,
    *,
    direction: str = "LR",
    node_kinds: Iterable[NodeKind] | None = None,
    edge_kinds: Iterable[DependencyKind] | None = None,
    max_nodes: int | None = None,
    show_edge_labels: bool = True,
) -> str:
    """Render a graph as a Mermaid ``flowchart``.

    Node ids are synthetic (``n0``, ``n1``) so arbitrary event names cannot
    break Mermaid's id grammar; labels stay human readable.
    """
    allowed_nodes = None if node_kinds is None else set(node_kinds)
    allowed_edges = kind_values(edge_kinds)
    nodes = [node for node in graph.nodes if allowed_nodes is None or node.kind in allowed_nodes]
    if max_nodes is not None and len(nodes) > max_nodes:
        nodes = nodes[:max_nodes]
    keep = {node.id for node in nodes}
    index = {node.id: position for position, node in enumerate(nodes)}

    lines = [f"flowchart {direction}"]
    for node in nodes:
        label = _escape(node.label)
        lines.append(f'    n{index[node.id]}["{label}"]:::{node.kind.value}')
    for edge in graph.edges:
        source = graph.node(edge.source)
        target = graph.node(edge.target)
        if source.id not in keep or target.id not in keep:
            continue
        if allowed_edges is not None and edge.kind.value not in allowed_edges:
            continue
        label = EDGE_LABELS.get(edge.kind.value, edge.kind.value)
        if edge.inferred:
            label = f"{label} (inferred)"
        arrow = "-.->" if edge.inferred else "-->"
        if show_edge_labels:
            lines.append(f"    n{index[source.id]} {arrow}|{label}| n{index[target.id]}")
        else:
            lines.append(f"    n{index[source.id]} {arrow} n{index[target.id]}")
    for kind in sorted({node.kind for node in nodes}, key=lambda item: item.value):
        lines.append("    " + CLASS_DEFS.get(kind, ""))
    return "\n".join(line for line in lines if line.strip()) + "\n"


def render_mermaid_sequence(
    messages: Iterable[tuple[str, str, str]], *, title: str | None = None
) -> str:
    """Render an ordered message flow (participants, event, label) as a sequence diagram."""
    lines = ["sequenceDiagram"]
    if title:
        lines.append(f"    title {title}")
    for source, target, label in messages:
        lines.append(f"    {source}->>{target}: {label}")
    return "\n".join(lines) + "\n"


__all__ = ["CLASS_DEFS", "EDGE_LABELS", "render_mermaid", "render_mermaid_sequence"]
