from __future__ import annotations

import asyncio
import os
import socket
import threading
import time
import uuid

from generation_jobs import (
    claim_generation_job,
    complete_generation_job,
    defer_generation_job,
    ensure_generation_jobs_table,
    fail_generation_job,
    renew_generation_job_lease,
)


LEASE_SECONDS = max(
    60,
    int(os.getenv("CLIP_JOB_LEASE_SECONDS", "180")),
)
POLL_SECONDS = max(
    1.0,
    float(os.getenv("CLIP_JOB_POLL_SECONDS", "3")),
)


class LeaseRenewer:
    def __init__(self, job_id: str, worker_id: str):
        self.job_id = job_id
        self.worker_id = worker_id
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"clip-job-lease-{job_id}",
            daemon=True,
        )

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        self._stop_event.set()
        self._thread.join(timeout=10)

    def _run(self):
        renewal_interval = max(10.0, LEASE_SECONDS / 3)
        while not self._stop_event.wait(renewal_interval):
            try:
                if not renew_generation_job_lease(
                    self.job_id,
                    self.worker_id,
                    LEASE_SECONDS,
                ):
                    print(
                        "GENERATION JOB LEASE RENEWAL LOST | "
                        f"job_id={self.job_id}"
                    )
                    return
            except Exception as error:
                print(
                    "GENERATION JOB LEASE RENEWAL FAILED | "
                    f"job_id={self.job_id} | error={error!r}"
                )


async def process_one_job(job: dict, worker_id: str) -> None:
    import main

    job_id = str(job["id"])
    if not main._ensure_clip_history_table():
        fail_generation_job(
            job_id,
            worker_id,
            "Clip history initialization failed in the worker.",
        )
        return
    main.app.state.clip_history_ready = True
    try:
        with LeaseRenewer(job_id, worker_id):
            result = await main._run_auto_generate_clip_pipeline(
                generation_job_id=job_id,
                generation_worker_id=worker_id,
            )
        message = str(result.get("message") or "") if isinstance(result, dict) else ""
        if "worker memory did not recover" in message.lower():
            defer_generation_job(job_id, worker_id, message)
            return
        result_clip_id = None
        if isinstance(result, dict):
            result_clip_id = str(
                result.get("id")
                or result.get("generated_clip_id")
                or ""
            ).strip() or None
        complete_generation_job(
            job_id,
            worker_id,
            result_clip_id,
            message or None,
        )
    except Exception as error:
        fail_generation_job(job_id, worker_id, repr(error))


async def run_worker() -> None:
    if not ensure_generation_jobs_table():
        raise RuntimeError("Generation job storage is unavailable.")
    worker_id = (
        os.getenv("CLIP_WORKER_ID", "").strip()
        or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    )
    print(f"CLIP WORKER STARTED | worker={worker_id}")
    while True:
        job = claim_generation_job(worker_id, LEASE_SECONDS)
        if job is None:
            await asyncio.sleep(POLL_SECONDS)
            continue
        await process_one_job(job, worker_id)


if __name__ == "__main__":
    asyncio.run(run_worker())
