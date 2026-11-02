"""RabbitMQ integration.

AMQP has no "list all exchanges" primitive, so enumeration goes through the
management HTTP API (``/api/queues`` and ``/api/exchanges``). EventLineage models
exchanges as topics and queues as consumer groups, which is the mapping that
matches how RabbitMQ-based event architectures are usually described.
"""

from __future__ import annotations

from typing import Any, ClassVar

from eventlineage.exceptions import IntegrationError
from eventlineage.integrations.base import Integration, IntegrationResult
from eventlineage.models.topic import Topic


class RabbitMqIntegration(Integration):
    """RabbitMQ exchanges and queues via the management API."""

    name: ClassVar[str] = "rabbitmq"
    requires: ClassVar[str | None] = "httpx"
    description: ClassVar[str] = "RabbitMQ exchanges and queues (management HTTP API)"

    def __init__(
        self,
        url: str | None = None,
        *,
        username: str | None = None,
        password: str | None = None,
        vhost: str = "/",
        verify: bool = True,
        timeout: float = 10.0,
        client: Any | None = None,
    ) -> None:
        super().__init__(url=url)
        self.url = (url or "").rstrip("/")
        self.username = username
        self.password = password
        self.vhost = vhost
        self.verify = verify
        self.timeout = timeout
        self._client = client

    def client(self) -> Any:
        """Return an HTTP client bound to the management API."""
        if self._client is not None:
            return self._client
        module = self.require_dependency()
        if not self.url:
            raise IntegrationError(
                "rabbitmq integration requires a management API 'url'",
                hint="set integrations.rabbitmq.url (for example http://localhost:15672/api)",
            )
        auth = (self.username, self.password) if self.username else None
        self._client = module.Client(
            base_url=self.url, auth=auth, verify=self.verify, timeout=self.timeout
        )
        return self._client

    def _get(self, path: str) -> Any:
        client = self.client()
        try:
            response = client.get(path)
        except Exception as exc:
            raise IntegrationError(f"rabbitmq request failed: {exc}") from exc
        if getattr(response, "status_code", 200) >= 400:
            raise IntegrationError(
                f"rabbitmq management API returned HTTP {response.status_code} for {path}"
            )
        return response.json()

    def exchanges(self) -> tuple[dict[str, Any], ...]:
        """List exchanges from the management API."""
        payload = self._get(f"/exchanges/{_quote(self.vhost)}") or []
        return tuple(
            entry
            for entry in payload
            if isinstance(entry, dict)
            and entry.get("name")
            and not str(entry["name"]).startswith("amq.")
        )

    def queues(self) -> tuple[dict[str, Any], ...]:
        """List queues from the management API."""
        payload = self._get(f"/queues/{_quote(self.vhost)}") or []
        return tuple(entry for entry in payload if isinstance(entry, dict) and entry.get("name"))

    def bindings(self, exchange: str) -> tuple[dict[str, Any], ...]:
        """Bindings leaving ``exchange``; the exchange listing does not include them."""
        path = f"/exchanges/{_quote(self.vhost)}/{_quote(exchange)}/bindings/source"
        payload = self._get(path) or []
        return tuple(entry for entry in payload if isinstance(entry, dict))

    def collect(self) -> IntegrationResult:
        """Fetch exchanges and queues and turn them into fragments."""
        result = IntegrationResult()
        for exchange in self.exchanges():
            name = str(exchange["name"])
            bindings = tuple(
                sorted(
                    {
                        str(binding.get("destination"))
                        for binding in self.bindings(name)
                        if binding.get("destination")
                    }
                )
            )
            result.add_topic(
                Topic(
                    name=name,
                    broker="rabbitmq",
                    config={
                        "type": str(exchange.get("type", "topic")),
                        "durable": bool(exchange.get("durable", True)),
                        "vhost": self.vhost,
                    },
                    description=f"RabbitMQ {exchange.get('type', 'topic')} exchange",
                    consumers=bindings,
                    source=self.source(f"rabbitmq://{self.vhost}/{name}"),
                )
            )
        for queue in self.queues():
            name = str(queue["name"])
            result.add_topic(
                Topic(
                    name=name,
                    broker="rabbitmq",
                    config={
                        "kind": "queue",
                        "durable": bool(queue.get("durable", True)),
                        "messages": queue.get("messages"),
                        "consumers": queue.get("consumers"),
                        "vhost": self.vhost,
                    },
                    description="RabbitMQ queue",
                    source=self.source(f"rabbitmq://{self.vhost}/{name}"),
                )
            )
        result.notes.append(
            f"discovered {len(result.topics)} exchange(s)/queue(s) on vhost {self.vhost!r}"
        )
        return result


def _quote(path: str) -> str:
    from urllib.parse import quote

    return quote(path, safe="")


__all__ = ["RabbitMqIntegration"]
