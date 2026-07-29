from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import socket
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from generation_jobs import (
    claim_generation_job,
    complete_generation_job,
    defer_generation_job,
    embedded_worker_ownership_is_alive,
    ensure_generation_jobs_table,
    fail_generation_job,
    get_generation_job,
    release_embedded_worker_ownership,
    renew_generation_job_lease,
    try_acquire_embedded_worker_ownership,
)


LEASE_SECONDS = max(60, int(os.getenv("CLIP_JOB_LEASE_SECONDS", "180")))
STANDALONE_POLL_SECONDS = max(
    1.0,
    float(os.getenv("CLIP_JOB_POLL_SECONDS", "3")),
)
EMBEDDED_POLL_SECONDS = max(
    1.0,
    float(os.getenv("EMBEDDED_CLIP_WORKER_POLL_SECONDS", "5")),
)
DEFERRED_RETRY_SECONDS = max(
    1,
    int(os.getenv("CLIP_JOB_DEFERRED_RETRY_SECONDS", "300")),
)
CHILD_RESULT_PREFIX = "pulseai-generation-result-"


def _worker_id() -> str:
    configured = os.getenv("CLIP_WORKER_ID", "").strip()
    return configured or (
        f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    )


def _cgroup_available_memory_mb() -> float | None:
    pairs = (
        (
            Path("/sys/fs/cgroup/memory.current"),
            Path("/sys/fs/cgroup/memory.max"),
        ),
        (
            Path("/sys/fs/cgroup/memory/memory.usage_in_bytes"),
            Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
        ),
    )
    for usage_path, limit_path in pairs:
        try:
            usage_bytes = int(usage_path.read_text(encoding="utf-8").strip())
            limit_text = limit_path.read_text(encoding="utf-8").strip()
            if limit_text == "max":
                continue
            limit_bytes = int(limit_text)
            if 0 < limit_bytes < (1 << 60):
                return max(
                    0.0,
                    (limit_bytes - usage_bytes) / (1024 * 1024),
                )
        except (FileNotFoundError, OSError, ValueError):
            continue
    try:
        for line in Path("/proc/meminfo").read_text(
            encoding="utf-8"
        ).splitlines():
            if line.startswith("MemAvailable:"):
                return float(line.split()[1]) / 1024
    except (FileNotFoundError, OSError, ValueError, IndexError):
        pass
    try:
        import psutil

        return psutil.virtual_memory().available / (1024 * 1024)
    except (ImportError, OSError):
        pass
    return None


def _whisper_memory_floor_mb() -> float:
    try:
        return max(
            0.0,
            float(os.getenv("WHISPER_MEMORY_FALLBACK_MB", "180")),
        )
    except ValueError:
        return 180.0


def _child_memory_admitted() -> tuple[bool, float | None, float]:
    available_mb = _cgroup_available_memory_mb()
    required_mb = _whisper_memory_floor_mb()
    return (
        available_mb is not None and available_mb >= required_mb,
        available_mb,
        required_mb,
    )


async def evaluate_claimed_job(job: dict[str, Any], worker_id: str) -> dict[str, Any]:
    """Execute one claimed job without applying its terminal database state."""
    import main

    job_id = str(job["id"])
    if not main._ensure_clip_history_table():
        return {
            "status": "failed",
            "error_message": "Clip history initialization failed in the worker.",
        }
    main.app.state.clip_history_ready = True
    try:
        result = await main._run_auto_generate_clip_pipeline(
            generation_job_id=job_id,
            generation_worker_id=worker_id,
        )
        message = (
            str(result.get("message") or "")
            if isinstance(result, dict)
            else ""
        )
        if "worker memory did not recover" in message.lower():
            return {
                "status": "deferred_memory",
                "error_message": message,
            }
        result_clip_id = None
        if isinstance(result, dict):
            result_clip_id = str(
                result.get("id")
                or result.get("generated_clip_id")
                or ""
            ).strip() or None
        return {
            "status": "completed",
            "result_clip_id": result_clip_id,
            "error_message": message or None,
        }
    except BaseException as error:
        return {
            "status": "failed",
            "error_message": repr(error),
        }


