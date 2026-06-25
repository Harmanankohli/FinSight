"""SQLite-backed A2A TaskStore — tasks survive process restarts.

Wraps InMemoryTaskStore for fast in-process get/list/delete (no re-query on
every call) and adds SQLite write-through so tasks are durable across restarts.
On cold start, all rows from a2a_tasks are loaded into the in-memory store once.
"""

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from a2a.server.context import ServerCallContext
from a2a.server.owner_resolver import OwnerResolver, resolve_user_scope
from a2a.server.tasks import InMemoryTaskStore
from a2a.server.tasks.task_store import TaskStore
from a2a.types import a2a_pb2
from a2a.types.a2a_pb2 import Task
from google.protobuf.json_format import MessageToJson, Parse

from shared.memory.store import DB_PATH, get_db, write_lock
from shared.settings import IST

logger = logging.getLogger(__name__)


class SQLiteTaskStore(TaskStore):
    """Persistent A2A task store backed by SQLite with in-memory warm cache."""

    def __init__(
        self,
        db_path: Path = DB_PATH,
        owner_resolver: OwnerResolver = resolve_user_scope,
    ) -> None:
        self._db_path = db_path
        self._owner_resolver = owner_resolver
        self._mem = InMemoryTaskStore(owner_resolver=owner_resolver)
        self._loaded = False
        self._load_lock = asyncio.Lock()

    async def _ensure_loaded(self) -> None:
        """Populate in-memory store from SQLite on first call (double-checked lock)."""
        if self._loaded:
            return
        async with self._load_lock:
            if self._loaded:
                return
            try:
                conn = await get_db(self._db_path)
                cursor = await conn.execute("SELECT task_id, owner, payload FROM a2a_tasks")
                rows = list(await cursor.fetchall())
                for _task_id, owner, payload in rows:
                    task = Parse(payload, Task())
                    # Use a bare dict as the owner's task map — avoids touching
                    # InMemoryTaskStore's private _impl while still bulk-loading
                    # at init before concurrent callers reach the store.
                    owner_tasks = self._mem._impl.tasks.setdefault(owner, {})
                    owner_tasks[task.id] = task
                logger.info("SQLiteTaskStore: loaded %d tasks from SQLite", len(rows))
            except Exception:
                logger.warning("SQLiteTaskStore: failed to pre-load tasks", exc_info=True)
            self._loaded = True

    async def save(self, task: Task, context: ServerCallContext) -> None:
        await self._ensure_loaded()
        owner = self._owner_resolver(context)
        payload = MessageToJson(task)
        now = datetime.now(IST).isoformat()
        async with write_lock():
            conn = await get_db(self._db_path)
            await conn.execute(
                """INSERT INTO a2a_tasks (task_id, owner, payload, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(task_id) DO UPDATE
                   SET payload = excluded.payload, updated_at = excluded.updated_at""",
                (task.id, owner, payload, now),
            )
            await conn.commit()
        await self._mem.save(task, context)

    async def get(self, task_id: str, context: ServerCallContext) -> Task | None:
        await self._ensure_loaded()
        return await self._mem.get(task_id, context)

    async def list(
        self,
        params: a2a_pb2.ListTasksRequest,
        context: ServerCallContext,
    ) -> a2a_pb2.ListTasksResponse:
        await self._ensure_loaded()
        return await self._mem.list(params, context)

    async def delete(self, task_id: str, context: ServerCallContext) -> None:
        await self._ensure_loaded()
        async with write_lock():
            conn = await get_db(self._db_path)
            await conn.execute("DELETE FROM a2a_tasks WHERE task_id = ?", (task_id,))
            await conn.commit()
        await self._mem.delete(task_id, context)
