"""SQLite history storage. Only final reports are persisted."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class Storage:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS reports (
                    id TEXT PRIMARY KEY,
                    topic TEXT NOT NULL,
                    depth TEXT NOT NULL,
                    report_md TEXT NOT NULL,
                    sources_json TEXT NOT NULL,
                    stats_json TEXT NOT NULL,
                    partial INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )"""
            )

    # ---- sync implementations

    def _save(self, rid: str, topic: str, depth: str, report_md: str,
              sources: list[dict], stats: dict, partial: bool) -> None:
        created = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT OR REPLACE INTO reports VALUES (?,?,?,?,?,?,?,?)",
                (rid, topic, depth, report_md, json.dumps(sources, ensure_ascii=False),
                 json.dumps(stats, ensure_ascii=False), int(partial), created),
            )

    def _list(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT id, topic, depth, partial, stats_json, created_at "
                "FROM reports ORDER BY created_at DESC LIMIT 200"
            ).fetchall()
        return [self._row_to_item(r) for r in rows]

    def _get(self, rid: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM reports WHERE id = ?", (rid,)).fetchone()
        if row is None:
            return None
        item = self._row_to_item(row)
        item["report"] = row["report_md"]
        item["sources"] = json.loads(row["sources_json"])
        return item

    def _delete(self, rid: str) -> bool:
        with closing(self._connect()) as conn, conn:
            cur = conn.execute("DELETE FROM reports WHERE id = ?", (rid,))
            return cur.rowcount > 0

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "topic": row["topic"], "depth": row["depth"],
            "partial": bool(row["partial"]), "stats": json.loads(row["stats_json"]),
            "created_at": row["created_at"],
        }

    # ---- async wrappers (SQLite calls are short, but never block the event loop)

    async def save_report(self, rid: str, topic: str, depth: str, report_md: str,
                          sources: list[dict], stats: dict, partial: bool) -> None:
        await asyncio.to_thread(self._save, rid, topic, depth, report_md, sources, stats, partial)

    async def list_reports(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list)

    async def get_report(self, rid: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get, rid)

    async def delete_report(self, rid: str) -> bool:
        return await asyncio.to_thread(self._delete, rid)