def _apply_terminal_result(
    job_id: str,
    worker_id: str,
    result: dict[str, Any],
) -> bool:
    status = result.get("status")
    message = str(result.get("error_message") or "")
    if status == "completed":
        return complete_generation_job(
            job_id,
            worker_id,
            result.get("result_clip_id"),
            message or None,
        )
    if status == "deferred_memory":
        return defer_generation_job(
            job_id,
            worker_id,
            message or "Worker memory did not recover.",
        )
    return fail_generation_job(
        job_id,
        worker_id,
        message or "Generation child exited without a result.",
    )


def _write_child_result(path: str, result: dict[str, Any]) -> None:
    destination = Path(path).resolve()
    temporary = destination.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(result), encoding="utf-8")
    os.replace(temporary, destination)


async def run_single_job_child(
    job_id: str,
    worker_id: str,
    result_path: str,
) -> int:
    job = get_generation_job(job_id)
    if (
        not job
        or str(job.get("claimed_by") or "") != worker_id
        or str(job.get("status") or "") not in {
            "claimed", "downloading", "transcribing", "scoring",
            "rendering", "uploading",
        }
    ):
        result = {
            "status": "failed",
            "error_message": "Generation job is not owned by this child.",
        }
        _write_child_result(result_path, result)
        return 2
    result = await evaluate_claimed_job(job, worker_id)
    _write_child_result(result_path, result)
    return 0 if result.get("status") != "failed" else 1


async def _terminate_and_reap(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        await process.wait()
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=10)
    except asyncio.TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
        await process.wait()


async def _run_claimed_job_child(
    job: dict[str, Any],
    worker_id: str,
) -> dict[str, Any]:
    job_id = str(job["id"])
    descriptor, result_path = tempfile.mkstemp(prefix=CHILD_RESULT_PREFIX, suffix=".json")
    os.close(descriptor)
    os.unlink(result_path)
    process: asyncio.subprocess.Process | None = None
    wait_task: asyncio.Task | None = None
    try:
        print(f"EMBEDDED WORKER CHILD START | job_id={job_id}")
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(Path(__file__).resolve()),
            "--run-job",
            job_id,
            "--worker-id",
            worker_id,
            "--result-file",
            result_path,
            start_new_session=True,
        )
        wait_task = asyncio.create_task(process.wait())
        renewal_interval = max(10.0, LEASE_SECONDS / 3)
        while not wait_task.done():
            done, _ = await asyncio.wait(
                {wait_task},
                timeout=renewal_interval,
            )
            if done:
                break
            if not renew_generation_job_lease(
                job_id,
                worker_id,
                LEASE_SECONDS,
            ):
                await _terminate_and_reap(process)
                return {
                    "status": "failed",
                    "error_message": "Generation job lease was lost.",
                }
        exit_status = await wait_task
        print(
            "EMBEDDED WORKER CHILD EXIT | "
            f"job_id={job_id} | exit_status={exit_status}"
        )
        try:
            result = json.loads(
                Path(result_path).read_text(encoding="utf-8")
            )
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            result = {
                "status": "failed",
                "error_message": (
                    "Generation child exited without a readable result "
                    f"(exit status {exit_status})."
                ),
            }
        if not isinstance(result, dict):
            return {
                "status": "failed",
                "error_message": "Generation child returned an invalid result.",
            }
        return result
    except asyncio.CancelledError:
        if process is not None:
            if wait_task is not None:
                wait_task.cancel()
                await asyncio.gather(wait_task, return_exceptions=True)
            await _terminate_and_reap(process)
            print(
                "EMBEDDED WORKER CHILD EXIT | "
                f"job_id={job_id} | exit_status={process.returncode} | "
                "reason=shutdown"
            )
        raise
    finally:
        try:
            os.unlink(result_path)
        except FileNotFoundError:
            pass


