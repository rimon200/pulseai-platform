from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta
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


def _trusted_automatic_job_predicate(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return (
        f"{prefix}trigger_type = 'automatic' "
        f"AND {prefix}requested_creator_id IS NOT NULL "
        f"AND {prefix}eligibility_stream_id IS NOT NULL"
    )


def _automatic_success_today_predicate(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return (
        f"{_trusted_automatic_job_predicate(alias)} "
        f"AND {prefix}status = 'completed' "
        f"AND {prefix}outcome = 'clip_created' "
        f"AND {prefix}result_clip_id IS NOT NULL "
        "AND EXISTS ("
        "SELECT 1 FROM twitch_clip_history AS generated_clip "
        f"WHERE generated_clip.generated_clip_id = {prefix}result_clip_id "
        "AND generated_clip.generated_at IS NOT NULL "
        "AND (generated_clip.generated_at AT TIME ZONE %s)::date = "
        "(NOW() AT TIME ZONE %s)::date)"
    )


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
                        provider TEXT NOT NULL DEFAULT 'twitch',
                        source_upload_id UUID,
                        requested_creator_id TEXT,
                        eligibility_stream_id TEXT,
                        eligibility_range_end TIMESTAMPTZ,
                        estimated_outbound_bytes BIGINT NOT NULL DEFAULT 0,
                        actual_outbound_bytes BIGINT NOT NULL DEFAULT 0,
                        result_clip_id TEXT,
                        outcome TEXT,
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
                    ALTER TABLE clip_generation_jobs
                    ADD COLUMN IF NOT EXISTS outcome TEXT
                    """
                )
                for definition in (
                    "provider TEXT NOT NULL DEFAULT 'twitch'",
                    "source_upload_id UUID",
                    "requested_creator_id TEXT",
                    "eligibility_stream_id TEXT",
                    "eligibility_range_end TIMESTAMPTZ",
                    "estimated_outbound_bytes BIGINT NOT NULL DEFAULT 0",
                    "actual_outbound_bytes BIGINT NOT NULL DEFAULT 0",
                ):
                    cursor.execute(
                        "ALTER TABLE clip_generation_jobs "
                        f"ADD COLUMN IF NOT EXISTS {definition}"
                    )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS auto_clip_automation_state (
                        creator_id TEXT PRIMARY KEY,
                        creator_login TEXT NOT NULL,
                        last_automatic_enqueue_at TIMESTAMPTZ,
                        last_successful_automatic_clip_at TIMESTAMPTZ,
                        last_eligibility_stream_id TEXT,
                        last_eligibility_range_end TIMESTAMPTZ,
                        last_skip_reason TEXT,
                        last_scheduler_check_at TIMESTAMPTZ,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS clip_generation_outbound_transfers (
                        id BIGSERIAL PRIMARY KEY,
                        job_id UUID NOT NULL REFERENCES clip_generation_jobs(id)
                            ON DELETE CASCADE,
                        destination TEXT NOT NULL,
                        bytes BIGINT NOT NULL CHECK (bytes > 0),
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
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
                cursor.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS clip_generation_jobs_youtube_upload_idx
                    ON clip_generation_jobs (provider, source_upload_id)
                    WHERE source_upload_id IS NOT NULL
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
    *,
    provider: str = "twitch",
    source_upload_id: str | None = None,
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
            if source_upload_id:
                cursor.execute(
                    """
                    SELECT * FROM clip_generation_jobs
                    WHERE provider = %s AND source_upload_id = %s
                    LIMIT 1 FOR UPDATE
                    """,
                    (provider, source_upload_id),
                )
                existing_source_job = cursor.fetchone()
                if existing_source_job:
                    connection.commit()
                    return dict(existing_source_job), False
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
                    id, status, trigger_type, requested_creator,
                    provider, source_upload_id
                ) VALUES (%s, 'queued', %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    job_id, normalized_trigger, requested_creator,
                    provider if provider in {"twitch", "youtube"} else "twitch",
                    source_upload_id,
                ),
            )
            created = cursor.fetchone()
        connection.commit()
    job = dict(created)
    print(
        "GENERATION JOB QUEUED | "
        f"job_id={job['id']} | trigger_type={normalized_trigger}"
    )
    return job, True


def automatic_usage_snapshot(
    creator_id: str | None = None,
    workspace_timezone: str = "UTC",
) -> dict[str, Any]:
    database_url = _database_url()
    if not database_url:
        raise RuntimeError("PostgreSQL is required for automatic usage.")
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT
                    COUNT(*) FILTER (
                        WHERE {_trusted_automatic_job_predicate()}
                          AND (created_at AT TIME ZONE %s)::date =
                              (NOW() AT TIME ZONE %s)::date
                    ) AS automatic_jobs_enqueued_today,
                    COUNT(DISTINCT result_clip_id) FILTER (
                        WHERE {_automatic_success_today_predicate()}
                    ) AS automatic_clips_created_today,
                    COALESCE(SUM(estimated_outbound_bytes) FILTER (
                        WHERE {_trusted_automatic_job_predicate()}
                          AND (created_at AT TIME ZONE %s)::date =
                              (NOW() AT TIME ZONE %s)::date
                    ), 0) AS estimated_outbound_bytes,
                    COALESCE(SUM(actual_outbound_bytes) FILTER (
                        WHERE trigger_type = 'automatic'
                          AND created_at >= date_trunc('day', NOW())
                    ), 0) AS actual_outbound_bytes,
                    COALESCE(AVG(NULLIF(actual_outbound_bytes, 0)) FILTER (
                        WHERE trigger_type = 'automatic'
                          AND outcome = 'clip_created'
                    ), 0) AS recent_average_bytes,
                    MAX(created_at) FILTER (
                        WHERE trigger_type = 'automatic'
                    ) AS last_automatic_run
                FROM clip_generation_jobs
                """,
                (
                    workspace_timezone,
                    workspace_timezone,
                    workspace_timezone,
                    workspace_timezone,
                    workspace_timezone,
                    workspace_timezone,
                ),
            )
            snapshot = dict(cursor.fetchone())
            snapshot["jobs_enqueued"] = snapshot[
                "automatic_jobs_enqueued_today"
            ]
            snapshot["clips_created"] = snapshot[
                "automatic_clips_created_today"
            ]
            cursor.execute(
                """
                SELECT COALESCE(SUM(transfer.bytes), 0) AS actual_outbound_bytes
                FROM clip_generation_outbound_transfers AS transfer
                JOIN clip_generation_jobs AS job ON job.id = transfer.job_id
                WHERE job.trigger_type = 'automatic'
                  AND (transfer.created_at AT TIME ZONE %s)::date =
                      (NOW() AT TIME ZONE %s)::date
                """,
                (workspace_timezone, workspace_timezone),
            )
            snapshot["actual_outbound_bytes"] = int(
                cursor.fetchone()["actual_outbound_bytes"]
            )
            if creator_id:
                cursor.execute(
                    f"""
                    SELECT
                        COUNT(DISTINCT result_clip_id) FILTER (
                            WHERE {_automatic_success_today_predicate()}
                        ) AS creator_clips_created,
                        MAX(created_at) FILTER (
                            WHERE trigger_type = 'automatic'
                        ) AS last_creator_enqueue
                    FROM clip_generation_jobs
                    WHERE requested_creator_id = %s
                    """,
                    (
                        workspace_timezone,
                        workspace_timezone,
                        creator_id,
                    ),
                )
                snapshot.update(dict(cursor.fetchone()))
                cursor.execute(
                    """
                    SELECT * FROM auto_clip_automation_state
                    WHERE creator_id = %s
                    """,
                    (creator_id,),
                )
                state = cursor.fetchone()
                snapshot["creator_state"] = dict(state) if state else {}
            else:
                cursor.execute(
                    """
                    SELECT last_skip_reason, last_scheduler_check_at
                    FROM auto_clip_automation_state
                    ORDER BY last_scheduler_check_at DESC NULLS LAST
                    LIMIT 1
                    """
                )
                state = cursor.fetchone()
                if state:
                    snapshot.update(dict(state))
    return snapshot


def record_automatic_skip(
    creator_id: str,
    creator_login: str,
    reason: str,
) -> None:
    database_url = _database_url()
    if not database_url:
        return
    import psycopg

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO auto_clip_automation_state (
                    creator_id, creator_login, last_skip_reason,
                    last_scheduler_check_at
                ) VALUES (%s, %s, %s, NOW())
                ON CONFLICT (creator_id) DO UPDATE SET
                    creator_login = EXCLUDED.creator_login,
                    last_skip_reason = EXCLUDED.last_skip_reason,
                    last_scheduler_check_at = NOW(),
                    updated_at = NOW()
                """,
                (creator_id, creator_login, reason),
            )
        connection.commit()


def enqueue_eligible_automatic_job(
    *,
    creator_login: str,
    creator_id: str,
    stream_id: str,
    range_end: datetime,
    estimated_outbound_bytes: int,
    creator_daily_limit: int,
    global_daily_limit: int,
    cooldown_minutes: int,
    outbound_daily_budget_bytes: int,
    workspace_timezone: str = "UTC",
) -> tuple[dict[str, Any] | None, str, dict[str, Any]]:
    database_url = _database_url()
    if not database_url:
        raise RuntimeError("PostgreSQL is required for automatic generation.")
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
                SELECT COUNT(*) AS active_count
                FROM clip_generation_jobs
                WHERE status = ANY(%s)
                """,
                (list(ACTIVE_JOB_STATUSES | {"deferred_memory"}),),
            )
            active_count = int(cursor.fetchone()["active_count"])
            cursor.execute(
                f"""
                SELECT
                    COUNT(*) FILTER (
                        WHERE {_trusted_automatic_job_predicate()}
                          AND (created_at AT TIME ZONE %s)::date =
                              (NOW() AT TIME ZONE %s)::date
                    ) AS daily_jobs,
                    COUNT(DISTINCT result_clip_id) FILTER (
                        WHERE {_automatic_success_today_predicate()}
                    ) AS daily_clips,
                    COALESCE(SUM(estimated_outbound_bytes) FILTER (
                        WHERE {_trusted_automatic_job_predicate()}
                          AND (created_at AT TIME ZONE %s)::date =
                              (NOW() AT TIME ZONE %s)::date
                    ), 0) AS estimated_bytes
                FROM clip_generation_jobs
                """,
                (
                    workspace_timezone,
                    workspace_timezone,
                    workspace_timezone,
                    workspace_timezone,
                    workspace_timezone,
                    workspace_timezone,
                ),
            )
            global_usage = dict(cursor.fetchone())
            cursor.execute(
                f"""
                SELECT
                    COUNT(DISTINCT result_clip_id) FILTER (
                        WHERE {_automatic_success_today_predicate()}
                    ) AS daily_creator_clips,
                    MAX(created_at) AS last_enqueue
                FROM clip_generation_jobs
                WHERE trigger_type = 'automatic'
                  AND requested_creator_id = %s
                """,
                (
                    workspace_timezone,
                    workspace_timezone,
                    creator_id,
                ),
            )
            creator_usage = dict(cursor.fetchone())
            cursor.execute(
                """
                SELECT last_eligibility_stream_id,
                       last_eligibility_range_end
                FROM auto_clip_automation_state
                WHERE creator_id = %s
                FOR UPDATE
                """,
                (creator_id,),
            )
            eligibility_state = cursor.fetchone()
            reason = ""
            if active_count:
                reason = "job_already_active"
            elif (
                eligibility_state
                and str(
                    eligibility_state["last_eligibility_stream_id"] or ""
                )
                == stream_id
                and eligibility_state["last_eligibility_range_end"]
                and eligibility_state["last_eligibility_range_end"]
                >= range_end
            ):
                reason = "no_new_material"
            elif int(creator_usage["daily_creator_clips"]) >= creator_daily_limit:
                reason = "creator_daily_limit"
            elif int(global_usage["daily_clips"]) >= global_daily_limit:
                reason = "global_daily_limit"
            elif (
                creator_usage["last_enqueue"]
                and creator_usage["last_enqueue"]
                > datetime.now().astimezone()
                - timedelta(minutes=cooldown_minutes)
            ):
                reason = "cooldown"
            elif (
                int(global_usage["estimated_bytes"])
                + estimated_outbound_bytes
                > outbound_daily_budget_bytes
            ):
                reason = "outbound_budget"
            if reason:
                cursor.execute(
                    """
                    INSERT INTO auto_clip_automation_state (
                        creator_id, creator_login, last_skip_reason,
                        last_scheduler_check_at
                    ) VALUES (%s, %s, %s, NOW())
                    ON CONFLICT (creator_id) DO UPDATE SET
                        creator_login = EXCLUDED.creator_login,
                        last_skip_reason = EXCLUDED.last_skip_reason,
                        last_scheduler_check_at = NOW(),
                        updated_at = NOW()
                    """,
                    (creator_id, creator_login, reason),
                )
                connection.commit()
                return None, reason, {
                    **global_usage,
                    **creator_usage,
                }
            job_id = uuid.uuid4()
            cursor.execute(
                """
                INSERT INTO clip_generation_jobs (
                    id, status, trigger_type, requested_creator,
                    requested_creator_id, eligibility_stream_id,
                    eligibility_range_end, estimated_outbound_bytes
                ) VALUES (
                    %s, 'queued', 'automatic', %s, %s, %s, %s, %s
                )
                RETURNING *
                """,
                (
                    job_id, creator_login, creator_id, stream_id,
                    range_end, estimated_outbound_bytes,
                ),
            )
            job = dict(cursor.fetchone())
            cursor.execute(
                """
                INSERT INTO auto_clip_automation_state (
                    creator_id, creator_login, last_automatic_enqueue_at,
                    last_eligibility_stream_id, last_eligibility_range_end,
                    last_skip_reason, last_scheduler_check_at
                ) VALUES (%s, %s, NOW(), %s, %s, NULL, NOW())
                ON CONFLICT (creator_id) DO UPDATE SET
                    creator_login = EXCLUDED.creator_login,
                    last_automatic_enqueue_at = NOW(),
                    last_eligibility_stream_id = EXCLUDED.last_eligibility_stream_id,
                    last_eligibility_range_end = EXCLUDED.last_eligibility_range_end,
                    last_skip_reason = NULL,
                    last_scheduler_check_at = NOW(),
                    updated_at = NOW()
                """,
                (creator_id, creator_login, stream_id, range_end),
            )
        connection.commit()
    print(
        "GENERATION JOB QUEUED | "
        f"job_id={job['id']} | trigger_type=automatic"
    )
    return job, "new_material", {**global_usage, **creator_usage}


