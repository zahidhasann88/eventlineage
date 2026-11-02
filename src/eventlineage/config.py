"""Project configuration: ``eventlineage.yaml``.

Configuration is optional: every setting has a default that makes
``eventlineage scan`` work in a directory with no config file at all. A file is
only needed to narrow discovery, change severities, or configure integrations.

Precedence, highest first:

1. explicit CLI flags,
2. environment variables (``EVENTLINEAGE_*``),
3. ``eventlineage.yaml``,
4. built-in defaults.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, PrivateAttr, field_validator, model_validator

from eventlineage.compatibility.result import CompatibilityMode
from eventlineage.exceptions import ConfigError
from eventlineage.models.base import EGBaseModel
from eventlineage.models.common import Severity
from eventlineage.utils.yaml_io import load_yaml

CONFIG_FILENAMES = (
    "eventlineage.yaml",
    "eventlineage.yml",
    ".eventlineage.yaml",
    ".eventlineage.yml",
)
CONFIG_ENV_VAR = "EVENTLINEAGE_CONFIG"

DEFAULT_EXCLUDES: tuple[str, ...] = (
    "**/.git/**",
    "**/.hg/**",
    "**/.svn/**",
    "**/node_modules/**",
    "**/.venv/**",
    "**/venv/**",
    "**/__pycache__/**",
    "**/.mypy_cache/**",
    "**/.pytest_cache/**",
    "**/.ruff_cache/**",
    "**/dist/**",
    "**/build/**",
    "**/target/**",
    "**/.eventlineage/**",
)

_SEMVER_RE = re.compile(r"^\d+(\.\d+)*")


class SuppressionConfig(EGBaseModel):
    """Silence a finding that is a known, accepted exception."""

    rule: str = Field(description="Rule id (``EG002``) or rule name (``event-without-consumer``)")
    subject: str | None = Field(
        default=None, description="Event id, service or topic the suppression applies to"
    )
    path: str | None = Field(
        default=None, description="Glob matched against the finding's source path"
    )
    reason: str | None = None


class ProtobufDiscoveryConfig(EGBaseModel):
    event_suffixes: tuple[str, ...] = ("Event", "Command", "Message")
    use_annotations: bool = Field(
        default=True, description="Treat messages marked with an @event doc comment as events"
    )
    include_services: bool = Field(
        default=True, description="Register gRPC services in .proto files as services"
    )


class DiscoveryConfig(EGBaseModel):
    """How the project tree is walked and which plugins run."""

    roots: tuple[str, ...] = (".",)
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = DEFAULT_EXCLUDES
    plugins: tuple[str, ...] = Field(
        default=(), description="Explicit plugin allow-list; empty means all available plugins"
    )
    disabled_plugins: tuple[str, ...] = ()
    follow_symlinks: bool = False
    max_file_size_kb: int = 4096
    skip_hidden_dirs: bool = True
    protobuf: ProtobufDiscoveryConfig = Field(default_factory=ProtobufDiscoveryConfig)

    @field_validator("exclude", "roots", "include", mode="before")
    @classmethod
    def _coerce_sequences(cls, value: Any) -> Any:
        if isinstance(value, str):
            return (value,)
        return value


class ValidationConfig(EGBaseModel):
    """Validation thresholds, severity overrides and suppressions."""

    fail_on: Severity = Severity.ERROR
    severity: dict[str, Severity] = Field(default_factory=dict)
    disabled_rules: tuple[str, ...] = ()
    suppress: tuple[SuppressionConfig, ...] = ()
    only_rules: tuple[str, ...] = ()

    @field_validator("fail_on", mode="before")
    @classmethod
    def _coerce_fail_on(cls, value: Any) -> Any:
        # YAML reads a bare ``off`` as boolean False.
        return Severity.coerce(value)

    @field_validator("severity", mode="before")
    @classmethod
    def _coerce_severity_map(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        # YAML reads a bare ``off`` as boolean False, so coercion handles bools.
        return {str(key): Severity.coerce(entry) for key, entry in value.items()}


class CompatibilityConfig(EGBaseModel):
    """Schema compatibility defaults for the project."""

    mode: CompatibilityMode = CompatibilityMode.BACKWARD
    fail_on_breaking: bool = Field(
        default=True, description="Whether `compare` exits 1 on a breaking change"
    )

    @field_validator("mode", mode="before")
    @classmethod
    def _coerce_mode(cls, value: Any) -> Any:
        return CompatibilityMode.coerce(value) if value is not None else CompatibilityMode.BACKWARD


class AnalysisConfig(EGBaseModel):
    """Graph and impact analysis settings."""

    infer_relationships: bool = True
    include_topics: bool = True
    max_impact_depth: int | None = None
    risk_high_services: int = 5
    risk_critical_services: int = 15


class CacheConfig(EGBaseModel):
    """Incremental scan cache settings."""

    enabled: bool = True
    directory: str = ".eventlineage/cache"
    max_bytes: int = 64 * 1024 * 1024


class ReportConfig(EGBaseModel):
    """Default report and rendering options."""

    title: str | None = None
    include_schemas: bool = False
    include_diagrams: bool = True
    max_events: int | None = Field(
        default=None, description="Cap the number of events listed in generated reports"
    )
    max_impacted_nodes: int = 200


class IntegrationsConfig(EGBaseModel):
    """Per-integration settings; empty unless configured."""

    kafka: dict[str, Any] = Field(default_factory=dict)
    schema_registry: dict[str, Any] = Field(default_factory=dict)
    rabbitmq: dict[str, Any] = Field(default_factory=dict)
    nats: dict[str, Any] = Field(default_factory=dict)

    @field_validator("kafka", "schema_registry", "rabbitmq", "nats", mode="before")
    @classmethod
    def _empty_to_dict(cls, value: Any) -> Any:
        return {} if value is None else value


class ProjectConfig(EGBaseModel):
    """Project identity and root directory."""

    name: str | None = None
    root: str = "."
    version: str | None = None


class Config(EGBaseModel):
    """The fully resolved EventLineage configuration."""

    version: int = 1
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    compatibility: CompatibilityConfig = Field(default_factory=CompatibilityConfig)
    analysis: AnalysisConfig = Field(default_factory=AnalysisConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)
    integrations: IntegrationsConfig = Field(default_factory=IntegrationsConfig)

    _source_path: Path | None = PrivateAttr(default=None)
    _project_root: Path | None = PrivateAttr(default=None)

    @field_validator("version")
    @classmethod
    def _check_version(cls, value: int) -> int:
        if value != 1:
            raise ValueError(f"unsupported configuration version {value} (this release reads 1)")
        return value

    @model_validator(mode="after")
    def _check_plugin_lists(self) -> Config:
        overlap = set(self.discovery.plugins) & set(self.discovery.disabled_plugins)
        if overlap:
            raise ValueError(
                f"plugins cannot be both enabled and disabled: {', '.join(sorted(overlap))}"
            )
        return self

    @property
    def source_path(self) -> Path | None:
        """Config file the settings were read from, if any."""
        return self._source_path

    @property
    def root(self) -> Path:
        """Absolute project root (config directory, or cwd when config-less)."""
        if self._project_root is not None:
            return self._project_root
        if self._source_path is not None:
            return self._source_path.parent
        return Path.cwd()

    @property
    def project_name(self) -> str:
        """Project name, defaulting to the project directory name."""
        return self.project.name or self.root.name or "eventlineage"

    @classmethod
    def find_config_file(cls, start: Path | str | None = None) -> Path | None:
        """Search ``start`` and its parents for a configuration file."""
        env_path = os.environ.get(CONFIG_ENV_VAR)
        if env_path:
            candidate = Path(env_path).expanduser()
            if not candidate.is_file():
                raise ConfigError(f"{CONFIG_ENV_VAR} points at a missing file", path=candidate)
            return candidate.resolve()
        current = Path(start or Path.cwd()).resolve()
        if current.is_file():
            current = current.parent
        for directory in (current, *current.parents):
            for name in CONFIG_FILENAMES:
                candidate = directory / name
                if candidate.is_file():
                    return candidate
        return None

    @classmethod
    def load(
        cls,
        path: Path | str | None = None,
        *,
        root: Path | str | None = None,
        required: bool = False,
    ) -> Config:
        """Load configuration, searching upwards from ``root`` when needed."""
        config_path: Path | None
        if path is not None:
            config_path = Path(path).expanduser()
            if not config_path.is_file():
                raise ConfigError("configuration file not found", path=config_path)
            config_path = config_path.resolve()
        else:
            config_path = cls.find_config_file(root)

        data: dict[str, Any] = {}
        if config_path is not None:
            try:
                raw = load_yaml(config_path.read_text(encoding="utf-8"))
            except yaml.YAMLError as exc:
                raise ConfigError(f"invalid YAML: {exc}", path=config_path) from exc
            except OSError as exc:  # pragma: no cover - filesystem failure
                raise ConfigError(str(exc), path=config_path) from exc
            if raw is None:
                raw = {}
            if not isinstance(raw, dict):
                raise ConfigError("configuration root must be a mapping", path=config_path)
            data = raw
        elif required:
            from eventlineage.exceptions import ProjectNotFoundError

            raise ProjectNotFoundError(str(Path(root or Path.cwd())))

        try:
            config = cls.model_validate(data)
        except Exception as exc:
            raise ConfigError(str(exc), path=config_path) from exc

        project_root = (
            Path(root)
            if root is not None
            else (config_path.parent if config_path is not None else Path.cwd())
        )
        config_root = Path(config.project.root)
        if not config_root.is_absolute():
            project_root = (project_root / config_root).resolve()
        else:
            project_root = config_root.resolve()
        config._source_path = config_path
        config._project_root = project_root
        try:
            config._apply_environment()
        except ValueError as exc:
            raise ConfigError(f"invalid environment override: {exc}") from exc
        return config

    def _apply_environment(self) -> None:
        """Apply the documented environment overrides."""
        if os.environ.get("EVENTLINEAGE_NO_CACHE"):
            self.cache = self.cache.model_copy(update={"enabled": False})
        if mode := os.environ.get("EVENTLINEAGE_COMPAT_MODE"):
            self.compatibility = self.compatibility.model_copy(
                update={"mode": CompatibilityMode.coerce(mode)}
            )
        if fail_on := os.environ.get("EVENTLINEAGE_FAIL_ON"):
            self.validation = self.validation.model_copy(
                update={"fail_on": Severity.coerce(fail_on)}
            )

    def with_validation_overrides(
        self,
        *,
        fail_on: Severity | None = None,
        only_rules: tuple[str, ...] | None = None,
        disabled_rules: tuple[str, ...] | None = None,
        severity: dict[str, Severity] | None = None,
    ) -> Config:
        """Return a copy with CLI validation flags applied."""
        updates: dict[str, Any] = {}
        if fail_on is not None:
            updates["fail_on"] = fail_on
        if only_rules is not None:
            updates["only_rules"] = only_rules
        if disabled_rules is not None:
            updates["disabled_rules"] = disabled_rules
        if severity:
            merged = {
                **{key: Severity.coerce(value) for key, value in self.validation.severity.items()}
            }
            merged.update(severity)
            updates["severity"] = merged
        if not updates:
            return self
        return self.model_copy(update={"validation": self.validation.model_copy(update=updates)})

    def fingerprint(self) -> str:
        """Hash of the settings that influence scan output (for cache keys)."""
        from eventlineage import __version__

        payload = {
            "eventlineage": __version__,
            "discovery": self.discovery.model_dump(mode="json"),
            "analysis": self.analysis.model_dump(mode="json"),
            "version": self.version,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode()
        ).hexdigest()[:16]

    def to_yaml(self) -> str:
        """Serialize to YAML (used by ``eventlineage init``)."""
        return yaml.safe_dump(
            self._for_dump(), sort_keys=False, default_flow_style=False, allow_unicode=True
        )

    def _for_dump(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_defaults=False, exclude_none=True)


def default_config_document() -> str:
    """Commented starter configuration written by ``eventlineage init``."""
    return """\
