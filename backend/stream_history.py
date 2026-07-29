from __future__ import annotations

import json
import os
import functools
import inspect
from datetime import datetime
from typing import Any


STREAM_HISTORY_ADVISORY_LOCK_ID = 22616960936427853
STREAM_STATES = {"pending", "partial", "exhausted", "succeeded"}


def _log_stream_write_failures(
    operation: str,
    table: str,
    columns: tuple[str, ...],
):
    def decorate(function):
        signature = inspect.signature(function)

        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except Exception as error:
                bound = signature.bind_partial(*args, **kwargs)
                parameter_types = ",".join(
                    f"{name}:{'none' if value is None else value.__class__.__name__}"
                    for name, value in sorted(bound.arguments.items())
                )
                print(
                    "STREAM HISTORY DB ERROR | "
                    f"operation={operation} | table={table} | "
                    f"columns={','.join(columns)} | "
                    f"parameter_types={parameter_types or 'none'} | "
                    f"error_type={error.__class__.__name__}"
                )
                raise

        return wrapped

    return decorate


def _database_url() -> str:
    return os.getenv("DATABASE_URL", "").strip()


def ensure_stream_history_tables() -> bool:
    database_url = _database_url()
    if not database_url:
        return False
    try:
        import psycopg

        with psycopg.connect(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    (STREAM_HISTORY_ADVISORY_LOCK_ID,),
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS auto_clip_stream_state (
                        provider TEXT NOT NULL DEFAULT 'twitch',
                        creator_id TEXT NOT NULL,
                        stream_id TEXT NOT NULL,
                        stream_started_at TIMESTAMPTZ NOT NULL,
                        stream_ended_at TIMESTAMPTZ,
                        is_refreshable BOOLEAN NOT NULL DEFAULT FALSE,
                        processing_state TEXT NOT NULL DEFAULT 'pending'
                            CHECK (
                                processing_state IN (
                                    'pending', 'partial', 'exhausted',
                                    'succeeded'
                                )
                            ),
                        last_evaluated_at TIMESTAMPTZ,
                        last_evaluated_range_end TIMESTAMPTZ,
                        evaluated_ranges JSONB NOT NULL DEFAULT '[]'::jsonb,
                        last_discovered_candidate_cursor TEXT,
                        retryable_failure_state TEXT,
                        exhausted_at TIMESTAMPTZ,
                        last_checked_at TIMESTAMPTZ,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        PRIMARY KEY (provider, creator_id, stream_id)
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS auto_clip_historical_cursor (
                        provider TEXT NOT NULL DEFAULT 'twitch',
                        creator_id TEXT NOT NULL,
                        next_before_timestamp TIMESTAMPTZ,
                        last_stream_id TEXT,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        PRIMARY KEY (provider, creator_id)
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS auto_clip_stream_lookup_idx
                    ON auto_clip_stream_state (
                        provider, creator_id, stream_started_at DESC
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS auto_clip_stream_state_idx
                    ON auto_clip_stream_state (
                        provider, creator_id, processing_state,
                        is_refreshable
                    )
                    """
                )
            connection.commit()
        return True
    except Exception as error:
        print(f"AUTO CLIP STREAM HISTORY MIGRATION FAILED | error={error!r}")
        return False


def _stream_values(stream: dict[str, Any]) -> tuple[str, datetime, datetime | None]:
    stream_id = str(stream.get("stream_id") or stream.get("id") or "").strip()
    started_at = stream.get("started_at") or stream.get("created_at")
    ended_at = stream.get("ended_at")
    if (
        not stream_id
        or not isinstance(started_at, datetime)
        or started_at.tzinfo is None
    ):
        raise ValueError("stream ID and timezone-aware start are required")
    normalized_end = (
        ended_at
        if isinstance(ended_at, datetime) and ended_at.tzinfo is not None
        else None
    )
    return stream_id, started_at, normalized_end


@_log_stream_write_failures(
    "register_newest_stream",
    "auto_clip_stream_state,auto_clip_historical_cursor",
    (
        "stream_id", "stream_started_at", "stream_ended_at",
        "is_refreshable", "processing_state", "next_before_timestamp",
    ),
)
def register_newest_stream(
    creator_id: str,
    stream: dict[str, Any],
) -> dict[str, Any]:
    database_url = _database_url()
    if not database_url:
        raise RuntimeError("PostgreSQL is required for stream history.")
    stream_id, started_at, ended_at = _stream_values(stream)
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (STREAM_HISTORY_ADVISORY_LOCK_ID,),
            )
            cursor.execute(
                """
                SELECT stream_id, stream_started_at
                FROM auto_clip_stream_state
                WHERE provider = 'twitch' AND creator_id = %s
                  AND is_refreshable = TRUE
                ORDER BY stream_started_at DESC
                LIMIT 1
                FOR UPDATE
                """,
                (creator_id,),
            )
            previous = cursor.fetchone()
            cursor.execute(
                """
                UPDATE auto_clip_stream_state
                SET is_refreshable = FALSE, updated_at = NOW()
                WHERE provider = 'twitch' AND creator_id = %s
                  AND stream_id <> %s AND is_refreshable = TRUE
                """,
                (creator_id, stream_id),
            )
            cursor.execute(
                """
                INSERT INTO auto_clip_stream_state (
                    provider, creator_id, stream_id, stream_started_at,
                    stream_ended_at, is_refreshable, processing_state
                ) VALUES (
                    'twitch', %s, %s, %s, %s, TRUE, 'pending'
                )
                ON CONFLICT (provider, creator_id, stream_id) DO UPDATE SET
                    stream_started_at = EXCLUDED.stream_started_at,
                    stream_ended_at = COALESCE(
                        EXCLUDED.stream_ended_at,
                        auto_clip_stream_state.stream_ended_at
                    ),
                    is_refreshable = TRUE,
                    processing_state = CASE
                        WHEN auto_clip_stream_state.processing_state = 'exhausted'
                        THEN 'partial'
                        ELSE auto_clip_stream_state.processing_state
                    END,
                    exhausted_at = NULL,
                    updated_at = NOW()
                RETURNING *
                """,
                (creator_id, stream_id, started_at, ended_at),
            )
            saved = dict(cursor.fetchone())
            if previous and str(previous["stream_id"]) != stream_id:
                cursor.execute(
                    """
                    INSERT INTO auto_clip_historical_cursor (
                        provider, creator_id, next_before_timestamp,
                        last_stream_id
                    ) VALUES ('twitch', %s, %s, NULL)
                    ON CONFLICT (provider, creator_id) DO UPDATE SET
                        next_before_timestamp = EXCLUDED.next_before_timestamp,
                        last_stream_id = NULL,
                        updated_at = NOW()
                    """,
                    (creator_id, started_at),
                )
        connection.commit()
    return saved


def get_stream_state(creator_id: str, stream_id: str) -> dict[str, Any] | None:
    database_url = _database_url()
    if not database_url:
        return None
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM auto_clip_stream_state
                WHERE provider = 'twitch' AND creator_id = %s
                  AND stream_id = %s
                """,
                (creator_id, stream_id),
            )
            row = cursor.fetchone()
            return dict(row) if row else None


@_log_stream_write_failures(
    "register_historical_stream",
    "auto_clip_stream_state",
    (
        "stream_id", "stream_started_at", "stream_ended_at",
        "is_refreshable", "processing_state",
    ),
)
def register_historical_stream(
    creator_id: str,
    stream: dict[str, Any],
) -> dict[str, Any]:
    database_url = _database_url()
    if not database_url:
        raise RuntimeError("PostgreSQL is required for stream history.")
    stream_id, started_at, ended_at = _stream_values(stream)
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (STREAM_HISTORY_ADVISORY_LOCK_ID,),
            )
            cursor.execute(
                """
                INSERT INTO auto_clip_stream_state (
                    provider, creator_id, stream_id, stream_started_at,
                    stream_ended_at, is_refreshable, processing_state
                ) VALUES (
                    'twitch', %s, %s, %s, %s, FALSE, 'pending'
                )
                ON CONFLICT (provider, creator_id, stream_id) DO UPDATE SET
                    stream_started_at = EXCLUDED.stream_started_at,
                    stream_ended_at = COALESCE(
                        EXCLUDED.stream_ended_at,
                        auto_clip_stream_state.stream_ended_at
                    ),
                    updated_at = NOW()
                RETURNING *
                """,
                (creator_id, stream_id, started_at, ended_at),
            )
            saved = dict(cursor.fetchone())
        connection.commit()
    return saved


def get_exhausted_stream_ids(creator_id: str) -> set[str]:
    database_url = _database_url()
    if not database_url:
        return set()
    import psycopg

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT stream_id FROM auto_clip_stream_state
                WHERE provider = 'twitch' AND creator_id = %s
                  AND processing_state = 'exhausted'
                  AND is_refreshable = FALSE
                """,
                (creator_id,),
            )
            return {str(row[0]) for row in cursor.fetchall()}


def get_historical_cursor(creator_id: str) -> dict[str, Any]:
    database_url = _database_url()
    if not database_url:
        return {}
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM auto_clip_historical_cursor
                WHERE provider = 'twitch' AND creator_id = %s
                """,
                (creator_id,),
            )
            row = cursor.fetchone()
            return dict(row) if row else {}


@_log_stream_write_failures(
    "update_stream_progress",
    "auto_clip_stream_state",
    (
        "processing_state", "last_evaluated_at",
        "last_evaluated_range_end", "evaluated_ranges",
        "last_discovered_candidate_cursor", "retryable_failure_state",
        "exhausted_at", "last_checked_at",
    ),
)
def update_stream_progress(
    creator_id: str,
    stream_id: str,
    *,
    processing_state: str,
    range_start: datetime | None = None,
    range_end: datetime | None = None,
    candidate_cursor: str | None = None,
    retryable_failure_state: str | None = None,
    checked_complete: bool = False,
) -> bool:
    if processing_state not in STREAM_STATES:
        raise ValueError(f"Unsupported stream state: {processing_state}")
    database_url = _database_url()
    if not database_url:
        return False
    import psycopg

    evaluated_range = (
        json.dumps(
            {
                "start": range_start.isoformat() if range_start else None,
                "end": range_end.isoformat() if range_end else None,
                "evaluated_at": datetime.now().astimezone().isoformat(),
            }
        )
        if range_start or range_end
        else None
    )
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (STREAM_HISTORY_ADVISORY_LOCK_ID,),
            )
            cursor.execute(
                """
                UPDATE auto_clip_stream_state SET
                    processing_state = CASE
                        WHEN processing_state = 'exhausted'
                             AND is_refreshable = FALSE
                        THEN 'exhausted'
                        ELSE %s
                    END,
                    last_evaluated_at = CASE
                        WHEN %s::timestamptz IS NOT NULL THEN NOW()
                        ELSE last_evaluated_at
                    END,
                    last_evaluated_range_end = COALESCE(
                        %s::timestamptz, last_evaluated_range_end
                    ),
                    evaluated_ranges = CASE
                        WHEN %s::jsonb IS NULL THEN evaluated_ranges
                        ELSE evaluated_ranges || jsonb_build_array(%s::jsonb)
                    END,
                    last_discovered_candidate_cursor = COALESCE(
                        %s, last_discovered_candidate_cursor
                    ),
                    retryable_failure_state = CASE
                        WHEN processing_state = 'exhausted'
                             AND is_refreshable = FALSE
                        THEN retryable_failure_state
                        ELSE %s
                    END,
                    exhausted_at = CASE
                        WHEN %s = 'exhausted' THEN NOW()
                        WHEN processing_state = 'exhausted'
                             AND is_refreshable = FALSE
                        THEN exhausted_at
                        ELSE NULL
                    END,
                    last_checked_at = CASE
                        WHEN %s THEN COALESCE(
                            %s::timestamptz,
                            last_checked_at
                        )
                        ELSE last_checked_at
                    END,
                    updated_at = NOW()
                WHERE provider = 'twitch' AND creator_id = %s
                  AND stream_id = %s
                RETURNING stream_id
                """,
                (
                    processing_state,
                    range_end,
                    range_end,
                    evaluated_range,
                    evaluated_range,
                    candidate_cursor,
                    retryable_failure_state,
                    processing_state,
                    checked_complete,
                    range_end,
                    creator_id,
                    stream_id,
                ),
            )
            updated = cursor.fetchone() is not None
        connection.commit()
    return updated


