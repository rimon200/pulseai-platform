from __future__ import annotations

import os
import uuid
from datetime import datetime
from typing import Any


ACTIVE_JOB_STATUSES = {
    "queued",
    "claimed",
    "downloading",
    "transcribing",
    "scoring",
    "rendering",
    "uploading",
}
TERMINAL_JOB_STATUSES = {"completed", "deferred_memory", "failed"}
JOB_ADVISORY_LOCK_ID = 22616960936427851
EMBEDDED_WORKER_ADVISORY_LOCK_ID = 22616960936427852


def _database_url() -> str:
    return os.getenv("DATABASE_URL", "").strip()


def ensure_generation_jobs_table() -> bool:
    database_url = _database_url()
    if not database_url:
        return False
    try:
        import psycopg

        with psycopg.connect(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    (JOB_ADVISORY_LOCK_ID,),
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS clip_generation_jobs (
                        id UUID PRIMARY KEY,
                        status TEXT NOT NULL,
                        trigger_type TEXT NOT NULL,
                        requested_creator TEXT,
                        result_clip_id TEXT,
                        error_message TEXT,
                        retry_count INTEGER NOT NULL DEFAULT 0,
                        claimed_by TEXT,
                        lease_expires_at TIMESTAMPTZ,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        started_at TIMESTAMPTZ,
                        completed_at TIMESTAMPTZ,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        CONSTRAINT clip_generation_jobs_status_check CHECK (
                            status IN (
                                'queued', 'claimed', 'downloading',
                                'transcribing', 'scoring', 'rendering',
                                'uploading', 'completed', 'deferred_memory',
                                'failed'
                            )
                        ),
                        CONSTRAINT clip_generation_jobs_trigger_check CHECK (
                            trigger_type IN ('manual', 'automatic')
                        )
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS clip_generation_jobs_claim_idx
                    ON clip_generation_jobs (
                        status, lease_expires_at, created_at
                    )
                    """
                )
            connection.commit()
        return True
    except Exception as error:
        print(f"GENERATION JOB MIGRATION FAILED | error={error!r}")
        return False


def _row_to_job(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def enqueue_generation_job(
    trigger_type: str,
    requested_creator: str | None = None,
) -> tuple[dict[str, Any], bool]:
    database_url = _database_url()
    if not database_url:
        raise RuntimeError("PostgreSQL is required for generation jobs.")
    normalized_trigger = (
        trigger_type if trigger_type in {"manual", "automatic"} else "manual"
    )
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (JOB_ADVISORY_LOCK_ID,),
            )
            cursor.execute(
                """
                SELECT * FROM clip_generation_jobs
                WHERE status = ANY(%s)
                ORDER BY created_at
                LIMIT 1
                FOR UPDATE
                """,
                (list(ACTIVE_JOB_STATUSES | {"deferred_memory"}),),
            )
            existing = cursor.fetchone()
            if existing:
                connection.commit()
                return dict(existing), False
            job_id = uuid.uuid4()
            cursor.execute(
                """
                INSERT INTO clip_generation_jobs (
                    id, status, trigger_type, requested_creator
                ) VALUES (%s, 'queued', %s, %s)
                RETURNING *
                """,
                (job_id, normalized_trigger, requested_creator),
            )
            created = cursor.fetchone()
        connection.commit()
    job = dict(created)
    print(
        "GENERATION JOB QUEUED | "
        f"job_id={job['id']} | trigger_type={normalized_trigger}"
    )
    return job, True


def get_generation_job(job_id: str) -> dict[str, Any] | None:
    database_url = _database_url()
    if not database_url:
        return None
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM clip_generation_jobs WHERE id = %s",
                (job_id,),
            )
            return _row_to_job(cursor.fetchone())


def try_acquire_embedded_worker_ownership() -> object | None:
    """Hold a PostgreSQL session lock for one worker loop across web processes."""
    database_url = _database_url()
    if not database_url:
        return None
    import psycopg

    connection = psycopg.connect(database_url, autocommit=True)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_try_advisory_lock(%s)",
                (EMBEDDED_WORKER_ADVISORY_LOCK_ID,),
            )
            acquired = bool(cursor.fetchone()[0])
        if acquired:
            return connection
    except Exception:
        connection.close()
        raise
    connection.close()
    return None


def embedded_worker_ownership_is_alive(connection: object) -> bool:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            return cursor.fetchone()[0] == 1
    except Exception:
        return False


def release_embedded_worker_ownership(connection: object | None) -> None:
    if connection is None:
        return
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_unlock(%s)",
                (EMBEDDED_WORKER_ADVISORY_LOCK_ID,),
            )
    finally:
        connection.close()


def claim_generation_job(
    worker_id: str,
    lease_seconds: int = 120,
    deferred_retry_seconds: int = 300,
) -> dict[str, Any] | None:
    database_url = _database_url()
    if not database_url:
        raise RuntimeError("PostgreSQL is required for generation jobs.")
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (JOB_ADVISORY_LOCK_ID,),
            )
            cursor.execute(
                """
                SELECT id FROM clip_generation_jobs
                WHERE status = ANY(%s)
                  AND lease_expires_at > NOW()
                LIMIT 1
                """,
                ([
                    "claimed", "downloading", "transcribing", "scoring",
                    "rendering", "uploading",
                ],),
            )
            if cursor.fetchone():
                connection.commit()
                return None
            cursor.execute(
                """
                SELECT *,
                       (
                           status NOT IN ('queued', 'deferred_memory')
                           AND lease_expires_at <= NOW()
                       ) AS stale_lease
                FROM clip_generation_jobs
                WHERE status = 'queued'
                   OR (
                       status = 'deferred_memory'
                       AND updated_at <= NOW() - (%s * INTERVAL '1 second')
                   )
                   OR (
                       status IN (
                           'claimed', 'downloading', 'transcribing',
                           'scoring', 'rendering', 'uploading'
                       )
                       AND lease_expires_at <= NOW()
                   )
                ORDER BY
                    CASE WHEN status = 'queued' THEN 0 ELSE 1 END,
                    created_at
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """,
                (deferred_retry_seconds,),
            )
            selected = cursor.fetchone()
            if not selected:
                connection.commit()
                return None
            stale_lease = bool(selected.pop("stale_lease"))
            cursor.execute(
                """
                UPDATE clip_generation_jobs SET
                    status = 'claimed',
                    claimed_by = %s,
                    lease_expires_at = NOW() + (%s * INTERVAL '1 second'),
                    started_at = COALESCE(started_at, NOW()),
                    completed_at = NULL,
                    error_message = NULL,
                    retry_count = retry_count + %s,
                    updated_at = NOW()
                WHERE id = %s
                RETURNING *
                """,
                (
                    worker_id,
                    lease_seconds,
                    1 if stale_lease or selected["status"] == "deferred_memory" else 0,
                    selected["id"],
                ),
            )
            claimed = cursor.fetchone()
        connection.commit()
    job = dict(claimed)
    if stale_lease:
        print(
            "GENERATION JOB LEASE RECOVERED | "
            f"job_id={job['id']} | worker={worker_id}"
        )
    print(
        "GENERATION JOB CLAIMED | "
        f"job_id={job['id']} | worker={worker_id}"
    )
    return job


def renew_generation_job_lease(
    job_id: str,
    worker_id: str,
    lease_seconds: int = 120,
) -> bool:
    return _update_owned_job(
        job_id,
        worker_id,
        """
        lease_expires_at = NOW() + (%s * INTERVAL '1 second'),
        updated_at = NOW()
        """,
        (lease_seconds,),
    )


def update_generation_job_stage(
    job_id: str,
    worker_id: str,
    stage: str,
    lease_seconds: int = 120,
) -> bool:
    if stage not in ACTIVE_JOB_STATUSES - {"queued"}:
        raise ValueError(f"Unsupported generation job stage: {stage}")
    updated = _update_owned_job(
        job_id,
        worker_id,
        """
        status = %s,
        lease_expires_at = NOW() + (%s * INTERVAL '1 second'),
        updated_at = NOW()
        """,
        (stage, lease_seconds),
    )
    if updated:
        print(f"GENERATION JOB STAGE | job_id={job_id} | stage={stage}")
    return updated


def complete_generation_job(
    job_id: str,
    worker_id: str,
    result_clip_id: str | None,
    message: str | None = None,
) -> bool:
    updated = _update_owned_job(
        job_id,
        worker_id,
        """
        status = 'completed',
        result_clip_id = %s,
        error_message = %s,
        lease_expires_at = NULL,
        completed_at = NOW(),
        updated_at = NOW()
        """,
        (result_clip_id, message),
    )
    if updated:
        print(
            "GENERATION JOB COMPLETED | "
            f"job_id={job_id} | result_clip_id={result_clip_id or 'none'}"
        )
    return updated


def defer_generation_job(
    job_id: str,
    worker_id: str,
    message: str,
) -> bool:
    updated = _update_owned_job(
        job_id,
        worker_id,
        """
        status = 'deferred_memory',
        error_message = %s,
        lease_expires_at = NULL,
        completed_at = NOW(),
        updated_at = NOW()
        """,
        (message,),
    )
    if updated:
        print(f"GENERATION JOB DEFERRED | job_id={job_id} | error={message}")
    return updated


def fail_generation_job(
    job_id: str,
    worker_id: str,
    message: str,
) -> bool:
    updated = _update_owned_job(
        job_id,
        worker_id,
        """
        status = 'failed',
        error_message = %s,
        lease_expires_at = NULL,
        completed_at = NOW(),
        updated_at = NOW()
        """,
        (message,),
    )
    if updated:
        print(f"GENERATION JOB FAILED | job_id={job_id} | error={message}")
    return updated


def _update_owned_job(
    job_id: str,
    worker_id: str,
    set_sql: str,
    parameters: tuple[Any, ...],
) -> bool:
    database_url = _database_url()
    if not database_url:
        return False
    import psycopg

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE clip_generation_jobs SET {set_sql}
                WHERE id = %s
                  AND claimed_by = %s
                  AND status NOT IN ('completed', 'failed')
                RETURNING id
                """,
                (*parameters, job_id, worker_id),
            )
            updated = cursor.fetchone()
        connection.commit()
    return updated is not None


def serialize_generation_job(job: dict[str, Any]) -> dict[str, Any]:
    serialized: dict[str, Any] = {}
    for key, value in job.items():
        if isinstance(value, datetime):
            serialized[key] = value.isoformat()
        elif isinstance(value, uuid.UUID):
            serialized[key] = str(value)
        else:
            serialized[key] = value
    return serialized
