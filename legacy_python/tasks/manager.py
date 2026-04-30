"""
Task manager: thread pool + in-memory state + WebSocket push.
All long-running operations (download, backtest, replay) go through here.
"""
import ctypes
import logging
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync

logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    PENDING = 'pending'
    RUNNING = 'running'
    COMPLETED = 'completed'
    FAILED = 'failed'
    CANCELLED = 'cancelled'


@dataclass
class TaskInfo:
    task_id: str
    name: str
    status: TaskStatus = TaskStatus.PENDING
    progress: int = 0
    message: str = ''
    result: Any = None
    error: str = ''
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    _cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _thread_id: Optional[int] = field(default=None, repr=False)


class TaskManager:
    """Singleton task manager with thread pool and WebSocket progress push."""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._tasks: dict[str, TaskInfo] = {}
        self._executor = ThreadPoolExecutor(max_workers=2)
        self._futures = {}

    def submit(self, name: str, func: Callable, *args, **kwargs) -> str:
        """Submit a task for background execution. Returns task_id."""
        task_id = str(uuid.uuid4())[:8]
        info = TaskInfo(task_id=task_id, name=name)
        self._tasks[task_id] = info

        def _wrapper():
            info._thread_id = threading.current_thread().ident
            info.status = TaskStatus.RUNNING
            info.started_at = time.time()
            self._push(task_id)
            try:
                result = func(task_id, *args, **kwargs)
                # Check if cancelled during execution
                if info._cancel_event.is_set():
                    info.status = TaskStatus.CANCELLED
                    info.message = '已取消'
                else:
                    info.status = TaskStatus.COMPLETED
                    info.progress = 100
                    info.result = result
                    info.message = '完成'
            except _TaskCancelled:
                logger.info(f"Task {task_id}: 被取消 (_TaskCancelled)")
                info.status = TaskStatus.CANCELLED
                info.message = '已取消'
            except Exception as e:
                if info._cancel_event.is_set():
                    logger.info(f"Task {task_id}: 执行中被取消")
                    info.status = TaskStatus.CANCELLED
                    info.message = '已取消'
                else:
                    info.status = TaskStatus.FAILED
                    info.error = str(e)
                    info.message = f'失败: {e}'
                    logger.error(f"Task {task_id} failed: {traceback.format_exc()}")
            finally:
                info._thread_id = None
                info.finished_at = time.time()
                self._push(task_id)

        try:
            future = self._executor.submit(_wrapper)
        except RuntimeError as e:
            # Python 3.14: executor shut down during reload — recreate
            logger.warning(f"submit: 线程池已关闭，重新创建: {e}")
            self._executor = ThreadPoolExecutor(max_workers=2)
            future = self._executor.submit(_wrapper)
        self._futures[task_id] = future
        return task_id

    def is_cancelled(self, task_id: str) -> bool:
        """Check if a task has been cancelled. Task functions should call this periodically."""
        info = self._tasks.get(task_id)
        return info._cancel_event.is_set() if info else False

    def update_progress(self, task_id: str, progress: int, message: str = ''):
        """Update task progress and push via WebSocket. Raises if cancelled."""
        info = self._tasks.get(task_id)
        if not info:
            logger.debug(f"update_progress: 任务 {task_id} 不存在，跳过更新")
            return
        # Check cancel flag on every progress update — natural checkpoint
        if info._cancel_event.is_set():
            raise _TaskCancelled()
        info.progress = min(progress, 99)  # 100 only on completion
        if message:
            info.message = message
        self._push(task_id)

    def get_status(self, task_id: str) -> Optional[dict]:
        info = self._tasks.get(task_id)
        if not info:
            logger.debug(f"get_status: 任务 {task_id} 不存在")
            return None
        return self._info_to_dict(info)

    def get_all_tasks(self) -> list[dict]:
        return [self._info_to_dict(self._tasks[tid]) for tid in sorted(
            self._tasks.keys(),
            key=lambda t: self._tasks[t].created_at,
            reverse=True
        )][:50]

    def cancel(self, task_id: str) -> bool:
        """Cancel a task. Sets cancel event and force-kills the thread if running."""
        info = self._tasks.get(task_id)
        if not info:
            return False

        # Already finished
        if info.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            return False

        # Set the cooperative cancel flag
        info._cancel_event.set()

        # For PENDING tasks, try to cancel the future
        future = self._futures.get(task_id)
        if future and not future.done():
            cancelled = future.cancel()
            if cancelled:
                info.status = TaskStatus.CANCELLED
                info.message = '已取消'
                info.finished_at = time.time()
                self._push(task_id)
                return True

        # For RUNNING tasks, force-kill the thread via async exception
        thread_id = info._thread_id
        if thread_id is not None:
            try:
                res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
                    ctypes.c_ulong(thread_id),
                    ctypes.py_object(_TaskCancelled),
                )
                if res == 0:
                    logger.warning(f"Task {task_id}: thread {thread_id} not found")
                elif res > 1:
                    # Reset if more than one thread affected
                    ctypes.pythonapi.PyThreadState_SetAsyncExc(
                        ctypes.c_ulong(thread_id), None
                    )
                    logger.warning(f"Task {task_id}: multiple threads affected, reset")
                else:
                    logger.info(f"Task {task_id}: kill signal sent to thread {thread_id}")
            except Exception as e:
                logger.warning(f"Task {task_id}: force kill failed: {e}")

        # Mark cancelled immediately for UI responsiveness
        info.status = TaskStatus.CANCELLED
        info.message = '正在取消...'
        self._push(task_id)
        return True

    def _info_to_dict(self, info: TaskInfo) -> dict:
        now = time.time()
        elapsed = None
        if info.started_at:
            end = info.finished_at or now
            elapsed = round(end - info.started_at, 1)
        return {
            'task_id': info.task_id,
            'name': info.name,
            'status': info.status.value,
            'progress': info.progress,
            'message': info.message,
            'result': info.result,
            'error': info.error,
            'created_at': info.created_at,
            'started_at': info.started_at,
            'finished_at': info.finished_at,
            'elapsed': elapsed,
        }

    def _push(self, task_id: str):
        """Push task status via Channels WebSocket."""
        try:
            channel_layer = get_channel_layer()
            if channel_layer:
                status = self.get_status(task_id)
                async_to_sync(channel_layer.group_send)(
                    'tasks',
                    {
                        'type': 'task_update',
                        'data': status,
                    }
                )
        except Exception as e:
            logger.debug(f"WebSocket push failed (no clients?): {e}")


class _TaskCancelled(Exception):
    """Internal exception raised inside task thread to interrupt execution."""
    pass


# Global singleton
task_manager = TaskManager()
