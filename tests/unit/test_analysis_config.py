"""Impact analysis, configuration loading, caching and discovery plugins."""

from __future__ import annotations

import json

import pytest
from tests.conftest import make_catalog, make_event

from eventlineage.analysis import RiskLevel, analyze_catalog, analyze_impact
from eventlineage.config import Config
from eventlineage.core.catalog import EventCatalog
from eventlineage.discovery import default_registry
from eventlineage.discovery.base import coerce_participant
from eventlineage.exceptions import ConfigError
from eventlineage.models.common import Severity
from eventlineage.models.participant import Service
from eventlineage.utils.cache import ScanCache


def chain_catalog() -> EventCatalog:
    """order.created -> payment.captured -> order.shipped -> order.archived."""
    return make_catalog(
        [
            make_event(
                "order.created",
                producers=["order-service"],
                consumers=["payment-service"],
                schema={"type": "object"},
            ),
            make_event(
                "payment.captured",
                producers=["payment-service"],
                consumers=["shipping-service"],
                schema={"type": "object"},
            ),
            make_event(
                "order.shipped",
                producers=["shipping-service"],
                consumers=["archive-service"],
                schema={"type": "object"},
            ),
            make_event(
                "order.archived",
                producers=["archive-service"],
                schema={"type": "object"},
            ),
        ],
        services=[
            Service(name="order-service", tier="tier-1"),
            Service(name="payment-service", tier="tier-1"),
            Service(name="shipping-service", tier="tier-2"),
            Service(name="archive-service", tier="tier-3"),
        ],
    )


class TestImpact:
    def test_direct_and_transitive_consumers(self) -> None:
        report = analyze_impact(chain_catalog(), "order.created")
        assert "payment-service" in set(report.direct_consumers)
        assert "shipping-service" in set(report.transitive_consumers)

    def test_affected_services_ordered_by_depth(self) -> None:
        report = analyze_impact(chain_catalog(), "order.created")
        depths = [service.depth for service in report.affected_services]
        assert depths == sorted(depths)

    def test_max_depth_limits_traversal(self) -> None:
        shallow = analyze_impact(chain_catalog(), "order.created", max_depth=1)
        deep = analyze_impact(chain_catalog(), "order.created")
        assert len(shallow.affected_services) <= len(deep.affected_services)

    def test_risk_increases_with_blast_radius(self) -> None:
        small = analyze_impact(make_catalog([make_event("a.b", consumers=["x"])]), "a.b")
        large = analyze_impact(chain_catalog(), "order.created")
        assert large.risk_score > small.risk_score

    def test_tier_one_services_raise_risk(self) -> None:
        low = analyze_impact(
            make_catalog(
                [make_event("a.b", producers=["p"], consumers=["c"])],
                services=[Service(name="p"), Service(name="c", tier="tier-3")],
            ),
            "a.b",
        )
        high = analyze_impact(
            make_catalog(
                [make_event("a.b", producers=["p"], consumers=["c"])],
                services=[Service(name="p"), Service(name="c", tier="tier-1")],
            ),
            "a.b",
        )
        assert high.risk_score >= low.risk_score

    def test_unknown_target_raises_with_a_hint(self) -> None:
        from eventlineage.exceptions import SymbolNotFoundError

        with pytest.raises(SymbolNotFoundError) as excinfo:
            analyze_impact(chain_catalog(), "no.such.event")
        assert excinfo.value.hint

    def test_report_is_serializable(self) -> None:
        payload = analyze_impact(chain_catalog(), "order.created").to_dict()
        assert payload["target"] == "order.created"
        assert isinstance(payload["affected_services"], list)

    def test_risk_level_ordering(self) -> None:
        assert RiskLevel.LOW.rank < RiskLevel.MEDIUM.rank < RiskLevel.HIGH.rank
        assert RiskLevel.from_score(0) is RiskLevel.LOW
        assert RiskLevel.from_score(100) is RiskLevel.CRITICAL

    def test_schemas_and_topics_are_listed(self) -> None:
        report = analyze_impact(chain_catalog(), "order.created")
        assert report.related_topics or report.related_topics == ()
        assert isinstance(report.affected_events, tuple)


class TestSummary:
    def test_summary_counts(self) -> None:
        summary = analyze_catalog(chain_catalog())
        assert summary.stats.events == 4
        assert summary.stats.services == 4
        assert summary.graph.nodes >= 4
        assert 0 <= summary.health_score <= 100

    def test_cycles_are_reported_once_per_component(self) -> None:
        catalog = make_catalog(
            [
                make_event("a.b", producers=["x"], consumers=["y"]),
                make_event("c.d", producers=["y"], consumers=["x"]),
            ],
            services=[Service(name="x"), Service(name="y")],
        )
        summary = analyze_catalog(catalog)
        assert summary.cycles
        assert len([cycle for cycle in summary.cycles if cycle.kind == "service"]) == 1

    def test_summary_is_deterministic(self) -> None:
        first = analyze_catalog(chain_catalog()).to_dict()
        second = analyze_catalog(chain_catalog()).to_dict()
        assert first == second

    def test_health_grade_is_letter(self) -> None:
        summary = analyze_catalog(chain_catalog())
        assert summary.health_grade in {"A", "B", "C", "D", "F"}


