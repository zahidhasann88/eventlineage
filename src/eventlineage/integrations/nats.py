"""NATS / JetStream integration.

Streams are the durable, replayable event containers in JetStream, so they map
to topics; their subjects are preserved in ``config`` for further analysis.
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from eventlineage.exceptions import IntegrationError
from eventlineage.integrations.base import Integration, IntegrationResult
from eventlineage.models.topic import Topic


class NatsIntegration(Integration):
    """JetStream streams via ``nats-py``."""

    name: ClassVar[str] = "nats"
    requires: ClassVar[str | None] = "nats"
    description: ClassVar[str] = "NATS JetStream streams and subjects"

    def __init__(
        self,
        servers: str | list[str] | None = None,
        *,
        client: Any | None = None,
        timeout: float = 5.0,
    ) -> None:
        super().__init__(servers=servers)
        self.servers = servers
        self.timeout = timeout
        self._client = client

    async def _streams(self) -> list[Any]:
        if self._client is not None:
            client = self._client
        else:
            module = self.require_dependency()
            client = await module.connect(
                servers=self.servers or "nats://localhost:4222", connect_timeout=self.timeout
            )
        try:
            jetstream = client.jetstream()
            return list(await jetstream.streams_info())
        finally:
            if self._client is None:
                await client.close()

    def collect(self) -> IntegrationResult:
        """Fetch stream subjects from the NATS server."""
        try:
            streams = asyncio.run(self._streams())
        except IntegrationError:
            raise
        except Exception as exc:
            raise IntegrationError(f"could not list NATS streams: {exc}") from exc
        result = IntegrationResult()
        for stream in streams:
            config = getattr(stream, "config", None)
            name = str(getattr(config, "name", "") or "")
            if not name:
                continue
            subjects = list(getattr(config, "subjects", []) or [])
            result.add_topic(
                Topic(
                    name=name,
                    broker="nats",
                    config={
                        "subjects": subjects,
                        "retention": str(getattr(config, "retention", "")),
                        "max_msgs": getattr(config, "max_msgs", None),
                        "storage": str(getattr(config, "storage", "")),
                    },
                    description="NATS JetStream stream",
                    metadata={"subjects": subjects},
                    source=self.source(f"nats://stream/{name}"),
                )
            )
        result.notes.append(f"discovered {len(result.topics)} NATS stream(s)")
        return result


__all__ = ["NatsIntegration"]
