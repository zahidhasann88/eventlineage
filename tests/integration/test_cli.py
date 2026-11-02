"""End-to-end CLI behaviour: exit codes, formats and file output."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eventlineage import __version__
from eventlineage.cli.main import app

runner = CliRunner()


def run(*args: str, cwd: Path | None = None) -> tuple[int, str, str]:
    result = runner.invoke(app, list(args))
    return result.exit_code, result.stdout, result.stderr


@pytest.fixture
def project(project_dir: Path) -> Path:
    return project_dir


class TestGlobalOptions:
    def test_help_lists_commands(self) -> None:
        code, out, _ = run("--help")
        assert code == 0
        for command in (
            "init",
            "scan",
            "analyze",
            "validate",
            "graph",
            "impact",
            "events",
            "services",
            "compare",
            "report",
            "export",
        ):
            assert command in out

    def test_version(self) -> None:
        code, out, _ = run("--version")
        assert code == 0
        assert __version__ in out

    def test_unknown_command_is_a_usage_error(self) -> None:
        code, out, err = run("frobnicate")
        assert code != 0
        assert "frobnicate" in (out + err)

    def test_empty_directory_is_a_usage_error(self, tmp_path: Path) -> None:
        code, out, err = run("scan", "--root", str(tmp_path))
        assert code == 2
        assert "no event sources" in (out + err).lower()

    def test_missing_root_is_a_usage_error(self, tmp_path: Path) -> None:
        code, out, err = run("scan", "--root", str(tmp_path / "nope"))
        assert code == 2
        assert "does not exist" in (out + err).lower()


class TestInspectCommands:
    def test_scan_table(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(project)
        code, out, _ = run("scan")
        assert code == 0
        assert "Events" in out

    def test_scan_json(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(project)
        code, out, _ = run("scan", "--format", "json")
        assert code == 0
        payload = json.loads(out)
        assert payload["summary"]["events"] == 4
        assert payload["summary"]["services"] == 3

    def test_events_filters(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(project)
        code, out, _ = run("events", "--format", "json")
        assert code == 0
        events = json.loads(out)
        assert {event["id"] for event in events} >= {"order.created", "payment.captured"}

        code, out, _ = run("events", "payment", "--format", "json")
        assert code == 0
        filtered = json.loads(out)
        assert [event["id"] for event in filtered] == ["payment.captured"]

    def test_services_and_topics(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(project)
        code, out, _ = run("services", "--format", "json")
        assert code == 0
        assert {service["name"] for service in json.loads(out)} == {
            "order-service",
            "payment-service",
            "shipping-service",
        }

        code, out, _ = run("topics", "--format", "json")
        assert code == 0
        assert {topic["name"] for topic in json.loads(out)} == {"orders.v1", "payments.v1"}

    def test_rules_and_plugins_are_listed(self) -> None:
        code, out, _ = run("rules")
        assert code == 0
        assert "EG001" in out

        code, out, _ = run("plugins")
        assert code == 0
        assert "asyncapi" in out

        code, out, _ = run("integrations")
        assert code == 0
        assert "kafka" in out


class TestAnalysisCommands:
    def test_validate_exit_code_and_summary(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(project)
        code, out, _err = run("validate", "--format", "json")
        payload = json.loads(out)
        assert payload["counts"]["error"] >= 0
        assert code in {0, 1}

    def test_validate_fail_on_threshold(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(project)
        code, _out, _ = run("validate", "--fail-on", "info", "--format", "json")
        assert code == 1

    def test_validate_can_switch_rules_off(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(project)
        _code, out, _ = run(
            "validate", "--disable", "EG002", "--disable", "EG011", "--format", "json"
        )
        payload = json.loads(out)
        assert "EG002" not in payload["by_rule"]

    def test_analyze_json(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(project)
        code, out, _ = run("analyze", "--format", "json")
        assert code == 0
        payload = json.loads(out)
        assert payload["stats"]["events"] == 4
        assert "health_score" in payload

    def test_impact_json(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(project)
        code, out, _ = run("impact", "order.created", "--format", "json")
        assert code == 0
        payload = json.loads(out)
        assert payload["target"] == "order.created"
        assert payload["risk"] in {"low", "medium", "high", "critical"}
        assert payload["affected_services"]

    def test_impact_unknown_event_exits_with_usage_error(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(project)
        code, out, err = run("impact", "no.such.event")
        assert code == 2
        assert "no.such.event" in (out + err)

    def test_impact_fail_on_risk(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(project)
        code, _, _ = run("impact", "order.created", "--fail-on-risk", "low", "--format", "json")
        assert code == 1


class TestOutputCommands:
    def test_graph_dot(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(project)
        code, out, _ = run("graph", "--format", "dot")
        assert code == 0
        assert out.startswith("digraph")

    def test_graph_mermaid(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(project)
        code, out, _ = run("graph", "--format", "mermaid")
        assert code == 0
        assert out.lstrip().startswith(("flowchart", "graph"))

    def test_graph_json_and_filters(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(project)
        _code, out, _ = run("graph", "--format", "json", "--services-only")
        payload = json.loads(out)
        assert set(payload["stats"]["node_kinds"]) == {"service"}

    def test_report_markdown_to_file(
        self, project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(project)
        target = tmp_path / "report.md"
        code, _out, _ = run("report", "--format", "markdown", "-o", str(target))
        assert code == 0
        text = target.read_text(encoding="utf-8")
        assert text.startswith("#")
        assert "## Overview" in text

    def test_export_json(
        self, project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(project)
        target = tmp_path / "catalog.json"
        code, _, _ = run("export", "-o", str(target))
        assert code == 0
        payload = json.loads(target.read_text(encoding="utf-8"))
        assert "catalog" in payload
        assert "analysis" in payload

    def test_report_is_reproducible(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(project)
        _, first, _ = run("report", "--format", "markdown")
        _, second, _ = run("report", "--format", "markdown")
        assert first == second


class TestLifecycleCommands:
    def test_init_creates_a_config(self, tmp_path: Path) -> None:
        target = tmp_path / "new-project"
        code, _out, _ = run("init", str(target), "--minimal")
        assert code == 0
        assert (target / "eventlineage.yaml").is_file()

    def test_init_detects_sources(self, tmp_path: Path) -> None:
        (tmp_path / "schemas").mkdir()
        (tmp_path / "schemas" / "orders.avsc").write_text(
            json.dumps({"type": "record", "name": "Order", "fields": []}), encoding="utf-8"
        )
        target = tmp_path / "detected"
        code, _, _ = run("init", str(target))
        assert code == 0

    def test_cache_info_and_clear(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(project)
        run("scan")
        code, out, _ = run("cache", "info")
        assert code == 0
        assert "cache" in out.lower()

        code, out, _ = run("cache", "clear")
        assert code == 0

    def test_compare_two_files(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        old = tmp_path / "old.avsc"
        new = tmp_path / "new.avsc"
        old.write_text(
            json.dumps(
                {"type": "record", "name": "A", "fields": [{"name": "id", "type": "string"}]}
            ),
            encoding="utf-8",
        )
        new.write_text(
            json.dumps(
                {
                    "type": "record",
                    "name": "A",
                    "fields": [
                        {"name": "id", "type": "string"},
                        {"name": "extra", "type": "string"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        code, out, _ = run("compare", str(old), str(new), "--format", "json")
        payload = json.loads(out)
        assert code == 1
        assert payload["compatible"] is False

        code, _, _ = run("compare", str(old), str(new), "--format", "json", "--no-fail")
        assert code == 0
