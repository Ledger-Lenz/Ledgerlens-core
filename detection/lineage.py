"""OpenLineage emitter and lineage graph builder (Issue-Lineage)."""

from __future__ import annotations

import json
import logging
import queue
import threading
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
import uuid

import httpx

from config.settings import settings

logger = logging.getLogger("ledgerlens.lineage")

_LINEAGE_SCHEMA = """
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
CREATE INDEX IF NOT EXISTS idx_lineage_events_run ON lineage_events(run_id, event_type);
CREATE TABLE IF NOT EXISTS lineage_model_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_time TEXT NOT NULL,
    run_id TEXT NOT NULL,
    job_namespace TEXT NOT NULL,
    job_name TEXT NOT NULL,
    model_name TEXT NOT NULL,
    model_version TEXT NOT NULL,
    model_dataset_name TEXT NOT NULL,
    training_data_sha256 TEXT NOT NULL,
    feature_version TEXT NOT NULL,
    config_sha256 TEXT NOT NULL,
    inputs_json TEXT NOT NULL,
    model_dataset_json TEXT NOT NULL,
    UNIQUE (run_id, model_dataset_name)
);
CREATE INDEX IF NOT EXISTS idx_lineage_model_name_version
    ON lineage_model_runs(model_name, model_version, event_time);
"""


@dataclass
class Dataset:
    namespace: str
    name: str
    facets: dict = field(default_factory=dict)


class ActiveRun:
    def __init__(
        self,
        run_id: str,
        job_name: str,
        inputs: list[Dataset],
        parent_run_id: str | None = None,
        emitter: LineageEmitter | None = None,
    ) -> None:
        self.run_id = run_id
        self.job_name = job_name
        self.inputs = list(inputs)
        self.outputs: list[Dataset] = []
        self.parent_run_id = parent_run_id
        self.emitter = emitter

    def add_input(self, dataset: Dataset) -> None:
        self.inputs.append(dataset)

    def add_output(self, dataset: Dataset) -> None:
        self.outputs.append(dataset)