@_log_stream_write_failures(
    "save_historical_cursor",
    "auto_clip_historical_cursor",
    ("next_before_timestamp", "last_stream_id"),
)
def save_historical_cursor(
    creator_id: str,
    *,
    next_before_timestamp: datetime,
    last_stream_id: str,
) -> None:
    database_url = _database_url()
    if not database_url:
        raise RuntimeError("PostgreSQL is required for historical cursors.")
    import psycopg

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (STREAM_HISTORY_ADVISORY_LOCK_ID,),
            )
            cursor.execute(
                """
                INSERT INTO auto_clip_historical_cursor (
                    provider, creator_id, next_before_timestamp,
                    last_stream_id
                ) VALUES ('twitch', %s, %s, %s)
                ON CONFLICT (provider, creator_id) DO UPDATE SET
                    next_before_timestamp = LEAST(
                        auto_clip_historical_cursor.next_before_timestamp,
                        EXCLUDED.next_before_timestamp
                    ),
                    last_stream_id = CASE
                        WHEN EXCLUDED.next_before_timestamp <=
                             auto_clip_historical_cursor.next_before_timestamp
                        THEN EXCLUDED.last_stream_id
                        ELSE auto_clip_historical_cursor.last_stream_id
                    END,
                    updated_at = NOW()
                """,
                (creator_id, next_before_timestamp, last_stream_id),
            )
        connection.commit()


