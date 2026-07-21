import asyncio
import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from config.settings import settings
from detection.risk_score import RiskScore

logger = logging.getLogger("ledgerlens.event_bus")


@dataclass
class PublishResult:
    published: int
    failed: int
    errors: list[str]


class RiskScoreEventBus(ABC):
    @abstractmethod
    def publish(self, scores: list[RiskScore]) -> PublishResult: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def get_health(self) -> dict[str, Any] | None:
        """Returns health status dictionary or None if disabled."""
        ...


def _serialize_event(score: RiskScore) -> bytes:
    """Serialize a RiskScore to a versioned JSON envelope."""
    payload = score.model_dump()
    # Ensure datetime is isoformat string
    payload["timestamp"] = score.timestamp.isoformat()
    if payload.get("latency_ms") is None:
        payload.pop("latency_ms", None)
    
    # Exclude None values for conformal prediction fields if not present
    if payload.get("score_lower") is None:
        payload.pop("score_lower", None)
    if payload.get("score_upper") is None:
        payload.pop("score_upper", None)
    if payload.get("prediction_set") is None:
        payload.pop("prediction_set", None)
    if payload.get("coverage_guarantee") is None:
        payload.pop("coverage_guarantee", None)

    envelope = {
        "schema_version": 1,
        "event": "risk_score.updated",
        "produced_at": datetime.now(timezone.utc).isoformat(),
        "producer": "ledgerlens-core",
        "data": payload,
    }
    return json.dumps(envelope).encode("utf-8")


class NullEventBus(RiskScoreEventBus):
    """No-op used when EVENT_BUS_BACKEND == 'none' (the default)."""
    
    def publish(self, scores: list[RiskScore]) -> PublishResult:
        if scores:
            logger.debug("NullEventBus ignoring %d scores", len(scores))
        return PublishResult(published=len(scores), failed=0, errors=[])
        
    def close(self) -> None:
        pass

    def get_health(self) -> dict[str, Any] | None:
        return None


class KafkaRiskScoreBus(RiskScoreEventBus):
    def __init__(self, bootstrap_servers: str, topic: str, sasl_password: str = "", client_id: str = "ledgerlens-core"):
        self.topic = topic
        self._last_publish = None
        self._failures = 0
        try:
            from confluent_kafka import Producer
        except ImportError:
            logger.warning("confluent-kafka not installed. Degrading KafkaRiskScoreBus to NullEventBus behavior.")
            self._producer = None
            return

        conf: dict[str, Any] = {
            "bootstrap.servers": bootstrap_servers,
            "client.id": client_id,
            "acks": "all",
            "message.timeout.ms": int(settings.event_bus_publish_timeout_seconds * 1000)
        }
        if sasl_password:
            conf["security.protocol"] = "SASL_SSL"
            conf["sasl.mechanism"] = "PLAIN"
            conf["sasl.username"] = "token" # assuming token based auth or similar convention
            conf["sasl.password"] = sasl_password
            
        self._producer = Producer(conf)

    def publish(self, scores: list[RiskScore]) -> PublishResult:
        if not self._producer:
            logger.warning("Kafka producer not initialized (confluent-kafka missing). Dropping %d scores.", len(scores))
            return PublishResult(published=0, failed=len(scores), errors=["confluent-kafka missing"])

        published = 0
        failed = 0
        errors = []

        for score in scores:
            key = f"{score.wallet}:{score.asset_pair}".encode("utf-8")
            value = _serialize_event(score)
            
            for attempt in range(settings.event_bus_max_retries):
                try:
                    self._producer.produce(self.topic, key=key, value=value)
                    published += 1
                    break
                except Exception as e:
                    if attempt == settings.event_bus_max_retries - 1:
                        failed += 1
                        self._failures += 1
                        errors.append(str(e))
                        logger.error("Failed to publish to Kafka after %d retries: %s", settings.event_bus_max_retries, str(e))
                    else:
                        time.sleep(settings.event_bus_retry_backoff_seconds)
                        
        if self._producer:
            self._producer.flush(timeout=settings.event_bus_publish_timeout_seconds)

        if published > 0:
            self._last_publish = datetime.now(timezone.utc).isoformat()

        return PublishResult(published=published, failed=failed, errors=errors)

    def close(self) -> None:
        if self._producer:
            self._producer.flush()

    def get_health(self) -> dict[str, Any]:
        if not self._producer:
            return {"status": "degraded", "reason": "confluent-kafka missing or not initialized", "failures": self._failures, "last_publish": self._last_publish}
        return {"status": "ok", "failures": self._failures, "last_publish": self._last_publish}


