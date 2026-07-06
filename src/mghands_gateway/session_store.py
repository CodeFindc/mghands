import asyncio
import json
import sqlite3
import threading
from pathlib import Path

from mghands_gateway.models import SessionRecord, SessionStatus, utc_now


class SessionStore:
    _lock = threading.Lock()

    def __init__(self, database_path: Path):
        self.database_path = database_path

    async def init(self) -> None:
        await asyncio.to_thread(self._init_sync)

    def _init_sync(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, sqlite3.connect(self.database_path) as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            db.commit()

    async def create(self, record: SessionRecord) -> SessionRecord:
        await self.init()
        try:
            await asyncio.to_thread(self._create_sync, record)
        except sqlite3.IntegrityError as exc:
            raise KeyError(record.session_id) from exc
        return record

    def _create_sync(self, record: SessionRecord) -> None:
        with self._lock, sqlite3.connect(self.database_path) as db:
            db.execute(
                'INSERT INTO sessions(session_id, data, created_at, updated_at) VALUES (?, ?, ?, ?)',
                self._to_row(record),
            )
            db.commit()

    async def get(self, session_id: str) -> SessionRecord | None:
        await self.init()
        row = await asyncio.to_thread(self._get_sync, session_id)
        if row is None:
            return None
        return SessionRecord.model_validate_json(row)

    def _get_sync(self, session_id: str) -> str | None:
        with self._lock, sqlite3.connect(self.database_path) as db:
            cursor = db.execute(
                'SELECT data FROM sessions WHERE session_id = ?', (session_id,)
            )
            row = cursor.fetchone()
            return row[0] if row else None

    async def save(self, record: SessionRecord) -> SessionRecord:
        await self.init()
        record.updated_at = utc_now()
        rowcount = await asyncio.to_thread(self._save_sync, record)
        if rowcount == 0:
            raise KeyError(record.session_id)
        return record

    def _save_sync(self, record: SessionRecord) -> int:
        with self._lock, sqlite3.connect(self.database_path) as db:
            cursor = db.execute(
                'UPDATE sessions SET data = ?, updated_at = ? WHERE session_id = ?',
                (
                    self._serialize_record(record),
                    record.updated_at.isoformat(),
                    record.session_id,
                ),
            )
            db.commit()
            return cursor.rowcount

    async def mark_deleted(self, session_id: str) -> SessionRecord:
        record = await self.require(session_id)
        record.status = SessionStatus.DELETED
        return await self.save(record)

    async def require(self, session_id: str) -> SessionRecord:
        record = await self.get(session_id)
        if record is None:
            raise KeyError(session_id)
        return record

    def _to_row(self, record: SessionRecord) -> tuple[str, str, str, str]:
        return (
            record.session_id,
            self._serialize_record(record),
            record.created_at.isoformat(),
            record.updated_at.isoformat(),
        )

    def _serialize_record(self, record: SessionRecord) -> str:
        data = record.model_dump(mode='json', context={'expose_secrets': True})
        return json.dumps(data, separators=(',', ':'))
