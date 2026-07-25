from __future__ import annotations

import os

from control_plane.storage import ControlPlaneStore


class MissingDatabaseUrl(RuntimeError):
    pass


def database_url_from_env() -> str | None:
    return os.environ.get("NYANSA_DATABASE_URL") or os.environ.get("DATABASE_URL")


def create_store_from_env() -> ControlPlaneStore:
    database_url = database_url_from_env()
    if not database_url:
        raise MissingDatabaseUrl(
            "DATABASE_URL or NYANSA_DATABASE_URL is required for the control plane"
        )

    from control_plane.postgres_storage import PostgresControlPlaneStore

    return PostgresControlPlaneStore(database_url)
