"""Deterministic synthetic topologies shared by the benchmarks."""

from __future__ import annotations

from eventlineage import Consumer, Event, EventCatalog, Producer, Service


def layered_catalog(
    events: int = 10_000,
    services: int = 500,
    *,
    fan_out: int = 2,
    topics: int = 100,
) -> EventCatalog:
    """A layered topology: one producer and ``fan_out`` consumers per event.

    This mirrors a real platform far better than a random graph: most services
    own a few events, a handful of core events are consumed broadly, and the
    event-to-event flow graph is a DAG with a realistic branching factor.
    """
    catalog = EventCatalog(project_name="synthetic")
    for index in range(events):
        producer = f"svc-{index % services:04d}"
        consumers = tuple(
            Consumer(service=f"svc-{(index * 7 + offset) % services:04d}")
            for offset in range(1, fan_out + 1)
        )
        catalog.add_event(
            Event(
                id=f"event.{index:05d}",
                name=f"event.{index:05d}",
                version="1.0.0",
                producers=(Producer(service=producer),),
                consumers=consumers,
                topics=(f"topic.{index % topics:03d}",),
            )
        )
    for index in range(services):
        catalog.add_service(Service(name=f"svc-{index:04d}", tier="tier-2"))
    return catalog.normalize()
