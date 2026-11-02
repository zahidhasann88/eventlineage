# Example: shop-platform

A small e-commerce platform whose event contracts use every supported format. It needs no broker or
network access.

```console
cd examples/shop-platform
eventlineage scan
eventlineage validate
eventlineage analyze
```

The scan finds 10 events, 8 services, 5 topics and 10 relationships. `validate` exits `1` on purpose,
because the project contains these problems:

| Rule | Problem |
| --- | --- |
| `EG006` | `inventory.reserved` and `orders.created` trigger each other |
| `EG009` | `search-indexer` is pinned to an incompatible version of `orders.created` |
| `EG010` | `orders.created` 1.2.0 has a breaking change |
| `EG001` | `legacy.audit.written` has a consumer but no producer |
| `EG013` | `orders.dlq` has no events |
| `EG014` | Three services are referenced but never declared |

## Try these

```console
eventlineage impact orders.created
eventlineage compare --event orders.created 1.0.0 1.2.0
eventlineage compare --event orders.created 1.0.0 1.2.0 --mode forward
eventlineage graph --services-only --format mermaid
eventlineage report --impact orders.created --output architecture.md
```

## What is in it

| Path | Format |
| --- | --- |
| `catalogs/orders.yaml` | Native catalog: owners, tiers, versions and relationships |
| `asyncapi/payments-api.yaml` | AsyncAPI 2.6 |
| `openapi/notifications.yaml` | OpenAPI with `x-events` and a webhook |
| `kafka/topics.json` | Kafka topic export |
| `registry/subjects.json` | Schema Registry export |
| `proto/inventory.proto` | Protobuf |
| `schemas/` | JSON Schema and Avro files |
| `eventlineage.yaml` | Project configuration, including one suppressed finding |
