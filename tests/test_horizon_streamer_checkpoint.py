import asyncio
from urllib.parse import parse_qs, urlparse

import pytest

from ingestion.checkpoint import CursorCheckpoint, FlushPolicy
from ingestion.horizon_streamer import HorizonStreamer


def _record(token: str, ledger: int = 42) -> dict:
    return {
        "id": token,
        "paging_token": token,
        "ledger": ledger,
        "ledger_close_time": "2026-06-24T09:00:00Z",
        "base_account": "GA1",
        "counter_account": "GA2",
        "base_asset_code": "XLM",
        "counter_asset_code": "USDC",
        "counter_asset_issuer": "GISSUER",
        "base_amount": "1",
        "counter_amount": "2",
        "price": {"n": "2", "d": "1"},
        "base_is_seller": True,
    }


@pytest.mark.asyncio
async def test_streamer_advances_checkpoint_at_flush_interval(tmp_path, monkeypatch):
    checkpoint = CursorCheckpoint(tmp_path / "cursor.json")
    saved_tokens = []
    original_save = checkpoint.save

    def record_save(token, ledger_sequence=None):
        saved_tokens.append(token)
        original_save(token, ledger_sequence)

    monkeypatch.setattr(checkpoint, "save", record_save)
    streamer = HorizonStreamer(
        asyncio.Queue(),
        checkpoint=checkpoint,
        flush_policy=FlushPolicy(max_events=2, max_seconds=999),
        rate_limit=10_000,
    )

    async def events():
        yield _record("100-0")
        yield _record("200-0")
        yield _record("300-0")

    monkeypatch.setattr(streamer, "stream_events", events)
    await streamer.run()

    assert saved_tokens == ["200-0", "300-0"]
    assert checkpoint.load() == "300-0"


@pytest.mark.asyncio
async def test_restart_uses_checkpoint_cursor_in_request(tmp_path, monkeypatch):
    checkpoint = CursorCheckpoint(tmp_path / "cursor.json")
    checkpoint.save("987654-0")
    streamer = HorizonStreamer(asyncio.Queue(), checkpoint=checkpoint)
    requested_urls = []

    class Response:
        def raise_for_status(self):
            return None

    class Client:
        async def get(self, url, headers):
            requested_urls.append(url)
            return Response()

    monkeypatch.setattr("ingestion.horizon_streamer.httpx.AsyncClient", lambda **_: Client())
    await streamer._connect()

    query = parse_qs(urlparse(requested_urls[0]).query)
    assert query["cursor"] == ["987654-0"]


@pytest.mark.asyncio
async def test_gone_checkpoint_falls_back_to_now(tmp_path, monkeypatch):
    checkpoint = CursorCheckpoint(tmp_path / "cursor.json")
    checkpoint.save("987654-0")
    streamer = HorizonStreamer(asyncio.Queue(), checkpoint=checkpoint)
    streamer._running = True

    import httpx

    request = httpx.Request("GET", "https://example/trades")
    response = httpx.Response(410, request=request)
    calls = 0

    async def connect():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.HTTPStatusError("gone", request=request, response=response)
        streamer.stop()
        return _EmptyClient()

    class _EmptyClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def aiter_lines(self):
            if False:
                yield ""

    monkeypatch.setattr(streamer, "_connect", connect)
    assert [item async for item in streamer.stream_events()] == []
    assert streamer._cursor == "now"
    assert checkpoint.load() is None