class LineageEmitter:
    def __init__(self, backend: Literal["console", "http", "none"] | None = None) -> None:
        self.backend = backend or settings.lineage_backend
        self._queue: queue.Queue[dict | None] = queue.Queue(maxsize=settings.lineage_queue_maxsize)
        self._worker_thread = threading.Thread(target=self._worker, daemon=True)
        self._worker_thread.start()

    @contextmanager
    def run(self, job_name: str, inputs: list[Dataset], parent_run_id: str | None = None):
        """Emits START on enter, COMPLETE on normal exit, FAIL on exception (re-raised)."""
        if not settings.lineage_enabled:
            # Yield dummy run when lineage is disabled
            dummy = ActiveRun(
                run_id="",
                job_name=job_name,
                inputs=[],
                parent_run_id=None,
                emitter=self,
            )
            yield dummy
            return

        run_id = str(uuid.uuid4())
        active_run = ActiveRun(
            run_id=run_id,
            job_name=job_name,
            inputs=inputs,
            parent_run_id=parent_run_id,
            emitter=self,
        )

        self._emit_event("START", active_run)

        try:
            yield active_run
        except Exception:
            self._emit_event("FAIL", active_run)
            raise
        else:
            self._emit_event("COMPLETE", active_run)

    def _emit_event(self, event_type: str, active_run: ActiveRun) -> None:
        event = {
            "eventType": event_type,
            "eventTime": datetime.now(timezone.utc).isoformat(),
            "run": {
                "runId": active_run.run_id,
                "facets": {}
            },
            "job": {
                "namespace": settings.openlineage_namespace,
                "name": active_run.job_name,
            },
            "inputs": [
                {
                    "namespace": ds.namespace,
                    "name": ds.name,
                    "facets": ds.facets,
                }
                for ds in active_run.inputs
            ],
            "outputs": [
                {
                    "namespace": ds.namespace,
                    "name": ds.name,
                    "facets": ds.facets,
                }
                for ds in active_run.outputs
            ],
            "producer": "https://github.com/Ledger-Lenz/Ledgerlens-core",
        }

        if active_run.parent_run_id:
            event["run"]["facets"]["parent"] = {
                "run": {
                    "runId": active_run.parent_run_id
                }
            }

        if self.backend == "console":
            logger.info("OpenLineage event: %s", json.dumps(event))

        try:
            self._queue.put_nowait(event)
        except queue.Full:
            logger.warning(
                "Lineage queue limit reached (%d). Dropping event: %s (%s)",
                settings.lineage_queue_maxsize,
                event_type,
                active_run.job_name,
            )

    def _worker(self) -> None:
        while True:
            try:
                event = self._queue.get()
                if event is None:
                    break

                # 1. Store locally in SQLite database
                try:
                    self._store_locally(event)
                except Exception as db_exc:
                    logger.error("Failed to persist lineage event to local DB: %s", db_exc)

                # 2. Forward to target backend
                if self.backend == "console":
                    logger.info("OpenLineage event: %s", json.dumps(event))
                elif self.backend == "http":
                    url = settings.openlineage_url
                    if url:
                        if not url.endswith("/api/v1/lineage"):
                            url = url.rstrip("/") + "/api/v1/lineage"
                        try:
                            response = httpx.post(url, json=event, timeout=5.0)
                            response.raise_for_status()
                        except Exception as http_exc:
                            logger.error("Failed to post lineage event to HTTP backend: %s", http_exc)
                
                self._queue.task_done()
            except Exception as w_exc:
                logger.error("Error in lineage background worker: %s", w_exc)

    def _store_locally(self, event: dict) -> None:
        import sqlite3
        db_path = settings.db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(db_path) as conn:
            conn.executescript(_LINEAGE_SCHEMA)
            conn.execute(
                """
                INSERT INTO lineage_events (
                    event_type, event_time, run_id, parent_run_id,
                    job_namespace, job_name, inputs_json, outputs_json, producer
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["eventType"],
                    event["eventTime"],
                    event["run"]["runId"],
                    event["run"]["facets"].get("parent", {}).get("run", {}).get("runId"),
                    event["job"]["namespace"],
                    event["job"]["name"],
                    json.dumps(event["inputs"]),
                    json.dumps(event["outputs"]),
                    event["producer"],
                )
            )
            if event["eventType"] == "COMPLETE":
                for dataset in event["outputs"]:
                    facets = dataset.get("facets", {})
                    model_name = facets.get("model_name")
                    model_version = facets.get("model_version")
                    if not model_name or not model_version:
                        continue
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO lineage_model_runs (
                            event_time, run_id, job_namespace, job_name,
                            model_name, model_version, model_dataset_name,
                            training_data_sha256, feature_version, config_sha256,
                            inputs_json, model_dataset_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event["eventTime"],
                            event["run"]["runId"],
                            event["job"]["namespace"],
                            event["job"]["name"],
                            model_name,
                            model_version,
                            dataset["name"],
                            facets.get("training_data_sha256", ""),
                            facets.get("feature_version", ""),
                            facets.get("config_sha256", ""),
                            json.dumps(event["inputs"]),
                            json.dumps(dataset),
                        ),
                    )

    def stop(self) -> None:
        self._queue.put(None)
        try:
            self._worker_thread.join(timeout=2.0)
        except Exception:
            pass


lineage = LineageEmitter()


def get_lineage_graph(dataset_name: str, db_path: str | None = None) -> dict:
    """Return the upstream and downstream lineage graph for the given dataset name.

    Traverses all recorded COMPLETE lineage runs using BFS.
    """
    import sqlite3

    conn = sqlite3.connect(db_path or settings.db_path)
    events = []
    try:
        cursor = conn.execute(
            """
            SELECT job_namespace, job_name, inputs_json, outputs_json, event_time, run_id, parent_run_id
            FROM lineage_events
            WHERE event_type = 'COMPLETE'
            ORDER BY event_time DESC
            """
        )
        for r in cursor.fetchall():
            events.append({
                "job_namespace": r[0],
                "job_name": r[1],
                "inputs": json.loads(r[2]),
                "outputs": json.loads(r[3]),
                "event_time": r[4],
                "run_id": r[5],
                "parent_run_id": r[6],
            })
    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()

    nodes = {}
    edges = set()

    for ev in events:
        job_key = f"job:{ev['job_namespace']}:{ev['job_name']}"
        nodes[job_key] = {
            "id": job_key,
            "type": "job",
            "name": ev["job_name"],
            "namespace": ev["job_namespace"],
        }

        for inp in ev["inputs"]:
            inp_key = f"dataset:{inp['namespace']}:{inp['name']}"
            nodes[inp_key] = {
                "id": inp_key,
                "type": "dataset",
                "name": inp["name"],
                "namespace": inp["namespace"],
            }
            edges.add((inp_key, job_key))

        for out in ev["outputs"]:
            out_key = f"dataset:{out['namespace']}:{out['name']}"
            nodes[out_key] = {
                "id": out_key,
                "type": "dataset",
                "name": out["name"],
                "namespace": out["namespace"],
            }
            edges.add((job_key, out_key))

    start_keys = []
    for key, nd in nodes.items():
        if nd["type"] == "dataset":
            if nd["name"] == dataset_name or dataset_name in nd["name"]:
                start_keys.append(key)

    if not start_keys:
        return {"nodes": [], "edges": []}

    adj = {k: set() for k in nodes}
    for src, tgt in edges:
        adj[src].add(tgt)
        adj[tgt].add(src)

    visited: set[str] = set()
    bfs_queue: deque[str] = deque(start_keys)
    for k in bfs_queue:
        visited.add(k)

    while bfs_queue:
        curr = bfs_queue.popleft()
        for neighbor in adj.get(curr, []):
            if neighbor not in visited:
                visited.add(neighbor)
                bfs_queue.append(neighbor)

    filtered_nodes = [nodes[k] for k in visited]
    filtered_edges = [
        {"source": src, "target": tgt}
        for src, tgt in edges
        if src in visited and tgt in visited
    ]

    return {
        "nodes": filtered_nodes,
        "edges": filtered_edges,
    }


def get_model_lineage(model: str, db_path: str | None = None) -> list[dict]:
    """Query completed training runs by model name, version, or output dataset."""
    import sqlite3

    if not model:
        raise ValueError("model identifier must not be empty")
    database = db_path or settings.db_path
    Path(database).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as conn:
        conn.executescript(_LINEAGE_SCHEMA)
        rows = conn.execute(
            """
            SELECT event_time, run_id, job_namespace, job_name, model_name,
                   model_version, model_dataset_name, training_data_sha256,
                   feature_version, config_sha256, inputs_json, model_dataset_json
            FROM lineage_model_runs
            WHERE model_name = ? OR model_version = ? OR model_dataset_name = ?
            ORDER BY event_time DESC, id DESC
            """,
            (model, model, model),
        ).fetchall()

    return [
        {
            "event_time": row[0],
            "run_id": row[1],
            "job": {"namespace": row[2], "name": row[3]},
            "model_name": row[4],
            "model_version": row[5],
            "model_dataset_name": row[6],
            "training_data_sha256": row[7],
            "feature_version": row[8],
            "config_sha256": row[9],
            "inputs": json.loads(row[10]),
            "model_dataset": json.loads(row[11]),
        }
        for row in rows
    ]
