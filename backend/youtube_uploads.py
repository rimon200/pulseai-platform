from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import re
import socket
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


SUPPORTED_SOURCE_TYPES = {
    "manual_upload",
    "creator_file_url",
    "creator_storage_prefix",
    "approved_ingestion_endpoint",
}
ACTIVE_UPLOAD_STATES = {"claimed", "downloading", "analyzing", "generating"}
TERMINAL_UPLOAD_STATES = {"completed", "skipped", "failed"}
YOUTUBE_UPLOAD_ADVISORY_LOCK_ID = 22616960936427856


class YouTubeSourceError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def youtube_config() -> dict[str, Any]:
    def bounded(name: str, default: int, minimum: int, maximum: int) -> int:
        try:
            return max(minimum, min(int(os.getenv(name, str(default))), maximum))
        except ValueError:
            return default

    return {
        "enabled": os.getenv("YOUTUBE_INTEGRATION_ENABLED", "false").strip().lower()
        in {"1", "true", "yes", "on"},
        "poll_interval_minutes": bounded("YOUTUBE_POLL_INTERVAL_MINUTES", 15, 1, 1440),
        "min_video_duration_minutes": bounded("YOUTUBE_MIN_VIDEO_DURATION_MINUTES", 12, 1, 1440),
        "max_source_duration_hours": bounded("YOUTUBE_MAX_SOURCE_DURATION_HOURS", 4, 1, 24),
        "clips_per_video": bounded("YOUTUBE_CLIPS_PER_VIDEO", 3, 1, 5),
        "max_clips_per_video": bounded("YOUTUBE_MAX_CLIPS_PER_VIDEO", 5, 1, 5),
        "clip_min_seconds": bounded("YOUTUBE_CLIP_MIN_SECONDS", 45, 15, 300),
        "clip_target_seconds": bounded("YOUTUBE_CLIP_TARGET_SECONDS", 90, 30, 300),
        "clip_max_seconds": bounded("YOUTUBE_CLIP_MAX_SECONDS", 300, 45, 300),
        "automatic_generation_enabled": os.getenv(
            "YOUTUBE_AUTOMATIC_GENERATION_ENABLED", "false"
        ).strip().lower() in {"1", "true", "yes", "on"},
        "max_videos_per_creator_per_day": bounded(
            "YOUTUBE_MAX_VIDEOS_PER_CREATOR_PER_DAY", 1, 0, 20
        ),
        "max_clips_per_creator_per_day": bounded(
            "YOUTUBE_MAX_CLIPS_PER_CREATOR_PER_DAY", 5, 0, 50
        ),
        "global_max_clips_per_day": bounded(
            "YOUTUBE_GLOBAL_MAX_CLIPS_PER_DAY", 10, 0, 100
        ),
    }


def _encryption_key() -> bytes:
    configured = os.getenv("YOUTUBE_SOURCE_ENCRYPTION_KEY", "").strip()
    if not configured:
        raise YouTubeSourceError(
            "encryption_unavailable",
            "YouTube source encryption is not configured.",
        )
    return hashlib.sha256(f"pulseai:youtube-source:{configured}".encode()).digest()


def encrypt_source_config(value: dict[str, Any]) -> str:
    from Cryptodome.Cipher import AES

    cipher = AES.new(_encryption_key(), AES.MODE_GCM)
    plaintext = json.dumps(value, separators=(",", ":")).encode("utf-8")
    ciphertext, tag = cipher.encrypt_and_digest(plaintext)
    return base64.urlsafe_b64encode(cipher.nonce + tag + ciphertext).decode("ascii")


def decrypt_source_config(value: object) -> dict[str, Any]:
    from Cryptodome.Cipher import AES

    payload = base64.urlsafe_b64decode(str(value or "").encode("ascii"))
    if len(payload) < 33:
        raise YouTubeSourceError("source_unavailable", "Source configuration is invalid.")
    nonce, tag, ciphertext = payload[:16], payload[16:32], payload[32:]
    cipher = AES.new(_encryption_key(), AES.MODE_GCM, nonce=nonce)
    result = json.loads(cipher.decrypt_and_verify(ciphertext, tag).decode("utf-8"))
    if not isinstance(result, dict):
        raise YouTubeSourceError("source_unavailable", "Source configuration is invalid.")
    return result


def _configured_allowed_hosts(config: dict[str, Any]) -> set[str]:
    global_hosts = {
        host.strip().lower().rstrip(".")
        for host in os.getenv("YOUTUBE_APPROVED_MEDIA_HOSTS", "").split(",")
        if host.strip()
    }
    creator_hosts = {
        str(host).strip().lower().rstrip(".")
        for host in config.get("allowed_hosts", [])
        if str(host).strip()
    }
    return global_hosts | creator_hosts


