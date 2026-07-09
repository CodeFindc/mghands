import asyncio
import json
import sqlite3
import threading
from pathlib import Path

from mghands_gateway.models import (
    AuthTokenRecord,
    ProjectRecord,
    ProjectSkillRecord,
    ProjectStatus,
    SessionRecord,
    SessionStatus,
    UserRecord,
    utc_now,
    LLMModelRecord,
)


class SessionStore:
    _lock = threading.Lock()

    def __init__(self, database_path: Path):
        self.database_path = database_path

    async def init(self) -> None:
        await asyncio.to_thread(self._init_sync)

    def _init_sync(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, sqlite3.connect(self.database_path) as db:
            db.execute('PRAGMA foreign_keys = ON')
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS auth_tokens (
                    token_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    token_hash TEXT UNIQUE NOT NULL,
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT,
                    created_at TEXT NOT NULL,
                    last_used_at TEXT
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    workspace_dir TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS project_skills (
                    project_id TEXT NOT NULL,
                    skill_name TEXT NOT NULL,
                    source_fingerprint TEXT,
                    metadata TEXT NOT NULL,
                    installed_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, skill_name)
                )
                """
            )
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
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS system_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS llm_models (
                    model_id TEXT PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    base_url TEXT,
                    api_key TEXT,
                    is_default INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._ensure_column(db, 'sessions', 'user_id', 'TEXT')
            self._ensure_column(db, 'sessions', 'project_id', 'TEXT')
            db.execute('CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)')
            db.execute('CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id)')
            db.execute('CREATE INDEX IF NOT EXISTS idx_projects_user ON projects(user_id)')
            db.commit()

    def _ensure_column(self, db: sqlite3.Connection, table: str, name: str, column_type: str) -> None:
        columns = {row[1] for row in db.execute(f'PRAGMA table_info({table})')}
        if name not in columns:
            db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {column_type}')

    async def user_count(self) -> int:
        await self.init()
        return await asyncio.to_thread(self._scalar_int, 'SELECT COUNT(*) FROM users', ())

    def _scalar_int(self, query: str, args: tuple[object, ...]) -> int:
        with self._lock, sqlite3.connect(self.database_path) as db:
            row = db.execute(query, args).fetchone()
            return int(row[0]) if row else 0

    async def create_user(self, record: UserRecord) -> UserRecord:
        await self.init()
        try:
            await asyncio.to_thread(self._create_user_sync, record)
        except sqlite3.IntegrityError as exc:
            raise KeyError(record.username) from exc
        return record

    def _create_user_sync(self, record: UserRecord) -> None:
        with self._lock, sqlite3.connect(self.database_path) as db:
            db.execute(
                'INSERT INTO users(user_id, username, password_hash, role, enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
                (
                    record.user_id,
                    record.username,
                    record.password_hash,
                    record.role.value,
                    int(record.enabled),
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                ),
            )
            db.commit()

    async def get_user_by_username(self, username: str) -> UserRecord | None:
        await self.init()
        row = await asyncio.to_thread(
            self._fetchone, 'SELECT * FROM users WHERE username = ?', (username,)
        )
        return self._user_from_row(row) if row else None

    async def get_user(self, user_id: str) -> UserRecord | None:
        await self.init()
        row = await asyncio.to_thread(self._fetchone, 'SELECT * FROM users WHERE user_id = ?', (user_id,))
        return self._user_from_row(row) if row else None

    async def list_users(self) -> list[UserRecord]:
        await self.init()
        rows = await asyncio.to_thread(self._fetchall, 'SELECT * FROM users ORDER BY created_at', ())
        return [self._user_from_row(row) for row in rows]

    async def update_user(self, record: UserRecord) -> UserRecord:
        await self.init()
        record.updated_at = utc_now()
        count = await asyncio.to_thread(self._update_user_sync, record)
        if count == 0:
            raise KeyError(record.user_id)
        return record

    def _update_user_sync(self, record: UserRecord) -> int:
        with self._lock, sqlite3.connect(self.database_path) as db:
            cursor = db.execute(
                'UPDATE users SET password_hash = ?, role = ?, enabled = ?, updated_at = ? WHERE user_id = ?',
                (record.password_hash, record.role.value, int(record.enabled), record.updated_at.isoformat(), record.user_id),
            )
            db.commit()
            return cursor.rowcount

    async def create_token(self, record: AuthTokenRecord) -> AuthTokenRecord:
        await self.init()
        await asyncio.to_thread(self._create_token_sync, record)
        return record

    def _create_token_sync(self, record: AuthTokenRecord) -> None:
        with self._lock, sqlite3.connect(self.database_path) as db:
            db.execute(
                'INSERT INTO auth_tokens(token_id, user_id, token_hash, expires_at, revoked_at, created_at, last_used_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
                (
                    record.token_id,
                    record.user_id,
                    record.token_hash,
                    record.expires_at.isoformat(),
                    record.revoked_at.isoformat() if record.revoked_at else None,
                    record.created_at.isoformat(),
                    record.last_used_at.isoformat() if record.last_used_at else None,
                ),
            )
            db.commit()

    async def get_token_by_hash(self, token_hash: str) -> AuthTokenRecord | None:
        await self.init()
        row = await asyncio.to_thread(
            self._fetchone, 'SELECT * FROM auth_tokens WHERE token_hash = ?', (token_hash,)
        )
        return self._token_from_row(row) if row else None

    async def touch_token(self, token_id: str) -> None:
        await self.init()
        await asyncio.to_thread(
            self._execute,
            'UPDATE auth_tokens SET last_used_at = ? WHERE token_id = ?',
            (utc_now().isoformat(), token_id),
        )

    async def revoke_token(self, token_id: str) -> None:
        await self.init()
        await asyncio.to_thread(
            self._execute,
            'UPDATE auth_tokens SET revoked_at = ? WHERE token_id = ?',
            (utc_now().isoformat(), token_id),
        )

    async def create_project(self, record: ProjectRecord) -> ProjectRecord:
        await self.init()
        try:
            await asyncio.to_thread(self._create_project_sync, record)
        except sqlite3.IntegrityError as exc:
            raise KeyError(record.project_id) from exc
        return record

    def _create_project_sync(self, record: ProjectRecord) -> None:
        with self._lock, sqlite3.connect(self.database_path) as db:
            db.execute(
                'INSERT INTO projects(project_id, user_id, name, workspace_dir, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
                (
                    record.project_id,
                    record.user_id,
                    record.name,
                    record.workspace_dir,
                    record.status.value,
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                ),
            )
            db.commit()

    async def get_project(self, project_id: str) -> ProjectRecord | None:
        await self.init()
        row = await asyncio.to_thread(self._fetchone, 'SELECT * FROM projects WHERE project_id = ?', (project_id,))
        return self._project_from_row(row) if row else None

    async def list_projects(self, user_id: str) -> list[ProjectRecord]:
        await self.init()
        rows = await asyncio.to_thread(
            self._fetchall,
            'SELECT * FROM projects WHERE user_id = ? AND status != ? ORDER BY created_at',
            (user_id, ProjectStatus.DELETED.value),
        )
        return [self._project_from_row(row) for row in rows]

    async def save_project(self, record: ProjectRecord) -> ProjectRecord:
        await self.init()
        record.updated_at = utc_now()
        count = await asyncio.to_thread(self._save_project_sync, record)
        if count == 0:
            raise KeyError(record.project_id)
        return record

    def _save_project_sync(self, record: ProjectRecord) -> int:
        with self._lock, sqlite3.connect(self.database_path) as db:
            cursor = db.execute(
                'UPDATE projects SET name = ?, status = ?, updated_at = ? WHERE project_id = ?',
                (record.name, record.status.value, record.updated_at.isoformat(), record.project_id),
            )
            db.commit()
            return cursor.rowcount

    async def upsert_project_skill(self, record: ProjectSkillRecord) -> ProjectSkillRecord:
        await self.init()
        record.updated_at = utc_now()
        await asyncio.to_thread(self._upsert_project_skill_sync, record)
        return record

    def _upsert_project_skill_sync(self, record: ProjectSkillRecord) -> None:
        data = record.metadata.model_dump_json()
        with self._lock, sqlite3.connect(self.database_path) as db:
            db.execute(
                """
                INSERT INTO project_skills(project_id, skill_name, source_fingerprint, metadata, installed_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, skill_name) DO UPDATE SET
                    source_fingerprint = excluded.source_fingerprint,
                    metadata = excluded.metadata,
                    updated_at = excluded.updated_at
                """,
                (
                    record.project_id,
                    record.skill_name,
                    record.source_fingerprint,
                    data,
                    record.installed_at.isoformat(),
                    record.updated_at.isoformat(),
                ),
            )
            db.commit()

    async def list_project_skills(self, project_id: str) -> list[ProjectSkillRecord]:
        await self.init()
        rows = await asyncio.to_thread(
            self._fetchall,
            'SELECT * FROM project_skills WHERE project_id = ? ORDER BY skill_name',
            (project_id,),
        )
        return [self._project_skill_from_row(row) for row in rows]

    async def get_active_session_for_project(self, project_id: str) -> SessionRecord | None:
        await self.init()
        rows = await asyncio.to_thread(
            self._fetchall,
            'SELECT data FROM sessions WHERE project_id = ? ORDER BY updated_at DESC',
            (project_id,),
        )
        for row in rows:
            record = SessionRecord.model_validate_json(row['data'])
            if record.status in {SessionStatus.CREATED, SessionStatus.RUNNING}:
                return record
        return None

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
                'INSERT INTO sessions(session_id, user_id, project_id, data, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)',
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
            cursor = db.execute('SELECT data FROM sessions WHERE session_id = ?', (session_id,))
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
                'UPDATE sessions SET user_id = ?, project_id = ?, data = ?, updated_at = ? WHERE session_id = ?',
                (
                    record.created_by_user_id,
                    record.project_id,
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

    async def list_sessions_for_project(self, project_id: str) -> list[SessionRecord]:
        rows = await asyncio.to_thread(self._list_sessions_for_project_sync, project_id)
        records = []
        for r in rows:
            try:
                records.append(SessionRecord.model_validate_json(r))
            except Exception:
                pass
        return records

    def _list_sessions_for_project_sync(self, project_id: str) -> list[str]:
        with self._lock, sqlite3.connect(self.database_path) as db:
            cursor = db.execute(
                'SELECT data FROM sessions WHERE project_id = ? ORDER BY updated_at DESC',
                (project_id,),
            )
            return [row[0] for row in cursor.fetchall()]


    def _to_row(self, record: SessionRecord) -> tuple[str, str | None, str | None, str, str, str]:
        return (
            record.session_id,
            record.created_by_user_id,
            record.project_id,
            self._serialize_record(record),
            record.created_at.isoformat(),
            record.updated_at.isoformat(),
        )

    def _serialize_record(self, record: SessionRecord) -> str:
        data = record.model_dump(mode='json', context={'expose_secrets': True})
        return json.dumps(data, separators=(',', ':'))

    def _fetchone(self, query: str, args: tuple[object, ...]) -> sqlite3.Row | None:
        with self._lock, sqlite3.connect(self.database_path) as db:
            db.row_factory = sqlite3.Row
            return db.execute(query, args).fetchone()

    def _fetchall(self, query: str, args: tuple[object, ...]) -> list[sqlite3.Row]:
        with self._lock, sqlite3.connect(self.database_path) as db:
            db.row_factory = sqlite3.Row
            return list(db.execute(query, args).fetchall())

    def _execute(self, query: str, args: tuple[object, ...]) -> None:
        with self._lock, sqlite3.connect(self.database_path) as db:
            db.execute(query, args)
            db.commit()

    def _user_from_row(self, row: sqlite3.Row) -> UserRecord:
        return UserRecord(
            user_id=row['user_id'],
            username=row['username'],
            password_hash=row['password_hash'],
            role=row['role'],
            enabled=bool(row['enabled']),
            created_at=row['created_at'],
            updated_at=row['updated_at'],
        )

    def _token_from_row(self, row: sqlite3.Row) -> AuthTokenRecord:
        return AuthTokenRecord(
            token_id=row['token_id'],
            user_id=row['user_id'],
            token_hash=row['token_hash'],
            expires_at=row['expires_at'],
            revoked_at=row['revoked_at'],
            created_at=row['created_at'],
            last_used_at=row['last_used_at'],
        )

    def _project_from_row(self, row: sqlite3.Row) -> ProjectRecord:
        return ProjectRecord(
            project_id=row['project_id'],
            user_id=row['user_id'],
            name=row['name'],
            workspace_dir=row['workspace_dir'],
            status=row['status'],
            created_at=row['created_at'],
            updated_at=row['updated_at'],
        )

    def _project_skill_from_row(self, row: sqlite3.Row) -> ProjectSkillRecord:
        return ProjectSkillRecord(
            project_id=row['project_id'],
            skill_name=row['skill_name'],
            source_fingerprint=row['source_fingerprint'],
            metadata=json.loads(row['metadata']),
            installed_at=row['installed_at'],
            updated_at=row['updated_at'],
        )

    def _model_from_row(self, row: sqlite3.Row) -> LLMModelRecord:
        return LLMModelRecord(
            model_id=row['model_id'],
            name=row['name'],
            provider=row['provider'],
            model=row['model'],
            base_url=row['base_url'],
            api_key=row['api_key'],
            is_default=bool(row['is_default']),
            created_at=row['created_at'],
            updated_at=row['updated_at'],
        )

    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        await self.init()
        row = await asyncio.to_thread(
            self._fetchone, 'SELECT value FROM system_settings WHERE key = ?', (key,)
        )
        return row['value'] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        await self.init()
        await asyncio.to_thread(self._set_setting_sync, key, value)

    def _set_setting_sync(self, key: str, value: str) -> None:
        with self._lock, sqlite3.connect(self.database_path) as db:
            db.execute(
                'INSERT INTO system_settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value',
                (key, value),
            )
            db.commit()

    async def get_all_settings(self) -> dict[str, str]:
        await self.init()
        rows = await asyncio.to_thread(
            self._fetchall, 'SELECT key, value FROM system_settings', ()
        )
        return {row['key']: row['value'] for row in rows}

    async def list_models(self) -> list[LLMModelRecord]:
        await self.init()
        rows = await asyncio.to_thread(
            self._fetchall, 'SELECT * FROM llm_models ORDER BY created_at', ()
        )
        return [self._model_from_row(row) for row in rows]

    async def create_model(self, record: LLMModelRecord) -> LLMModelRecord:
        await self.init()
        await asyncio.to_thread(self._create_model_sync, record)
        return record

    def _create_model_sync(self, record: LLMModelRecord) -> None:
        with self._lock, sqlite3.connect(self.database_path) as db:
            if record.is_default:
                db.execute('UPDATE llm_models SET is_default = 0')
            db.execute(
                """
                INSERT INTO llm_models(model_id, name, provider, model, base_url, api_key, is_default, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.model_id,
                    record.name,
                    record.provider,
                    record.model,
                    record.base_url,
                    record.api_key,
                    int(record.is_default),
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                ),
            )
            db.commit()

    async def get_model(self, model_id: str) -> LLMModelRecord | None:
        await self.init()
        row = await asyncio.to_thread(
            self._fetchone, 'SELECT * FROM llm_models WHERE model_id = ?', (model_id,)
        )
        return self._model_from_row(row) if row else None

    async def update_model(self, record: LLMModelRecord) -> LLMModelRecord:
        await self.init()
        record.updated_at = utc_now()
        await asyncio.to_thread(self._update_model_sync, record)
        return record

    def _update_model_sync(self, record: LLMModelRecord) -> None:
        with self._lock, sqlite3.connect(self.database_path) as db:
            if record.is_default:
                db.execute('UPDATE llm_models SET is_default = 0 WHERE model_id != ?', (record.model_id,))
            db.execute(
                """
                UPDATE llm_models SET name = ?, provider = ?, model = ?, base_url = ?, api_key = ?, is_default = ?, updated_at = ?
                WHERE model_id = ?
                """,
                (
                    record.name,
                    record.provider,
                    record.model,
                    record.base_url,
                    record.api_key,
                    int(record.is_default),
                    record.updated_at.isoformat(),
                    record.model_id,
                ),
            )
            db.commit()

    async def delete_model(self, model_id: str) -> None:
        await self.init()
        await asyncio.to_thread(self._delete_model_sync, model_id)

    def _delete_model_sync(self, model_id: str) -> None:
        with self._lock, sqlite3.connect(self.database_path) as db:
            db.execute('DELETE FROM llm_models WHERE model_id = ?', (model_id,))
            db.commit()

    async def get_default_model(self) -> LLMModelRecord | None:
        await self.init()
        row = await asyncio.to_thread(
            self._fetchone, 'SELECT * FROM llm_models WHERE is_default = 1 LIMIT 1', ()
        )
        return self._model_from_row(row) if row else None
