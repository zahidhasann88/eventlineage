# Schema compatibility

EventLineage compares two versions of an event contract and tells you whether the
change is safe — for whom, and under which rollout order.

```console
$ eventlineage compare --event orders.created 1.2.0 1.3.0
$ eventlineage compare schemas/order.v1.json schemas/order.v2.json
```

## The three modes

A change is never simply "breaking". It is breaking *for a direction of upgrade*,
because producers and consumers are deployed independently.

| Mode | Question it answers | Breaking means |
| --- | --- | --- |
| `backward` | can **new readers** process **old data**? | consumers must be upgraded *after* the producer |
| `forward` | can **old readers** process **new data**? | consumers must be upgraded *before* the producer |
| `full` | both at once | the change needs a lockstep or dual-publish rollout |

`full` is the worst of the two directions, which is what most schema registries
enforce (`FULL_TRANSITIVE` in Confluent terms).

Every detected change is classified in all three directions at once, so a single
comparison tells you the full story:

```console
$ eventlineage compare --event orders.created 1.0.0 1.2.0 --format json | jq '.changes[0].impacts'
{
  "backward": "compatible",
  "forward": "compatible",
  "full": "compatible"
}
```

The mode selected with `--mode` (or `compatibility.mode` in the configuration)
decides which of those three is reported as `impact` on each change and whether
the command exits `1`.

## Impact levels

| Level | Meaning | What to do |
| --- | --- | --- |
| `compatible` | old and new shapes interoperate | ship it |
| `potentially_breaking` | works if consumers are lenient (ignore unknown fields, tolerate nulls) | check the consumer contract, then ship |
| `breaking` | old data cannot be read with the new schema, or vice versa | new event version (or major bump) and a migration plan |

Each change also carries a `remediation` string — the concrete rollout advice for
that specific change, not a generic warning.

## Change types

| Change | Backward | Forward | Notes |
| --- | --- | --- | --- |
| `field_added` (optional) | compatible | compatible | the safe case |
| `field_added` (required) | **breaking** | compatible | new readers demand data old messages lack |
| `field_removed` (optional) | compatible | potentially breaking | old readers may still expect it |
| `field_removed` (required) | compatible | **breaking** | |
| `field_renamed` | **breaking** | **breaking** | compatible only when an alias is declared (Avro `aliases`, JSON Schema `x-alias`); potentially breaking in both directions for Protobuf (same number, new name) |
| `type_changed` (narrowing) | **breaking** | compatible | `long` → `int` |
| `type_changed` (widening) | compatible | potentially breaking | `int` → `long`, `float` → `double` |
| `required_added` | **breaking** | compatible | |
| `required_removed` | compatible | potentially breaking | |
| `nullability_changed` (nullable → non-null) | **breaking** | compatible | old data may hold nulls |
| `nullability_changed` (non-null → nullable) | compatible | potentially breaking | old readers must handle `null` |
| `enum_value_added` | compatible | **breaking** | unless consumers treat enums as open |
| `enum_value_removed` | **breaking** | compatible | |
| `cardinality_changed` | **breaking** | **breaking** | scalar ↔ array/map |
| `constraint_tightened` | **breaking** | compatible | `maxLength`, `minimum`, regex |
| `constraint_loosened` | compatible | potentially breaking | |
| `default_changed` | compatible | compatible | still reported, because it changes behaviour |
| `root_type_changed` | **breaking** | **breaking** | record → enum, object → array |
| `field_number_changed` | **breaking** | **breaking** | Protobuf |

## Per-format semantics

### JSON Schema

- `required` drives required/optional classification; a field missing from
  `required` is optional even if it has no default.
- `type` may be a list (`["string", "null"]`) — the nullability is extracted and
  the remaining type compared.
- `enum`, `const`, `format`, `pattern`, `minimum`/`maximum`, `minLength`/
  `maxLength`, `minItems`/`maxItems` are compared as constraints.