def validate_authorized_media_url(
    value: object,
    *,
    allowed_hosts: set[str],
    resolved_addresses: list[str] | None = None,
) -> str:
    raw = str(value or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise YouTubeSourceError("source_host_rejected", "Source URL must use HTTPS.")
    if parsed.username or parsed.password or parsed.fragment:
        raise YouTubeSourceError("source_host_rejected", "Source URL is not permitted.")
    hostname = parsed.hostname.lower().rstrip(".")
    if hostname in {"localhost", "localhost.localdomain"}:
        raise YouTubeSourceError("source_host_rejected", "Local source hosts are not permitted.")
    if hostname.endswith((".youtube.com", ".youtu.be")) or hostname in {
        "youtube.com", "youtu.be", "www.youtube.com",
    }:
        raise YouTubeSourceError(
            "source_host_rejected", "YouTube playback URLs are not approved media sources."
        )
    if hostname not in {host.lower().rstrip(".") for host in allowed_hosts}:
        raise YouTubeSourceError("source_host_rejected", "Source host is not allowlisted.")
    addresses = resolved_addresses
    if addresses is None:
        try:
            addresses = list({item[4][0] for item in socket.getaddrinfo(hostname, 443)})
        except socket.gaierror as error:
            raise YouTubeSourceError("source_unavailable", "Source host did not resolve.") from error
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as error:
            raise YouTubeSourceError("source_host_rejected", "Source host resolved unsafely.") from error
        if not ip.is_global:
            raise YouTubeSourceError(
                "source_host_rejected", "Private-network source hosts are not permitted."
            )
    safe_path = unquote(parsed.path or "")
    if "\x00" in safe_path:
        raise YouTubeSourceError("source_host_rejected", "Source URL is invalid.")
    return raw


def validate_source_configuration(
    source_type: object, config: dict[str, Any], *, resolve_dns: bool = True,
) -> dict[str, Any]:
    normalized_type = str(source_type or "").strip().lower()
    if normalized_type not in SUPPORTED_SOURCE_TYPES:
        raise YouTubeSourceError("source_type_rejected", "Unsupported source-media strategy.")
    clean = {**config}
    allowed_hosts = _configured_allowed_hosts(clean)
    if normalized_type == "manual_upload":
        upload_path = str(clean.get("path") or "").strip()
        if upload_path:
            resolved = Path(upload_path).resolve(strict=False)
            upload_root = Path(
                os.getenv(
                    "YOUTUBE_MANUAL_UPLOAD_ROOT",
                    str(Path(__file__).resolve().parent / "youtube_uploads"),
                )
            ).resolve()
            if resolved.parent != upload_root or resolved.suffix.lower() not in {
                ".mp4", ".mov", ".mkv", ".webm",
            }:
                raise YouTubeSourceError(
                    "source_host_rejected", "Manual source path is outside the upload root."
                )
            clean["path"] = str(resolved)
    else:
        key = "url" if normalized_type in {
            "creator_file_url", "approved_ingestion_endpoint"
        } else "prefix"
        addresses = None if resolve_dns else ["8.8.8.8"]
        clean[key] = validate_authorized_media_url(
            clean.get(key), allowed_hosts=allowed_hosts, resolved_addresses=addresses,
        )
    clean["allowed_hosts"] = sorted(allowed_hosts)
    return clean


def ensure_youtube_tables(database_url: str) -> bool:
    if not database_url:
        return False
    try:
        import psycopg

        with psycopg.connect(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(%s)", (YOUTUBE_UPLOAD_ADVISORY_LOCK_ID,))
                creator_columns = {
                    "uploads_playlist_id": "TEXT",
                    "authorized_media_source_type": "TEXT",
                    "authorized_media_source_config_encrypted": "TEXT",
                    "youtube_last_checked_at": "TIMESTAMPTZ",
                    "youtube_last_video_id": "TEXT",
                    "youtube_last_successful_request_at": "TIMESTAMPTZ",
                    "youtube_last_polling_error": "TEXT",
                }
                from psycopg import sql
                for name, definition in creator_columns.items():
                    cursor.execute(
                        sql.SQL("ALTER TABLE monitored_creators ADD COLUMN IF NOT EXISTS {} ")
                        .format(sql.Identifier(name)) + sql.SQL(definition)
                    )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS youtube_uploads (
                        id UUID PRIMARY KEY,
                        creator_id BIGINT NOT NULL REFERENCES monitored_creators(id),
                        provider TEXT NOT NULL DEFAULT 'youtube',
                        platform_video_id TEXT NOT NULL,
                        title TEXT NOT NULL,
                        description TEXT,
                        published_at TIMESTAMPTZ,
                        duration_seconds INTEGER,
                        thumbnail_url TEXT,
                        source_status TEXT NOT NULL DEFAULT 'not_configured',
                        source_reference_encrypted TEXT,
                        processing_status TEXT NOT NULL DEFAULT 'detected',
                        processing_error TEXT,
                        detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        claimed_at TIMESTAMPTZ,
                        claim_expires_at TIMESTAMPTZ,
                        claimed_by TEXT,
                        completed_at TIMESTAMPTZ,
                        clips_requested INTEGER NOT NULL DEFAULT 0,
                        clips_created INTEGER NOT NULL DEFAULT 0,
                        selection_diagnostics JSONB NOT NULL DEFAULT '[]'::jsonb,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE (provider, platform_video_id),
                        CHECK (provider = 'youtube')
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS youtube_uploads_claim_idx
                    ON youtube_uploads (processing_status, claim_expires_at, published_at)
                    """
                )
            connection.commit()
        return True
    except Exception as error:
        print(f"YOUTUBE UPLOAD MIGRATION FAILED | error={error!r}")
        return False


def classify_youtube_upload(upload: dict[str, Any]) -> tuple[str, str]:
    config = youtube_config()
    minimum = config["min_video_duration_minutes"] * 60
    maximum = config["max_source_duration_hours"] * 3600
    duration = upload.get("duration_seconds")
    privacy = str(upload.get("privacy_status") or "")
    live = str(upload.get("live_broadcast_content") or "none")
    upload_status = str(upload.get("upload_status") or "processed")
    if privacy != "public" or upload_status not in {"processed", "uploaded"}:
        return "private_or_deleted", "skipped"
    if live in {"live", "upcoming"}:
        return "livestream_placeholder", "skipped"
    if duration is None:
        return "duration_unavailable", "skipped"
    if int(duration) < minimum:
        return "video_too_short", "skipped"
    if int(duration) > maximum:
        return "source_too_long", "skipped"
    return "eligible", "detected"


def store_detected_uploads(
    database_url: str, creator: dict[str, Any], uploads: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    import psycopg
    from psycopg.rows import dict_row

    stored = []
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            for upload in uploads:
                video_id = str(upload.get("video_id") or "").strip()
                duration = upload.get("duration_seconds")
                if not video_id:
                    continue
                reason, processing_status = classify_youtube_upload(upload)
                source_configured = bool(creator.get("authorized_media_source_type"))
                source_status = "ready" if source_configured else "not_configured"
                cursor.execute(
                    """
                    INSERT INTO youtube_uploads (
                        id, creator_id, platform_video_id, title, description,
                        published_at, duration_seconds, thumbnail_url,
                        source_status, processing_status, processing_error
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (provider, platform_video_id) DO NOTHING
                    RETURNING *
                    """,
                    (
                        uuid.uuid4(), creator["id"], video_id,
                        upload.get("title") or "Untitled YouTube upload",
                        upload.get("description") or "", upload.get("published_at"),
                        duration, upload.get("thumbnail_url") or "", source_status,
                        processing_status, None if reason == "eligible" else reason,
                    ),
                )
                row = cursor.fetchone()
                if row:
                    saved = dict(row)
                    stored.append(saved)
                    print(
                        "YOUTUBE UPLOAD DETECTED | "
                        f"creator_id={creator['id']} | video_id={video_id} | "
                        f"duration_seconds={duration or 'unknown'} | "
                        f"published_at={upload.get('published_at') or 'unknown'}"
                    )
                    print(
                        "YOUTUBE SOURCE STATUS | "
                        f"creator_id={creator['id']} | video_id={video_id} | "
                        f"status={source_status}"
                    )
            newest = str((uploads or [{}])[0].get("video_id") or "")
            cursor.execute(
                """
                UPDATE monitored_creators SET youtube_last_checked_at = NOW(),
                    youtube_last_video_id = COALESCE(NULLIF(%s, ''), youtube_last_video_id),
                    youtube_last_successful_request_at = NOW(),
                    youtube_last_polling_error = NULL, updated_at = NOW()
                WHERE id = %s AND provider = 'youtube'
                """,
                (newest, creator["id"]),
            )
        connection.commit()
    return stored


def list_youtube_uploads(
    database_url: str, *, limit: int = 25, creator_id: int | None = None,
) -> list[dict[str, Any]]:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT upload.*, creator.platform_display_name AS channel_name,
                       creator.platform_channel_slug AS channel_slug,
                       creator.authorized_media_source_type
                FROM youtube_uploads AS upload
                JOIN monitored_creators AS creator ON creator.id = upload.creator_id
                WHERE (%s IS NULL OR upload.creator_id = %s)
                ORDER BY upload.published_at DESC NULLS LAST, upload.id DESC
                LIMIT %s
                """,
                (creator_id, creator_id, max(1, min(limit, 100))),
            )
            return [dict(row) for row in cursor.fetchall()]


def get_youtube_upload(database_url: str, upload_id: str) -> dict[str, Any] | None:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT upload.*, creator.platform_user_id,
                       creator.platform_channel_slug,
                       creator.platform_display_name,
                       creator.authorized_media_source_type,
                       creator.authorized_media_source_config_encrypted
                FROM youtube_uploads AS upload
                JOIN monitored_creators AS creator ON creator.id = upload.creator_id
                WHERE upload.id = %s
                """,
                (upload_id,),
            )
            row = cursor.fetchone()
    return dict(row) if row else None


def configure_creator_source(
    database_url: str,
    creator_id: int,
    source_type: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    import psycopg
    from psycopg.rows import dict_row

    validated = validate_source_configuration(source_type, config)
    encrypted = encrypt_source_config(validated)
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE monitored_creators SET
                    authorized_media_source_type = %s,
                    authorized_media_source_config_encrypted = %s,
                    platform_connection_status = 'connected',
                    platform_connection_error = NULL, updated_at = NOW()
                WHERE id = %s AND provider = 'youtube'
                RETURNING id, provider, platform_channel_slug,
                          authorized_media_source_type
                """,
                (source_type, encrypted, creator_id),
            )
            row = cursor.fetchone()
            if row:
                cursor.execute(
                    """
                    UPDATE youtube_uploads SET source_status = 'ready', updated_at = NOW()
                    WHERE creator_id = %s AND source_status = 'not_configured'
                    """,
                    (creator_id,),
                )
        connection.commit()
    if not row:
        raise YouTubeSourceError("not_found", "YouTube creator was not found.")
    return dict(row)


def set_upload_processing_result(
    database_url: str,
    upload_id: str,
    *,
    status: str,
    error: str | None = None,
    clips_requested: int | None = None,
    clips_created: int | None = None,
    diagnostics: list[dict[str, Any]] | None = None,
) -> bool:
    if status not in {
        "detected", "claimed", "downloading", "analyzing", "generating",
        "completed", "skipped", "failed",
    }:
        raise ValueError("Invalid YouTube upload processing status.")
    import psycopg

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE youtube_uploads SET processing_status = %s,
                    processing_error = %s,
                    clips_requested = COALESCE(%s, clips_requested),
                    clips_created = COALESCE(%s, clips_created),
                    selection_diagnostics = COALESCE(%s::jsonb, selection_diagnostics),
                    completed_at = CASE WHEN %s IN ('completed','skipped') THEN NOW()
                                        ELSE completed_at END,
                    claimed_by = CASE WHEN %s IN ('completed','skipped','failed')
                                      THEN NULL ELSE claimed_by END,
                    claim_expires_at = CASE WHEN %s IN ('completed','skipped','failed')
                                            THEN NULL ELSE claim_expires_at END,
                    updated_at = NOW()
                WHERE id = %s RETURNING id
                """,
                (
                    status, error, clips_requested, clips_created,
                    json.dumps(diagnostics) if diagnostics is not None else None,
                    status, status, status, upload_id,
                ),
            )
            updated = cursor.fetchone()
        connection.commit()
    return updated is not None


def resolve_upload_source(upload: dict[str, Any]) -> tuple[str, str]:
    source_type = str(upload.get("authorized_media_source_type") or "")
    encrypted = str(upload.get("source_reference_encrypted") or "")
    if encrypted:
        config = decrypt_source_config(encrypted)
    else:
        creator_config = str(
            upload.get("authorized_media_source_config_encrypted") or ""
        )
        if not source_type or not creator_config:
            raise YouTubeSourceError(
                "youtube_source_not_configured",
                "This YouTube creator does not yet have an approved source-media connection.",
            )
        config = decrypt_source_config(creator_config)
    config = validate_source_configuration(source_type, config)
    video_id = str(upload.get("platform_video_id") or "")
    if source_type == "manual_upload":
        path = str(config.get("path") or "")
        if not path or not Path(path).is_file():
            raise YouTubeSourceError("source_unavailable", "Manual source file is unavailable.")
        return source_type, path
    if source_type == "creator_file_url":
        url = str(config.get("url") or "").replace("{video_id}", video_id)
    elif source_type == "creator_storage_prefix":
        url = str(config.get("prefix") or "").rstrip("/") + f"/{video_id}.mp4"
    else:
        separator = "&" if "?" in str(config.get("url") or "") else "?"
        url = str(config.get("url") or "") + f"{separator}video_id={video_id}"
    return source_type, validate_authorized_media_url(
        url, allowed_hosts=_configured_allowed_hosts(config)
    )


def authorized_hosts_for_upload(upload: dict[str, Any]) -> set[str]:
    encrypted = str(
        upload.get("source_reference_encrypted")
        or upload.get("authorized_media_source_config_encrypted")
        or ""
    )
    if not encrypted:
        return set()
    return _configured_allowed_hosts(decrypt_source_config(encrypted))


def claim_youtube_upload(
    database_url: str, upload_id: str, worker_id: str, *, lease_seconds: int = 900,
) -> dict[str, Any] | None:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (YOUTUBE_UPLOAD_ADVISORY_LOCK_ID,))
            cursor.execute(
                """
                UPDATE youtube_uploads SET processing_status = 'claimed',
                    claimed_at = NOW(), claimed_by = %s,
                    claim_expires_at = NOW() + (%s * INTERVAL '1 second'),
                    processing_error = NULL, updated_at = NOW()
                WHERE id = %s
                  AND source_status = 'ready'
                  AND (
                    processing_status IN ('detected', 'failed')
                    OR (processing_status IN ('claimed','downloading','analyzing','generating')
                        AND claim_expires_at <= NOW())
                  )
                RETURNING *
                """,
                (worker_id, lease_seconds, upload_id),
            )
            row = cursor.fetchone()
        connection.commit()
    return dict(row) if row else None


def select_clip_candidates(
    transcript_segments: list[dict[str, Any]], duration_seconds: float,
    *, requested: int | None = None,
) -> list[dict[str, Any]]:
    config = youtube_config()
    count = min(
        requested or config["clips_per_video"], config["max_clips_per_video"], 5
    )
    minimum = config["clip_min_seconds"]
    target = config["clip_target_seconds"]
    maximum = min(config["clip_max_seconds"], 300)
    segments = sorted(
        [item for item in transcript_segments if float(item.get("end") or 0) > 0],
        key=lambda item: float(item.get("start") or 0),
    )
    if not segments:
        return []
    candidates = []
    excluded_terms = re.compile(r"\b(sponsor|sponsored|subscribe|welcome back|intro|outro)\b", re.I)
    energy_terms = re.compile(
        r"[!?]|\b(amazing|never|why|what|crazy|insane|laugh|wrong|right|finally|actually)\b",
        re.I,
    )
    for anchor_index, anchor in enumerate(segments):
        anchor_text = str(anchor.get("text") or "")
        anchor_start = float(anchor.get("start") or 0)
        if anchor_start < 30 or excluded_terms.search(anchor_text):
            continue
        end_target = min(duration_seconds, anchor_start + target)
        selected = [
            item for item in segments[anchor_index:]
            if float(item.get("start") or 0) < end_target
        ]
        if not selected:
            continue
        start = max(0.0, anchor_start - 8.0)
        end = min(
            duration_seconds,
            max(float(selected[-1].get("end") or end_target), start + minimum),
        )
        end = min(end, start + maximum)
        if end - start < minimum:
            continue
        text = " ".join(str(item.get("text") or "").strip() for item in selected)
        score = min(
            100,
            40 + len(energy_terms.findall(text)) * 6
            + min(20, len(text.split()) // 12),
        )
        candidate = {
            "start_seconds": round(start, 3),
            "end_seconds": round(end, 3),
            "duration_seconds": round(end - start, 3),
            "score": score,
            "transcript": text,
            "segments": [
                {**item, "start": max(0.0, float(item.get("start") or 0) - start),
                 "end": max(0.0, float(item.get("end") or 0) - start)}
                for item in selected
            ],
            "selection_reason": "coherent_transcript_window_with_editorial_energy",
        }
        if any(
            min(end, existing["end_seconds"]) - max(start, existing["start_seconds"])
            > 0.35 * min(end - start, existing["duration_seconds"])
            for existing in candidates
        ):
            continue
        candidates.append(candidate)
    candidates.sort(key=lambda item: (-item["score"], item["start_seconds"]))
    return candidates[:count]