# EventLineage configuration — https://github.com/zahidhasann88/eventlineage
version: 1

project:
  name: my-platform
  root: .

discovery:
  # Directories searched for event definitions (relative to this file).
  roots:
    - .
  # Extra glob patterns to include; empty means "every file a plugin recognises".
  include: []
  exclude:
    - "**/node_modules/**"
    - "**/.venv/**"
    - "**/dist/**"
    - "**/.eventlineage/**"
  # Restrict or disable built-in discovery plugins.
  plugins: []
  disabled_plugins: []
  protobuf:
    event_suffixes: [Event, Command, Message]
    use_annotations: true
    include_services: true

validation:
  # error | warning | info | off — the threshold that makes `validate` exit 1.
  fail_on: warning
  # Per-rule severity overrides.
  severity:
    EG002: info
  disabled_rules: []
  # Accepted exceptions, e.g. a legacy event with a known missing producer.
  suppress: []
  #  - rule: EG001
  #    subject: legacy.audit.written
  #    reason: produced by a vendor system outside this repository

compatibility:
  mode: backward      # backward | forward | full
  fail_on_breaking: true

analysis:
  infer_relationships: true
  include_topics: true

cache:
  enabled: true
  directory: .eventlineage/cache

report:
  include_schemas: false
  include_diagrams: true

integrations:
  kafka: {}
  schema_registry: {}
  rabbitmq: {}
  nats: {}
"""


__all__ = [
    "CONFIG_ENV_VAR",
    "CONFIG_FILENAMES",
    "DEFAULT_EXCLUDES",
    "AnalysisConfig",
    "CacheConfig",
    "CompatibilityConfig",
    "Config",
    "DiscoveryConfig",
    "IntegrationsConfig",
    "ProjectConfig",
    "ReportConfig",
    "SuppressionConfig",
    "ValidationConfig",
    "default_config_document",
]
