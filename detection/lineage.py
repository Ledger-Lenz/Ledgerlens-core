"""OpenLineage emitter and lineage graph builder (Issue-Lineage)."""

from __future__ import annotations

import json
import logging
import queue
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal
import uuid

import httpx

from config.settings import settings

logger = logging.getLogger("ledgerlens.lineage")


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
        conn = None
        try:
            conn = sqlite3.connect(settings.db_path)
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
            conn.commit()
        except sqlite3.OperationalError as op_err:
            logger.debug("lineage_events table operational error: %s", op_err)
        finally:
            if conn:
                conn.close()

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

    visited = set()
    queue_list = list(start_keys)
    for k in queue_list:
        visited.add(k)

    while queue_list:
        curr = queue_list.pop(0)
        for neighbor in adj.get(curr, []):
            if neighbor not in visited:
                visited.add(neighbor)
                queue_list.append(neighbor)

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