- `$defs`/`definitions` are resolved for local `$ref`s, including a `$ref` or `allOf`
  at the document root; a recursive reference stops at the point of recursion, and
  remote references are never fetched, which keeps comparisons hermetic.
- `x-alias` (a string) or `x-aliases` (a list) on the new property declares a rename.
  Without one, a rename is a removal plus an addition.
- `oneOf`/`anyOf` branches are compared as a set, like Avro unions.
- `additionalProperties` is compared at every level, including the root. Setting it
  to `false` is `constraint_tightened`; dropping that is `constraint_loosened`; a
  missing keyword means `true`.

### Avro

- `aliases` make a rename compatible in both directions.
- A field with a `default` is optional regardless of position; without one it is
  required, and adding it is backward-breaking (Avro's reader schema rules).
- A two-branch union with `null` is reduced to nullability + value type
  (`["null", "string"]`). Larger unions are compared as sets of branches: adding one
  is forward-breaking, removing one is backward-breaking, reordering changes nothing.
- Promotions follow the specification: `int` → `long` → `float` → `double`, and
  `string` ↔ `bytes`.
- `logicalType` is preserved and compared (`decimal`, `timestamp-millis`, …).

### Protobuf

Protobuf is compared on the wire, not on the schema's good looks:

- **field numbers** identify fields; keeping the number under a new name is
  `potentially_breaking` (only JSON encoding sees it), a renumbering or a number
  reused for another wire type is `breaking` in both directions,
- types are compared by wire group (`varint`, `fixed32`, `fixed64`, `bytes`). Inside
  a group a change is compatible or potentially breaking; across groups it is
  breaking, and so are `int*` ↔ `sint*` (zigzag encoded) and `float`/`double` ↔
  `fixed*` (same width, different meaning),
- `repeated` ↔ singular is `cardinality_changed`, breaking both ways,
- `compare old.proto new.proto` covers every top-level message, matched by name, with
  paths such as `Order.total` when a file holds several. A new message is compatible;
  a removed one is potentially breaking, and any field that used it reports its own
  change. Nested messages are compared where a field uses them,
- proto3 enums are open, so `enum_value_added` is compatible backward and only
  potentially breaking forward.

## Where compatibility is used

| Surface | Effect |
| --- | --- |
| `eventlineage compare` | direct comparison, exits `1` when breaking |
| rule `EG010` | compares consecutive versions of every event in the catalog |
| rule `EG009` | compares the versions different producers/consumers are pinned to |
| `eventlineage report` | includes a compatibility section when history exists |
| SDK | `project.version_history()` returns one comparison per version pair |

`participant-schema-incompatibility` (`EG009`) is the rule that catches the
real-world failure: a producer that already emits `1.2.0` while one consumer is
still pinned to `1.0.0`, with a breaking change in between — a bug that no schema
registry will catch for you, because both versions are individually valid.

## In CI

```yaml
compatibility:
  mode: backward
  fail_on_breaking: true   # false: `compare` reports but always exits 0
```

Two practical workflows:

```console
# fail the PR when a contract change breaks a consumer, whatever the mode
$ eventlineage compare --event orders.created "$(git show origin/main:version.txt)" 1.3.0
```

```console
# gate the whole repository
$ eventlineage validate --fail-on error     # includes EG009/EG010
```

`--no-fail` does the same for one run, for reporting jobs that should never fail a
build.

## What it does not do

- It does not validate message *data* against a schema (that is a runtime
  concern and needs the payload).
- It does not make network calls to a registry; feed it an export
  (`registry/subjects.json`) or call the `SchemaRegistryIntegration` adapter from
  Python.
- It does not compare the items of a schema whose root is an array.
- It does not infer intent. A semantically wrong but structurally compatible
  change (`units: cents → dollars` with the same `integer` type) is invisible to
  any structural comparator, including this one.
