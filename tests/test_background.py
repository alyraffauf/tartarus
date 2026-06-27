"""BackgroundRegistry tests.

These launch real short-lived `sh` subprocesses directly (no bwrap), so they
exercise the registry's tracking, log tailing, signalling, and async monitor in
isolation from the jail. The jailed launch path is covered by test_jail.py.
"""

import asyncio
import os
import subprocess
import threading

import pytest

from tartarus.background import BackgroundError, BackgroundRegistry
from tartarus.jail import BackgroundHandle


def _launch(args: list[str], log_path: str) -> BackgroundHandle:
    log_file = open(log_path, "wb")
    proc = subprocess.Popen(
        args, stdout=log_file, stderr=subprocess.STDOUT, start_new_session=True
    )
    log_file.close()
    return BackgroundHandle(proc=proc, pgid=os.getpgid(proc.pid), log_path=log_path)


class MemorySink:
    def __init__(self):
        self.records = []

    def record(self, event) -> None:
        self.records.append(event)


def test_register_assigns_sequential_ids(tmp_path):
    registry = BackgroundRegistry()
    one = _launch(["sh", "-c", "true"], str(tmp_path / "1.log"))
    two = _launch(["sh", "-c", "true"], str(tmp_path / "2.log"))

    assert registry.register("cap", one) == "bg-1"
    assert registry.register("cap", two) == "bg-2"


def test_status_reflects_exit_without_a_loop(tmp_path):
    # No event loop/monitor attached: status must refresh liveness itself.
    registry = BackgroundRegistry()
    handle = _launch(["sh", "-c", "sleep 0.2; exit 0"], str(tmp_path / "s.log"))
    registry.register("cap", handle)

    assert "running" in registry.status("bg-1")
    handle.proc.wait(timeout=5)
    assert "exited with code 0" in registry.status("bg-1")


def test_output_reads_log_from_offset(tmp_path):
    registry = BackgroundRegistry()
    handle = _launch(["sh", "-c", "printf abcdef"], str(tmp_path / "o.log"))
    handle.proc.wait(timeout=5)
    registry.register("cap", handle)

    assert registry.output("bg-1") == "abcdef"
    assert registry.output("bg-1", 3) == "def"


def test_output_truncates_logs_larger_than_the_tail_window(tmp_path):
    # A log far larger than the budget must come back as only its final window
    # (with the truncation marker), exercising the seek-to-tail read path.
    registry = BackgroundRegistry(output_truncate=10)
    payload = "HEAD" + "x" * 200 + "TAIL"
    handle = _launch(["sh", "-c", f"printf '%s' {payload}"], str(tmp_path / "big.log"))
    handle.proc.wait(timeout=5)
    registry.register("cap", handle)

    output = registry.output("bg-1")

    assert output.startswith("...(truncated)\n")
    assert output.endswith("xxxxxxTAIL")  # the last output_truncate characters
    assert "HEAD" not in output


def test_stop_kills_a_running_task(tmp_path):
    registry = BackgroundRegistry()
    handle = _launch(["sh", "-c", "sleep 30"], str(tmp_path / "k.log"))
    registry.register("cap", handle)

    assert "SIGTERM" in registry.stop("bg-1")
    handle.proc.wait(timeout=5)
    assert "exited" in registry.status("bg-1")


def test_shutdown_all_reaps_running_tasks(tmp_path):
    registry = BackgroundRegistry()
    handle = _launch(["sh", "-c", "sleep 30"], str(tmp_path / "r.log"))
    registry.register("cap", handle)

    registry.shutdown_all()

    handle.proc.wait(timeout=5)
    assert handle.proc.returncode is not None


def test_unknown_task_raises():
    registry = BackgroundRegistry()
    with pytest.raises(BackgroundError, match="unknown background task"):
        registry.status("bg-404")


def test_completion_is_audited(tmp_path):
    sink = MemorySink()
    registry = BackgroundRegistry(audit=sink)
    handle = _launch(["sh", "-c", "printf hi; exit 2"], str(tmp_path / "a.log"))
    handle.proc.wait(timeout=5)
    registry.register("cap", handle)

    registry.status("bg-1")  # refresh → finalize → one completion record

    assert len(sink.records) == 1
    assert sink.records[0].tool_name == "cap"
    assert sink.records[0].exec_result.code == 2
    # Idempotent: refreshing again does not double-record.
    registry.status("bg-1")
    assert len(sink.records) == 1


def test_monitor_enqueues_notice_on_exit(tmp_path):
    async def run():
        notices: asyncio.Queue = asyncio.Queue()
        registry = BackgroundRegistry(notices=notices, loop=asyncio.get_running_loop())
        handle = _launch(["sh", "-c", "printf hello; exit 3"], str(tmp_path / "n.log"))
        bg_id = registry.register("cap", handle)
        notice = await asyncio.wait_for(notices.get(), timeout=5)
        return bg_id, notice

    bg_id, notice = asyncio.run(run())

    assert bg_id == "bg-1"
    assert notice.task_id == "bg-1"
    assert notice.capability == "cap"
    assert notice.exit_code == 3
    assert "hello" in notice.output_tail


def test_register_is_thread_safe(tmp_path):
    registry = BackgroundRegistry()
    handles = [
        _launch(["sh", "-c", "true"], str(tmp_path / f"{i}.log")) for i in range(50)
    ]
    ids: list[str] = []
    errors: list[Exception] = []

    def register_one(handle):
        try:
            ids.append(registry.register("cap", handle))
        except Exception as exc:  # pragma: no cover - failures should fail the test
            errors.append(exc)

    threads = [threading.Thread(target=register_one, args=(h,)) for h in handles]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert len(ids) == 50
    assert len(set(ids)) == 50
    assert sorted(ids, key=lambda value: int(value.split("-", 1)[1])) == [
        f"bg-{i}" for i in range(1, 51)
    ]


def test_finalize_is_idempotent_under_lock(tmp_path):
    sink = MemorySink()
    registry = BackgroundRegistry(audit=sink)
    handle = _launch(["sh", "-c", "printf hi; exit 2"], str(tmp_path / "a.log"))
    handle.proc.wait(timeout=5)
    registry.register("cap", handle)
    task = registry._get("bg-1")

    # The first call finalizes; the second must not record again or stop proxy twice.
    assert registry._finalize(task, 2) is None
    assert registry._finalize(task, 2) is None
    assert len(sink.records) == 1
