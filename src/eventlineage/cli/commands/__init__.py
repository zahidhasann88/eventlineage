"""Typer command modules.

``analysis`` (analyze/validate/compare/impact), ``inspect`` (events/services/
topics), ``lifecycle`` (init/scan/rules/plugins/integrations/cache) and ``output``
(graph/report/export). Each module exposes plain functions that ``cli.main``
registers on the application, so the command table lives in exactly one place.
"""

from __future__ import annotations

__all__ = ["analysis", "inspect", "lifecycle", "output"]
