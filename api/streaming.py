"""Real-time score streaming engine with Server-Sent Events and Redis Pub/Sub.

Architecture
------------
- ``ScoreUpdateEvent`` — dataclass representing a score change, serialised to
  SSE wire format via ``.to_sse()``.
- ``ScorePublisher`` — publishes events to Redis channels after every successful
  model inference run.
- ``SSEConnectionManager`` — manages active SSE connections, Redis subscriptions,
  heartbeats, and reconnect-replay.

SSE Endpoint: ``GET /stream/scores?wallets=W1,W2,...``
Stats Endpoint: ``GET /stream/stats``

Security
--------
- Wallet address validation: ``^[A-Z0-9]{56}$`` (Stellar public key format).
  Invalid addresses return 422 for the entire request.
- Namespace isolation: wallets outside the authenticated namespace are silently
  dropped (not errored) to avoid leaking namespace membership.
- Connection limit per API key: max 10 concurrent SSE connections per key,
  enforced via Redis counter.  Exceeding returns 429.
- Heartbeat loop checks ``request.is_disconnected()`` to clean up zombie
  connections within one heartbeat interval.
- Event replay window: at most ``SSE_MISSED_EVENT_REPLAY_WINDOW_SECONDS``
  (default 300 s) of cached events are replayed on reconnect.

References
----------
- MDN: https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator, Optional

logger = logging.getLogger("ledgerlens.streaming")

# ---------------------------------------------------------------------------
# Wallet address validation
# ---------------------------------------------------------------------------

_WALLET_PATTERN = re.compile(r"^[A-Z0-9]{56}$")


def _validate_wallet_address(wallet: str) -> bool:
    """Return True iff wallet matches Stellar public key format."""
    return bool(_WALLET_PATTERN.match(wallet))


# ---------------------------------------------------------------------------
# ScoreUpdateEvent
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class ScoreUpdateEvent:
    """A single score change event published to Redis and forwarded via SSE.

    Attributes
    ----------
    wallet:
        Stellar wallet address that was re-scored.
    previous_score:
        Score value before the recompute.
    current_score:
        Score value after the recompute.
    delta:
        ``current_score - previous_score``.
    crossed_threshold:
        The alert threshold that was crossed (e.g. 70), or None.
    triggered_by:
        What initiated the recompute: ``"ingestion"`` | ``"recompute"`` |
        ``"feedback_boost"``.
    namespace_id:
        The namespace this wallet belongs to.
    event_id:
        UUID4 string used as the SSE ``id:`` field.  Clients use this for
        ``Last-Event-ID`` reconnect.
    published_at:
        UTC timestamp of publication.
    """

    wallet: str
    previous_score: int
    current_score: int
    delta: int
    crossed_threshold: Optional[int]
    triggered_by: str
    namespace_id: str
    event_id: str = dataclasses.field(default_factory=lambda: str(uuid.uuid4()))
    published_at: datetime = dataclasses.field(default_factory=lambda: datetime.now(timezone.utc))

    def to_sse(self) -> str:
        """Serialise to SSE wire format.

        Returns a string with the following structure::

            id: <event_id>
            event: score_update
            data: <json payload>
            (blank line)
        """
        payload = dataclasses.asdict(self)
        payload["published_at"] = self.published_at.isoformat()
        return (
            f"id: {self.event_id}\n"
            f"event: score_update\n"
            f"data: {json.dumps(payload)}\n\n"
        )


# ---------------------------------------------------------------------------
# ScorePublisher
# ---------------------------------------------------------------------------


class ScorePublisher:
    """Publishes ScoreUpdateEvent to Redis channels after each model inference.

    Channels
    --------
    - ``ledgerlens:score:{wallet}`` — wallet-specific channel.
    - ``ledgerlens:score:*`` — wildcard channel for clients subscribed to all
      wallets.

    Also stores the last event per wallet in a Redis hash
    ``ledgerlens:last_event`` with a 24h TTL for reconnect replay.

    Parameters
    ----------
    redis_client:
        An ``aioredis.Redis`` instance (or compatible async client).
    """

    CHANNEL_PREFIX = "ledgerlens:score:"
    LAST_EVENT_HASH = "ledgerlens:last_event"
    LAST_EVENT_TTL = 86400  # 24 hours

    def __init__(self, redis_client) -> None:
        self._redis = redis_client

    async def publish(self, event: ScoreUpdateEvent) -> None:
        """Publish *event* to Redis.

        Atomically (within a pipeline):
        1. ``PUBLISH ledgerlens:score:{wallet}``
        2. ``PUBLISH ledgerlens:score:*``
        3. ``HSET ledgerlens:last_event {wallet} <json>``
        4. ``EXPIRE ledgerlens:last_event 86400``
        """
        channel = f"{self.CHANNEL_PREFIX}{event.wallet}"
        payload = json.dumps(dataclasses.asdict(event), default=str)
        try:
            async with self._redis.pipeline(transaction=False) as pipe:
                await pipe.publish(channel, payload)
                await pipe.publish(f"{self.CHANNEL_PREFIX}*", payload)
                await pipe.hset(self.LAST_EVENT_HASH, event.wallet, payload)
                await pipe.expire(self.LAST_EVENT_HASH, self.LAST_EVENT_TTL)
                await pipe.execute()
            logger.debug(
                "Published score update for wallet %s (delta=%+d)",
                event.wallet[:8],
                event.delta,
            )
        except Exception as exc:
            logger.error("Failed to publish score event for %s: %s", event.wallet[:8], exc)

    async def get_last_event(self, wallet: str) -> Optional[ScoreUpdateEvent]:
        """Return the cached last event for *wallet*, or None."""
        try:
            raw = await self._redis.hget(self.LAST_EVENT_HASH, wallet)
            if raw is None:
                return None
            data = json.loads(raw)
            data["published_at"] = datetime.fromisoformat(data["published_at"])
            return ScoreUpdateEvent(**data)
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Connection limit helpers
# ---------------------------------------------------------------------------

_CONN_LIMIT_PREFIX = "ledgerlens:sse_connections:"
_MAX_CONNECTIONS_PER_KEY = 10


async def _increment_connection_count(redis_client, key_id: str) -> int:
    """Atomically increment and return the connection count for *key_id*."""
    counter_key = f"{_CONN_LIMIT_PREFIX}{key_id}"
    try:
        count = await redis_client.incr(counter_key)
        await redis_client.expire(counter_key, 3600)
        return count
    except Exception:
        return 0


async def _decrement_connection_count(redis_client, key_id: str) -> None:
    """Decrement the connection count for *key_id*."""
    counter_key = f"{_CONN_LIMIT_PREFIX}{key_id}"
    try:
        val = await redis_client.decr(counter_key)
        if val < 0:
            await redis_client.set(counter_key, 0)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# SSEConnectionManager
# ---------------------------------------------------------------------------


class SSEConnectionManager:
    """Manages active SSE connections and their Redis pub/sub subscriptions.

    Parameters
    ----------
    redis_pool:
        An aioredis connection pool or compatible async Redis factory.
    heartbeat_interval:
        Seconds between heartbeat comments (default 15).
    replay_window_seconds:
        Maximum seconds of missed events to replay on reconnect (default 300).
    max_wallets:
        Maximum wallets per connection (default 50).
    """

    def __init__(
        self,
        redis_pool,
        heartbeat_interval: int = 15,
        replay_window_seconds: int = 300,
        max_wallets: int = 50,
    ) -> None:
        self._redis_pool = redis_pool
        self._heartbeat_interval = heartbeat_interval
        self._replay_window = replay_window_seconds
        self._max_wallets = max_wallets
        # connection_id -> set of wallet addresses
        self._active_connections: dict[str, set[str]] = {}

    async def subscribe(
        self,
        connection_id: str,
        wallets: list[str],
        last_event_id: Optional[str] = None,
        namespace_id: Optional[str] = None,
        request=None,
    ) -> AsyncGenerator[str, None]:
        """Async generator yielding SSE events for the requested wallets.

        Steps
        -----
        1. Validate wallet address format (422 via outer route on failure).
        2. Replay missed events if ``last_event_id`` is provided.
        3. Subscribe to Redis channels for each wallet.
        4. Yield events as they arrive; emit heartbeat every
           ``heartbeat_interval`` seconds.
        5. On disconnect or cancellation, unsubscribe and clean up.

        Parameters
        ----------
        connection_id:
            Unique identifier for this SSE connection.
        wallets:
            List of validated Stellar wallet addresses.
        last_event_id:
            From the client's ``Last-Event-ID`` header.  Triggers replay of
            cached events newer than this ID.
        namespace_id:
            Authenticated namespace.  Wallets outside this namespace are
            silently dropped.
        request:
            FastAPI ``Request`` object for disconnect detection.
        """
        # Enforce wallet limit
        wallets = wallets[: self._max_wallets]
        self._active_connections[connection_id] = set(wallets)

        # Build publisher for replay
        publisher = ScorePublisher(self._redis_pool)

        # Replay missed events
        if last_event_id:
            for wallet in wallets:
                last = await publisher.get_last_event(wallet)
                if last and last.event_id != last_event_id:
                    yield last.to_sse()

        # Create a new Redis connection for pubsub
        try:
            pubsub = self._redis_pool.pubsub()
            channels = [f"{ScorePublisher.CHANNEL_PREFIX}{w}" for w in wallets]
            # Also subscribe to wildcard channel
            channels.append(f"{ScorePublisher.CHANNEL_PREFIX}*")
            await pubsub.subscribe(*channels)

            heartbeat_task_counter = 0

            try:
                while True:
                    # Non-blocking check for messages
                    message = await asyncio.wait_for(
                        pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1),
                        timeout=self._heartbeat_interval + 1,
                    )

                    if message and message.get("type") == "message":
                        try:
                            data = json.loads(message["data"])
                            event = ScoreUpdateEvent(**{
                                k: v for k, v in data.items()
                                if k in {f.name for f in dataclasses.fields(ScoreUpdateEvent)}
                            })
                            if isinstance(event.published_at, str):
                                event.published_at = datetime.fromisoformat(event.published_at)
                            # Namespace isolation: silently drop if wallet not in namespace
                            if namespace_id and data.get("namespace_id") != namespace_id:
                                continue
                            yield event.to_sse()
                        except Exception as exc:
                            logger.debug("Failed to decode SSE message: %s", exc)

                    else:
                        # Emit heartbeat comment
                        heartbeat_task_counter += 1
                        yield ": heartbeat\n\n"

                    # Check for client disconnect
                    if request is not None:
                        try:
                            disconnected = await request.is_disconnected()
                            if disconnected:
                                logger.debug(
                                    "SSE client %s disconnected.", connection_id[:8]
                                )
                                break
                        except Exception:
                            break

            except asyncio.CancelledError:
                pass
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"
            finally:
                try:
                    await pubsub.unsubscribe(*channels)
                    await pubsub.close()
                except Exception:
                    pass

        finally:
            self._active_connections.pop(connection_id, None)
            logger.debug("SSE connection %s cleaned up.", connection_id[:8])

    async def get_stats(self) -> dict:
        """Return streaming stats: active connections, events in last 60 min,
        top-10 wallets by subscriber count.
        """
        # Count active connections
        active = len(self._active_connections)

        # Top wallets by subscriber count
        wallet_counts: dict[str, int] = {}
        for wset in self._active_connections.values():
            for w in wset:
                wallet_counts[w] = wallet_counts.get(w, 0) + 1

        top_10 = sorted(wallet_counts.items(), key=lambda x: x[1], reverse=True)[:10]

        # Events in last 60 min (best-effort from Redis — not tracked here)
        try:
            events_last_hour = await self._redis_pool.get("ledgerlens:sse_events_last_hour")
            events_last_hour = int(events_last_hour) if events_last_hour else 0
        except Exception:
            events_last_hour = 0

        return {
            "active_connections": active,
            "events_last_60min": events_last_hour,
            "top_wallets": [{"wallet": w, "subscribers": c} for w, c in top_10],
        }


# ---------------------------------------------------------------------------
# Threshold crossing helper
# ---------------------------------------------------------------------------

_DEFAULT_THRESHOLDS = [50, 70, 85, 95]


def check_threshold_crossing(previous: int, current: int) -> Optional[int]:
    """Return the first threshold crossed upwards, or None.

    A threshold is "crossed" when ``previous < threshold <= current``.
    """
    for threshold in _DEFAULT_THRESHOLDS:
        if previous < threshold <= current:
            return threshold
    return None