async def _owned_worker_loop(
    stop_event: asyncio.Event,
    worker_id: str,
    poll_seconds: float,
    ownership: object,
) -> None:
    while not stop_event.is_set():
        if not embedded_worker_ownership_is_alive(ownership):
            return
        job = claim_generation_job(
            worker_id,
            LEASE_SECONDS,
            DEFERRED_RETRY_SECONDS,
        )
        if job is None:
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=poll_seconds,
                )
            except asyncio.TimeoutError:
                pass
            continue
        job_id = str(job["id"])
        print(f"EMBEDDED WORKER CLAIMED JOB | job_id={job_id}")
        admitted, available_mb, required_mb = _child_memory_admitted()
        if not admitted:
            available_label = (
                f"{available_mb:.1f}" if available_mb is not None else "unknown"
            )
            message = (
                "Generation deferred before child spawn because available "
                f"memory was {available_label} MB; {required_mb:.1f} MB required."
            )
            print(
                "EMBEDDED WORKER MEMORY DEFERRED | "
                f"job_id={job_id} | available_mb={available_label} | "
                f"required_mb={required_mb:.1f}"
            )
            defer_generation_job(job_id, worker_id, message)
            continue
        try:
            result = await _run_claimed_job_child(job, worker_id)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            result = {
                "status": "failed",
                "error_message": (
                    "Generation child could not be started or monitored: "
                    f"{error!r}"
                ),
            }
        _apply_terminal_result(job_id, worker_id, result)


async def run_worker_loop(
    stop_event: asyncio.Event,
    *,
    embedded: bool,
    poll_seconds: float | None = None,
) -> None:
    if not ensure_generation_jobs_table():
        raise RuntimeError("Generation job storage is unavailable.")
    interval = poll_seconds or (
        EMBEDDED_POLL_SECONDS if embedded else STANDALONE_POLL_SECONDS
    )
    worker_id = _worker_id()
    standby_logged = False
    while not stop_event.is_set():
        ownership = None
        try:
            ownership = try_acquire_embedded_worker_ownership()
        except Exception as error:
            print(f"EMBEDDED WORKER STANDBY | reason={error!r}")
        if ownership is None:
            if not standby_logged:
                print("EMBEDDED WORKER STANDBY")
                standby_logged = True
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            continue
        standby_logged = False
        print(
            "EMBEDDED WORKER STARTED | "
            f"worker={worker_id} | mode={'embedded' if embedded else 'standalone'}"
        )
        try:
            try:
                while (
                    not stop_event.is_set()
                    and embedded_worker_ownership_is_alive(ownership)
                ):
                    await _owned_worker_loop(
                        stop_event,
                        worker_id,
                        interval,
                        ownership,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                print(f"EMBEDDED WORKER STANDBY | reason={error!r}")
        finally:
            try:
                release_embedded_worker_ownership(ownership)
            except Exception as error:
                print(f"EMBEDDED WORKER STOPPED | release_error={error!r}")
            else:
                print(f"EMBEDDED WORKER STOPPED | worker={worker_id}")


async def run_worker() -> None:
    stop_event = asyncio.Event()
    await run_worker_loop(stop_event, embedded=False)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-job")
    parser.add_argument("--worker-id")
    parser.add_argument("--result-file")
    return parser.parse_args()


def main() -> int:
    arguments = _parse_args()
    if arguments.run_job:
        if not arguments.worker_id or not arguments.result_file:
            raise SystemExit(
                "--worker-id and --result-file are required with --run-job"
            )
        return asyncio.run(
            run_single_job_child(
                arguments.run_job,
                arguments.worker_id,
                arguments.result_file,
            )
        )
    asyncio.run(run_worker())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