@_log_stream_write_failures(
    "exhaust_stream_and_advance_cursor",
    "auto_clip_stream_state,auto_clip_historical_cursor",
    (
        "processing_state", "exhausted_at", "last_checked_at",
        "next_before_timestamp", "last_stream_id",
    ),
)
def exhaust_stream_and_advance_cursor(
    creator_id: str,
    stream_id: str,
    *,
    range_start: datetime,
    range_end: datetime,
) -> None:
    """Atomically exhaust one older stream and advance its creator cursor."""
    database_url = _database_url()
    if not database_url:
        raise RuntimeError("PostgreSQL is required for stream exhaustion.")
    import psycopg

    evaluated_range = json.dumps(
        {
            "start": range_start.isoformat(),
            "end": range_end.isoformat(),
            "evaluated_at": datetime.now().astimezone().isoformat(),
        }
    )
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (STREAM_HISTORY_ADVISORY_LOCK_ID,),
            )
            cursor.execute(
                """
                UPDATE auto_clip_stream_state SET
                    processing_state = 'exhausted',
                    last_evaluated_at = NOW(),
                    last_evaluated_range_end = %s,
                    evaluated_ranges = evaluated_ranges
                        || jsonb_build_array(%s::jsonb),
                    retryable_failure_state = NULL,
                    exhausted_at = NOW(),
                    last_checked_at = %s,
                    updated_at = NOW()
                WHERE provider = 'twitch' AND creator_id = %s
                  AND stream_id = %s AND is_refreshable = FALSE
                RETURNING stream_id
                """,
                (
                    range_end,
                    evaluated_range,
                    range_end,
                    creator_id,
                    stream_id,
                ),
            )
            if cursor.fetchone() is None:
                raise RuntimeError(
                    "historical stream was unavailable for exhaustion"
                )
            cursor.execute(
                """
                INSERT INTO auto_clip_historical_cursor (
                    provider, creator_id, next_before_timestamp,
                    last_stream_id
                ) VALUES ('twitch', %s, %s, %s)
                ON CONFLICT (provider, creator_id) DO UPDATE SET
                    next_before_timestamp = LEAST(
                        auto_clip_historical_cursor.next_before_timestamp,
                        EXCLUDED.next_before_timestamp
                    ),
                    last_stream_id = CASE
                        WHEN EXCLUDED.next_before_timestamp <=
                             auto_clip_historical_cursor.next_before_timestamp
                        THEN EXCLUDED.last_stream_id
                        ELSE auto_clip_historical_cursor.last_stream_id
                    END,
                    updated_at = NOW()
                """,
                (creator_id, range_start, stream_id),
            )
        connection.commit()
