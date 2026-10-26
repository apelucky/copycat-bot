"""Pick Postgres when DATABASE_URL is set, otherwise the local SQLite file."""
from __future__ import annotations

import logging
from typing import Any

from ..config import AppConfig
from .postgres_repo import normalize_database_url, safe_db_target
from .sqlite_repo import SqliteRepo

log = logging.getLogger(__name__)


def _redact(message: str, url: str) -> str:
    text = str(message).replace(url, "DATABASE_URL")
    try:
        from psycopg.conninfo import conninfo_to_dict

        password = conninfo_to_dict(normalize_database_url(url)).get("password")
    except Exception:  # noqa: BLE001
        password = None
    if password:
        text = text.replace(str(password), "***")
    return text[:400]


def open_repo(cfg: AppConfig) -> Any:
    if cfg.database_url:
        from .postgres_repo import PostgresRepo

        log.info("database: postgres %s", safe_db_target(cfg.database_url))
        try:
            return PostgresRepo(cfg.database_url)
        except Exception as e:  # noqa: BLE001
            # `from None` so a traceback cannot echo the connection string (it contains the password).
            raise SystemExit(
                f"Could not open Postgres at {safe_db_target(cfg.database_url)}: {_redact(str(e), cfg.database_url)}. "
                "Use the Supabase Connect dialog → Session pooler or Transaction pooler, with the database password filled in."
            ) from None
    log.info("database: sqlite %s", cfg.db_path)
    return SqliteRepo(cfg.db_path)
