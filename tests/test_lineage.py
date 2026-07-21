import json
import logging
import queue
import sqlite3
import pytest
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from config.settings import settings
from detection.lineage import LineageEmitter, Dataset, get_lineage_graph
from api.main import app


@pytest.fixture
def clean_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "ledgerlens.db")
    monkeypatch.setattr(settings, "db_path", db_path)
    
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS lineage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            event_time TEXT NOT NULL,
            run_id TEXT NOT NULL,
            parent_run_id TEXT,
            job_namespace TEXT NOT NULL,
            job_name TEXT NOT NULL,
            inputs_json TEXT NOT NULL,
            outputs_json TEXT NOT NULL,
            producer TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    conn.commit()
    conn.close()
    return db_path


def test_lineage_context_manager_success(clean_db, monkeypatch):
    monkeypatch.setattr(settings, "lineage_enabled", True)
    monkeypatch.setattr(settings, "lineage_backend", "none")
    
    emitter = LineageEmitter()
    
    inputs = [Dataset(namespace="ns", name="in_ds")]
    with emitter.run("job_success", inputs) as run:
        assert run.run_id != ""
        run.add_output(Dataset(namespace="ns", name="out_ds"))
        
    emitter.stop()
    
    # Check SQLite DB
    conn = sqlite3.connect(clean_db)
    cursor = conn.execute("SELECT event_type, job_name, inputs_json, outputs_json FROM lineage_events ORDER BY id")
    rows = cursor.fetchall()
    conn.close()
    
    assert len(rows) == 2
    assert rows[0][0] == "START"
    assert rows[0][1] == "job_success"
    assert "in_ds" in rows[0][2]
    
    assert rows[1][0] == "COMPLETE"
    assert rows[1][1] == "job_success"
    assert "out_ds" in rows[1][3]


def test_lineage_context_manager_fail(clean_db, monkeypatch):
    monkeypatch.setattr(settings, "lineage_enabled", True)
    monkeypatch.setattr(settings, "lineage_backend", "none")
    
    emitter = LineageEmitter()
    
    inputs = [Dataset(namespace="ns", name="in_ds")]
    
    with pytest.raises(ValueError, match="Boom"):
        with emitter.run("job_fail", inputs):
            raise ValueError("Boom")
            
    emitter.stop()
    
    # Check SQLite DB
    conn = sqlite3.connect(clean_db)
    cursor = conn.execute("SELECT event_type, job_name FROM lineage_events ORDER BY id")
    rows = cursor.fetchall()
    conn.close()
    
    assert len(rows) == 2
    assert rows[0][0] == "START"
    assert rows[1][0] == "FAIL"


def test_console_backend(clean_db, monkeypatch, caplog):
    monkeypatch.setattr(settings, "lineage_enabled", True)
    monkeypatch.setattr(settings, "lineage_backend", "console")
    
    emitter = LineageEmitter()
    
    with caplog.at_level(logging.INFO, logger="ledgerlens.lineage"):
        with emitter.run("job_console", []):
            pass
        emitter.stop()
    
    # Assert JSON logged
    console_logs = [rec.message for rec in caplog.records if "OpenLineage event:" in rec.message]
    assert len(console_logs) >= 2
    
    # Verify START
    start_log = console_logs[0]
    payload = json.loads(start_log.replace("OpenLineage event: ", ""))
    assert payload["eventType"] == "START"
    assert payload["job"]["name"] == "job_console"


def test_http_backend(clean_db, monkeypatch, mock_httpx_post):
    monkeypatch.setattr(settings, "lineage_enabled", True)
    monkeypatch.setattr(settings, "lineage_backend", "http")
    monkeypatch.setattr(settings, "openlineage_url", "http://marquez:5000")
    
    emitter = LineageEmitter()
    
    with emitter.run("job_http", []):
        pass
        
    emitter.stop()
    
    assert mock_httpx_post.call_count >= 2
    args, kwargs = mock_httpx_post.call_args
    assert args[0] == "http://marquez:5000/api/v1/lineage"
    assert "json" in kwargs


@pytest.fixture
def mock_httpx_post(monkeypatch):
    mock_post = MagicMock()
    mock_post.return_value.status_code = 200
    monkeypatch.setattr("httpx.post", mock_post)
    return mock_post


def test_http_backend_failure_resilience(clean_db, monkeypatch, caplog):
    monkeypatch.setattr(settings, "lineage_enabled", True)
    monkeypatch.setattr(settings, "lineage_backend", "http")
    monkeypatch.setattr(settings, "openlineage_url", "http://marquez:5000")
    
    def raise_err(*args, **kwargs):
        raise RuntimeError("Network split!")
        
    monkeypatch.setattr("httpx.post", raise_err)
    
    emitter = LineageEmitter()
    
    # Context manager should not crash and pipeline should proceed
    with caplog.at_level(logging.ERROR, logger="ledgerlens.lineage"):
        with emitter.run("job_http_resilience", []):
            pass
        emitter.stop()
    
    errors = [rec.message for rec in caplog.records if "Failed to post lineage event to HTTP backend" in rec.message]
    assert len(errors) >= 2


