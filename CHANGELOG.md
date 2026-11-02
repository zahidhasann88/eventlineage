# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-11-02

First public release.

- **Discovery:** nine built-in formats (JSON Schema, Avro, Protobuf, AsyncAPI 2.x and 3.x, OpenAPI,
  Kafka topic exports, Schema Registry exports, native catalogs and service configs), third-party
  plugins, and an incremental scan cache.
- **Dependency graph:** services, events and topics, with traversal, cycle detection, connected
  components, upstream and downstream analysis, and orphan detection.
- **Validation:** 17 rules with configurable severity, suppression, custom rules from installed
  packages, and remediation guidance on every finding.
- **Schema compatibility:** JSON Schema, Avro and Protobuf comparison in backward, forward and full
  modes, separating breaking from potentially breaking changes.
- **Impact analysis:** affected services, events, topics and schemas, with dependency depth and a
  risk level.
- **Reports:** JSON, Markdown, DOT and Mermaid, identical between runs unless a timestamp is requested.
- **CLI:** `init`, `scan`, `analyze`, `validate`, `graph`, `impact`, `events`, `services`, `topics`,
  `compare`, `report`, `export`, `pull`, `rules`, `plugins`, `integrations` and `cache`, with stable
  exit codes.
- **Python SDK:** the `Project` facade and typed models.
- **Integrations:** optional `pull` adapters for Kafka, Schema Registry, RabbitMQ and NATS. Everything
  else works offline.

[0.1.0]: https://github.com/zahidhasann88/eventlineage/releases/tag/v0.1.0
