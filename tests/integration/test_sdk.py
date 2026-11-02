"""SDK-level integration: ``Project``, reporting and reproducible scans."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eventlineage import Project, open_project, render_dot, render_json, render_markdown_report
from eventlineage.exceptions import ProjectNotFoundError


@pytest.fixture
def project(project_dir: Path) -> Project:
    return Project.open(project_dir)


class TestProjectLifecycle:
    def test_open_and_root(self, project_dir: Path) -> None:
        project = Project.open(project_dir)
        assert project.root == project_dir.resolve()

    def test_open_missing_project_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ProjectNotFoundError):
            Project.open(tmp_path, require_config=True)

    def test_create_minimal(self, tmp_path: Path) -> None:
        target = tmp_path / "fresh"
        created = Project.create(target, name="fresh", minimal=True)
        assert Path(created).exists() or (target / "eventlineage.yaml").is_file()

    def test_module_level_helpers(self, project_dir: Path) -> None:
        project = open_project(project_dir)
        assert project.root.name == "sample-project"


class TestScanning:
    def test_scan_returns_catalog(self, project: Project) -> None:
        result = project.scan()
        assert result.summary.events == 4
        assert result.summary.services == 3
        assert result.summary.files_parsed >= 1

    def test_scan_is_idempotent(self, project: Project) -> None:
        first = project.scan(incremental=False).catalog.fingerprint()
        second = project.scan(incremental=False).catalog.fingerprint()
        assert first == second

    def test_incremental_scan_reuses_cache(self, project_dir: Path) -> None:
        Project.open(project_dir).scan(incremental=True)
        result = Project.open(project_dir).scan(incremental=True)
        assert result.summary.files_cached >= 1

    def test_catalog_and_graph_helpers(self, project: Project) -> None:
        catalog = project.catalog()
        assert "order.created" in catalog.events
        graph = project.graph()
        assert graph.node_count > 0
        assert graph.contains("event:order.created")

    def test_stats(self, project: Project) -> None:
        stats = project.stats()
        assert stats["events"] == 4

    def test_scan_is_deterministic_across_processes(self, project_dir: Path) -> None:
        import subprocess
        import sys

        script = (
            "import json,sys;"
            "from eventlineage import Project;"
            f"p=Project.open(r'{project_dir}');"
            "print(p.scan(incremental=False).catalog.fingerprint())"
        )
        output = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, check=True
        ).stdout.strip()
        local = Project.open(project_dir).scan(incremental=False).catalog.fingerprint()
        assert output == local


class TestValidationAndAnalysis:
    def test_validate(self, project: Project) -> None:
        report = project.validate()
        assert report.rules_run
        assert isinstance(report.exit_code(), int)

    def test_analyze(self, project: Project) -> None:
        summary = project.analyze()
        assert summary.stats.events == 4
        assert 0 <= summary.health_score <= 100

    def test_impact(self, project: Project) -> None:
        report = project.impact("order.created")
        assert report.resolved
        assert report.affected_services
        assert report.max_depth >= 1

    def test_impact_can_be_repeated(self, project: Project) -> None:
        reports = [project.impact(name) for name in ("order.created", "payment.captured")]
        assert len(reports) == 2
        assert all(report.resolved for report in reports)


class TestCompatibility:
    def test_compare_versions(self, project: Project) -> None:
        comparison = project.compare_versions("order.created", "1.0.0", "2.0.0")
        assert comparison.old_version == "1.0.0"
        assert comparison.report.compatible is False

    def test_version_history(self, project: Project) -> None:
        history = project.version_history("order.created")
        assert [comparison.new_version for comparison in history] == ["2.0.0"]

    def test_compare_schemas_from_files(self, project: Project) -> None:
        report = project.compare_schemas(
            project.root / "schemas/order.created.v1.json",
            project.root / "schemas/order.created.v2.json",
        )
        assert report.compatible is False


class TestReporting:
    def test_json_report(self, project: Project) -> None:
        payload = json.loads(project.report(format="json"))
        assert "catalog" in payload
        assert "analysis" in payload

    def test_markdown_report_has_sections(self, project: Project) -> None:
        text = project.report(format="markdown")
        assert "## Overview" in text
        assert "## Events" in text

    def test_dot_and_mermaid(self, project: Project) -> None:
        assert project.report(format="dot").startswith("digraph")
        assert project.report(format="mermaid").lstrip().startswith(("flowchart", "graph"))

    def test_report_is_reproducible(self, project: Project) -> None:
        assert project.report(format="markdown") == project.report(format="markdown")

    def test_timestamp_is_opt_in(self, project: Project) -> None:
        assert "2026-01-01" not in project.report(format="markdown")
        assert "2026-01-01" in project.report(
            format="markdown", generated_at="2026-01-01T00:00:00Z"
        )

    def test_render_helpers(self, project: Project) -> None:
        summary = project.analyze()
        catalog = project.catalog()
        assert render_json({"catalog": catalog.to_dict()}).startswith("{")
        assert render_markdown_report(catalog, summary=summary).startswith("#")
        assert render_dot(project.graph()).startswith("digraph")


class TestExampleProject:
    def test_example_scans_cleanly(self, example_project: Path) -> None:
        project = Project.open(example_project)
        result = project.scan(incremental=False)
        assert result.summary.events >= 10
        assert result.summary.files_failed == 0

    def test_example_validation_finds_the_cycle(self, example_project: Path) -> None:
        project = Project.open(example_project)
        report = project.validate()
        cycle_findings = [finding for finding in report.findings if finding.rule == "EG006"]
        assert cycle_findings

    def test_example_impact_reaches_many_services(self, example_project: Path) -> None:
        project = Project.open(example_project)
        report = project.impact("orders.created")
        assert len(report.affected_services) >= 5
        assert report.risk in {"medium", "high", "critical"}