def test_lineage_queue_drops_when_full(clean_db, monkeypatch, caplog):
    monkeypatch.setattr(settings, "lineage_enabled", True)
    monkeypatch.setattr(settings, "lineage_backend", "none")
    # Set max queue size to 1
    monkeypatch.setattr(settings, "lineage_queue_maxsize", 1)
    
    # We subclass/monkeypatch the worker thread to block or simply not process so queue fills up
    emitter = LineageEmitter()
    # Force queue to fill by shutting down worker thread or placing dummy elements
    while not emitter._queue.full():
        try:
            emitter._queue.put_nowait({"dummy": "event"})
        except queue.Full:
            break
            
    with caplog.at_level(logging.WARNING, logger="ledgerlens.lineage"):
        # This will emit START, which should fail to put_nowait and log warning
        with emitter.run("job_dropped", []):
            pass
        emitter.stop()
    
    warnings = [rec.message for rec in caplog.records if "Lineage queue limit reached" in rec.message]
    assert len(warnings) > 0


def test_lineage_api_endpoint(clean_db, monkeypatch):
    # Setup test events in SQLite
    conn = sqlite3.connect(clean_db)
    conn.execute(
        """
        INSERT INTO lineage_events (
            event_type, event_time, run_id, parent_run_id, job_namespace, job_name, inputs_json, outputs_json, producer
        ) VALUES ('COMPLETE', '2026-07-18T12:00:00Z', 'run-1', NULL, 'ledgerlens-core', 'ingestion.historical_loader.fetch_chunk',
        '[{"namespace": "horizon", "name": "trades", "facets": {}}]',
        '[{"namespace": "ledgerlens-core.sqlite", "name": "trades", "facets": {}}]',
        'test')
        """
    )
    conn.execute(
        """
        INSERT INTO lineage_events (
            event_type, event_time, run_id, parent_run_id, job_namespace, job_name, inputs_json, outputs_json, producer
        ) VALUES ('COMPLETE', '2026-07-18T12:01:00Z', 'run-2', 'run-1', 'ledgerlens-core', 'feature_engineering.build_feature_vector',
        '[{"namespace": "ledgerlens-core.sqlite", "name": "trades", "facets": {}}]',
        '[{"namespace": "ledgerlens-core.sqlite", "name": "feature_distribution_snapshots", "facets": {}}]',
        'test')
        """
    )
    conn.commit()
    conn.close()

    client = TestClient(app)

    # 1. 503 if admin key is unset/empty
    original_key = settings.ledgerlens_admin_api_key
    try:
        object.__setattr__(settings, "ledgerlens_admin_api_key", "")
        resp = client.get("/v1/admin/lineage/trades")
        assert resp.status_code == 503

        # 2. 401 if admin key is set but header missing
        object.__setattr__(settings, "ledgerlens_admin_api_key", "secret-admin-key")
        resp = client.get("/v1/admin/lineage/trades")
        assert resp.status_code == 401

        # 3. 403 if admin key is set but header is wrong
        resp = client.get("/v1/admin/lineage/trades", headers={"X-LedgerLens-Admin-Key": "wrong-key"})
        assert resp.status_code == 403

        # 4. Correct graph shape when admin key matches
        resp = client.get("/v1/admin/lineage/trades", headers={"X-LedgerLens-Admin-Key": "secret-admin-key"})
        assert resp.status_code == 200
        graph = resp.json()
        assert "nodes" in graph
        assert "edges" in graph
        
        node_ids = {nd["id"] for nd in graph["nodes"]}
        assert "dataset:horizon:trades" in node_ids
        assert "job:ledgerlens-core:ingestion.historical_loader.fetch_chunk" in node_ids
        assert "dataset:ledgerlens-core.sqlite:trades" in node_ids
        assert "job:ledgerlens-core:feature_engineering.build_feature_vector" in node_ids
        assert "dataset:ledgerlens-core.sqlite:feature_distribution_snapshots" in node_ids

        # 5. Test legacy redirect endpoint
        resp = client.get("/admin/lineage/trades", follow_redirects=False)
        assert resp.status_code == 302
        assert "/v1/admin/lineage/trades" in resp.headers["location"]
    finally:
        object.__setattr__(settings, "ledgerlens_admin_api_key", original_key)


def test_lineage_graph_not_found(clean_db):
    graph = get_lineage_graph("nonexistent-dataset", db_path=clean_db)
    assert graph == {"nodes": [], "edges": []}