class TestConfig:
    def test_defaults(self, project_dir: object) -> None:
        config = Config.load(root=project_dir, required=True)  # type: ignore[arg-type]
        assert config.version == 1
        assert config.validation.fail_on is not None
        assert config.cache.enabled is True

    def test_yaml_loading(self, make_project: object) -> None:
        root = make_project(  # type: ignore[operator]
            config={"version": 1, "project": {"name": "demo", "root": "."}}
        )
        config = Config.load(root=root, required=True)
        assert config.project.name == "demo"
        assert config.root == root

    def test_missing_file_raises(self, tmp_path: object) -> None:
        with pytest.raises(ConfigError):
            Config.load(root=tmp_path, required=True)  # type: ignore[arg-type]

    def test_missing_file_can_fall_back_to_defaults(self, tmp_path: object) -> None:
        config = Config.load(root=tmp_path)  # type: ignore[arg-type]
        assert config.version == 1

    def test_invalid_yaml_raises(self, make_project: object, tmp_path: object) -> None:
        root = make_project(config="version: 1\nproject: [unclosed\n")  # type: ignore[operator]
        with pytest.raises(ConfigError):
            Config.load(root=root, required=True)

    def test_severity_overrides_accept_yaml_booleans(self, make_project: object) -> None:
        root = make_project(  # type: ignore[operator]
            config={"version": 1, "validation": {"severity": {"EG012": "off"}}}
        )
        config = Config.load(root=root, required=True)
        assert config.validation.severity["EG012"] is Severity.OFF

    def test_environment_overrides(
        self, make_project: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = make_project(config={"version": 1, "project": {"name": "demo"}})  # type: ignore[operator]
        monkeypatch.setenv("EVENTLINEAGE_COMPAT_MODE", "full")
        monkeypatch.setenv("EVENTLINEAGE_NO_CACHE", "1")
        monkeypatch.setenv("EVENTLINEAGE_FAIL_ON", "info")
        config = Config.load(root=root, required=True)
        assert config.compatibility.mode.value == "full"
        assert config.cache.enabled is False
        assert config.validation.fail_on is Severity.INFO

    def test_config_env_var_points_at_a_file(
        self, make_project: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = make_project(config={"version": 1, "project": {"name": "demo"}})  # type: ignore[operator]
        monkeypatch.setenv("EVENTLINEAGE_CONFIG", str(root / "eventlineage.yaml"))
        config = Config.load()
        assert config.project.name == "demo"

    def test_config_is_immutable_copyable(self, project_dir: object) -> None:
        config = Config.load(root=project_dir, required=True)  # type: ignore[arg-type]
        clone = config.model_copy(deep=True)
        assert clone.project.name == config.project.name

    def test_discovery_roots_are_resolved(self, make_project: object) -> None:
        root = make_project(  # type: ignore[operator]
            config={"version": 1, "discovery": {"roots": ["events", "schemas"]}},
        )
        config = Config.load(root=root, required=True)
        assert config.discovery.roots


class TestCache:
    def test_roundtrip(self, tmp_path: object) -> None:
        path = tmp_path / "cache" / "scan.json"  # type: ignore[operator]
        cache = ScanCache(path, "fingerprint")
        cache.put("events/a.yaml", "digest", "catalog", {"events": ["a.b"]})
        cache.save()
        reloaded = ScanCache.load(path, "fingerprint")
        entry = reloaded.get("events/a.yaml", "digest")
        assert entry is not None
        assert entry.payload == {"events": ["a.b"]}
        assert entry.plugin == "catalog"

    def test_digest_change_invalidates(self, tmp_path: object) -> None:
        cache = ScanCache(tmp_path / "cache.json", "fp")  # type: ignore[operator]
        cache.put("a.yaml", "digest-1", "catalog", {"events": []})
        assert cache.get("a.yaml", "digest-2") is None
        assert cache.get("a.yaml", "digest-1") is not None

    def test_corrupt_cache_is_ignored(self, tmp_path: object) -> None:
        path = tmp_path / "cache.json"  # type: ignore[operator]
        path.write_text("{not json", encoding="utf-8")
        cache = ScanCache.load(path, "fp")
        assert cache.get("a.yaml", "digest") is None

    def test_version_mismatch_is_ignored(self, tmp_path: object) -> None:
        path = tmp_path / "cache.json"  # type: ignore[operator]
        path.write_text(json.dumps({"version": 999, "entries": {}}), encoding="utf-8")
        cache = ScanCache.load(path, "fp")
        assert cache.get("a.yaml", "digest") is None

    def test_prune_drops_removed_files(self, tmp_path: object) -> None:
        cache = ScanCache(tmp_path / "cache.json", "fp")  # type: ignore[operator]
        cache.put("a.yaml", "digest", "catalog", {"events": []})
        cache.put("b.yaml", "digest", "catalog", {"events": []})
        cache.prune(keep={"a.yaml"})
        assert cache.get("b.yaml", "digest") is None


class TestDiscoveryRegistry:
    def test_plugins_are_registered(self) -> None:
        names = set(default_registry().names())
        assert {
            "asyncapi",
            "openapi",
            "catalog",
            "avro",
            "json-schema",
            "protobuf",
            "kafka-topics",
            "service-config",
            "schema-registry-export",
        } <= names

    def test_plugin_lookup(self) -> None:
        registry = default_registry()
        plugin = registry.get("avro")
        assert plugin.name == "avro"
        with pytest.raises(KeyError):
            registry.get("does-not-exist")

    def test_coerce_participant_shapes(self) -> None:
        assert coerce_participant("svc", consumer=False).service == "svc"
        assert coerce_participant({"name": "svc"}, consumer=True).service == "svc"
        assert coerce_participant({"service": "svc", "group": "g"}, consumer=True).group == "g"  # type: ignore[attr-defined]
