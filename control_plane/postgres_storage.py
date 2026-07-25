from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from typing import Any

from psycopg import Connection
from psycopg_pool import ConnectionPool
from psycopg.types.json import Jsonb

from control_plane.models import ControlPlaneState
from control_plane.storage import ControlPlaneStore


class _PostgresStateLock:
    def __init__(self, store: "PostgresControlPlaneStore"):
        self.store = store

    def __enter__(self):
        depth = getattr(self.store._local, "depth", 0)
        if depth:
            self.store._local.depth = depth + 1
            return self

        conn = self.store._connect()
        self.store._ensure_schema(conn)
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (self.store.lock_key,))
        self.store._local.conn = conn
        self.store._local.depth = 1
        return self

    def __exit__(self, exc_type, exc, tb):
        depth = getattr(self.store._local, "depth", 0)
        if depth > 1:
            self.store._local.depth = depth - 1
            return False

        conn: Connection[Any] | None = getattr(self.store._local, "conn", None)
        try:
            if conn is not None:
                if exc_type is None:
                    conn.commit()
                else:
                    conn.rollback()
        finally:
            if conn is not None:
                checkout = getattr(self.store._local, "checkout", None)
                if checkout is not None:
                    checkout.__exit__(exc_type, exc, tb)
            self.store._local.conn = None
            self.store._local.checkout = None
            self.store._local.depth = 0
        return False


class PostgresControlPlaneStore(ControlPlaneStore):
    """Control-plane state backed by Postgres with cross-process write locking."""

    schema_version = 1
    state_id = "default"
    lock_key = int.from_bytes(
        hashlib.sha256(b"nyansa-control-plane-state").digest()[:8],
        byteorder="big",
        signed=True,
    )

    def __init__(
        self,
        database_url: str,
        key_dir: Path | None = None,
        *,
        min_pool_size: int = 1,
        max_pool_size: int = 10,
    ):
        super().__init__(key_dir or Path("/var/lib/nyansa-control-plane/state.json"))
        self.database_url = database_url
        self._pool = ConnectionPool(
            conninfo=database_url,
            min_size=min_pool_size,
            max_size=max_pool_size,
            open=False,
            check=ConnectionPool.check_connection,
            name="control-plane",
        )
        self._pool.open(wait=True)
        self._local = threading.local()
        self._lock = _PostgresStateLock(self)

    def _connect(self) -> Connection[Any]:
        checkout = self._pool.connection()
        conn = checkout.__enter__()
        self._local.checkout = checkout
        return conn

    def close(self) -> None:
        self._pool.close()

    def _ensure_schema(self, conn: Connection[Any]) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS control_plane_migrations (
                version integer PRIMARY KEY,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS control_plane_state (
                id text PRIMARY KEY,
                state jsonb NOT NULL,
                updated_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        conn.execute(
            """
            INSERT INTO control_plane_migrations (version)
            VALUES (%s)
            ON CONFLICT (version) DO NOTHING
            """,
            (self.schema_version,),
        )

    def _active_connection(self) -> Connection[Any] | None:
        return getattr(self._local, "conn", None)

    def health_check(self) -> dict[str, Any]:
        with self._pool.connection() as conn:
            self._ensure_schema(conn)
            conn.execute("SELECT 1").fetchone()
            version = conn.execute(
                "SELECT max(version) FROM control_plane_migrations"
            ).fetchone()[0]
        return {
            "ok": True,
            "backend": "postgres",
            "schema_version": version,
            "pool": self._pool.get_stats(),
        }

    def _load(self) -> ControlPlaneState:
        conn = self._active_connection()
        if conn is None:
            with self._pool.connection() as conn:
                self._ensure_schema(conn)
                row = conn.execute(
                    "SELECT state FROM control_plane_state WHERE id = %s",
                    (self.state_id,),
                ).fetchone()
                if row is None:
                    return self._with_default_policies(ControlPlaneState())
                return self._with_default_policies(ControlPlaneState(**row[0]))

        row = conn.execute(
            "SELECT state FROM control_plane_state WHERE id = %s FOR UPDATE",
            (self.state_id,),
        ).fetchone()
        if row is None:
            return self._with_default_policies(ControlPlaneState())
        return self._with_default_policies(ControlPlaneState(**row[0]))

    def _save(self, state: ControlPlaneState) -> None:
        conn = self._active_connection()
        if conn is None:
            with self._pool.connection() as conn:
                self._ensure_schema(conn)
                self._save_with_connection(conn, state)
            return

        self._save_with_connection(conn, state)

    def _save_with_connection(self, conn: Connection[Any], state: ControlPlaneState) -> None:
        conn.execute(
            """
            INSERT INTO control_plane_state (id, state, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (id)
            DO UPDATE SET state = EXCLUDED.state, updated_at = now()
            """,
            (self.state_id, Jsonb(state.model_dump(mode="json"))),
        )
