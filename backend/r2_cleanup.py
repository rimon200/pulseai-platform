from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from storage_service import delete_video_object_with_result, get_video_object_size


PUBLISHED_STATUSES = {"published"}
ACTIVE_STATUSES = {
    "queued", "processing", "retryable", "scheduled", "publishing",
    "uploaded_to_inbox",
}
FAILED_STATUSES = {
    "failed", "publish_failed", "rejected", "rejected_low_score",
    "intentionally_skipped", "abandoned", "unusable",
}
ACTIVE_JOB_STATUSES = {"queued", "claimed", "processing", "deferred_memory"}


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {
        "1", "true", "yes", "on",
    }


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def cleanup_config() -> dict[str, Any]:
    return {
        "enabled": _env_bool("R2_CLEANUP_ENABLED", False),
        "dry_run": _env_bool("R2_CLEANUP_DRY_RUN", True),
        "unpublished_retention_days": _env_int(
            "R2_UNPUBLISHED_RETENTION_DAYS", 30, 1, 3650,
        ),
        "failed_retention_days": _env_int(
            "R2_FAILED_RETENTION_DAYS", 7, 1, 3650,
        ),
        "batch_size": _env_int("R2_CLEANUP_BATCH_SIZE", 50, 1, 500),
        "poll_hours": _env_int("R2_CLEANUP_POLL_HOURS", 24, 1, 720),
    }


