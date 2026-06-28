"""BackgroundRegistry: track detached capability runs (PLAN.md §6.9).

A capability declared `kind = "background"` is launched detached by the jail and
handed here. The registry assigns it a short id (`bg-1`, `bg-2`, …), tails its
combined output, and reports liveness to the control-plane capabilities
(`bg_status` / `bg_output` / `bg_stop`).

The broker registers tasks inline on the event loop thread. The registry
spawns a per-task asyncio monitor for completion notifications.
`status`/`output` refresh liveness directly so they work even with no loop
attached (e.g. unit tests that poll).
"""

import asyncio
import os
import signal
import threading
from dataclasses import dataclass
from datetime import UTC, datetime

from tartarus.audit import AuditEvent, AuditSink, NullAuditLog
from tartarus.jail import BackgroundHandle, ExecResult
from tartarus.models import ToolResult

DEFAULT_OUTPUT_TRUNCATE_CHARS = 10_000
MAX_UTF8_BYTES_PER_CHAR = 4


class BackgroundError(Exception):
    """Raised when a control op references an unknown task."""


@dataclass(frozen=True)
class Notice:
    """A completion notice the loop turns into a transcript message."""

    task_id: str
    capability: str
    exit_code: int
    output_tail: str
    network_summary: str | None = None


@dataclass
class _Task:
    task_id: str
    capability: str
    handle: BackgroundHandle
    started_at: str
    status: str = "running"  # "running" | "exited"
    exit_code: int | None = None
    monitor: asyncio.Task | None = None
    _finalized: bool = False


class BackgroundRegistry:
    def __init__(
        self,
        notices: "asyncio.Queue[Notice] | None" = None,
        loop: asyncio.AbstractEventLoop | None = None,
        output_truncate: int = DEFAULT_OUTPUT_TRUNCATE_CHARS,
        audit: AuditSink | None = None,
    ):
        self._tasks: dict[str, _Task] = {}
        self._counter = 0
        self._notices = notices
        self._loop = loop
        self._output_truncate = output_truncate
        self._audit = audit if audit is not None else NullAuditLog()
        self._lock = threading.RLock()

    def register(self, capability: str, handle: BackgroundHandle) -> str:
        """Track a freshly launched task and return its id. Thread-safe."""
        with self._lock:
            self._counter += 1
            bg_id = f"bg-{self._counter}"
            task = _Task(
                task_id=bg_id,
                capability=capability,
                handle=handle,
                started_at=datetime.now(UTC).isoformat(),
            )
            self._tasks[bg_id] = task
        if self._loop is not None:
            self._loop.call_soon(self._start_monitor, task)
        return bg_id

    # --- control-plane operations (called by the broker) --------------------

    def status(self, task_id: str | None) -> str:
        task = self._get(task_id)
        self._refresh(task)
        if task.status == "running":
            return (
                f"{task.task_id} ({task.capability}): running since {task.started_at}"
            )
        return (
            f"{task.task_id} ({task.capability}): exited with code {task.exit_code} "
            f"(started {task.started_at})"
        )

    def output(self, task_id: str | None, offset: int = 0) -> str:
        task = self._get(task_id)
        self._refresh(task)
        text = self._read_log(task, offset)
        if not text:
            return "(no output)"
        return text

    def stop(self, task_id: str | None) -> str:
        task = self._get(task_id)
        self._refresh(task)
        if task.status == "exited":
            return f"{task.task_id} already exited with code {task.exit_code}"
        self._kill(task)
        return f"{task.task_id} sent SIGTERM"

    # --- teardown -----------------------------------------------------------

    def shutdown_all(self) -> None:
        """Kill every running task, stop its proxy, cancel its monitor."""
        with self._lock:
            tasks = list(self._tasks.values())
        for task in tasks:
            if task.status == "running":
                self._kill(task)
            if task.handle.proxy is not None:
                task.handle.proxy.stop()
            if task.monitor is not None:
                task.monitor.cancel()

    @property
    def has_running(self) -> bool:
        return any(self._is_running(task) for task in self._tasks.values())

    # --- internals ----------------------------------------------------------

    def _start_monitor(self, task: _Task) -> None:
        assert self._loop is not None
        task.monitor = self._loop.create_task(self._monitor(task))

    async def _monitor(self, task: _Task) -> None:
        code = await asyncio.to_thread(task.handle.proc.wait)
        summary = self._finalize(task, code)
        if self._notices is not None:
            await self._notices.put(
                Notice(
                    task_id=task.task_id,
                    capability=task.capability,
                    exit_code=code,
                    output_tail=self._read_log(task, 0),
                    network_summary=summary,
                )
            )

    def _finalize(self, task: _Task, code: int) -> str | None:
        """Record the exit once and tear down the task's proxy. Idempotent."""
        with self._lock:
            if task._finalized:
                return None
            task._finalized = True
            task.status = "exited"
            task.exit_code = code

        summary = None
        if task.handle.proxy is not None:
            summary = task.handle.proxy.summary()
            task.handle.proxy.stop()

        self._audit_completion(task, code, summary)
        return summary

    def _audit_completion(self, task: _Task, code: int, summary: str | None) -> None:
        """Append the second audit record for a background task: its completion.

        The launch was already audited by the broker; this closes the loop with
        the exit code and output size, reusing the same JSONL sink.
        """
        tail = self._read_log(task, 0)
        self._audit.record(
            AuditEvent(
                call_id=task.task_id,
                tool_name=task.capability,
                arguments={},
                command="(background completion)",
                exec_result=ExecResult(code, tail, "", network_summary=summary),
                result=ToolResult(task.task_id, tail, is_error=code != 0),
            )
        )

    def _refresh(self, task: _Task) -> None:
        """Pick up an exit even when no async monitor is running (poll path)."""
        if task.status != "running":
            return
        code = task.handle.proc.poll()
        if code is not None:
            self._finalize(task, code)

    def _is_running(self, task: _Task) -> bool:
        self._refresh(task)
        return task.status == "running"

    def _kill(self, task: _Task) -> None:
        try:
            os.killpg(task.handle.pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

    def _read_log(self, task: _Task, offset: int) -> str:
        try:
            with open(task.handle.log_path, "rb") as log_file:
                size = log_file.seek(0, os.SEEK_END)
                # Read only the final window that can survive truncation, not the
                # whole (possibly huge) log. output_truncate counts characters but
                # the log is bytes, so budget the worst case: a UTF-8 character is
                # at most MAX_UTF8_BYTES_PER_CHAR bytes. A seek may split a leading
                # character; decode(errors="replace") absorbs it and the tail slice
                # below discards it.
                tail_byte_budget = self._output_truncate * MAX_UTF8_BYTES_PER_CHAR
                window_start = max(offset, size - tail_byte_budget)
                log_file.seek(window_start)
                # Bound the read explicitly: a concurrent writer may have grown
                # the file past the sampled size, and read() alone would follow
                # it to the new EOF, defeating the memory bound.
                data = log_file.read(tail_byte_budget)
        except OSError:
            return ""
        text = data.decode("utf-8", "replace")
        if window_start > offset or len(text) > self._output_truncate:
            # Keep the tail: for a long-running task the recent output matters most.
            return "...(truncated)\n" + text[-self._output_truncate :]
        return text

    def _get(self, task_id: str | None) -> _Task:
        with self._lock:
            if task_id not in self._tasks:
                raise BackgroundError(f"unknown background task {task_id!r}")
            return self._tasks[task_id]
