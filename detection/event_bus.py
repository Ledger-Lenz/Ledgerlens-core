import asyncio
import importlib.util
import json
import logging
import sqlite3
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from config.settings import settings
from detection.risk_score import RiskScore
from detection.tracing import current_trace_id

logger = logging.getLogger("ledgerlens.event_bus")


@dataclass
class PublishResult:
    published: int
    failed: int
    errors: list[str]


@dataclass
class DeadLetter:
    id: int
    backend: str
    key: bytes | None
    value: bytes
    error: str
    replay_attempts: int
    created_at: str


@dataclass
class ReplayResult:
    replayed: int
    failed: int
    remaining: int


_DLQ_SCHEMA = """
CREATE TABLE IF NOT EXISTS event_bus_dead_letters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    backend TEXT NOT NULL,
    key BLOB,
    value BLOB NOT NULL,
    error TEXT NOT NULL,
    replay_attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
"""


class EventBusDeadLetterStore:
    """SQLite-backed dead-letter store for events that exhausted their retry budget.

    Persisted (rather than in-memory) so events survive restarts and can be
    replayed from a separate process via ``ledgerlens event-bus-replay``.
    """

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path or settings.db_path)
        try:
            conn.executescript(_DLQ_SCHEMA)
            with conn:
                yield conn
        finally:
            conn.close()

    def add(self, backend: str, key: bytes | None, value: bytes, error: str) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO event_bus_dead_letters (backend, key, value, error, created_at) VALUES (?, ?, ?, ?, ?)",
                    (backend, key, value, error[:500], datetime.now(timezone.utc).isoformat()),
                )
        except sqlite3.Error:
            # Never let the dead-letter path break publishing; the counter
            # below still fires so the loss is alertable.
            logger.critical("Failed to persist dead-lettered event (backend=%s)", backend, exc_info=True)
        try:
            from api.metrics import event_bus_dead_lettered_total

            event_bus_dead_lettered_total.labels(backend=backend).inc()
        except Exception:
            pass
        logger.error("Event dead-lettered after exhausting retries (backend=%s): %s", backend, error)

    def entries(self, limit: int | None = None) -> list[DeadLetter]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, backend, key, value, error, replay_attempts, created_at "
                "FROM event_bus_dead_letters ORDER BY id LIMIT ?",
                (-1 if limit is None else limit,),
            ).fetchall()
        return [DeadLetter(*row) for row in rows]

    def count(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM event_bus_dead_letters").fetchone()[0]

    def oldest_age_seconds(self) -> float:
        with self._connect() as conn:
            row = conn.execute("SELECT MIN(created_at) FROM event_bus_dead_letters").fetchone()
        if not row or row[0] is None:
            return 0.0
        return (datetime.now(timezone.utc) - datetime.fromisoformat(row[0])).total_seconds()

    def remove(self, entry_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM event_bus_dead_letters WHERE id = ?", (entry_id,))

    def record_failed_replay(self, entry_id: int, error: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE event_bus_dead_letters SET replay_attempts = replay_attempts + 1, error = ? WHERE id = ?",
                (error[:500], entry_id),
            )


_dead_letter_store = EventBusDeadLetterStore()


def get_dead_letter_store() -> EventBusDeadLetterStore:
    return _dead_letter_store


class RiskScoreEventBus(ABC):
    backend = "none"

    @abstractmethod
    def publish(self, scores: list[RiskScore]) -> PublishResult: ...

    def send_raw(self, key: bytes | None, value: bytes) -> None:
        """Deliver one pre-serialised event, raising on failure (used by replay)."""
        raise NotImplementedError(f"{type(self).__name__} cannot replay events")

    def replay_dead_letters(
        self, store: EventBusDeadLetterStore | None = None, limit: int | None = None
    ) -> ReplayResult:
        """Re-send dead-lettered events; successes are removed, failures stay queued."""
        store = store or get_dead_letter_store()
        replayed = failed = 0
        for entry in store.entries(limit=limit):
            try:
                self.send_raw(entry.key, entry.value)
            except Exception as e:
                failed += 1
                store.record_failed_replay(entry.id, str(e))
                result = "failed"
            else:
                replayed += 1
                store.remove(entry.id)
                result = "replayed"
            try:
                from api.metrics import event_bus_dead_letter_replays_total

                event_bus_dead_letter_replays_total.labels(result=result).inc()
            except Exception:
                pass
        return ReplayResult(replayed=replayed, failed=failed, remaining=store.count())

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
    trace_id = current_trace_id()
    if trace_id:
        envelope["trace_id"] = trace_id
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
    backend = "kafka"

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
                        get_dead_letter_store().add(self.backend, key, value, str(e))
                    else:
                        time.sleep(settings.event_bus_retry_backoff_seconds)
                        
        if self._producer:
            self._producer.flush(timeout=settings.event_bus_publish_timeout_seconds)

        if published > 0:
            self._last_publish = datetime.now(timezone.utc).isoformat()

        return PublishResult(published=published, failed=failed, errors=errors)

    def send_raw(self, key: bytes | None, value: bytes) -> None:
        if not self._producer:
            raise RuntimeError("confluent-kafka missing")
        self._producer.produce(self.topic, key=key, value=value)
        if self._producer.flush(timeout=settings.event_bus_publish_timeout_seconds):
            raise RuntimeError("Kafka flush timed out")

    def close(self) -> None:
        if self._producer:
            self._producer.flush()

    def get_health(self) -> dict[str, Any]:
        if not self._producer:
            return {"status": "degraded", "reason": "confluent-kafka missing or not initialized", "failures": self._failures, "last_publish": self._last_publish}
        return {"status": "ok", "failures": self._failures, "last_publish": self._last_publish}


class NATSRiskScoreBus(RiskScoreEventBus):
    backend = "nats"

    def __init__(self, servers: str, subject: str, token: str = "", stream: str = "LEDGERLENS_RISKSCORES"):
        self.servers = servers
        self.subject = subject
        self.token = token
        self.stream = stream
        self._nc = None
        self._js = None
        self._last_publish = None
        self._failures = 0

        if importlib.util.find_spec("nats") is None:
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
                key = f"{score.wallet}:{score.asset_pair}".encode("utf-8")
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
                            get_dead_letter_store().add(self.backend, key, value, str(e))
                        else:
                            await asyncio.sleep(settings.event_bus_retry_backoff_seconds)
                            
        self._loop.run_until_complete(_publish_all())
        if published > 0:
            self._last_publish = datetime.now(timezone.utc).isoformat()
        return PublishResult(published=published, failed=failed, errors=errors)

    def send_raw(self, key: bytes | None, value: bytes) -> None:
        if not self._nc or not self._js:
            raise RuntimeError("NATS not connected")
        self._loop.run_until_complete(
            self._js.publish(self.subject, value, timeout=settings.event_bus_publish_timeout_seconds)
        )

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
_bus_lock: threading.Lock = threading.Lock()


def get_event_bus() -> RiskScoreEventBus:
    global _bus_instance
    if _bus_instance is not None:
        return _bus_instance

    with _bus_lock:
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