def clip_cleanup_eligibility(
    clip: dict[str, Any],
    *,
    now: datetime | None = None,
    config: dict[str, Any] | None = None,
    active_job: bool = False,
) -> dict[str, Any]:
    settings = config or cleanup_config()
    current = now or datetime.now(timezone.utc)
    status = str(clip.get("status") or "").strip().lower()
    object_key = str(clip.get("object_key") or "").strip()
    if not object_key:
        return {"eligible": False, "reason": "missing_object_key", "age_days": 0}
    if clip.get("object_deleted_at"):
        return {"eligible": False, "reason": "already_deleted", "age_days": 0}
    deletion_pending_at = clip.get("deletion_pending_at")
    if isinstance(deletion_pending_at, datetime):
        if deletion_pending_at.tzinfo is None:
            deletion_pending_at = deletion_pending_at.replace(tzinfo=timezone.utc)
        if current - deletion_pending_at.astimezone(timezone.utc) < timedelta(hours=2):
            return {"eligible": False, "reason": "active_job", "age_days": 0}
    if status in PUBLISHED_STATUSES or clip.get("published_at"):
        return {"eligible": False, "reason": "published", "age_days": 0}
    if any(bool(clip.get(field)) for field in (
        "retention_locked", "is_favorited", "is_retained",
    )):
        return {"eligible": False, "reason": "locked", "age_days": 0}
    if active_job:
        return {"eligible": False, "reason": "active_job", "age_days": 0}
    if status in ACTIVE_STATUSES or clip.get("scheduled_for"):
        reason = "scheduled" if status == "scheduled" or clip.get("scheduled_for") else "active_job"
        return {"eligible": False, "reason": reason, "age_days": 0}
    if status == "failed" and int(clip.get("retry_count") or 0) < 2:
        return {"eligible": False, "reason": "active_job", "age_days": 0}

    timestamp = (
        clip.get("generated_at") or clip.get("created_at")
        or clip.get("last_processed_at") or clip.get("first_seen_at")
    )
    if not isinstance(timestamp, datetime):
        return {"eligible": False, "reason": "too_new", "age_days": 0}
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    age_days = max(0, int((current - timestamp.astimezone(timezone.utc)).total_seconds() // 86400))
    failed = status in FAILED_STATUSES
    retention_days = (
        settings["failed_retention_days"]
        if failed else settings["unpublished_retention_days"]
    )
    if age_days < retention_days:
        return {"eligible": False, "reason": "too_new", "age_days": age_days}
    return {
        "eligible": True,
        "reason": "failed_retention" if failed else "unpublished_retention",
        "age_days": age_days,
        "retention_days": retention_days,
    }


def _active_job_exists(cursor, generated_clip_id: str) -> bool:
    cursor.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM clip_generation_jobs
            WHERE result_clip_id = %s AND status = ANY(%s)
        ) AS active_job
        """,
        (generated_clip_id, list(ACTIVE_JOB_STATUSES)),
    )
    row = cursor.fetchone()
    return bool(row["active_job"] if isinstance(row, dict) else row[0])


def _load_cleanup_rows(cursor, batch_size: int) -> list[dict[str, Any]]:
    cursor.execute(
        """
        SELECT generated_clip_id, status, object_key, generated_at, created_at,
               first_seen_at, last_processed_at, published_at, scheduled_for,
               retry_count, retention_locked, is_favorited, is_retained,
               deletion_pending_at, deleted_at, object_deleted_at,
               object_size_bytes
        FROM twitch_clip_history
        WHERE provider = 'twitch'
          AND generated_clip_id IS NOT NULL
          AND object_key IS NOT NULL AND object_key <> ''
          AND object_deleted_at IS NULL
          AND published_at IS NULL
          AND status <> ALL(%s)
          AND retention_locked = FALSE
          AND is_favorited = FALSE
          AND is_retained = FALSE
          AND scheduled_for IS NULL
          AND (status <> 'failed' OR retry_count >= 2)
        ORDER BY COALESCE(generated_at, created_at, first_seen_at) ASC
        LIMIT %s
        """,
        (list(PUBLISHED_STATUSES | ACTIVE_STATUSES), batch_size),
    )
    return [dict(row) for row in cursor.fetchall()]


def cleanup_report(database_url: str, *, config: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = config or cleanup_config()
    if not database_url:
        raise RuntimeError("PostgreSQL is required for R2 cleanup.")
    import psycopg
    from psycopg.rows import dict_row

    summary = {
        "dry_run": bool(settings["dry_run"]), "candidates": 0,
        "deleted": 0, "skipped": 0, "failed": 0,
        "bytes_reclaimed": 0, "estimated_reclaimable_bytes": 0,
    }
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            rows = _load_cleanup_rows(cursor, int(settings["batch_size"]))
            for clip in rows:
                clip_id = str(clip["generated_clip_id"])
                active_job = _active_job_exists(cursor, clip_id)
                decision = clip_cleanup_eligibility(
                    clip, config=settings, active_job=active_job,
                )
                cursor.execute(
                    """
                    UPDATE twitch_clip_history SET last_cleanup_checked_at = NOW()
                    WHERE generated_clip_id = %s
                    """,
                    (clip_id,),
                )
                if not decision["eligible"]:
                    summary["skipped"] += 1
                    print(
                        "R2 CLEANUP SKIPPED | "
                        f"clip_id={clip_id} | reason={decision['reason']}"
                    )
                    continue
                summary["candidates"] += 1
                object_key = str(clip["object_key"])
                object_size = clip.get("object_size_bytes")
                if object_size is None:
                    object_size = get_video_object_size(object_key)
                    if object_size is not None:
                        cursor.execute(
                            """
                            UPDATE twitch_clip_history SET object_size_bytes = %s
                            WHERE generated_clip_id = %s AND object_key = %s
                            """,
                            (object_size, clip_id, object_key),
                        )
                object_size = int(object_size or 0)
                summary["estimated_reclaimable_bytes"] += object_size
                print(
                    "R2 CLEANUP CANDIDATE | "
                    f"clip_id={clip_id} | status={clip['status']} | "
                    f"age_days={decision['age_days']} | object_key={object_key} | "
                    f"reason={decision['reason']}"
                )
                if settings["dry_run"]:
                    continue

                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"r2-cleanup:{clip_id}",),
                )
                cursor.execute(
                    "SELECT * FROM twitch_clip_history WHERE generated_clip_id = %s FOR UPDATE",
                    (clip_id,),
                )
                current_clip = dict(cursor.fetchone())
                current_active_job = _active_job_exists(cursor, clip_id)
                current_decision = clip_cleanup_eligibility(
                    current_clip, config=settings, active_job=current_active_job,
                )
                if not current_decision["eligible"]:
                    summary["skipped"] += 1
                    print(
                        "R2 CLEANUP SKIPPED | "
                        f"clip_id={clip_id} | reason={current_decision['reason']}"
                    )
                    continue
                cursor.execute(
                    """
                    UPDATE twitch_clip_history
                    SET deletion_pending_at = NOW(), deletion_reason = %s,
                        deletion_error = NULL, last_cleanup_checked_at = NOW()
                    WHERE generated_clip_id = %s AND object_key = %s
                      AND object_deleted_at IS NULL
                    RETURNING generated_clip_id
                    """,
                    (current_decision["reason"], clip_id, object_key),
                )
                if not cursor.fetchone():
                    summary["skipped"] += 1
                    continue
                connection.commit()

                deletion = delete_video_object_with_result(object_key)
                if not deletion["deleted"]:
                    summary["failed"] += 1
                    cursor.execute(
                        """
                        UPDATE twitch_clip_history
                        SET deletion_pending_at = NULL, deletion_error = %s,
                            last_cleanup_checked_at = NOW()
                        WHERE generated_clip_id = %s AND object_deleted_at IS NULL
                        """,
                        (str(deletion.get("error") or "r2_delete_failed")[:1000], clip_id),
                    )
                    connection.commit()
                    continue
                deleted_bytes = int(deletion.get("bytes") or object_size)
                cursor.execute(
                    """
                    UPDATE twitch_clip_history
                    SET status = 'archived', archived_at = COALESCE(archived_at, NOW()),
                        deleted_at = NOW(), object_deleted_at = NOW(),
                        deletion_pending_at = NULL, deletion_error = NULL,
                        object_size_bytes = COALESCE(object_size_bytes, %s),
                        last_cleanup_checked_at = NOW()
                    WHERE generated_clip_id = %s AND object_key = %s
                      AND object_deleted_at IS NULL
                    """,
                    (deleted_bytes, clip_id, object_key),
                )
                connection.commit()
                summary["deleted"] += 1
                summary["bytes_reclaimed"] += deleted_bytes
                print(
                    "R2 OBJECT DELETED | "
                    f"clip_id={clip_id} | object_key={object_key} | "
                    f"bytes={deleted_bytes} | reason={current_decision['reason']}"
                )
    print(
        "R2 CLEANUP SUMMARY | "
        f"dry_run={str(summary['dry_run']).lower()} | "
        f"candidates={summary['candidates']} | deleted={summary['deleted']} | "
        f"skipped={summary['skipped']} | failed={summary['failed']} | "
        f"bytes_reclaimed={summary['bytes_reclaimed']}"
    )
    return summary


def cleanup_storage_snapshot(database_url: str) -> dict[str, Any]:
    if not database_url:
        return {
            "represented_objects": 0, "published_count": 0,
            "published_bytes": 0, "unpublished_count": 0,
            "unpublished_bytes": 0, "failed_count": 0,
            "failed_bytes": 0, "objects_deleted": 0,
            "bytes_reclaimed": 0, "last_cleanup_run": None,
            "size_unknown_count": 0, "eligible_objects": 0,
            "estimated_reclaimable_bytes": 0,
        }
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE object_deleted_at IS NULL) represented_objects,
                    COUNT(*) FILTER (WHERE status = 'published' AND object_deleted_at IS NULL) published_count,
                    COALESCE(SUM(object_size_bytes) FILTER (WHERE status = 'published' AND object_deleted_at IS NULL), 0) published_bytes,
                    COUNT(*) FILTER (WHERE status <> 'published' AND status <> ALL(%s) AND object_deleted_at IS NULL) unpublished_count,
                    COALESCE(SUM(object_size_bytes) FILTER (WHERE status <> 'published' AND status <> ALL(%s) AND object_deleted_at IS NULL), 0) unpublished_bytes,
                    COUNT(*) FILTER (WHERE status = ANY(%s) AND object_deleted_at IS NULL) failed_count,
                    COALESCE(SUM(object_size_bytes) FILTER (WHERE status = ANY(%s) AND object_deleted_at IS NULL), 0) failed_bytes,
                    COUNT(*) FILTER (WHERE object_size_bytes IS NULL AND object_deleted_at IS NULL) size_unknown_count,
                    COUNT(*) FILTER (WHERE object_deleted_at IS NOT NULL) objects_deleted,
                    COALESCE(SUM(object_size_bytes) FILTER (WHERE object_deleted_at IS NOT NULL), 0) bytes_reclaimed,
                    MAX(last_cleanup_checked_at) last_cleanup_run
                FROM twitch_clip_history WHERE provider = 'twitch'
                  AND object_key IS NOT NULL AND object_key <> ''
                """,
                (list(FAILED_STATUSES), list(FAILED_STATUSES), list(FAILED_STATUSES), list(FAILED_STATUSES)),
            )
            snapshot = dict(cursor.fetchone())
            cursor.execute(
                """
                SELECT generated_clip_id, status, object_key, generated_at,
                       created_at, first_seen_at, last_processed_at,
                       published_at, scheduled_for, retry_count,
                       retention_locked, is_favorited, is_retained,
                       object_deleted_at, object_size_bytes
                FROM twitch_clip_history
                WHERE provider = 'twitch' AND object_key IS NOT NULL
                  AND object_key <> '' AND object_deleted_at IS NULL
                """
            )
            rows = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                """
                SELECT DISTINCT result_clip_id FROM clip_generation_jobs
                WHERE result_clip_id IS NOT NULL AND status = ANY(%s)
                """,
                (list(ACTIVE_JOB_STATUSES),),
            )
            active_ids = {str(row["result_clip_id"]) for row in cursor.fetchall()}
    settings = cleanup_config()
    eligible = [
        clip for clip in rows
        if clip_cleanup_eligibility(
            clip,
            config=settings,
            active_job=str(clip["generated_clip_id"]) in active_ids,
        )["eligible"]
    ]
    snapshot["eligible_objects"] = len(eligible)
    snapshot["estimated_reclaimable_bytes"] = sum(
        int(clip.get("object_size_bytes") or 0) for clip in eligible
    )
    return snapshot


async def cleanup_loop(database_url: str, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        settings = cleanup_config()
        if settings["enabled"]:
            try:
                await asyncio.to_thread(cleanup_report, database_url, config=settings)
            except Exception as error:
                print(f"R2 CLEANUP LOOP FAILED | error_type={error.__class__.__name__}")
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=float(settings["poll_hours"]) * 3600,
            )
        except asyncio.TimeoutError:
            pass