class NATSRiskScoreBus(RiskScoreEventBus):
    def __init__(self, servers: str, subject: str, token: str = "", stream: str = "LEDGERLENS_RISKSCORES"):
        self.servers = servers
        self.subject = subject
        self.token = token
        self.stream = stream
        self._nc = None
        self._js = None
        self._last_publish = None
        self._failures = 0
        
        try:
            import nats  # noqa: F401 -- availability probe; re-imported properly in _connect()
        except ImportError:
            logger.warning("nats-py not installed. Degrading NATSRiskScoreBus to NullEventBus behavior.")
            return
            
        # We need async initialization, but this is a synchronous method in the pipeline.
        # It's better to implement an async publish method or handle event loop inside.
        # But for nats-py which is async, we'll need an event loop.
        # Let's write a synchronous wrapper around it for the pipeline.
        self._loop = asyncio.new_event_loop()
        self._loop.run_until_complete(self._connect())

    async def _connect(self):
        try:
            import nats
            opts = {"servers": self.servers.split(",")}
            if self.token:
                opts["token"] = self.token
            self._nc = await nats.connect(**opts)
            self._js = self._nc.jetstream()
            
            try:
                await self._js.add_stream(name=self.stream, subjects=[self.subject])
            except Exception:
                # Stream might already exist
                pass
        except Exception as e:
            logger.error("Failed to connect to NATS: %s", str(e))
            self._nc = None

    def publish(self, scores: list[RiskScore]) -> PublishResult:
        if not self._nc or not self._js:
            return PublishResult(published=0, failed=len(scores), errors=["NATS not connected"])

        published = 0
        failed = 0
        errors = []

        async def _publish_all():
            nonlocal published, failed, errors
            for score in scores:
                value = _serialize_event(score)
                for attempt in range(settings.event_bus_max_retries):
                    try:
                        # NATS JetStream uses Nats-Msg-Id for deduplication if needed, but we rely on downstream idempotency
                        await self._js.publish(self.subject, value, timeout=settings.event_bus_publish_timeout_seconds)
                        published += 1
                        break
                    except Exception as e:
                        if attempt == settings.event_bus_max_retries - 1:
                            failed += 1
                            self._failures += 1
                            errors.append(str(e))
                            logger.error("Failed to publish to NATS after %d retries: %s", settings.event_bus_max_retries, str(e))
                        else:
                            await __import__('asyncio').sleep(settings.event_bus_retry_backoff_seconds)
                            
        self._loop.run_until_complete(_publish_all())
        if published > 0:
            self._last_publish = datetime.now(timezone.utc).isoformat()
        return PublishResult(published=published, failed=failed, errors=errors)

    def close(self) -> None:
        if self._nc:
            async def _close():
                await self._nc.close()
            self._loop.run_until_complete(_close())
            self._loop.close()

    def get_health(self) -> dict[str, Any]:
        if not self._nc or not self._js:
            return {"status": "degraded", "reason": "NATS not connected", "failures": self._failures, "last_publish": self._last_publish}
        return {"status": "ok", "failures": self._failures, "last_publish": self._last_publish}


_bus_instance: RiskScoreEventBus | None = None

def get_event_bus() -> RiskScoreEventBus:
    global _bus_instance
    if _bus_instance is not None:
        return _bus_instance
        
    backend = settings.event_bus_backend.lower()
    if backend == "kafka":
        _bus_instance = KafkaRiskScoreBus(
            bootstrap_servers=settings.event_bus_kafka_bootstrap_servers,
            topic=settings.event_bus_kafka_topic,
            sasl_password=settings.event_bus_kafka_sasl_password,
        )
    elif backend == "nats":
        _bus_instance = NATSRiskScoreBus(
            servers=settings.event_bus_nats_servers,
            subject=settings.event_bus_nats_subject,
            token=settings.event_bus_nats_token,
        )
    else:
        _bus_instance = NullEventBus()
        
    return _bus_instance