def record_generation_job_outbound_bytes(
    job_id: str | None,
    byte_count: int,
    destination: str = "r2",
) -> bool:
    if not job_id or byte_count <= 0:
        return False
    return _record_outbound_transfer(job_id, byte_count, destination)


def record_clip_outbound_bytes(
    clip_id: str | None,
    byte_count: int,
    destination: str = "tiktok",
) -> bool:
    """Attribute a later Render-originated publish upload to its generation job."""
    normalized_clip_id = str(clip_id or "").strip()
    if not normalized_clip_id or byte_count <= 0:
        return False
    database_url = _database_url()
    if not database_url:
        return False
    import psycopg

    job_id = None
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id FROM clip_generation_jobs
                WHERE result_clip_id = %s
                ORDER BY completed_at DESC NULLS LAST
                LIMIT 1
                """,
                (normalized_clip_id,),
            )
            row = cursor.fetchone()
            job_id = str(row[0]) if row else None
    return _record_outbound_transfer(job_id, byte_count, destination)


def _record_outbound_transfer(
    job_id: str | None,
    byte_count: int,
    destination: str,
) -> bool:
    normalized_job_id = str(job_id or "").strip()
    if not normalized_job_id or byte_count <= 0:
        return False
    database_url = _database_url()
    if not database_url:
        return False
    import psycopg

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE clip_generation_jobs
                SET actual_outbound_bytes = actual_outbound_bytes + %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (int(byte_count), normalized_job_id),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return False
            cursor.execute(
                """
                INSERT INTO clip_generation_outbound_transfers (
                    job_id, destination, bytes
                ) VALUES (%s, %s, %s)
                """,
                (
                    normalized_job_id,
                    str(destination or "other"),
                    int(byte_count),
                ),
            )
        connection.commit()
    return True


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
                    result_clip_id = NULL,
                    outcome = NULL,
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
    outcome: str | None = None,
) -> bool:
    normalized_clip_id = str(result_clip_id or "").strip() or None
    normalized_outcome = (
        str(outcome or "").strip()
        or ("clip_created" if normalized_clip_id else "no_clip_found")
    )
    updated = _update_owned_job(
        job_id,
        worker_id,
        """
        status = 'completed',
        result_clip_id = %s,
        outcome = %s,
        error_message = %s,
        lease_expires_at = NULL,
        completed_at = NOW(),
        updated_at = NOW()
        """,
        (normalized_clip_id, normalized_outcome, message),
    )
    if updated:
        if normalized_outcome == "clip_created" and normalized_clip_id:
            _record_automatic_success(job_id)
        print(
            "GENERATION JOB COMPLETED | "
            f"job_id={job_id} | result_clip_id={normalized_clip_id or 'none'} | "
            f"outcome={normalized_outcome}"
        )
    return updated


def _record_automatic_success(job_id: str) -> None:
    database_url = _database_url()
    if not database_url:
        return
    import psycopg

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO auto_clip_automation_state (
                    creator_id, creator_login,
                    last_successful_automatic_clip_at
                )
                SELECT requested_creator_id, requested_creator, NOW()
                FROM clip_generation_jobs
                WHERE id = %s AND trigger_type = 'automatic'
                  AND requested_creator_id IS NOT NULL
                ON CONFLICT (creator_id) DO UPDATE SET
                    creator_login = EXCLUDED.creator_login,
                    last_successful_automatic_clip_at = NOW(),
                    updated_at = NOW()
                """,
                (job_id,),
            )
        connection.commit()


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
    *,
    require_owner: bool = True,
) -> bool:
    database_url = _database_url()
    if not database_url:
        return False
    import psycopg

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            owner_clause = "AND claimed_by = %s" if require_owner else ""
            owner_parameters = (worker_id,) if require_owner else ()
            cursor.execute(
                f"""
                UPDATE clip_generation_jobs SET {set_sql}
                WHERE id = %s
                  {owner_clause}
                  AND status NOT IN ('completed', 'failed')
                RETURNING id
                """,
                (*parameters, job_id, *owner_parameters),
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
