"""EventLineage — event dependency intelligence for event-driven architectures.

Quick start::

    from eventlineage import Project

    project = Project.open(".")
    catalog = project.scan().catalog
    print(project.analyze().summary())
    print(project.impact("orders.created").summary())
"""

from __future__ import annotations

from eventlineage.analysis import analyze_catalog, analyze_impact
from eventlineage.compatibility import (
    CompatibilityMode,
    CompatibilityReport,
    compare_definitions,
    compare_schemas,
)
from eventlineage.config import Config
from eventlineage.core.catalog import EventCatalog, build_catalog
from eventlineage.core.scanner import Scanner, ScanResult, scan_project
from eventlineage.exceptions import (
    ConfigError,
    DiscoveryError,
    EventLineageError,
    IntegrationUnavailableError,
    ParseError,
    ProjectNotFoundError,
    SymbolNotFoundError,
)
from eventlineage.graph import Graph, build_graph, service_flow_graph
from eventlineage.models import (
    Consumer,
    Dependency,
    Event,
    EventRelationship,
    EventSchema,
    EventVersion,
    Producer,
    SchemaField,
    SchemaFormat,
    Service,
    Severity,
    Topic,
)
from eventlineage.project import Project, open_project, require_project
from eventlineage.reporting import (
    render_dot,
    render_json,
    render_markdown_report,
    render_mermaid,
)
from eventlineage.validation import ValidationReport, Validator, validate_catalog

__version__ = "0.1.0"

__all__ = [
    "CompatibilityMode",
    "CompatibilityReport",
    "Config",
    "ConfigError",
    "Consumer",
    "Dependency",
    "DiscoveryError",
    "Event",
    "EventCatalog",
    "EventLineageError",
    "EventRelationship",
    "EventSchema",
    "EventVersion",
    "Graph",
    "IntegrationUnavailableError",
    "ParseError",
    "Producer",
    "Project",
    "ProjectNotFoundError",
    "ScanResult",
    "Scanner",
    "SchemaField",
    "SchemaFormat",
    "Service",
    "Severity",
    "SymbolNotFoundError",
    "Topic",
    "ValidationReport",
    "Validator",
    "__version__",
    "analyze_catalog",
    "analyze_impact",
    "build_catalog",
    "build_graph",
    "compare_definitions",
    "compare_schemas",
    "open_project",
    "render_dot",
    "render_json",
    "render_markdown_report",
    "render_mermaid",
    "require_project",
    "scan_project",
    "service_flow_graph",
    "validate_catalog",
]
