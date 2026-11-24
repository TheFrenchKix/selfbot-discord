"""État explicite d'une instance (remplace progressivement les globals)."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Coroutine, Optional

from .config import Settings, mask_token

log = logging.getLogger("selfbot.instance")


@dataclass
class InstanceState:
    settings: Settings
    client: Any = None
    db: Any = None

    # tâches de fond suivies pour un arrêt propre
    _tasks: set = field(default_factory=set, repr=False)

    @property
    def token(self) -> str:
        return self.settings.token

    @property
    def masked_token(self) -> str:
        return mask_token(self.settings.token)

    def spawn(self, coro: Coroutine, name: Optional[str] = None) -> "asyncio.Task":
        """``create_task`` suivi: les exceptions sont loguées, l'arrêt annule tout."""
        task = asyncio.ensure_future(coro)
        if name and hasattr(task, "set_name"):
            task.set_name(name)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: "asyncio.Task") -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("Tâche %s en échec: %r", getattr(task, "get_name", lambda: "?")(), exc, exc_info=exc)

    async def shutdown(self, timeout: float = 5.0) -> None:
        tasks = [t for t in self._tasks if not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)
        self._tasks.clear()
        if self.db is not None:
            try:
                self.db.close()
            except Exception:  # pragma: no cover
                pass
