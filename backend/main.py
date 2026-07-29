from __future__ import annotations

import base64
import ctypes
import gc
import hashlib
import json
import math
import os
import re
import secrets
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import uuid
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from pathlib import Path
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from urllib.parse import parse_qs, unquote, urlencode, urlparse
import subprocess
import traceback
from download_service import DownloadService
import asyncio
import time
try:
    import psutil
except ImportError:
    psutil = None
    import resource
from ai import (
    generate_ai_title_package,
    generate_ai_description,
    generate_tiktok_caption_package,
    release_whisper_model,
)
from video_editing import create_tiktok_edited_video
from storage_service import object_storage_enabled, upload_video
from generation_jobs import (
    enqueue_generation_job,
    ensure_generation_jobs_table,
    get_generation_job,
    serialize_generation_job,
    update_generation_job_stage,
)
from stream_history import (
    ensure_stream_history_tables,
    get_exhausted_stream_ids,
    get_historical_cursor,
    get_stream_state,
    register_historical_stream,
    register_newest_stream,
    save_historical_cursor,
    update_stream_progress,
)

load_dotenv()


app = FastAPI(title="PulseAI Backend")
download_service = DownloadService()
AUTO_CLIP_INTERVAL_SECONDS = 300
AUTO_CLIP_MIN_SCORE = int(os.getenv("AUTO_CLIP_MIN_SCORE", "45"))
AUTO_CLIP_CANDIDATE_COUNT = 3
AUTO_CLIP_HISTORY_MAX_RETRIES = 2
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
app.state.tiktok_pkce_verifiers = {}
app.state.clip_history_ready = False
TIKTOK_RECONNECT_REQUIRED_MESSAGE = (
    "TikTok authorization expired. Reconnect TikTok in Settings."
)


def _get_current_rss_mb() -> float:
    if psutil is not None:
        process = psutil.Process(os.getpid())
        return process.memory_info().rss / (1024 * 1024)

    rss_value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return rss_value / (1024 * 1024)
    return rss_value / 1024


def _get_available_memory_mb() -> float | None:
    cgroup_pairs = (
        (
            Path("/sys/fs/cgroup/memory.current"),
            Path("/sys/fs/cgroup/memory.max"),
        ),
        (
            Path("/sys/fs/cgroup/memory/memory.usage_in_bytes"),
            Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
        ),
    )
    for usage_path, limit_path in cgroup_pairs:
        try:
            usage_bytes = int(usage_path.read_text(encoding="utf-8").strip())
            limit_text = limit_path.read_text(encoding="utf-8").strip()
            if limit_text == "max":
                continue
            limit_bytes = int(limit_text)
            if 0 < limit_bytes < (1 << 60):
                return max(0.0, (limit_bytes - usage_bytes) / (1024 * 1024))
        except (FileNotFoundError, OSError, ValueError):
            continue
    if psutil is None:
        return None
    return psutil.virtual_memory().available / (1024 * 1024)


def _trim_native_memory(stage: str) -> bool:
    before_rss_mb = _get_current_rss_mb()
    trimmed = False
    try:
        libc = ctypes.CDLL(None)
        malloc_trim = getattr(libc, "malloc_trim", None)
        if malloc_trim is not None:
            malloc_trim.argtypes = [ctypes.c_size_t]
            malloc_trim.restype = ctypes.c_int
            trimmed = bool(malloc_trim(0))
    except (AttributeError, OSError, TypeError, ValueError):
        trimmed = False
    after_rss_mb = _get_current_rss_mb()
    print(
        "MEMORY TRIM | "
        f"stage={stage} | before_rss_mb={before_rss_mb:.1f} | "
        f"after_rss_mb={after_rss_mb:.1f} | supported={str(trimmed).lower()}"
    )
    return trimmed


def _log_memory_check(stage: str, candidate_number: int, total_candidates: int) -> None:
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    rss_mb = _get_current_rss_mb()
    available_mb = _get_available_memory_mb()
    available_label = (
        f"{available_mb:.1f}" if available_mb is not None else "unknown"
    )
    print(
        f"MEMORY CHECK | stage={stage} | candidate={candidate_number}/{total_candidates} | "
        f"rss_mb={rss_mb:.1f} | available_mb={available_label} | ts={timestamp}"
    )


def _log_memory_admission(
    stage: str,
    available_memory_mb: float | None,
    required_memory_mb: float,
    admitted: bool,
) -> None:
    available_label = (
        f"{available_memory_mb:.1f}"
        if available_memory_mb is not None
        else "unknown"
    )
    print(
        "MEMORY CHECK | "
        f"stage={stage} | "
        f"available_mb={available_label} | "
        f"required_mb={required_memory_mb:.1f} | "
        f"decision={'allow' if admitted else 'defer'}"
    )


def _log_candidate_rejection(
    candidate: dict[str, object] | None,
    rejection_reason: str,
    *,
    streamer: str = "",
    viral_score: object = None,
) -> None:
    clip = candidate or {}
    candidate_identifier = str(
        clip.get("twitch_clip_id")
        or clip.get("id")
        or clip.get("created_at")
        or clip.get("timestamp")
        or "unknown"
    )
    source = str(
        streamer
        or clip.get("creator")
        or clip.get("creator_name")
        or clip.get("broadcaster_name")
        or "unknown"
    )
    score = viral_score
    if score is None:
        score = clip.get("score")
    score_label = "unknown"
    try:
        if score is not None:
            score_label = f"{float(score):.2f}"
    except (TypeError, ValueError):
        score_label = str(score)
    print(
        "CANDIDATE REJECTION | "
        f"streamer={source} | "
        f"candidate_identifier={candidate_identifier} | "
        f"viral_score={score_label} | "
        f"rejection_reason={rejection_reason}"
    )


def _apply_visual_layout_memory_fallback(
    visual_layout: dict[str, object],
    available_memory_mb: float | None,
) -> dict[str, object]:
    try:
        threshold_mb = max(
            0.0,
            float(os.getenv("VIDEO_LAYOUT_MEMORY_FALLBACK_MB", "120")),
        )
    except ValueError:
        threshold_mb = 120.0
    if (
        available_memory_mb is None
        or available_memory_mb >= threshold_mb
        or visual_layout.get("mode") == "single_subject"
    ):
        return visual_layout
    print(
        "VISUAL LAYOUT FALLBACK | mode=single_subject | "
        f"reason=low_available_memory | available_mb={available_memory_mb:.1f} | "
        f"threshold_mb={threshold_mb:.1f}"
    )
    return {
        "mode": "single_subject",
        "confidence": 1.0,
        "reason": (
            "split layout disabled because available memory was below "
            f"{threshold_mb:.0f} MB"
        ),
        "version": str(visual_layout.get("version") or "layout-v1"),
        "sample_count": int(visual_layout.get("sample_count") or 0),
        "regions": [],
    }


def _whisper_memory_admitted(
    available_memory_mb: float | None = None,
) -> tuple[bool, float | None, float]:
    try:
        required_memory_mb = max(
            0.0,
            float(os.getenv("WHISPER_MEMORY_FALLBACK_MB", "180")),
        )
    except ValueError:
        required_memory_mb = 180.0
    if available_memory_mb is None:
        available_memory_mb = _get_available_memory_mb()
    admitted = (
        available_memory_mb >= required_memory_mb
        if available_memory_mb is not None
        else not bool(DATABASE_URL)
    )
    return admitted, available_memory_mb, required_memory_mb


def _whisper_memory_recheck_seconds() -> float:
    try:
        configured_seconds = float(
            os.getenv("WHISPER_MEMORY_RECHECK_SECONDS", "3")
        )
    except ValueError:
        configured_seconds = 3.0
    return max(0.0, min(configured_seconds, 30.0))


def _recheck_whisper_memory_once(
    candidate_number: int,
    total_candidates: int,
    admission_point: str,
) -> tuple[bool, float | None, float]:
    cooldown_seconds = _whisper_memory_recheck_seconds()
    gc.collect()
    _trim_native_memory(f"{admission_point}_cooldown")
    print(
        "WHISPER MEMORY COOLDOWN START | "
        f"candidate={candidate_number}/{total_candidates} | "
        f"admission_point={admission_point} | "
        f"cooldown_seconds={cooldown_seconds:.1f}"
    )
    time.sleep(cooldown_seconds)
    admitted, available_memory_mb, required_memory_mb = (
        _whisper_memory_admitted()
    )
    available_label = (
        f"{available_memory_mb:.1f}"
        if available_memory_mb is not None
        else "unknown"
    )
    result_label = "PASSED" if admitted else "FAILED"
    print(
        f"WHISPER MEMORY RECHECK {result_label} | "
        f"candidate={candidate_number}/{total_candidates} | "
        f"admission_point={admission_point} | "
        f"available_mb={available_label} | "
        f"required_mb={required_memory_mb:.1f}"
    )
    return admitted, available_memory_mb, required_memory_mb


def _admit_candidate_batch_memory(
    total_candidates: int,
    batch_attempt: int,
) -> tuple[bool, float | None, float]:
    gc.collect()
    _trim_native_memory("candidate_batch_start")
    _log_memory_check(
        stage="before_candidate_download_admission",
        candidate_number=0,
        total_candidates=total_candidates,
    )
    admitted, available_memory_mb, required_memory_mb = (
        _whisper_memory_admitted()
    )
    _log_memory_admission(
        "before_download",
        available_memory_mb,
        required_memory_mb,
        admitted,
    )
    if not admitted:
        admitted, available_memory_mb, required_memory_mb = (
            _recheck_whisper_memory_once(
                0,
                total_candidates,
                "candidate_batch_start",
            )
        )
        _log_memory_admission(
            "before_download",
            available_memory_mb,
            required_memory_mb,
            admitted,
        )
    if not admitted:
        available_label = (
            f"{available_memory_mb:.1f}"
            if available_memory_mb is not None
            else "unknown"
        )
        print(
            "MEMORY BASELINE UNRECOVERED | "
            f"batch={batch_attempt}/2 | available_mb={available_label} | "
            f"required_mb={required_memory_mb:.1f}"
        )
    return admitted, available_memory_mb, required_memory_mb


def _log_performance_timing(
    stage: str,
    elapsed_seconds: float,
    candidate_number: int | None = None,
    total_candidates: int | None = None,
) -> None:
    candidate_label = "-"
    if candidate_number is not None and total_candidates is not None:
        candidate_label = f"{candidate_number}/{total_candidates}"

    print(
        "PERFORMANCE TIMING | "
        f"stage={stage} | "
        f"candidate={candidate_label} | "
        f"elapsed_seconds={elapsed_seconds:.3f}"
    )


def _get_full_evaluation_count() -> int:
    raw_value = os.getenv("AUTO_CLIP_FULL_EVALUATION_COUNT", "2")
    try:
        configured_count = int(raw_value)
    except (TypeError, ValueError):
        configured_count = 2
    return max(1, min(configured_count, AUTO_CLIP_CANDIDATE_COUNT))


def _bounded_environment_int(
    variable_name: str,
    default: int,
    minimum: int,
    maximum: int | None = None,
) -> int:
    try:
        value = int(os.getenv(variable_name, str(default)))
    except (TypeError, ValueError):
        value = default
    value = max(minimum, value)
    return min(value, maximum) if maximum is not None else value


def _processing_lease_minutes() -> int:
    return _bounded_environment_int(
        "AUTO_CLIP_PROCESSING_LEASE_MINUTES",
        default=45,
        minimum=5,
    )


def _publish_lease_minutes() -> int:
    return _bounded_environment_int(
        "AUTO_CLIP_PUBLISH_LEASE_MINUTES",
        default=30,
        minimum=5,
    )


def _published_media_retention_days() -> int:
    return _bounded_environment_int(
        "PUBLISHED_MEDIA_RETENTION_DAYS",
        default=7,
        minimum=0,
    )


def _twitch_max_pages() -> int:
    return _bounded_environment_int(
        "AUTO_CLIP_TWITCH_MAX_PAGES",
        default=5,
        minimum=1,
        maximum=20,
    )


def _select_duration_profile(clip: dict[str, object]) -> dict[str, object]:
    source_duration = max(0.0, float(clip.get("duration") or 0))
    long_enabled = os.getenv("AUTO_CLIP_LONGFORM_ENABLED", "true").lower() == "true"
    target_percent = _bounded_environment_int(
        "AUTO_CLIP_LONGFORM_TARGET_PERCENT", 30, 0, 100
    )
    long_min = float(os.getenv("AUTO_CLIP_LONG_MIN_SECONDS", "60"))
    long_target = float(os.getenv("AUTO_CLIP_LONG_TARGET_SECONDS", "75"))
    long_max = float(os.getenv("AUTO_CLIP_LONG_MAX_SECONDS", "90"))
    short_min = float(os.getenv("AUTO_CLIP_SHORT_MIN_SECONDS", "18"))
    short_max = float(os.getenv("AUTO_CLIP_SHORT_MAX_SECONDS", "40"))
    stable_key = str(clip.get("twitch_clip_id") or clip.get("id") or "")
    bucket = int(hashlib.sha256(stable_key.encode()).hexdigest()[:8], 16) % 100
    coherent_context = (
        source_duration >= long_min
        and len(str(clip.get("transcript") or "").split()) >= 80
        and len(clip.get("segments") or []) >= 6
    )
    wants_long = long_enabled and bucket < target_percent
    if wants_long and coherent_context:
        requested = min(long_target, long_max, source_duration)
        print(
            "LONGFORM CLIP ELIGIBLE | "
            f"duration={source_duration:.1f} | requested={requested:.1f}"
        )
        print(f"LONGFORM BOUNDARIES | start=0.0 | end={requested:.1f}")
        profile = "long"
        eligible_reason = "source_has_duration_transcript_and_scene_continuity"
        rejection_reason = ""
    else:
        requested = min(
            max(short_min, min(source_duration, short_max)),
            source_duration,
        )
        profile = "short"
        eligible_reason = ""
        rejection_reason = (
            "longform_not_selected"
            if not wants_long
            else "insufficient_coherent_context_without_padding"
        )
        if wants_long:
            print(
                "LONGFORM CLIP SKIPPED | "
                f"reason={rejection_reason} | duration={source_duration:.1f}"
            )
    print(
        "CLIP DURATION PROFILE SELECTED | "
        f"profile={profile} | requested={requested:.1f} | actual={requested:.1f}"
    )
    if profile == "long":
        print(f"LONGFORM ACTUAL DURATION | seconds={requested:.1f}")
    return {
        "duration_profile": profile,
        "requested_duration": requested,
        "actual_duration": requested,
        "longform_eligible_reason": eligible_reason,
        "longform_rejection_reason": rejection_reason,
    }


def _canonical_twitch_clip_id(value: object) -> str:
    text = unquote(str(value or "").strip())
    if not text:
        return ""
    parsed = urlparse(text if "://" in text else f"https://clips.twitch.tv/{text}")
    host = parsed.netloc.lower().split(":", 1)[0]
    path_parts = [part for part in parsed.path.split("/") if part]
    clip_id = ""
    query_clip = str(parse_qs(parsed.query).get("clip", [""])[0]).strip()
    if query_clip and host in {
        "clips.twitch.tv",
        "www.clips.twitch.tv",
        "player.twitch.tv",
    }:
        clip_id = query_clip
    elif host in {"clips.twitch.tv", "www.clips.twitch.tv"} and path_parts:
        clip_id = path_parts[0]
    elif host in {"twitch.tv", "www.twitch.tv", "m.twitch.tv"}:
        for marker in ("clip", "clips"):
            if marker in path_parts:
                marker_index = path_parts.index(marker)
                if marker_index + 1 < len(path_parts):
                    clip_id = path_parts[marker_index + 1]
                    break
    elif "://" not in text:
        clip_id = text.split("?", 1)[0].split("#", 1)[0].strip("/")
    clip_id = clip_id.strip()
    return clip_id if re.fullmatch(r"[A-Za-z0-9_-]+", clip_id) else ""


def _canonical_twitch_clip_url(clip_id_or_url: object) -> str:
    clip_id = _canonical_twitch_clip_id(clip_id_or_url)
    return f"https://clips.twitch.tv/{clip_id}" if clip_id else ""


def _normalized_twitch_identifiers(clip: dict[str, object]) -> tuple[str, str]:
    original_url = clip.get("public_url") or clip.get("url")
    url_clip_id = _canonical_twitch_clip_id(original_url)
    explicit_clip_id = (
        _canonical_twitch_clip_id(clip.get("twitch_clip_id"))
        or _canonical_twitch_clip_id(clip.get("id"))
    )
    if url_clip_id and explicit_clip_id and url_clip_id != explicit_clip_id:
        print(
            "CLIP HISTORY IDENTIFIER MISMATCH | "
            f"explicit_clip_id={explicit_clip_id} | url_clip_id={url_clip_id}"
        )
        return "", ""
    clip_id = url_clip_id or explicit_clip_id
    return clip_id, _canonical_twitch_clip_url(clip_id)


def _parse_twitch_created_at(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _parse_twitch_duration_seconds(value: object) -> int:
    text = str(value or "").strip().lower()
    if not text:
        return 0
    match = re.fullmatch(
        r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?",
        text,
    )
    if not match:
        return 0
    hours, minutes, seconds = (
        int(component or 0) for component in match.groups()
    )
    return hours * 3600 + minutes * 60 + seconds


def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _fast_prerank_candidates(
    twitch_clips: list[dict[str, Any]],
    existing_clip_ids: set[str],
    existing_clip_urls: set[str],
) -> tuple[list[dict[str, object]], bool]:
    now = datetime.now(timezone.utc)
    maximum_log_views = max(
        (
            math.log1p(_nonnegative_int(clip.get("view_count")))
            for clip in twitch_clips
        ),
        default=0.0,
    )
    title_token_counts: dict[str, int] = {}
    candidate_title_tokens: list[set[str]] = []
    for twitch_clip in twitch_clips:
        title_tokens = {
            token
            for token in re.findall(
                r"[a-z0-9']+",
                str(twitch_clip.get("title") or "").lower(),
            )
            if len(token) >= 4
        }
        candidate_title_tokens.append(title_tokens)
        for token in title_tokens:
            title_token_counts[token] = title_token_counts.get(token, 0) + 1

    ranked: list[dict[str, object]] = []
    sufficient_metadata = True
    signal_words = {
        "clutch",
        "crazy",
        "fail",
        "funny",
        "insane",
        "reaction",
        "rage",
        "record",
        "surprise",
        "unexpected",
        "win",
    }

    for candidate_index, twitch_clip in enumerate(twitch_clips, start=1):
        clip_id, public_url = _normalized_twitch_identifiers(twitch_clip)
        title = str(twitch_clip.get("title") or "").strip()
        created_at = _parse_twitch_created_at(twitch_clip.get("created_at"))
        view_count = _nonnegative_int(twitch_clip.get("view_count"))
        try:
            duration = max(0.0, float(twitch_clip.get("duration") or 0.0))
        except (TypeError, ValueError):
            duration = 0.0

        available_signals = sum(
            (
                "view_count" in twitch_clip,
                created_at is not None,
                duration > 0,
                bool(title),
                bool(twitch_clip.get("creator_name") or twitch_clip.get("game_id")),
            )
        )
        if available_signals < 3:
            sufficient_metadata = False

        view_score = (
            35.0 * math.log1p(view_count) / maximum_log_views
            if maximum_log_views > 0
            else 0.0
        )
        age_hours = (
            max(0.0, (now - created_at).total_seconds() / 3600.0)
            if created_at is not None
            else 168.0
        )
        freshness_score = 25.0 * max(0.0, 1.0 - (age_hours / 168.0))

        if 15.0 <= duration <= 45.0:
            duration_score = 20.0
        elif 5.0 <= duration < 15.0:
            duration_score = 20.0 * ((duration - 5.0) / 10.0)
        elif 45.0 < duration <= 60.0:
            duration_score = 20.0 * ((60.0 - duration) / 15.0)
        else:
            duration_score = 0.0

        title_tokens = candidate_title_tokens[candidate_index - 1]
        signal_word_score = min(9.0, 3.0 * len(title_tokens & signal_words))
        unique_token_count = sum(
            1 for token in title_tokens if title_token_counts.get(token) == 1
        )
        title_score = signal_word_score + min(6.0, 2.0 * unique_token_count)
        metadata_score = min(5.0, float(available_signals))
        already_processed = clip_id in existing_clip_ids or public_url in existing_clip_urls
        total_score = (
            view_score
            + freshness_score
            + duration_score
            + title_score
            + metadata_score
        )
        ranked.append(
            {
                "candidate_index": candidate_index,
                "search_tier": int(twitch_clip.get("_search_tier", 1)),
                "score": total_score,
                "title_score": title_score,
                "reasons": (
                    f"views={view_score:.2f}/35(view_count={view_count}); "
                    f"freshness={freshness_score:.2f}/25(age_hours={age_hours:.1f}); "
                    f"duration={duration_score:.2f}/20(seconds={duration:.1f}); "
                    f"title={title_score:.2f}/15("
                    f"signal_words={len(title_tokens & signal_words)},"
                    f"unique_tokens={unique_token_count}); "
                    f"metadata={metadata_score:.2f}/5; "
                    f"already_processed={str(already_processed).lower()}"
                ),
            }
        )

    ranked.sort(
        key=lambda item: (
            -int(item["search_tier"]),
            float(item["score"]),
            -int(item["candidate_index"]),
        ),
        reverse=True,
    )
    return ranked, sufficient_metadata


def _transcribe_video_with_segments_subprocess(video_path: str) -> dict[str, object]:
    file_descriptor, output_json_path = tempfile.mkstemp(suffix=".json")
    os.close(file_descriptor)
    if os.path.exists(output_json_path):
        os.remove(output_json_path)

    command = [
        sys.executable,
        str(BASE_DIR / "ai.py"),
        "--transcribe-worker",
        "--video-path",
        video_path,
        "--output-json",
        output_json_path,
    ]

    try:
        worker_started_at = time.perf_counter()
        completed = subprocess.run(
            command,
            check=True,
            timeout=180,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        _log_performance_timing(
            stage="whisper_worker_total",
            elapsed_seconds=time.perf_counter() - worker_started_at,
        )
        if completed.stderr:
            worker_output = completed.stderr.strip()
            if worker_output:
                print(worker_output)
        if not os.path.exists(output_json_path):
            raise RuntimeError("Transcription worker did not create an output JSON file.")

        with open(output_json_path, "r", encoding="utf-8") as file:
            payload = json.load(file)

        transcript = payload.get("transcript")
        segments = payload.get("segments")

        if not isinstance(transcript, str):
            raise RuntimeError("Transcription worker returned an invalid transcript.")
        if not isinstance(segments, list):
            raise RuntimeError("Transcription worker returned invalid segments.")

        normalized_segments = []
        for segment in segments:
            if not isinstance(segment, dict):
                raise RuntimeError("Transcription worker returned an invalid segment entry.")
            if not all(key in segment for key in ("start", "end", "text")):
                raise RuntimeError("Transcription worker returned a segment with missing fields.")
            normalized_segments.append(
                {
                    "start": float(segment["start"]),
                    "end": float(segment["end"]),
                    "text": str(segment["text"]),
                }
            )

        return {
            "transcript": transcript,
            "segments": normalized_segments,
        }
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            f"Transcription worker timed out after {error.timeout} seconds."
        ) from error
    except subprocess.CalledProcessError as error:
        stderr = (error.stderr or "").strip()
        raise RuntimeError(
            f"Transcription worker failed: {stderr or 'no stderr output'}"
        ) from error
    except (json.JSONDecodeError, OSError, TypeError, ValueError) as error:
        raise RuntimeError(f"Transcription worker returned invalid JSON: {error}") from error
    finally:
        if os.path.exists(output_json_path):
            os.remove(output_json_path)
        gc.collect()
        _trim_native_memory("whisper_subprocess_cleanup")


def _score_multimodal_clip_subprocess(
    video_path: str,
    transcript: str,
    creator: str,
    game: str,
    stream_title: str,
    viewer_count: object,
    duration: object,
) -> dict[str, object]:
    input_descriptor, input_json_path = tempfile.mkstemp(suffix=".json")
    output_descriptor, output_json_path = tempfile.mkstemp(suffix=".json")
    os.close(input_descriptor)
    os.close(output_descriptor)
    try:
        with open(input_json_path, "w", encoding="utf-8") as input_file:
            json.dump(
                {
                    "video_path": video_path,
                    "transcript": transcript,
                    "creator": creator,
                    "game": game,
                    "stream_title": stream_title,
                    "viewer_count": viewer_count,
                    "duration": duration,
                },
                input_file,
            )
        os.remove(output_json_path)
        completed = subprocess.run(
            [
                sys.executable,
                str(BASE_DIR / "ai.py"),
                "--score-worker",
                "--input-json",
                input_json_path,
                "--output-json",
                output_json_path,
            ],
            check=True,
            timeout=180,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        if completed.stderr.strip():
            print(completed.stderr.strip())
        with open(output_json_path, "r", encoding="utf-8") as output_file:
            result = json.load(output_file)
        if not isinstance(result, dict) or "score" not in result:
            raise RuntimeError("Scoring worker returned an invalid result.")
        return result
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("Multimodal scoring worker timed out.") from error
    except subprocess.CalledProcessError as error:
        raise RuntimeError(
            "Multimodal scoring worker failed: "
            f"{(error.stderr or '').strip() or 'no stderr output'}"
        ) from error
    finally:
        for temporary_path in (input_json_path, output_json_path):
            try:
                os.remove(temporary_path)
            except FileNotFoundError:
                pass
        gc.collect()
        _trim_native_memory("multimodal_scoring_cleanup")


def _detect_visual_layout_subprocess(video_path: str) -> dict[str, object]:
    output_descriptor, output_json_path = tempfile.mkstemp(suffix=".json")
    os.close(output_descriptor)
    try:
        os.remove(output_json_path)
        completed = subprocess.run(
            [
                sys.executable,
                str(BASE_DIR / "video_editing.py"),
                "--detect-layout-worker",
                "--video-path",
                video_path,
                "--output-json",
                output_json_path,
            ],
            check=True,
            timeout=60,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        if completed.stderr.strip():
            print(completed.stderr.strip())
        with open(output_json_path, "r", encoding="utf-8") as output_file:
            result = json.load(output_file)
        if not isinstance(result, dict) or "mode" not in result:
            raise RuntimeError("Layout worker returned an invalid result.")
        return result
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError) as error:
        print(f"VISUAL LAYOUT WORKER FAILED | error={error!r}")
        return {
            "mode": "single_subject",
            "confidence": 1.0,
            "reason": "layout worker unavailable",
            "version": "layout-v1",
            "sample_count": 0,
            "regions": [],
        }
    finally:
        try:
            os.remove(output_json_path)
        except FileNotFoundError:
            pass
        gc.collect()
        _trim_native_memory("visual_layout_detection_cleanup")


def _cleanup_candidate_video_impl(
    video_path: object,
    candidate_number: int,
    persisted_clips: list[dict[str, object]],
) -> None:
    path_text = str(video_path or "").strip()
    if not path_text:
        print(
            "CANDIDATE VIDEO CLEANUP REJECTED | "
            f"candidate={candidate_number} | reason=empty_path"
        )
        return

    downloads_root = (BASE_DIR / "downloads").resolve()
    candidate_path = Path(path_text)
    try:
        if candidate_path.is_symlink():
            print(
                "CANDIDATE VIDEO CLEANUP REJECTED | "
                f"candidate={candidate_number} | "
                f"path={candidate_path} | reason=symlink"
            )
            return

        resolved_candidate = candidate_path.resolve(strict=False)
        if (
            resolved_candidate.parent != downloads_root
            or resolved_candidate == downloads_root
        ):
            print(
                "CANDIDATE VIDEO CLEANUP REJECTED | "
                f"candidate={candidate_number} | "
                f"path={resolved_candidate} | reason=outside_or_nested"
            )
            return
        if not resolved_candidate.is_file():
            print(
                "CANDIDATE VIDEO CLEANUP REJECTED | "
                f"candidate={candidate_number} | "
                f"path={resolved_candidate} | reason=not_a_file"
            )
            return

        protected_paths: set[Path] = set()
        for persisted_clip in persisted_clips:
            for key in ("raw_video_path", "video_path"):
                persisted_path_text = str(persisted_clip.get(key) or "").strip()
                if persisted_path_text:
                    protected_paths.add(Path(persisted_path_text).resolve(strict=False))
        if resolved_candidate in protected_paths:
            print(
                "CANDIDATE VIDEO CLEANUP REJECTED | "
                f"candidate={candidate_number} | "
                f"path={resolved_candidate} | reason=persisted_media"
            )
            return

        resolved_candidate.unlink()
        print(
            "CANDIDATE VIDEO CLEANUP | "
            f"candidate={candidate_number} | path={resolved_candidate}"
        )
    except Exception as error:
        print(
            "CANDIDATE VIDEO CLEANUP FAILED | "
            f"candidate={candidate_number} | "
            f"path={candidate_path} | error={error!r}"
        )


def _cleanup_candidate_video(
    video_path: object,
    candidate_number: int,
    persisted_clips: list[dict[str, object]],
) -> None:
    try:
        _cleanup_candidate_video_impl(
            video_path,
            candidate_number,
            persisted_clips,
        )
    finally:
        gc.collect()
        _trim_native_memory("candidate_media_cleanup")


def _fully_evaluate_candidate(
    twitch_clip: dict[str, Any],
    candidate_number: int,
    total_candidates: int,
    creator: dict[str, Any],
    stream: dict[str, Any],
    stream_title: str,
    viewer_count: int,
    persisted_clips: list[dict[str, object]],
    generation_job_id: str | None = None,
    generation_worker_id: str | None = None,
) -> dict[str, object]:
    candidate_started_at = time.perf_counter()
    video_path = ""
    failure_stage = "candidate_construction"
    expensive_evaluation_started = False

    try:
        twitch_clip_id = str(twitch_clip.get("id", "")).strip()
        public_url = str(
            twitch_clip.get("url")
            or f"https://clips.twitch.tv/{twitch_clip_id}"
        )
        clip: dict[str, object] = {
            "title": stream_title,
            "creator": creator["name"],
            "status": "Ready to review",
            "viewer_count": viewer_count,
            "game": stream.get("game_name"),
            "started_at": stream.get("started_at"),
            "thumbnail_url": stream.get("thumbnail_url"),
            "twitch_clip_id": twitch_clip_id,
            "twitch_edit_url": twitch_clip.get("edit_url"),
            "public_url": public_url,
            "candidate_number": candidate_number,
            "_search_tier": int(twitch_clip.get("_search_tier", 1)),
            "source_stream_id": twitch_clip.get("_stream_id"),
            "duration": float(twitch_clip.get("duration") or 0),
        }

        failure_stage = "download"
        _set_generation_job_stage(
            generation_job_id,
            generation_worker_id,
            "downloading",
        )
        _log_memory_check(
            stage="before_ytdlp_download",
            candidate_number=candidate_number,
            total_candidates=total_candidates,
        )
        download_started_at = time.perf_counter()
        video_path = download_twitch_clip(public_url, twitch_clip_id)
        gc.collect()
        _log_performance_timing(
            stage="ytdlp_download",
            candidate_number=candidate_number,
            total_candidates=total_candidates,
            elapsed_seconds=time.perf_counter() - download_started_at,
        )
        _log_memory_check(
            stage="after_yt_dlp_process_cleanup",
            candidate_number=candidate_number,
            total_candidates=total_candidates,
        )
        if not video_path:
            return {
                "success": False,
                "clip": None,
                "video_path": "",
                "failure_stage": "download",
                "error": "download failed",
                "memory_deferred_before_download": False,
                "memory_rejected_after_download": False,
                "expensive_evaluation_started": False,
            }

        clip["video_path"] = video_path
        _log_memory_check(
            stage="before_whisper_admission",
            candidate_number=candidate_number,
            total_candidates=total_candidates,
        )
        admitted, available_memory_mb, required_memory_mb = (
            _whisper_memory_admitted()
        )
        _log_memory_admission(
            "before_whisper",
            available_memory_mb,
            required_memory_mb,
            admitted,
        )
        if not admitted:
            admitted, available_memory_mb, required_memory_mb = (
                _recheck_whisper_memory_once(
                    candidate_number,
                    total_candidates,
                    "after_download",
                )
            )
            _log_memory_admission(
                "before_whisper",
                available_memory_mb,
                required_memory_mb,
                admitted,
            )
        if not admitted:
            failure_stage = "whisper_admission"
            available_label = (
                f"{available_memory_mb:.1f}"
                if available_memory_mb is not None
                else "unknown"
            )
            print(
                "WHISPER ADMISSION REJECTED | "
                f"candidate={candidate_number}/{total_candidates} | "
                f"available_mb={available_label} | "
                f"required_mb={required_memory_mb:.1f}"
            )
            _cleanup_candidate_video(
                video_path,
                candidate_number,
                persisted_clips,
            )
            video_path = ""
            (
                rescue_allowed,
                rescue_available_memory_mb,
                rescue_required_memory_mb,
            ) = _whisper_memory_admitted()
            _log_memory_admission(
                "before_download",
                rescue_available_memory_mb,
                rescue_required_memory_mb,
                rescue_allowed,
            )
            return {
                "success": False,
                "clip": None,
                "video_path": "",
                "failure_stage": failure_stage,
                "error": (
                    "insufficient memory before Whisper: "
                    f"available={available_label} MB, "
                    f"required={required_memory_mb:.1f} MB"
                ),
                "memory_deferred_before_download": False,
                "memory_rejected_after_download": True,
                "expensive_evaluation_started": False,
                "rescue_allowed": rescue_allowed,
                "available_memory_mb": available_memory_mb,
                "required_memory_mb": required_memory_mb,
            }
        processing_error: Exception | None = None
        release_error: Exception | None = None
        expensive_evaluation_started = True
        try:
            failure_stage = "whisper"
            _set_generation_job_stage(
                generation_job_id,
                generation_worker_id,
                "transcribing",
            )
            _log_memory_check(
                stage="before_whisper_transcription",
                candidate_number=candidate_number,
                total_candidates=total_candidates,
            )
            transcription_started_at = time.perf_counter()
            transcription = _transcribe_video_with_segments_subprocess(video_path)
            _log_performance_timing(
                stage="whisper_transcription",
                candidate_number=candidate_number,
                total_candidates=total_candidates,
                elapsed_seconds=time.perf_counter() - transcription_started_at,
            )
            _log_memory_check(
                stage="after_whisper_transcription",
                candidate_number=candidate_number,
                total_candidates=total_candidates,
            )
            clip["transcript"] = transcription.get("transcript", "")
            clip["segments"] = transcription.get("segments", [])

            failure_stage = "multimodal_scoring"
            _set_generation_job_stage(
                generation_job_id,
                generation_worker_id,
                "scoring",
            )
            _log_memory_check(
                stage="before_multimodal_scoring",
                candidate_number=candidate_number,
                total_candidates=total_candidates,
            )
            scoring_started_at = time.perf_counter()
            multimodal = _score_multimodal_clip_subprocess(
                video_path,
                str(clip["transcript"]),
                str(clip["creator"]),
                str(clip.get("game") or ""),
                str(clip["title"]),
                clip["viewer_count"],
                clip.get("duration", 0),
            )
            _log_performance_timing(
                stage="multimodal_scoring",
                candidate_number=candidate_number,
                total_candidates=total_candidates,
                elapsed_seconds=time.perf_counter() - scoring_started_at,
            )
            _log_memory_check(
                stage="after_multimodal_scoring",
                candidate_number=candidate_number,
                total_candidates=total_candidates,
            )
            clip["viral_score"] = multimodal["score"]
            clip["score"] = multimodal["score"]
            clip["score_reason"] = multimodal["reason"]
            clip["score_hook"] = multimodal["hook"]
            clip["visual_score"] = multimodal["visual_score"]
            clip["transcript_score"] = multimodal["transcript_score"]
            clip["context_score"] = multimodal["context_score"]
            clip["score_confidence"] = multimodal["confidence"]
            clip["decision"] = multimodal["decision"]
        except Exception as error:
            processing_error = error
        finally:
            try:
                _log_memory_check(
                    stage="before_whisper_release",
                    candidate_number=candidate_number,
                    total_candidates=total_candidates,
                )
                release_whisper_model()
                _log_memory_check(
                    stage="after_whisper_release",
                    candidate_number=candidate_number,
                    total_candidates=total_candidates,
                )
            except Exception as error:
                release_error = error
                print(
                    "WHISPER MODEL RELEASE FAILED | "
                    f"candidate={candidate_number}/{total_candidates} | "
                    f"error={error!r}"
                )

        if processing_error is not None:
            raise processing_error
        if release_error is not None:
            failure_stage = "model_release"
            raise release_error

        return {
            "success": True,
            "clip": clip,
            "video_path": video_path,
            "failure_stage": None,
            "error": None,
            "memory_deferred_before_download": False,
            "memory_rejected_after_download": False,
            "expensive_evaluation_started": True,
        }
    except Exception as error:
        if video_path:
            _cleanup_candidate_video(
                video_path,
                candidate_number,
                persisted_clips,
            )
        return {
            "success": False,
            "clip": None,
            "video_path": video_path,
            "failure_stage": failure_stage,
            "error": repr(error),
            "memory_deferred_before_download": False,
            "memory_rejected_after_download": False,
            "expensive_evaluation_started": expensive_evaluation_started,
        }
    finally:
        _log_performance_timing(
            stage="candidate_processing_total",
            candidate_number=candidate_number,
            total_candidates=total_candidates,
            elapsed_seconds=time.perf_counter() - candidate_started_at,
        )


def _generate_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _get_tiktok_authorization_url(state: str, code_challenge: str) -> str:
    client_key = os.getenv("TIKTOK_CLIENT_KEY")
    redirect_uri = os.getenv("TIKTOK_REDIRECT_URI")

    if not client_key or not redirect_uri:
        raise HTTPException(
            status_code=500,
            detail="TikTok client key and redirect URI must be configured.",
        )

    params = {
        "client_key": client_key,
        "response_type": "code",
        "scope": "user.info.basic,video.upload",
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }

    return "https://www.tiktok.com/v2/auth/authorize/?" + urlencode(params)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")
TWITCH_REDIRECT_URI = os.getenv(
    "TWITCH_REDIRECT_URI",
    "http://localhost:8000/auth/twitch/callback"
)

app.state.auto_clip_task = None
app.state.embedded_clip_worker_task = None
app.state.embedded_clip_worker_stop_event = None
app.state.clip_generation_admission_lock = asyncio.Lock()
app.state.clip_generation_busy = False
app.state.clip_generation_db_lease = None
app.state.video_edit_lock = asyncio.Lock()

CLIP_GENERATION_ADVISORY_LOCK_ID = 22616960936427850


def _try_acquire_generation_db_lease() -> object | None:
    if not DATABASE_URL:
        return True
    try:
        import psycopg

        connection = psycopg.connect(DATABASE_URL, autocommit=True)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_try_advisory_lock(%s)",
                (CLIP_GENERATION_ADVISORY_LOCK_ID,),
            )
            acquired = bool(cursor.fetchone()[0])
        if not acquired:
            connection.close()
            return None
        print("CLIP GENERATION LEASE ACQUIRED")
        return connection
    except Exception as error:
        print(f"CLIP GENERATION LEASE FAILED | error={error!r}")
        return None


def _release_generation_db_lease(lease: object) -> None:
    if lease is True or lease is None:
        return
    try:
        with lease.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_unlock(%s)",
                (CLIP_GENERATION_ADVISORY_LOCK_ID,),
            )
    except Exception as error:
        print(f"CLIP GENERATION LEASE RELEASE FAILED | error={error!r}")
    finally:
        try:
            lease.close()
        except Exception as error:
            print(
                "CLIP GENERATION LEASE CONNECTION CLOSE FAILED | "
                f"error={error!r}"
            )


async def try_begin_clip_generation() -> bool:
    async with app.state.clip_generation_admission_lock:
        if app.state.clip_generation_busy:
            return False
        database_lease = _try_acquire_generation_db_lease()
        if database_lease is None:
            return False
        app.state.clip_generation_busy = True
        app.state.clip_generation_db_lease = database_lease
        return True


async def end_clip_generation() -> None:
    async with app.state.clip_generation_admission_lock:
        database_lease = app.state.clip_generation_db_lease
        app.state.clip_generation_db_lease = None
        app.state.clip_generation_busy = False
        _release_generation_db_lease(database_lease)


async def _auto_clip_loop():
    await asyncio.sleep(AUTO_CLIP_INTERVAL_SECONDS)
    print("AUTO MODE STARTED")

    while True:
        print("AUTO CYCLE START")
        try:
            job, created = enqueue_generation_job("automatic")
            print(
                "AUTO GENERATION JOB | "
                f"job_id={job['id']} | created={str(created).lower()}"
            )
        except Exception as error:
            print("AUTO ERROR:", repr(error))
        try:
            await _run_auto_publish_once()
        except Exception as error:
            print(f"AUTO PUBLISH SKIPPED | reason={error!r}")
        try:
            await _poll_one_tiktok_post_status()
        except Exception as error:
            print(f"TIKTOK POST STATUS | error={error!r}")
        print("AUTO CYCLE COMPLETE")
        await asyncio.sleep(AUTO_CLIP_INTERVAL_SECONDS)


async def _run_auto_publish_once() -> None:
    if not DATABASE_URL or not getattr(app.state, "clip_history_ready", False):
        return
    import psycopg

    with psycopg.connect(DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT auto_publish_enabled, auto_publish_approved,
                       daily_limit, min_gap_minutes
                FROM pulseai_settings WHERE singleton = TRUE
                """
            )
            settings = cursor.fetchone()
            if not settings or not settings[0] or not settings[1]:
                print("AUTO PUBLISH SKIPPED | reason=disabled_or_unapproved")
                return
            cursor.execute(
                """
                SELECT COUNT(*), MAX(publish_attempted_at)
                FROM twitch_clip_history
                WHERE publish_attempted_at >= date_trunc('day', NOW())
                """
            )
            attempts_today, last_attempt = cursor.fetchone()
            if attempts_today >= settings[2]:
                print("AUTO PUBLISH SKIPPED | reason=daily_limit")
                return
            if (
                last_attempt
                and last_attempt
                > datetime.now(timezone.utc) - timedelta(minutes=settings[3])
            ):
                print("AUTO PUBLISH SKIPPED | reason=minimum_gap")
                return
            cursor.execute(
                """
                SELECT generated_clip_id FROM twitch_clip_history
                WHERE status = 'scheduled' AND scheduled_for <= NOW()
                ORDER BY scheduled_for LIMIT 1
                """
            )
            row = cursor.fetchone()
    if not row:
        print("AUTO PUBLISH SKIPPED | reason=no_due_clip")
        return
    print(f"AUTO PUBLISH CLAIMED | clip_id={row[0]}")
    await publish_clip_to_tiktok({"id": row[0]})


@app.on_event("startup")
async def _start_auto_clip_task():
    app.state.clip_history_ready = False
    _ensure_oauth_tokens_table()
    if not _ensure_clip_history_table():
        print("CLIP HISTORY INITIALIZATION FAILED")
        return
    if not ensure_generation_jobs_table():
        print("GENERATION JOB INITIALIZATION FAILED")
        return
    if not ensure_stream_history_tables():
        print("AUTO CLIP STREAM HISTORY INITIALIZATION FAILED")
        return
    app.state.clip_history_ready = True
    print("CLIP HISTORY READY")
    if app.state.auto_clip_task is None or app.state.auto_clip_task.done():
        app.state.auto_clip_task = asyncio.create_task(_auto_clip_loop())
    embedded_worker_enabled = (
        os.getenv("EMBEDDED_CLIP_WORKER_ENABLED", "true").strip().lower()
        in {"1", "true", "yes", "on"}
    )
    if (
        embedded_worker_enabled
        and (
            app.state.embedded_clip_worker_task is None
            or app.state.embedded_clip_worker_task.done()
        )
    ):
        import clip_worker

        stop_event = asyncio.Event()
        app.state.embedded_clip_worker_stop_event = stop_event
        app.state.embedded_clip_worker_task = asyncio.create_task(
            clip_worker.run_worker_loop(stop_event, embedded=True)
        )


@app.on_event("shutdown")
async def _stop_auto_clip_task():
    embedded_stop_event = app.state.embedded_clip_worker_stop_event
    embedded_task = app.state.embedded_clip_worker_task
    if embedded_stop_event is not None:
        embedded_stop_event.set()
    if embedded_task is not None:
        embedded_task.cancel()
        try:
            await embedded_task
        except asyncio.CancelledError:
            pass
        finally:
            app.state.embedded_clip_worker_task = None
            app.state.embedded_clip_worker_stop_event = None

    task = app.state.auto_clip_task
    if task is None:
        return

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

BASE_DIR = Path(__file__).resolve().parent
CREATORS_FILE = BASE_DIR / "creators.json"
CREATOR_CURSOR_FILE = BASE_DIR / "creator_cursor.json"
TWITCH_USER_TOKEN_FILE = BASE_DIR / "twitch_user_token.json"
TIKTOK_USER_TOKEN_FILE = BASE_DIR / "tiktok_user_token.json"


def _ensure_clip_history_table() -> bool:
    if not DATABASE_URL:
        return True
    try:
        import psycopg
        from psycopg import sql

        with psycopg.connect(DATABASE_URL) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    (22616960936427849,),
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS twitch_clip_history (
                        provider TEXT NOT NULL DEFAULT 'twitch',
                        clip_id TEXT NOT NULL,
                        clip_url TEXT,
                        creator_id TEXT,
                        creator_name TEXT,
                        created_at TIMESTAMPTZ,
                        first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        last_processed_at TIMESTAMPTZ,
                        status TEXT NOT NULL DEFAULT 'discovered',
                        viral_score INTEGER,
                        published_at TIMESTAMPTZ,
                        publish_attempted_at TIMESTAMPTZ,
                        provider_publish_id TEXT,
                        failure_stage TEXT,
                        retry_count INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (provider, clip_id)
                    )
                    """
                )
                cursor.execute(
                    """
                    ALTER TABLE twitch_clip_history
                    ADD COLUMN IF NOT EXISTS publish_attempted_at TIMESTAMPTZ
                    """
                )
                cursor.execute(
                    """
                    ALTER TABLE twitch_clip_history
                    ADD COLUMN IF NOT EXISTS provider_publish_id TEXT
                    """
                )
                additive_columns = {
                    "generated_clip_id": "TEXT",
                    "display_title": "TEXT",
                    "category": "TEXT",
                    "local_video_path": "TEXT",
                    "raw_video_path": "TEXT",
                    "object_key": "TEXT",
                    "durable_url": "TEXT",
                    "ai_post_caption": "TEXT",
                    "ai_hashtags": "JSONB",
                    "ai_tiktok_description": "TEXT",
                    "caption_generation_version": "TEXT",
                    "transcript": "TEXT",
                    "duration_profile": "TEXT",
                    "requested_duration": "DOUBLE PRECISION",
                    "actual_duration": "DOUBLE PRECISION",
                    "longform_eligible_reason": "TEXT",
                    "longform_rejection_reason": "TEXT",
                    "scheduled_for": "TIMESTAMPTZ",
                    "approved_at": "TIMESTAMPTZ",
                    "archived_at": "TIMESTAMPTZ",
                    "tiktok_publish_mode": "TEXT",
                    "tiktok_post_status": "TEXT",
                    "tiktok_failure_reason": "TEXT",
                    "tiktok_last_status_check": "TIMESTAMPTZ",
                    "tiktok_published_at": "TIMESTAMPTZ",
                    "poll_claimed_at": "TIMESTAMPTZ",
                    "poll_check_count": "INTEGER NOT NULL DEFAULT 0",
                    "generated_at": "TIMESTAMPTZ",
                    "title_version": "TEXT",
                    "views": "BIGINT",
                    "likes": "BIGINT",
                    "comments": "BIGINT",
                    "shares": "BIGINT",
                    "last_metrics_sync": "TIMESTAMPTZ",
                    "source_creator_id": "TEXT",
                    "source_stream_id": "TEXT",
                    "source_platform": "TEXT",
                    "rights_status": "TEXT",
                    "authorization_reference": "TEXT",
                    "authorization_notes": "TEXT",
                    "visual_layout_mode": "TEXT",
                    "visual_layout_confidence": "DOUBLE PRECISION",
                    "visual_layout_reason": "TEXT",
                    "visual_layout_version": "TEXT",
                    "reaction_region": "JSONB",
                    "content_region": "JSONB",
                    "generated_title": "TEXT",
                    "title_event_summary": "TEXT",
                    "title_relevance_score": "DOUBLE PRECISION",
                    "title_generation_version": "TEXT",
                    "title_fallback_used": "BOOLEAN",
                }
                for column_name, column_type in additive_columns.items():
                    cursor.execute(
                        sql.SQL(
                            "ALTER TABLE twitch_clip_history "
                            "ADD COLUMN IF NOT EXISTS {} "
                        ).format(sql.Identifier(column_name))
                        + sql.SQL(column_type)
                    )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pulseai_settings (
                        singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
                        post_mode TEXT NOT NULL DEFAULT 'draft',
                        auto_publish_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                        auto_publish_approved BOOLEAN NOT NULL DEFAULT FALSE,
                        daily_limit INTEGER NOT NULL DEFAULT 2,
                        min_gap_minutes INTEGER NOT NULL DEFAULT 180,
                        timezone TEXT NOT NULL DEFAULT 'UTC',
                        privacy_level TEXT,
                        allow_comments BOOLEAN NOT NULL DEFAULT TRUE,
                        allow_duet BOOLEAN NOT NULL DEFAULT TRUE,
                        allow_stitch BOOLEAN NOT NULL DEFAULT TRUE,
                        longform_target_percent INTEGER NOT NULL DEFAULT 30,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS monitored_creators (
                        id BIGSERIAL PRIMARY KEY,
                        twitch_user_id TEXT,
                        login TEXT NOT NULL UNIQUE,
                        display_name TEXT NOT NULL,
                        enabled BOOLEAN NOT NULL DEFAULT TRUE,
                        priority INTEGER NOT NULL DEFAULT 0,
                        notes TEXT,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                _backfill_monitored_creators(cursor)
                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS twitch_clip_history_status_idx
                    ON twitch_clip_history (provider, status)
                    """
                )
                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS twitch_clip_history_creator_created_idx
                    ON twitch_clip_history (creator_id, created_at DESC)
                    """
                )
                cursor.execute(
                    """
                    SELECT provider, clip_id, clip_url, creator_id, creator_name,
                           created_at, first_seen_at, last_processed_at, status,
                           viral_score, published_at, publish_attempted_at,
                           provider_publish_id, failure_stage, retry_count
                    FROM twitch_clip_history
                    WHERE provider = 'twitch'
                    FOR UPDATE
                    """
                )
                history_rows = cursor.fetchall()
                status_precedence = {
                    "published": 15,
                    "archived": 14,
                    "rejected": 13,
                    "generated": 12,
                    "uploaded_to_inbox": 11,
                    "publishing": 10,
                    "scheduled": 9,
                    "approved": 8,
                    "ready_for_review": 7,
                    "publish_failed": 6,
                    "rejected_low_score": 5,
                    "fully_evaluated": 4,
                    "processing": 3,
                    "failed": 2,
                    "intentionally_skipped": 2,
                    "discovered": 1,
                }
                reconciled_rows: dict[str, list[object]] = {}
                duplicate_counts: dict[str, int] = {}
                original_ids_by_canonical_id: dict[str, list[str]] = {}
                for row in history_rows:
                    canonical_id, canonical_url = _normalized_twitch_identifiers(
                        {
                            "twitch_clip_id": row[1],
                            "public_url": row[2],
                        }
                    )
                    if not canonical_id:
                        canonical_id = _canonical_twitch_clip_id(row[2])
                        canonical_url = _canonical_twitch_clip_url(canonical_id)
                    if not canonical_id:
                        invalid_key = f"invalid:{row[1]}"
                        invalid_row = list(row)
                        invalid_row[2] = None
                        reconciled_rows[invalid_key] = invalid_row
                        duplicate_counts[invalid_key] = 1
                        cursor.execute(
                            """
                            UPDATE twitch_clip_history
                            SET clip_url = NULL
                            WHERE provider = 'twitch' AND clip_id = %s
                            """,
                            (row[1],),
                        )
                        continue
                    original_ids_by_canonical_id.setdefault(canonical_id, []).append(
                        str(row[1])
                    )
                    normalized_row = list(row)
                    normalized_row[1] = canonical_id
                    normalized_row[2] = canonical_url or None
                    existing = reconciled_rows.get(canonical_id)
                    if existing is None:
                        reconciled_rows[canonical_id] = normalized_row
                        duplicate_counts[canonical_id] = 1
                        continue
                    duplicate_counts[canonical_id] += 1
                    existing_rank = status_precedence.get(str(existing[8]), 0)
                    incoming_rank = status_precedence.get(str(normalized_row[8]), 0)
                    survivor, other = (
                        (normalized_row, existing)
                        if incoming_rank > existing_rank
                        else (existing, normalized_row)
                    )
                    survivor[6] = min(
                        value for value in (existing[6], normalized_row[6]) if value is not None
                    )
                    survivor[7] = max(
                        (value for value in (existing[7], normalized_row[7]) if value is not None),
                        default=None,
                    )
                    survivor[10] = max(
                        (value for value in (existing[10], normalized_row[10]) if value is not None),
                        default=None,
                    )
                    survivor[14] = max(int(existing[14] or 0), int(normalized_row[14] or 0))
                    for field_index in (3, 4, 5, 9, 11, 12, 13):
                        if survivor[field_index] is None:
                            survivor[field_index] = other[field_index]
                    reconciled_rows[canonical_id] = survivor
                for canonical_id, original_ids in original_ids_by_canonical_id.items():
                    reconciled_row = reconciled_rows[canonical_id]
                    if len(original_ids) == 1:
                        cursor.execute(
                            """
                            UPDATE twitch_clip_history
                            SET clip_id = %s, clip_url = %s
                            WHERE provider = 'twitch' AND clip_id = %s
                            """,
                            (canonical_id, reconciled_row[2], original_ids[0]),
                        )
                        continue
                    metadata_columns = tuple(additive_columns)
                    cursor.execute(
                        sql.SQL("SELECT clip_id, {} FROM twitch_clip_history "
                                "WHERE provider = 'twitch' AND clip_id = ANY(%s)")
                        .format(
                            sql.SQL(", ").join(
                                sql.Identifier(name) for name in metadata_columns
                            )
                        ),
                        (original_ids,),
                    )
                    metadata_rows = cursor.fetchall()
                    status_by_id = {
                        str(row[1]): str(row[8]) for row in history_rows
                    }
                    metadata_rows.sort(
                        key=lambda row: status_precedence.get(
                            status_by_id.get(str(row[0]), ""), 0
                        ),
                        reverse=True,
                    )
                    reconciled_metadata = [
                        next(
                            (
                                row[index]
                                for row in metadata_rows
                                if row[index] is not None
                            ),
                            None,
                        )
                        for index in range(1, len(metadata_columns) + 1)
                    ]
                    cursor.execute(
                        """
                        DELETE FROM twitch_clip_history
                        WHERE provider = 'twitch' AND clip_id = ANY(%s)
                        """,
                        (original_ids,),
                    )
                    cursor.execute(
                        """
                        INSERT INTO twitch_clip_history (
                            provider, clip_id, clip_url, creator_id, creator_name,
                            created_at, first_seen_at, last_processed_at, status,
                            viral_score, published_at, publish_attempted_at,
                            provider_publish_id, failure_stage, retry_count
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s
                        )
                        """,
                        reconciled_row,
                    )
                    cursor.execute(
                        sql.SQL("UPDATE twitch_clip_history SET {} "
                                "WHERE provider = 'twitch' AND clip_id = %s")
                        .format(
                            sql.SQL(", ").join(
                                sql.SQL("{} = %s").format(sql.Identifier(name))
                                for name in metadata_columns
                            )
                        ),
                        [*reconciled_metadata, canonical_id],
                    )
                for canonical_id, count in duplicate_counts.items():
                    if count > 1:
                        print(
                            "CLIP HISTORY MIGRATION DUPLICATE URL RECONCILED | "
                            f"clip_id={canonical_id} | rows={count}"
                        )
                cursor.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS twitch_clip_history_url_idx
                    ON twitch_clip_history (provider, clip_url)
                    WHERE clip_url IS NOT NULL
                    """
                )
                if not _backfill_clip_history(cursor):
                    raise RuntimeError("clip history backfill failed")
            connection.commit()
        print("MONITORED CREATORS READY")
        return True
    except Exception as error:
        print(f"CLIP HISTORY DB ERROR | operation=init | error={error!r}")
        return False


def _clip_history_upsert(
    clip: dict[str, object],
    status_value: str,
    failure_stage: object = None,
    increment_retry: bool = False,
) -> bool:
    clip_id, clip_url = _normalized_twitch_identifiers(clip)
    if not clip_id:
        return False
    if not DATABASE_URL:
        return True
    try:
        import psycopg

        with psycopg.connect(DATABASE_URL) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO twitch_clip_history (
                        provider, clip_id, clip_url, creator_id, creator_name,
                        created_at, status, viral_score, last_processed_at,
                        published_at, failure_stage, retry_count,
                        source_stream_id
                    )
                    VALUES (
                        'twitch', %s, %s, %s, %s, %s, %s, %s,
                        CASE WHEN %s = 'discovered' THEN NULL ELSE NOW() END,
                        CASE WHEN %s = 'published' THEN NOW() ELSE NULL END,
                        %s, CASE WHEN %s THEN 1 ELSE 0 END, %s
                    )
                    ON CONFLICT (provider, clip_id) DO UPDATE SET
                        clip_url = COALESCE(EXCLUDED.clip_url, twitch_clip_history.clip_url),
                        creator_id = COALESCE(EXCLUDED.creator_id, twitch_clip_history.creator_id),
                        creator_name = COALESCE(EXCLUDED.creator_name, twitch_clip_history.creator_name),
                        created_at = COALESCE(EXCLUDED.created_at, twitch_clip_history.created_at),
                        status = CASE
                            WHEN twitch_clip_history.status = 'published' THEN 'published'
                            WHEN EXCLUDED.status = 'discovered'
                                THEN twitch_clip_history.status
                            WHEN twitch_clip_history.status = 'generated'
                                 AND EXCLUDED.status IN ('discovered', 'processing')
                                THEN 'generated'
                            ELSE EXCLUDED.status
                        END,
                        viral_score = COALESCE(EXCLUDED.viral_score, twitch_clip_history.viral_score),
                        last_processed_at = COALESCE(EXCLUDED.last_processed_at, twitch_clip_history.last_processed_at),
                        published_at = COALESCE(EXCLUDED.published_at, twitch_clip_history.published_at),
                        failure_stage = EXCLUDED.failure_stage,
                        retry_count = twitch_clip_history.retry_count
                            + CASE WHEN %s THEN 1 ELSE 0 END,
                        source_stream_id = COALESCE(
                            EXCLUDED.source_stream_id,
                            twitch_clip_history.source_stream_id
                        )
                    """,
                    (
                        clip_id,
                        clip_url,
                        clip.get("creator_id") or clip.get("broadcaster_id"),
                        clip.get("creator") or clip.get("creator_name"),
                        clip.get("created_at"),
                        status_value,
                        clip.get("viral_score") or clip.get("score"),
                        status_value,
                        status_value,
                        failure_stage,
                        increment_retry,
                        increment_retry,
                        clip.get("_stream_id") or clip.get("source_stream_id"),
                    ),
                )
            connection.commit()
        print(
            "CLIP HISTORY STATUS UPDATED | "
            f"clip_id={clip_id} | status={status_value}"
        )
        return True
    except Exception as error:
        print(
            "CLIP HISTORY DB ERROR | "
            f"operation=status_update | clip_id={clip_id} | error={error!r}"
        )
        return False


def _claim_clip_for_processing(clip: dict[str, object]) -> bool:
    clip_id, clip_url = _normalized_twitch_identifiers(clip)
    if not clip_id:
        print("CLIP HISTORY CLAIM SKIPPED | reason=missing_canonical_clip_id")
        return False
    if not DATABASE_URL:
        return True
    lease_minutes = _processing_lease_minutes()
    try:
        import psycopg

        with psycopg.connect(DATABASE_URL) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT status, last_processed_at
                    FROM twitch_clip_history
                    WHERE provider = 'twitch' AND clip_id = %s
                    FOR UPDATE
                    """,
                    (clip_id,),
                )
                prior_row = cursor.fetchone()
                stale_processing = bool(
                    prior_row
                    and prior_row[0] == "processing"
                    and (
                        prior_row[1] is None
                        or prior_row[1]
                        < datetime.now(timezone.utc) - timedelta(minutes=lease_minutes)
                    )
                )
                cursor.execute(
                    """
                    INSERT INTO twitch_clip_history (
                        provider, clip_id, clip_url, creator_id, creator_name,
                        created_at, status, last_processed_at, retry_count
                    )
                    VALUES ('twitch', %s, %s, %s, %s, %s, 'processing', NOW(), 0)
                    ON CONFLICT (provider, clip_id) DO UPDATE SET
                        clip_url = COALESCE(
                            twitch_clip_history.clip_url, EXCLUDED.clip_url
                        ),
                        creator_id = COALESCE(
                            twitch_clip_history.creator_id, EXCLUDED.creator_id
                        ),
                        creator_name = COALESCE(
                            twitch_clip_history.creator_name, EXCLUDED.creator_name
                        ),
                        created_at = COALESCE(
                            twitch_clip_history.created_at, EXCLUDED.created_at
                        ),
                        status = 'processing',
                        last_processed_at = NOW(),
                        failure_stage = NULL
                    WHERE twitch_clip_history.status = 'discovered'
                       OR (
                            twitch_clip_history.status = 'failed'
                            AND twitch_clip_history.retry_count < %s
                       )
                       OR (
                            twitch_clip_history.status = 'processing'
                            AND (
                                twitch_clip_history.last_processed_at IS NULL
                                OR twitch_clip_history.last_processed_at
                                   < NOW() - (%s * INTERVAL '1 minute')
                            )
                       )
                    RETURNING clip_id
                    """,
                    (
                        clip_id,
                        clip_url,
                        clip.get("creator_id") or clip.get("broadcaster_id"),
                        clip.get("creator") or clip.get("creator_name"),
                        clip.get("created_at"),
                        AUTO_CLIP_HISTORY_MAX_RETRIES,
                        lease_minutes,
                    ),
                )
                claimed_row = cursor.fetchone()
            connection.commit()
        if claimed_row is None:
            print(f"CLIP HISTORY CLAIM SKIPPED | clip_id={clip_id}")
            return False
        if stale_processing:
            print(
                "CLIP HISTORY STALE PROCESSING RECLAIMED | "
                f"clip_id={clip_id} | lease_minutes={lease_minutes}"
            )
        return True
    except Exception as error:
        print(
            "CLIP HISTORY DB ERROR | "
            f"operation=claim | clip_id={clip_id} | error={error!r}"
        )
        print(f"CLIP HISTORY CLAIM SKIPPED | clip_id={clip_id}")
        return False


def _claim_clip_for_publishing(clip: dict[str, object]) -> bool:
    clip_id, _ = _normalized_twitch_identifiers(clip)
    if not clip_id:
        print("CLIP PUBLISH CLAIM SKIPPED | reason=invalid_identifier")
        return False
    if not DATABASE_URL:
        return True
    try:
        import psycopg

        with psycopg.connect(DATABASE_URL) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE twitch_clip_history
                    SET status = 'publishing',
                        publish_attempted_at = NOW(),
                        failure_stage = NULL
                    WHERE provider = 'twitch'
                      AND clip_id = %s
                      AND (
                          status IN (
                              'generated', 'ready_for_review', 'approved',
                              'scheduled', 'publish_failed'
                          )
                          OR (
                              status = 'publishing'
                              AND (
                                  publish_attempted_at IS NULL
                                  OR publish_attempted_at
                                     < NOW() - (%s * INTERVAL '1 minute')
                              )
                          )
                      )
                    RETURNING clip_id
                    """,
                    (clip_id, _publish_lease_minutes()),
                )
                claimed = cursor.fetchone()
            connection.commit()
        if claimed is None:
            print(f"CLIP PUBLISH CLAIM SKIPPED | clip_id={clip_id}")
            return False
        return True
    except Exception as error:
        print(
            "CLIP HISTORY DB ERROR | "
            f"operation=publish_claim | clip_id={clip_id} | error={error!r}"
        )
        print(f"CLIP PUBLISH CLAIM SKIPPED | clip_id={clip_id}")
        return False


def _restore_clip_after_publish_failure(clip: dict[str, object]) -> bool:
    clip_id, _ = _normalized_twitch_identifiers(clip)
    if not DATABASE_URL:
        return True
    try:
        import psycopg

        with psycopg.connect(DATABASE_URL) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE twitch_clip_history
                    SET status = 'publish_failed',
                        publish_attempted_at = NULL,
                        failure_stage = 'tiktok_publish'
                    WHERE provider = 'twitch'
                      AND clip_id = %s
                      AND status = 'publishing'
                    RETURNING clip_id
                    """,
                    (clip_id,),
                )
                restored = cursor.fetchone()
            connection.commit()
        return restored is not None
    except Exception as error:
        print(
            "CLIP HISTORY DB ERROR | "
            f"operation=publish_restore | clip_id={clip_id} | error={error!r}"
        )
        return False


def _mark_clip_published(
    clip: dict[str, object],
    provider_publish_id: str,
) -> bool:
    clip_id, _ = _normalized_twitch_identifiers(clip)
    if not clip_id:
        return False
    if not DATABASE_URL:
        return True
    try:
        import psycopg

        with psycopg.connect(DATABASE_URL) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE twitch_clip_history
                    SET status = 'published',
                        published_at = NOW(),
                        provider_publish_id = %s,
                        failure_stage = NULL
                    WHERE provider = 'twitch'
                      AND clip_id = %s
                      AND status = 'publishing'
                    RETURNING clip_id
                    """,
                    (provider_publish_id, clip_id),
                )
                published = cursor.fetchone()
            connection.commit()
        return published is not None
    except Exception as error:
        print(
            "CLIP HISTORY DB ERROR | "
            f"operation=publish_complete | clip_id={clip_id} | error={error!r}"
        )
        return False


def _mark_clip_uploaded_to_inbox(
    clip: dict[str, object],
    publish_id: str,
) -> bool:
    clip_id, _ = _normalized_twitch_identifiers(clip)
    if not DATABASE_URL:
        return True
    try:
        import psycopg

        with psycopg.connect(DATABASE_URL) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE twitch_clip_history SET
                        status = 'uploaded_to_inbox',
                        provider_publish_id = %s,
                        tiktok_publish_mode = 'draft',
                        tiktok_post_status = 'PROCESSING_UPLOAD',
                        tiktok_last_status_check = NULL,
                        failure_stage = NULL
                    WHERE provider = 'twitch' AND clip_id = %s
                      AND status = 'publishing'
                    RETURNING clip_id
                    """,
                    (publish_id, clip_id),
                )
                row = cursor.fetchone()
            connection.commit()
        return row is not None
    except Exception as error:
        print(
            "CLIP HISTORY DB ERROR | operation=draft_complete | "
            f"clip_id={clip_id} | error={error!r}"
        )
        return False


def _write_terminal_clip_history(
    clip: dict[str, object],
    status_value: str,
    failure_stage: object = None,
    increment_retry: bool = False,
) -> bool:
    written = _clip_history_upsert(
        clip,
        status_value,
        failure_stage=failure_stage,
        increment_retry=increment_retry,
    )
    if DATABASE_URL and not written:
        clip_id, _ = _normalized_twitch_identifiers(clip)
        print(
            "CLIP HISTORY TERMINAL STATUS WRITE FAILED | "
            f"clip_id={clip_id or 'missing'} | status={status_value} | "
            "history_not_safely_persisted=true"
        )
    return written


def _backfill_clip_history(cursor: object) -> bool:
    if not DATABASE_URL:
        return True
    sources = (
        (BASE_DIR / "clips.json", "generated"),
        (BASE_DIR / "published_clips.json", "published"),
    )
    imported = 0
    for source_path, default_status in sources:
        try:
            with source_path.open("r", encoding="utf-8") as file:
                records = json.load(file)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            records = []
        for record in records if isinstance(records, list) else []:
            if not isinstance(record, dict):
                continue
            clip_id, clip_url = _normalized_twitch_identifiers(record)
            if not clip_id:
                print(
                    "CLIP HISTORY DB ERROR | "
                    f"operation=backfill | source={source_path.name} | "
                    "error=missing_stable_twitch_identifier"
                )
                continue
            status_value = (
                "published"
                if str(record.get("status") or "").lower() == "published"
                else default_status
            )
            try:
                cursor.execute(
                    """
                    INSERT INTO twitch_clip_history (
                        provider, clip_id, clip_url, creator_id, creator_name,
                        created_at, status, viral_score, published_at,
                        failure_stage, retry_count
                    ) VALUES (
                        'twitch', %s, %s, %s, %s, %s, %s, %s, %s, %s, 0
                    )
                    ON CONFLICT (provider, clip_id) DO UPDATE SET
                        clip_url = COALESCE(
                            twitch_clip_history.clip_url, EXCLUDED.clip_url
                        ),
                        creator_id = COALESCE(
                            twitch_clip_history.creator_id, EXCLUDED.creator_id
                        ),
                        creator_name = COALESCE(
                            twitch_clip_history.creator_name, EXCLUDED.creator_name
                        ),
                        created_at = COALESCE(
                            twitch_clip_history.created_at, EXCLUDED.created_at
                        ),
                        viral_score = COALESCE(
                            twitch_clip_history.viral_score, EXCLUDED.viral_score
                        ),
                        published_at = COALESCE(
                            twitch_clip_history.published_at, EXCLUDED.published_at
                        ),
                        failure_stage = COALESCE(
                            twitch_clip_history.failure_stage, EXCLUDED.failure_stage
                        ),
                        status = CASE
                            WHEN twitch_clip_history.status IN (
                                'ready_for_review', 'approved', 'scheduled',
                                'publishing', 'uploaded_to_inbox', 'published',
                                'publish_failed', 'rejected', 'archived'
                            ) THEN twitch_clip_history.status
                            WHEN CASE twitch_clip_history.status
                                WHEN 'published' THEN 9 WHEN 'generated' THEN 8
                                WHEN 'publishing' THEN 7
                                WHEN 'rejected_low_score' THEN 6
                                WHEN 'fully_evaluated' THEN 5
                                WHEN 'processing' THEN 4 WHEN 'failed' THEN 3
                                WHEN 'intentionally_skipped' THEN 2 ELSE 1 END
                              >= CASE EXCLUDED.status
                                WHEN 'published' THEN 9 WHEN 'generated' THEN 8
                                WHEN 'publishing' THEN 7
                                WHEN 'rejected_low_score' THEN 6
                                WHEN 'fully_evaluated' THEN 5
                                WHEN 'processing' THEN 4 WHEN 'failed' THEN 3
                                WHEN 'intentionally_skipped' THEN 2 ELSE 1 END
                            THEN twitch_clip_history.status
                            ELSE EXCLUDED.status
                        END
                    """,
                    (
                        clip_id,
                        clip_url,
                        record.get("creator_id") or record.get("broadcaster_id"),
                        record.get("creator") or record.get("creator_name"),
                        record.get("created_at"),
                        status_value,
                        record.get("viral_score") or record.get("score"),
                        record.get("published_at"),
                        record.get("failure_stage"),
                    ),
                )
                imported += 1
            except Exception as error:
                print(
                    "CLIP HISTORY DB ERROR | "
                    f"operation=backfill | clip_id={clip_id} | error={error!r}"
                )
                return False
    print(f"CLIP HISTORY BACKFILL | records={imported}")
    return True


def _load_clip_history_exclusions() -> tuple[set[str], set[str], bool]:
    if not DATABASE_URL:
        clips: list[dict[str, object]] = []
        for path in (BASE_DIR / "clips.json", BASE_DIR / "published_clips.json"):
            try:
                with path.open("r", encoding="utf-8") as file:
                    payload = json.load(file)
                if isinstance(payload, list):
                    clips.extend(item for item in payload if isinstance(item, dict))
            except (FileNotFoundError, OSError, json.JSONDecodeError):
                pass
        return (
            {
                identifiers[0]
                for item in clips
                if (identifiers := _normalized_twitch_identifiers(item))[0]
            },
            {
                identifiers[1]
                for item in clips
                if (identifiers := _normalized_twitch_identifiers(item))[1]
            },
            True,
        )
    try:
        import psycopg

        with psycopg.connect(DATABASE_URL) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT clip_id, clip_url FROM twitch_clip_history
                    WHERE provider = 'twitch' AND (
                        status IN (
                            'fully_evaluated', 'rejected_low_score',
                            'generated', 'publishing', 'published',
                            'intentionally_skipped', 'ready_for_review',
                            'approved', 'scheduled', 'uploaded_to_inbox',
                            'publish_failed', 'rejected', 'archived'
                        )
                        OR (
                            status = 'processing'
                            AND last_processed_at >=
                                NOW() - (%s * INTERVAL '1 minute')
                        )
                        OR (status = 'failed' AND retry_count >= %s)
                    )
                    """,
                    (
                        _processing_lease_minutes(),
                        AUTO_CLIP_HISTORY_MAX_RETRIES,
                    ),
                )
                rows = cursor.fetchall()
        normalized_rows = [
            _normalized_twitch_identifiers(
                {
                    "twitch_clip_id": row[0],
                    "public_url": row[1],
                }
            )
            for row in rows
        ]
        return (
            {clip_id for clip_id, _ in normalized_rows if clip_id},
            {clip_url for _, clip_url in normalized_rows if clip_url},
            True,
        )
    except Exception as error:
        print(f"CLIP HISTORY DB ERROR | operation=load | error={error!r}")
        return set(), set(), False


def _ensure_oauth_tokens_table() -> None:
    if not DATABASE_URL:
        return

    try:
        import psycopg

        with psycopg.connect(DATABASE_URL) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS oauth_tokens (
                        provider TEXT PRIMARY KEY,
                        token_data JSONB NOT NULL,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
            connection.commit()
    except Exception as error:
        print(f"OAUTH TOKEN STORAGE INIT FAILED: {error.__class__.__name__}")


def _load_token_data_from_file(token_file: Path) -> dict[str, object] | None:
    try:
        with token_file.open("r", encoding="utf-8") as file:
            payload = json.load(file)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None

    if not isinstance(payload, dict):
        return None
    return payload


def _save_token_data_to_file(token_file: Path, token_data: dict[str, object]) -> None:
    try:
        with token_file.open("w", encoding="utf-8") as file:
            json.dump(token_data, file, indent=2)
    except OSError as error:
        print(f"OAUTH TOKEN FILE SAVE FAILED: {error.__class__.__name__}")


def _load_oauth_token_data(provider: str) -> dict[str, object] | None:
    if not DATABASE_URL:
        return None

    try:
        import psycopg

        with psycopg.connect(DATABASE_URL) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT token_data FROM oauth_tokens WHERE provider = %s",
                    (provider,),
                )
                row = cursor.fetchone()
    except Exception as error:
        print(
            f"OAUTH TOKEN DB LOAD FAILED: provider={provider} "
            f"error={error.__class__.__name__}"
        )
        return None

    if not row:
        return None

    token_data = row[0]
    if isinstance(token_data, dict):
        return token_data
    return None


def _save_oauth_token_data(provider: str, token_data: dict[str, object]) -> bool:
    if not DATABASE_URL:
        return False

    try:
        import psycopg
        from psycopg.types.json import Jsonb

        with psycopg.connect(DATABASE_URL) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO oauth_tokens (provider, token_data, updated_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (provider)
                    DO UPDATE SET
                        token_data = EXCLUDED.token_data,
                        updated_at = NOW()
                    """,
                    (provider, Jsonb(token_data)),
                )
            connection.commit()
        return True
    except Exception as error:
        print(
            f"OAUTH TOKEN DB SAVE FAILED: provider={provider} "
            f"error={error.__class__.__name__}"
        )
        return False


def _load_provider_token_data(
    provider: str,
    token_file: Path,
) -> dict[str, object] | None:
    token_data = _load_oauth_token_data(provider)
    if token_data is not None:
        return token_data

    fallback_token_data = _load_token_data_from_file(token_file)
    if fallback_token_data is None:
        return None

    _save_oauth_token_data(provider, fallback_token_data)
    return fallback_token_data


def _save_provider_token_data(
    provider: str,
    token_file: Path,
    token_data: dict[str, object],
) -> None:
    _save_oauth_token_data(provider, token_data)
    _save_token_data_to_file(token_file, token_data)


def _is_within_directory(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False


def _resolve_allowed_video_path(video_path: str) -> Path:
    if not isinstance(video_path, str) or not video_path.strip():
        raise HTTPException(status_code=400, detail="Clip is missing video_path.")

    raw_path = Path(video_path.strip())
    candidate_paths = (
        [raw_path]
        if raw_path.is_absolute()
        else [BASE_DIR.parent / raw_path, BASE_DIR / raw_path]
    )

    allowed_roots = [
        (BASE_DIR.parent / "downloads").resolve(),
        (BASE_DIR / "downloads").resolve(),
    ]

    allowed_candidate_exists = False

    for candidate_path in candidate_paths:
        resolved_candidate = candidate_path.resolve(strict=False)

        if not any(
            _is_within_directory(resolved_candidate, root)
            for root in allowed_roots
        ):
            continue

        allowed_candidate_exists = True
        if resolved_candidate.is_file():
            return resolved_candidate

    if allowed_candidate_exists:
        raise HTTPException(status_code=404, detail="Video file no longer exists.")

    raise HTTPException(status_code=403, detail="Unsafe video path.")


def _build_safe_download_filename(clip: dict, clip_id: str) -> str:
    name_source = str(clip.get("ai_title") or clip.get("title") or clip_id)
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", name_source).strip("._")

    if not safe_stem:
        safe_stem = f"clip_{clip_id}"

    if not safe_stem.lower().endswith(".mp4"):
        safe_stem = f"{safe_stem}.mp4"

    return safe_stem

DEFAULT_CREATORS = [
    {
        "name": "Kai Cenat",
        "channel": "kaicenat",
    },
    {
        "name": "xQc",
        "channel": "xqc",
    },
]


class CreatorCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    channel: str = Field(min_length=1, max_length=100)


def clean_channel_name(channel: str) -> str:
    return channel.strip().removeprefix("@").lower()


def _load_creators_from_json() -> list[dict[str, str]]:
    if not CREATORS_FILE.exists():
        return DEFAULT_CREATORS.copy()
    try:
        with CREATORS_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (json.JSONDecodeError, OSError) as error:
        raise HTTPException(
            status_code=500,
            detail=f"Unable to read creators.json: {error}",
        ) from error
    creators = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        channel = clean_channel_name(str(item.get("channel", "")))
        if name and channel:
            creators.append({"name": name, "channel": channel})
    return creators


def _backfill_monitored_creators(cursor: object) -> None:
    creators = _load_creators_from_json()
    inserted = 0
    for priority, creator in enumerate(creators):
        cursor.execute(
            """
            INSERT INTO monitored_creators (
                login, display_name, enabled, priority
            ) VALUES (%s, %s, TRUE, %s)
            ON CONFLICT (login) DO NOTHING
            RETURNING login
            """,
            (creator["channel"], creator["name"], priority),
        )
        if cursor.fetchone():
            inserted += 1
    print(
        "MONITORED CREATOR BACKFILL | "
        f"source_records={len(creators)} | inserted={inserted}"
    )


def load_creators() -> list[dict[str, str]]:
    if not DATABASE_URL:
        return _load_creators_from_json()
    if not getattr(app.state, "clip_history_ready", False):
        raise HTTPException(status_code=503, detail="Creator storage is unavailable.")
    try:
        import psycopg

        with psycopg.connect(DATABASE_URL) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT display_name, login FROM monitored_creators
                    WHERE enabled = TRUE
                    ORDER BY priority, created_at, id
                    """
                )
                rows = cursor.fetchall()
        return [{"name": row[0], "channel": row[1]} for row in rows]
    except Exception as error:
        print(f"MONITORED CREATOR LOAD FAILED | error={error!r}")
        raise HTTPException(
            status_code=503,
            detail="Creator storage is unavailable.",
        ) from error


def save_creators(creators: list[dict[str, str]]) -> None:
    try:
        with CREATORS_FILE.open("w", encoding="utf-8") as file:
            json.dump(creators, file, indent=2)
    except OSError as error:
        raise HTTPException(
            status_code=500,
            detail=f"Unable to save creators.json: {error}",
        ) from error


def _normalize_cursor_index(raw_index: object, creator_count: int) -> int:
    if creator_count <= 0:
        return 0

    try:
        normalized = int(raw_index)
    except (TypeError, ValueError):
        return 0

    return normalized % creator_count


def _load_creator_cursor(creator_count: int) -> int:
    if creator_count <= 0:
        return 0

    try:
        with CREATOR_CURSOR_FILE.open("r", encoding="utf-8") as file:
            payload = json.load(file)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return 0

    if not isinstance(payload, dict):
        return 0

    return _normalize_cursor_index(payload.get("next_index", 0), creator_count)


def _save_creator_cursor(next_index: int, creator_count: int) -> None:
    if creator_count <= 0:
        return

    normalized_next_index = _normalize_cursor_index(next_index, creator_count)
    payload = {"next_index": normalized_next_index}

    try:
        with CREATOR_CURSOR_FILE.open("w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2)
    except OSError as error:
        print(f"ROUND ROBIN | cursor_persist_failed={error}")


PUBLISHED_FILE = "published_clips.json"


def load_published():
    try:
        with open(PUBLISHED_FILE, "r") as file:
            return json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_published(data):
    with open(PUBLISHED_FILE, "w") as file:
        json.dump(data, file, indent=2)


def _normalize_clip_status_value(status_value: object) -> str:
    status_text = str(status_value or "").strip().lower()
    if status_text == "published":
        return "Published"
    if status_text == "ready to review":
        return "Ready to review"
    return ""


def _is_clip_published(clip: dict[str, object]) -> bool:
    return _normalize_clip_status_value(clip.get("status")) == "Published"


def _get_stable_clip_identifiers(clip: dict[str, object]) -> dict[str, str]:
    identifiers: dict[str, str] = {}
    for key in ("id", "twitch_clip_id", "public_url"):
        value = clip.get(key)
        if value is None:
            continue
        normalized_value = str(value).strip()
        if normalized_value:
            identifiers[key] = normalized_value
    return identifiers


def _find_clip_index_by_stable_identifier(
    clips: list[dict[str, object]],
    target_clip: dict[str, object],
) -> int | None:
    target_identifiers = _get_stable_clip_identifiers(target_clip)
    if not target_identifiers:
        return None

    clip_id = target_identifiers.get("id")
    if clip_id:
        for index, existing_clip in enumerate(clips):
            if str(existing_clip.get("id", "")).strip() == clip_id:
                return index

    twitch_clip_id = target_identifiers.get("twitch_clip_id")
    if twitch_clip_id:
        for index, existing_clip in enumerate(clips):
            if str(existing_clip.get("twitch_clip_id", "")).strip() == twitch_clip_id:
                return index

    public_url = target_identifiers.get("public_url")
    if public_url:
        for index, existing_clip in enumerate(clips):
            if str(existing_clip.get("public_url", "")).strip() == public_url:
                return index

    return None


def _contains_clip_with_same_identifier(
    clip_collection: list[dict[str, object]],
    target_clip: dict[str, object],
) -> bool:
    target_identifiers = _get_stable_clip_identifiers(target_clip)
    if not target_identifiers:
        return False

    for existing_clip in clip_collection:
        existing_identifiers = _get_stable_clip_identifiers(existing_clip)
        if not existing_identifiers:
            continue
        for key in ("id", "twitch_clip_id", "public_url"):
            if (
                key in target_identifiers
                and key in existing_identifiers
                and target_identifiers[key] == existing_identifiers[key]
            ):
                return True

    return False


def _derive_emphasis_moments(
    transcript_segments: list[dict[str, object]],
    duration_seconds: float,
) -> list[dict[str, float]]:
    safe_duration = max(0.0, float(duration_seconds or 0.0))
    opening_end = 2.0
    if safe_duration > 0.0:
        opening_end = min(2.2, max(1.5, safe_duration * 0.08))

    moments: list[dict[str, float]] = [
        {
            "start": 0.0,
            "end": opening_end,
            "zoom": 1.10,
        }
    ]

    if not isinstance(transcript_segments, list) or not transcript_segments:
        if safe_duration > 10.0:
            midpoint_start = max(2.5, (safe_duration * 0.5) - 0.8)
            moments.append(
                {
                    "start": midpoint_start,
                    "end": min(midpoint_start + 1.4, safe_duration),
                    "zoom": 1.06,
                }
            )
        return moments

    emphasis_keywords = {
        "wow",
        "no way",
        "what",
        "wait",
        "clutch",
        "insane",
        "crazy",
        "scream",
        "screamed",
        "laugh",
        "lmao",
        "lol",
        "omg",
        "bro",
        "lets go",
        "let's go",
        "1v",
        "ace",
    }

    scored_candidates: list[tuple[float, float]] = []
    for segment in transcript_segments[:80]:
        if not isinstance(segment, dict):
            continue

        text = str(segment.get("text", "") or "").strip()
        if not text:
            continue

        try:
            start = float(segment.get("start", 0.0) or 0.0)
            end = float(segment.get("end", start) or start)
        except (TypeError, ValueError):
            continue

        segment_duration = max(0.6, end - start)
        segment_center = start + (segment_duration * 0.45)

        lowered_text = text.lower()
        score = 0.0
        for keyword in emphasis_keywords:
            if keyword in lowered_text:
                score += 1.0
        if "!" in text:
            score += 0.6
        if text.isupper() and len(text) > 5:
            score += 0.6
        if len(text.split()) <= 4:
            score += 0.2

        if score <= 0:
            continue
        scored_candidates.append((score, max(0.0, segment_center)))

    scored_candidates.sort(key=lambda item: item[0], reverse=True)

    chosen_centers: list[float] = []
    for score, center in scored_candidates:
        if len(chosen_centers) >= 3:
            break
        if center < opening_end + 0.6:
            continue
        if any(abs(center - existing) < 2.5 for existing in chosen_centers):
            continue
        chosen_centers.append(center)

    chosen_centers.sort()
    for center in chosen_centers:
        start = max(opening_end + 0.3, center - 0.7)
        end = start + 1.4
        if safe_duration > 0.0:
            if start >= safe_duration:
                continue
            end = min(end, safe_duration)
        moments.append(
            {
                "start": start,
                "end": end,
                "zoom": 1.06,
            }
        )

    return moments[:4]


def verify_twitch_credentials() -> None:
    if not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
        raise HTTPException(
            status_code=500,
            detail="Twitch credentials are missing from backend/.env",
        )


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return int(float(stripped))
        except ValueError:
            return None
    return None


def _extract_tiktok_token_fields(token_data: dict[str, object]) -> dict[str, object]:
    data_payload = token_data.get("data")
    data = data_payload if isinstance(data_payload, dict) else {}

    def pick_field(field_name: str) -> object:
        value = data.get(field_name)
        if value is not None:
            return value
        return token_data.get(field_name)

    return {
        "access_token": pick_field("access_token"),
        "refresh_token": pick_field("refresh_token"),
        "expires_in": pick_field("expires_in"),
        "refresh_expires_in": pick_field("refresh_expires_in"),
        "open_id": pick_field("open_id"),
        "scope": pick_field("scope"),
        "expires_at": pick_field("expires_at"),
        "issued_at": pick_field("issued_at"),
    }


def _normalize_tiktok_token_data(
    token_data: dict[str, object],
    existing_token_data: dict[str, object] | None = None,
) -> dict[str, object]:
    merged_token_data: dict[str, object] = {}
    if isinstance(existing_token_data, dict):
        merged_token_data.update(existing_token_data)
    merged_token_data.update(token_data)

    existing_fields = (
        _extract_tiktok_token_fields(existing_token_data)
        if isinstance(existing_token_data, dict)
        else {}
    )
    fields = _extract_tiktok_token_fields(merged_token_data)

    access_token = fields.get("access_token")
    if not access_token and existing_fields:
        access_token = existing_fields.get("access_token")

    refresh_token = fields.get("refresh_token")
    if not refresh_token and existing_fields:
        refresh_token = existing_fields.get("refresh_token")

    expires_in = _coerce_int(fields.get("expires_in"))
    if expires_in is None and existing_fields:
        expires_in = _coerce_int(existing_fields.get("expires_in"))

    refresh_expires_in = _coerce_int(fields.get("refresh_expires_in"))
    if refresh_expires_in is None and existing_fields:
        refresh_expires_in = _coerce_int(existing_fields.get("refresh_expires_in"))

    open_id = fields.get("open_id")
    if open_id is None and existing_fields:
        open_id = existing_fields.get("open_id")

    scope = fields.get("scope")
    if scope is None and existing_fields:
        scope = existing_fields.get("scope")

    now = int(time.time())
    merged_token_data["issued_at"] = now
    if access_token:
        merged_token_data["access_token"] = access_token
    if refresh_token:
        merged_token_data["refresh_token"] = refresh_token
    if expires_in is not None:
        merged_token_data["expires_in"] = expires_in
    if refresh_expires_in is not None:
        merged_token_data["refresh_expires_in"] = refresh_expires_in
    if open_id is not None:
        merged_token_data["open_id"] = open_id
    if scope is not None:
        merged_token_data["scope"] = scope

    if expires_in is not None and access_token:
        merged_token_data["expires_at"] = now + expires_in

    data_payload = merged_token_data.get("data")
    data = dict(data_payload) if isinstance(data_payload, dict) else {}
    if access_token:
        data["access_token"] = access_token
    if refresh_token:
        data["refresh_token"] = refresh_token
    if expires_in is not None:
        data["expires_in"] = expires_in
    if refresh_expires_in is not None:
        data["refresh_expires_in"] = refresh_expires_in
    if open_id is not None:
        data["open_id"] = open_id
    if scope is not None:
        data["scope"] = scope
    data["issued_at"] = merged_token_data["issued_at"]
    if "expires_at" in merged_token_data:
        data["expires_at"] = merged_token_data["expires_at"]
    merged_token_data["data"] = data

    return merged_token_data


def _is_tiktok_access_token_expiring(token_data: dict[str, object], skew_seconds: int) -> bool:
    fields = _extract_tiktok_token_fields(token_data)
    expires_at = _coerce_int(fields.get("expires_at"))
    if expires_at is None:
        return False
    return expires_at <= int(time.time()) + skew_seconds


def _reconnect_tiktok_exception() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail=TIKTOK_RECONNECT_REQUIRED_MESSAGE,
    )


def _refresh_tiktok_user_access_token(
    current_token_data: dict[str, object] | None = None,
) -> dict[str, object]:
    token_data = current_token_data
    if token_data is None:
        token_data = _load_provider_token_data("tiktok", TIKTOK_USER_TOKEN_FILE)

    if token_data is None:
        raise _reconnect_tiktok_exception()

    fields = _extract_tiktok_token_fields(token_data)
    refresh_token = fields.get("refresh_token")
    if not refresh_token:
        raise _reconnect_tiktok_exception()

    client_key = os.getenv("TIKTOK_CLIENT_KEY")
    client_secret = os.getenv("TIKTOK_CLIENT_SECRET")
    if not client_key or not client_secret:
        raise HTTPException(
            status_code=500,
            detail="TikTok OAuth configuration is incomplete.",
        )

    response = httpx.post(
        "https://open.tiktokapis.com/v2/oauth/token/",
        data={
            "client_key": client_key,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=15.0,
    )

    if response.status_code != 200:
        raise _reconnect_tiktok_exception()

    refreshed_payload = response.json()
    if not isinstance(refreshed_payload, dict):
        raise _reconnect_tiktok_exception()

    refreshed_token_data = _normalize_tiktok_token_data(
        refreshed_payload,
        existing_token_data=token_data,
    )
    refreshed_fields = _extract_tiktok_token_fields(refreshed_token_data)
    if not refreshed_fields.get("access_token"):
        raise _reconnect_tiktok_exception()

    _save_provider_token_data("tiktok", TIKTOK_USER_TOKEN_FILE, refreshed_token_data)
    return refreshed_token_data


def _response_contains_access_token_invalid(response: httpx.Response) -> bool:
    if response.status_code == 401:
        return True

    response_text = (response.text or "").lower()
    if "access_token_invalid" in response_text:
        return True

    try:
        payload = response.json()
    except ValueError:
        return False

    if isinstance(payload, dict):
        token_error_value = payload.get("error")
        if isinstance(token_error_value, str) and "access_token_invalid" in token_error_value.lower():
            return True

        if isinstance(token_error_value, dict):
            for key in ("code", "message"):
                value = token_error_value.get(key)
                if isinstance(value, str) and "access_token_invalid" in value.lower():
                    return True

        for key in ("code", "message", "error_description"):
            value = payload.get(key)
            if isinstance(value, str) and "access_token_invalid" in value.lower():
                return True

    return False

def get_twitch_user_access_token() -> str:
    token_data = _load_provider_token_data("twitch", TWITCH_USER_TOKEN_FILE)

    if token_data is None:
        raise HTTPException(
            status_code=401,
            detail="Twitch account is not connected. Visit /auth/twitch first.",
        )

    access_token = token_data.get("access_token")

    if not access_token:
        raise HTTPException(
            status_code=401,
            detail="Twitch user access token is missing.",
        )

    return access_token

def refresh_twitch_user_access_token() -> str:
    token_data = _load_provider_token_data("twitch", TWITCH_USER_TOKEN_FILE)
    if token_data is None:
        raise HTTPException(
            status_code=401,
            detail="Twitch account is not connected. Visit /auth/twitch first.",
        )

    refresh_token = token_data.get("refresh_token")

    if not refresh_token:
        raise HTTPException(
            status_code=401,
            detail="Twitch refresh token is missing. Reconnect Twitch.",
        )

    response = httpx.post(
        "https://id.twitch.tv/oauth2/token",
        params={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": TWITCH_CLIENT_ID,
            "client_secret": TWITCH_CLIENT_SECRET,
        },
        timeout=15.0,
    )

    if response.status_code != 200:
        raise HTTPException(
            status_code=401,
            detail=f"Twitch token refresh failed: {response.text}",
        )

    refreshed_data = response.json()

    token_data["access_token"] = refreshed_data["access_token"]
    token_data["refresh_token"] = refreshed_data.get(
        "refresh_token",
        refresh_token,
    )
    token_data["expires_in"] = refreshed_data.get("expires_in")
    token_data["scope"] = refreshed_data.get(
        "scope",
        token_data.get("scope", []),
    )

    _save_provider_token_data("twitch", TWITCH_USER_TOKEN_FILE, token_data)

    return token_data["access_token"]

async def get_twitch_access_token() -> str:
    verify_twitch_credentials()

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            "https://id.twitch.tv/oauth2/token",
            params={
                "client_id": TWITCH_CLIENT_ID,
                "client_secret": TWITCH_CLIENT_SECRET,
                "grant_type": "client_credentials",
            },
        )

    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Twitch authentication failed: {response.text}",
        )

    token_data = response.json()
    access_token = token_data.get("access_token")

    if not access_token:
        raise HTTPException(
            status_code=502,
            detail="Twitch did not return an access token.",
        )

    return access_token


async def get_twitch_channel_data(channel_name: str) -> dict[str, Any]:
    clean_channel = clean_channel_name(channel_name)

    if not clean_channel:
        raise HTTPException(
            status_code=400,
            detail="A Twitch channel name is required.",
        )

    try:
        access_token = await get_twitch_access_token()

        headers = {
            "Client-Id": TWITCH_CLIENT_ID,
            "Authorization": f"Bearer {access_token}",
        }

        timeout = httpx.Timeout(15.0, connect=20.0)

        async with httpx.AsyncClient(timeout=timeout) as client:
            user_response = await client.get(
                "https://api.twitch.tv/helix/users",
                headers=headers,
                params={"login": clean_channel},
            )

            if user_response.status_code != 200:
                raise HTTPException(
                    status_code=502,
                    detail=f"Twitch user request failed: {user_response.text}",
                )

            users = user_response.json().get("data", [])

            if not users:
                raise HTTPException(
                    status_code=404,
                    detail=f'Twitch channel "{clean_channel}" was not found.',
                )

            user = users[0]

            stream_response = await client.get(
                "https://api.twitch.tv/helix/streams",
                headers=headers,
                params={"user_login": clean_channel},
            )

            if stream_response.status_code != 200:
                raise HTTPException(
                    status_code=502,
                    detail=f"Twitch stream request failed: {stream_response.text}",
                )

            videos_response = await client.get(
                "https://api.twitch.tv/helix/videos",
                headers=headers,
                params={
                    "user_id": user["id"],
                    "type": "archive",
                    "first": 100,
                },
            )
            if videos_response.status_code != 200:
                raise HTTPException(
                    status_code=502,
                    detail=f"Twitch videos request failed: {videos_response.text}",
                )

        streams = stream_response.json().get("data", [])
        completed_streams = []
        for video in videos_response.json().get("data", []):
            started_at = _parse_twitch_created_at(
                video.get("streamed_at") or video.get("created_at")
            )
            if started_at is None:
                continue
            duration_seconds = _parse_twitch_duration_seconds(
                video.get("duration")
            )
            completed_streams.append(
                {
                    "stream_id": str(video.get("stream_id") or video.get("id") or ""),
                    "video_id": str(video.get("id") or ""),
                    "started_at": started_at,
                    "ended_at": (
                        started_at + timedelta(seconds=duration_seconds)
                        if duration_seconds
                        else started_at
                    ),
                    "title": video.get("title"),
                    "game_name": None,
                    "thumbnail_url": video.get("thumbnail_url"),
                    "is_live": False,
                }
            )
        completed_streams.sort(
            key=lambda item: item["started_at"],
            reverse=True,
        )

        print("Checking channel:", clean_channel)
        print("Streams returned:", streams)

        if not streams:
            return {
                "channel": user["login"],
                "user_id": user["id"],
                "display_name": user["display_name"],
                "profile_image_url": user["profile_image_url"],
                "is_live": False,
                "stream_id": None,
                "status": "OFFLINE",
                "title": None,
                "game_name": None,
                "viewer_count": 0,
                "started_at": None,
                "thumbnail_url": None,
                "completed_streams": completed_streams,
                "newest_completed_stream": (
                    completed_streams[0] if completed_streams else None
                ),
            }

        stream = streams[0]
        thumbnail_url = stream.get("thumbnail_url")

        if thumbnail_url:
            thumbnail_url = (
                thumbnail_url.replace("{width}", "640")
                .replace("{height}", "360")
            )

        return {
            "channel": user["login"],
            "user_id": user["id"],
            "display_name": user["display_name"],
            "profile_image_url": user["profile_image_url"],
            "is_live": True,
            "stream_id": stream.get("id"),
            "status": "LIVE",
            "title": stream.get("title"),
            "game_name": stream.get("game_name"),
            "viewer_count": stream.get("viewer_count", 0),
            "started_at": stream.get("started_at"),
            "thumbnail_url": thumbnail_url,
            "completed_streams": completed_streams,
            "newest_completed_stream": (
                completed_streams[0] if completed_streams else None
            ),
        }

    except httpx.RequestError as error:
        print(f"TWITCH CONNECTION ERROR for {clean_channel}: {repr(error)}")

        return {
            "channel": clean_channel,
            "user_id": None,
            "display_name": clean_channel,
            "profile_image_url": None,
            "is_live": False,
            "status": "UNAVAILABLE",
            "title": None,
            "game_name": None,
            "viewer_count": 0,
            "started_at": None,
            "thumbnail_url": None,
        }

async def create_twitch_clip(broadcaster_id: str) -> dict:
    def get_twitch_clip_url(twitch_clip_id: str) -> str:
        return f"https://clips.twitch.tv/{twitch_clip_id}"

    user_access_token = get_twitch_user_access_token()

    headers = {
        "Client-Id": TWITCH_CLIENT_ID,
        "Authorization": f"Bearer {user_access_token}",
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            "https://api.twitch.tv/helix/clips",
            headers=headers,
            params={"broadcaster_id": broadcaster_id},
        )

        if response.status_code == 401:
            user_access_token = refresh_twitch_user_access_token()
            headers["Authorization"] = f"Bearer {user_access_token}"

            response = await client.post(
                "https://api.twitch.tv/helix/clips",
                headers=headers,
                params={"broadcaster_id": broadcaster_id},
            )

    print("TWITCH CLIP STATUS:", response.status_code)
    print("TWITCH CLIP RESPONSE:", response.text)

    if response.status_code != 202:
        raise HTTPException(
            status_code=502,
            detail=f"Twitch clip creation failed: {response.text}",
        )

    clips = response.json().get("data", [])

    if not clips:
        raise HTTPException(
            status_code=502,
            detail="Twitch did not return clip data.",
        )

    clip = clips[0]

    clip["public_url"] = get_twitch_clip_url(clip["id"])

    return clip


async def wait_for_twitch_clip(clip_id: str) -> dict:
    try:
        access_token = await get_twitch_access_token()
    except Exception as error:
        print(
            f"TWITCH CLIP AVAILABILITY CHECK FAILED for {clip_id}:",
            repr(error),
        )
        return None

    headers = {
        "Client-Id": TWITCH_CLIENT_ID,
        "Authorization": f"Bearer {access_token}",
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        for attempt in range(8):
            try:
                response = await client.get(
                    "https://api.twitch.tv/helix/clips",
                    headers=headers,
                    params={"id": clip_id},
                )
            except httpx.RequestError as error:
                print(
                    f"TWITCH CLIP AVAILABILITY CHECK FAILED for {clip_id}:",
                    repr(error),
                )
                return None

            if response.status_code == 200:
                clips = response.json().get("data", [])
                if clips:
                    return clips[0]
            else:
                print(
                    f"TWITCH CLIP AVAILABILITY CHECK FAILED for {clip_id}: "
                    f"HTTP {response.status_code} {response.text}"
                )
                return None

            if attempt < 7:
                await asyncio.sleep(2)

    print(
        f"TWITCH CLIP UNAVAILABLE after 15 seconds; skipping candidate {clip_id}"
    )
    return None


def _auto_clip_max_streams_to_search() -> int:
    return _bounded_environment_int(
        "AUTO_CLIP_MAX_STREAMS_TO_SEARCH",
        default=10,
        minimum=1,
        maximum=100,
    )


def _auto_clip_lookback_days() -> int:
    return _bounded_environment_int(
        "AUTO_CLIP_LOOKBACK_DAYS",
        default=30,
        minimum=1,
        maximum=365,
    )


def _select_stream_search_target(
    creator_id: str,
    channel_data: dict[str, Any],
    *,
    historical_only: bool,
    job_skipped_stream_ids: frozenset[str] = frozenset(),
) -> dict[str, Any] | None:
    now = datetime.now(timezone.utc)
    completed_streams = [
        dict(stream)
        for stream in channel_data.get("completed_streams", [])
        if isinstance(stream, dict)
        and isinstance(stream.get("started_at"), datetime)
        and str(stream.get("stream_id") or "")
    ]
    if channel_data.get("is_live") and channel_data.get("stream_id"):
        newest = {
            "stream_id": str(channel_data["stream_id"]),
            "video_id": "",
            "started_at": _parse_twitch_created_at(channel_data.get("started_at")),
            "ended_at": None,
            "title": channel_data.get("title"),
            "game_name": channel_data.get("game_name"),
            "thumbnail_url": channel_data.get("thumbnail_url"),
            "is_live": True,
        }
    else:
        newest = (
            dict(completed_streams[0])
            if completed_streams
            else None
        )
    if not newest or not isinstance(newest.get("started_at"), datetime):
        return None

    newest_state = register_newest_stream(creator_id, newest)
    if not historical_only:
        print(
            "AUTO CLIP NEWEST STREAM REFRESH | "
            f"creator_id={creator_id} | stream_id={newest['stream_id']} | "
            f"is_live={str(bool(newest.get('is_live'))).lower()}"
        )
        newest["_stream_state"] = newest_state
        newest["_is_newest"] = True
        return newest

    cursor = get_historical_cursor(creator_id)
    before_timestamp = cursor.get("next_before_timestamp") or newest["started_at"]
    exhausted_ids = get_exhausted_stream_ids(creator_id)
    lookback_cutoff = now - timedelta(days=_auto_clip_lookback_days())
    print(
        "AUTO CLIP HISTORICAL RESUME | "
        f"creator_id={creator_id} | "
        f"before={before_timestamp.isoformat()} | "
        f"last_stream_id={cursor.get('last_stream_id') or 'none'}"
    )
    for stream in completed_streams:
        stream_id = str(stream["stream_id"])
        started_at = stream["started_at"]
        if started_at >= before_timestamp or started_at < lookback_cutoff:
            continue
        if stream_id == str(newest["stream_id"]):
            continue
        if stream_id in job_skipped_stream_ids:
            continue
        if stream_id in exhausted_ids:
            print(
                "AUTO CLIP EXHAUSTED STREAM SKIPPED | "
                f"creator_id={creator_id} | stream_id={stream_id}"
            )
            continue
        state = get_stream_state(creator_id, stream_id)
        if state is None:
            state = register_historical_stream(creator_id, stream)
        stream["_stream_state"] = state
        stream["_is_newest"] = False
        return stream
    return None


def _stream_discovery_start(
    stream_target: dict[str, Any],
) -> datetime:
    state = stream_target.get("_stream_state") or {}
    stream_started_at = stream_target["started_at"]
    prior_checked_at = state.get("last_checked_at")
    return (
        max(stream_started_at, prior_checked_at - timedelta(seconds=1))
        if stream_target.get("_is_newest")
        and isinstance(prior_checked_at, datetime)
        else stream_started_at
    )


async def fetch_twitch_clips_for_stream(
    broadcaster_id: str,
    stream_target: dict[str, Any],
    ignored_clip_ids: set[str],
    ignored_clip_urls: set[str],
    limit: int,
) -> tuple[list[dict[str, Any]], bool, str]:
    app.state.current_stream_grace_active = False
    try:
        access_token = await get_twitch_access_token()
    except Exception as error:
        print(
            f"TWITCH CLIP FETCH FAILED for broadcaster {broadcaster_id}:",
            repr(error),
        )
        return [], False, ""
    headers = {
        "Client-Id": TWITCH_CLIENT_ID,
        "Authorization": f"Bearer {access_token}",
    }
    now = datetime.now(timezone.utc)
    stream_started_at = stream_target["started_at"]
    discovery_start = _stream_discovery_start(stream_target)
    params: dict[str, object] = {
        "broadcaster_id": broadcaster_id,
        "first": 100,
        "started_at": discovery_start.isoformat().replace("+00:00", "Z"),
        "ended_at": now.isoformat().replace("+00:00", "Z"),
    }
    expected_video_id = str(stream_target.get("video_id") or "")
    seen_ids = {_canonical_twitch_clip_id(value) for value in ignored_clip_ids}
    seen_urls = {_canonical_twitch_clip_url(value) for value in ignored_clip_urls}
    fresh_clips: list[dict[str, Any]] = []
    cursor_value = ""
    pagination_complete = False
    async with httpx.AsyncClient(timeout=15.0) as client:
        for page_number in range(1, _twitch_max_pages() + 1):
            page_params = dict(params)
            if cursor_value:
                page_params["after"] = cursor_value
            try:
                response = await client.get(
                    "https://api.twitch.tv/helix/clips",
                    headers=headers,
                    params=page_params,
                )
            except httpx.RequestError as error:
                print(
                    f"TWITCH CLIP FETCH FAILED for broadcaster {broadcaster_id}:",
                    repr(error),
                )
                return fresh_clips, False, cursor_value
            if response.status_code != 200:
                print(
                    "TWITCH CLIP FETCH FAILED:",
                    response.status_code,
                    response.text,
                )
                return fresh_clips, False, cursor_value
            payload = response.json()
            for twitch_clip in sorted(
                payload.get("data", []),
                key=lambda clip: (
                    _parse_twitch_created_at(clip.get("created_at"))
                    or datetime.min.replace(tzinfo=timezone.utc)
                ),
                reverse=True,
            ):
                clip_video_id = str(twitch_clip.get("video_id") or "")
                if expected_video_id and clip_video_id != expected_video_id:
                    continue
                if stream_target.get("is_live") and clip_video_id:
                    continue
                clip_id, canonical_url = _normalized_twitch_identifiers(twitch_clip)
                if (
                    not clip_id
                    or clip_id in seen_ids
                    or canonical_url in seen_urls
                ):
                    continue
                twitch_clip["_search_tier"] = (
                    1 if stream_target.get("_is_newest") else 2
                )
                twitch_clip["_stream_id"] = stream_target["stream_id"]
                twitch_clip["_canonical_clip_id"] = clip_id
                twitch_clip["_canonical_clip_url"] = canonical_url
                _clip_history_upsert(twitch_clip, "discovered")
                fresh_clips.append(twitch_clip)
                seen_ids.add(clip_id)
                seen_urls.add(canonical_url)
                if len(fresh_clips) >= limit:
                    return fresh_clips, False, cursor_value
            cursor_value = str(
                payload.get("pagination", {}).get("cursor") or ""
            ).strip()
            if not cursor_value:
                pagination_complete = True
                break
    if (
        not fresh_clips
        and stream_target.get("is_live")
        and (
            now - stream_started_at
        ).total_seconds() / 60.0
        < _nonnegative_int(
            os.getenv("AUTO_CLIP_CURRENT_STREAM_GRACE_MINUTES", "20")
        )
    ):
        app.state.current_stream_grace_active = True
    return fresh_clips, pagination_complete, cursor_value


async def fetch_fresh_twitch_clips(
    broadcaster_id: str,
    stream_started_at: object,
    ignored_clip_ids: set[str],
    ignored_clip_urls: set[str],
    limit: int = 5,
) -> list[dict[str, Any]]:
    app.state.current_stream_grace_active = False
    try:
        access_token = await get_twitch_access_token()
    except Exception as error:
        print(
            f"TWITCH CLIP FETCH FAILED for broadcaster {broadcaster_id}:",
            repr(error),
        )
        return []

    headers = {
        "Client-Id": TWITCH_CLIENT_ID,
        "Authorization": f"Bearer {access_token}",
    }

    now = datetime.now(timezone.utc)
    stream_start = _parse_twitch_created_at(stream_started_at)
    grace_minutes = max(
        0,
        _nonnegative_int(os.getenv("AUTO_CLIP_CURRENT_STREAM_GRACE_MINUTES", "20")),
    )
    print(
        "CURRENT STREAM WINDOW | "
        f"broadcaster_id={broadcaster_id} | "
        f"started_at={stream_start.isoformat() if stream_start else 'unknown'} | "
        f"ended_at={now.isoformat()}"
    )
    if stream_start is None:
        app.state.current_stream_grace_active = True
        print(
            "CURRENT STREAM GRACE ACTIVE | "
            "reason=missing_stream_started_at"
        )
        return []
    cutoff_24_hours = now - timedelta(hours=24)
    cutoff_7_days = now - timedelta(days=7)
    cutoff_30_days = now - timedelta(days=30)
    tiers = [
        ("current_stream", stream_start, now),
        ("last_24_hours", cutoff_24_hours, min(stream_start, now)),
        ("last_7_days", cutoff_7_days, min(cutoff_24_hours, stream_start)),
        ("last_30_days", cutoff_30_days, min(cutoff_7_days, stream_start)),
        (
            "archive",
            datetime(2016, 5, 26, tzinfo=timezone.utc),
            min(cutoff_30_days, stream_start),
        ),
    ]
    fresh_clips: list[dict[str, Any]] = []
    seen_ids = {_canonical_twitch_clip_id(value) for value in ignored_clip_ids}
    seen_urls = {_canonical_twitch_clip_url(value) for value in ignored_clip_urls}
    max_pages = _twitch_max_pages()

    async with httpx.AsyncClient(timeout=15.0) as client:
        for tier_number, (tier_name, started_at, ended_at) in enumerate(
            tiers,
            start=1,
        ):
            if tier_number > 1:
                print(
                    "FALLBACK TIER ACTIVATED | "
                    f"tier={tier_number} | name={tier_name}"
                )
            if ended_at <= started_at:
                continue
            params: dict[str, object] = {
                "broadcaster_id": broadcaster_id,
                "first": 100,
                "started_at": started_at.isoformat().replace("+00:00", "Z"),
                "ended_at": ended_at.isoformat().replace("+00:00", "Z"),
            }
            print(
                "CLIP SEARCH TIER | "
                f"tier={tier_number} | name={tier_name} | "
                f"started_at={params['started_at']} | ended_at={params['ended_at']}"
            )
            cursor_value = ""
            tier_fresh_count = 0
            for page_number in range(1, max_pages + 1):
                page_params = dict(params)
                if cursor_value:
                    page_params["after"] = cursor_value
                print(
                    "CLIP SEARCH PAGE | "
                    f"tier={tier_number} | name={tier_name} | "
                    f"page={page_number}/{max_pages} | "
                    f"cursor={'set' if cursor_value else 'initial'}"
                )
                try:
                    response = await client.get(
                        "https://api.twitch.tv/helix/clips",
                        headers=headers,
                        params=page_params,
                    )
                except httpx.RequestError as error:
                    print(
                        f"TWITCH CLIP FETCH FAILED for broadcaster {broadcaster_id}:",
                        repr(error),
                    )
                    return []
                if response.status_code != 200:
                    print(
                        "TWITCH CLIP FETCH FAILED:",
                        response.status_code,
                        response.text,
                    )
                    return []
                payload = response.json()
                tier_clips = sorted(
                    payload.get("data", []),
                    key=lambda clip: (
                        _parse_twitch_created_at(clip.get("created_at"))
                        or datetime.min.replace(tzinfo=timezone.utc)
                    ),
                    reverse=True,
                )
                for twitch_clip in tier_clips:
                    clip_id, canonical_url = _normalized_twitch_identifiers(twitch_clip)
                    if (
                        not clip_id
                        or clip_id in seen_ids
                        or canonical_url in seen_urls
                    ):
                        print(
                            "CLIP EXCLUDED BY HISTORY | "
                            f"clip_id={clip_id or 'missing'} | tier={tier_name}"
                        )
                        continue
                    twitch_clip["_search_tier"] = tier_number
                    twitch_clip["_canonical_clip_id"] = clip_id
                    twitch_clip["_canonical_clip_url"] = canonical_url
                    _clip_history_upsert(twitch_clip, "discovered")
                    fresh_clips.append(twitch_clip)
                    tier_fresh_count += 1
                    seen_ids.add(clip_id)
                    seen_urls.add(canonical_url)
                    if len(fresh_clips) >= limit:
                        break
                if len(fresh_clips) >= limit:
                    break
                cursor_value = str(
                    payload.get("pagination", {}).get("cursor") or ""
                ).strip()
                if not cursor_value:
                    break

            if tier_fresh_count:
                print(
                    "FRESH CLIPS FOUND | "
                    f"tier={tier_number} | name={tier_name} | "
                    f"count={tier_fresh_count} | total={len(fresh_clips)}"
                )
            if len(fresh_clips) >= limit:
                return fresh_clips

            if tier_number == 1:
                stream_age_minutes = (now - stream_start).total_seconds() / 60.0
                if tier_fresh_count == 0 and stream_age_minutes < grace_minutes:
                    app.state.current_stream_grace_active = True
                    print(
                        "CURRENT STREAM GRACE ACTIVE | "
                        f"age_minutes={stream_age_minutes:.1f} | "
                        f"grace_minutes={grace_minutes}"
                    )
                    return []

    return fresh_clips

def download_twitch_clip(clip_url: str, output_name: str) -> str:
    output_path = download_service.download_with_ytdlp(clip_url, output_name)
    return output_path or None


async def upload_tiktok_draft(video_path: str) -> dict:
    token_response = _load_provider_token_data("tiktok", TIKTOK_USER_TOKEN_FILE)
    if token_response is None:
        raise HTTPException(
            status_code=401,
            detail="TikTok account is not connected.",
        )

    token_fields = _extract_tiktok_token_fields(token_response)
    access_token = token_fields.get("access_token")
    if not access_token or _is_tiktok_access_token_expiring(token_response, skew_seconds=300):
        token_response = _refresh_tiktok_user_access_token(token_response)
        token_fields = _extract_tiktok_token_fields(token_response)
        access_token = token_fields.get("access_token")

    if not access_token:
        raise _reconnect_tiktok_exception()

    video_file = Path(video_path)
    if not video_file.is_file():
        raise HTTPException(
            status_code=400,
            detail=f"Video file not found: {video_path}",
        )

    video_size = video_file.stat().st_size

    async def stream_video():
        with video_file.open("rb") as file:
            while chunk := await asyncio.to_thread(file.read, 1024 * 1024):
                yield chunk
    if video_size == 0:
        raise HTTPException(
            status_code=400,
            detail="Video file is empty.",
        )

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=UTF-8",
    }
    init_payload = {
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": video_size,
            "chunk_size": video_size,
            "total_chunk_count": 1,
        }
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        init_response = await client.post(
            "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/",
            headers=headers,
            json=init_payload,
        )

        if _response_contains_access_token_invalid(init_response):
            token_response = _refresh_tiktok_user_access_token(token_response)
            refreshed_access_token = _extract_tiktok_token_fields(token_response).get("access_token")
            if not refreshed_access_token:
                raise _reconnect_tiktok_exception()

            headers["Authorization"] = f"Bearer {refreshed_access_token}"
            init_response = await client.post(
                "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/",
                headers=headers,
                json=init_payload,
            )

            if _response_contains_access_token_invalid(init_response):
                raise _reconnect_tiktok_exception()

        if init_response.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail="TikTok draft upload initialization failed.",
            )

        try:
            init_result = init_response.json()
        except ValueError as error:
            raise HTTPException(
                status_code=502,
                detail="TikTok draft upload initialization returned an invalid response.",
            ) from error

        if not isinstance(init_result, dict):
            raise HTTPException(
                status_code=502,
                detail="TikTok draft upload initialization returned an invalid payload.",
            )

        upload_data = init_result.get("data", {})
        upload_url = upload_data.get("upload_url")
        publish_id = upload_data.get("publish_id")

        if not upload_url or not publish_id:
            raise HTTPException(
                status_code=502,
                detail="TikTok did not return an upload URL and publish ID.",
            )

        upload_response = await client.put(
            upload_url,
            content=stream_video(),
            headers={
                "Content-Type": "video/mp4",
                "Content-Length": str(video_size),
                "Content-Range": f"bytes 0-{video_size - 1}/{video_size}",
            },
        )

    if upload_response.status_code not in {200, 201, 202, 204}:
        raise HTTPException(
            status_code=502,
            detail="TikTok draft video upload failed.",
        )

    return {
        "publish_id": publish_id,
        "upload_result": upload_response.json()
        if upload_response.content
        else None,
    }


async def _get_tiktok_creator_info() -> dict[str, object]:
    token_data = _load_provider_token_data("tiktok", TIKTOK_USER_TOKEN_FILE)
    access_token = _extract_tiktok_token_fields(token_data or {}).get("access_token")
    if not access_token:
        raise _reconnect_tiktok_exception()
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            "https://open.tiktokapis.com/v2/post/publish/creator_info/query/",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            json={},
        )
    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail="TikTok creator publishing capabilities are unavailable.",
        )
    data = response.json().get("data", {})
    print(
        "TIKTOK CREATOR INFO | "
        f"max_duration={data.get('max_video_post_duration_sec', 'unknown')}"
    )
    return data


async def _poll_one_tiktok_post_status() -> None:
    if not DATABASE_URL:
        return
    interval_seconds = _bounded_environment_int(
        "TIKTOK_STATUS_POLL_INTERVAL_SECONDS", 30, 10
    )
    max_checks = _bounded_environment_int("TIKTOK_STATUS_MAX_CHECKS", 20, 1)
    import psycopg

    with psycopg.connect(DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE twitch_clip_history SET poll_claimed_at = NOW()
                WHERE (provider, clip_id) = (
                    SELECT provider, clip_id FROM twitch_clip_history
                    WHERE status IN ('uploaded_to_inbox', 'publishing')
                      AND provider_publish_id IS NOT NULL
                      AND poll_check_count < %s
                      AND (
                          poll_claimed_at IS NULL
                          OR poll_claimed_at < NOW() - INTERVAL '2 minutes'
                      )
                      AND (
                          tiktok_last_status_check IS NULL
                          OR tiktok_last_status_check
                             < NOW() - (%s * INTERVAL '1 second')
                      )
                    ORDER BY publish_attempted_at LIMIT 1
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING clip_id, provider_publish_id
                """,
                (max_checks, interval_seconds),
            )
            claimed = cursor.fetchone()
        connection.commit()
    if not claimed:
        return
    clip_id, publish_id = claimed
    token_data = _load_provider_token_data("tiktok", TIKTOK_USER_TOKEN_FILE)
    access_token = _extract_tiktok_token_fields(token_data or {}).get("access_token")
    if not access_token:
        return
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            "https://open.tiktokapis.com/v2/post/publish/status/fetch/",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            json={"publish_id": publish_id},
        )
    if response.status_code != 200:
        print(f"TIKTOK POST STATUS | clip_id={clip_id} | http={response.status_code}")
        return
    data = response.json().get("data", {})
    post_status = str(data.get("status") or "UNKNOWN")
    failure_reason = str(data.get("fail_reason") or "")
    print(f"TIKTOK POST STATUS | clip_id={clip_id} | status={post_status}")
    completed = post_status == "PUBLISH_COMPLETE"
    failed = post_status in {"FAILED", "PUBLISH_FAILED"}
    with psycopg.connect(DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE twitch_clip_history SET
                    status = CASE WHEN %s THEN 'published'
                                  WHEN %s THEN 'publish_failed'
                                  ELSE status END,
                    tiktok_post_status = %s,
                    tiktok_failure_reason = NULLIF(%s, ''),
                    tiktok_last_status_check = NOW(),
                    poll_check_count = poll_check_count + 1,
                    tiktok_published_at = CASE WHEN %s THEN NOW()
                                              ELSE tiktok_published_at END,
                    published_at = CASE WHEN %s THEN NOW() ELSE published_at END,
                    poll_claimed_at = NULL
                WHERE provider = 'twitch' AND clip_id = %s
                """,
                (
                    completed, failed, post_status, failure_reason,
                    completed, completed, clip_id,
                ),
            )
        connection.commit()
    if completed:
        print(f"TIKTOK POST COMPLETE | clip_id={clip_id}")
    elif failed:
        print(f"TIKTOK POST FAILED | clip_id={clip_id} | reason={failure_reason}")


@app.get("/auth/twitch/validate")
async def validate_twitch_token():
    access_token = get_twitch_user_access_token()

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(
            "https://id.twitch.tv/oauth2/validate",
            headers={"Authorization": f"OAuth {access_token}"},
        )

    if response.status_code != 200:
        raise HTTPException(
            status_code=401,
            detail=f"Twitch token validation failed: {response.text}",
        )

    return response.json()

@app.get("/")
def home():
    return {"message": "PulseAI Backend Running!"}


@app.get("/twitch/stream/{channel_name}")
async def get_twitch_stream(channel_name: str):
    return await get_twitch_channel_data(channel_name)


@app.get("/creators")
async def get_creators():
    saved_creators = load_creators()

    creator_results = []

    for creator in saved_creators:
        try:
            twitch_data = await get_twitch_channel_data(creator["channel"])
            creator_results.append(twitch_data)
        except HTTPException as error:
            creator_results.append(
                {
                    "display_name": creator["name"],
                    "channel": creator["channel"],
                    "status": "ERROR",
                    "is_live": False,
                    "viewer_count": 0,
                    "title": None,
                    "game_name": None,
                    "started_at": None,
                    "profile_image_url": None,
                    "thumbnail_url": None,
                    "error": error.detail,
                }
            )

    return creator_results


@app.post("/creators", status_code=status.HTTP_201_CREATED)
async def add_creator(creator: CreatorCreate):
    clean_name = creator.name.strip()
    clean_channel = clean_channel_name(creator.channel)

    if not clean_name or not clean_channel:
        raise HTTPException(
            status_code=400,
            detail="Creator name and Twitch channel are required.",
        )

    saved_creators = load_creators()

    already_exists = any(
        existing["channel"] == clean_channel
        for existing in saved_creators
    )

    if already_exists:
        raise HTTPException(
            status_code=409,
            detail="That Twitch creator is already being monitored.",
        )

    twitch_data = await get_twitch_channel_data(clean_channel)

    if DATABASE_URL:
        try:
            import psycopg

            with psycopg.connect(DATABASE_URL) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO monitored_creators (
                            twitch_user_id, login, display_name, enabled
                        ) VALUES (%s, %s, %s, TRUE)
                        ON CONFLICT (login) DO UPDATE SET
                            twitch_user_id = COALESCE(
                                EXCLUDED.twitch_user_id,
                                monitored_creators.twitch_user_id
                            ),
                            display_name = EXCLUDED.display_name,
                            enabled = TRUE,
                            updated_at = NOW()
                        RETURNING id, (xmax = 0) AS inserted
                        """,
                        (
                            twitch_data.get("user_id"),
                            twitch_data["channel"],
                            twitch_data["display_name"] or clean_name,
                        ),
                    )
                    saved_row = cursor.fetchone()
                connection.commit()
            if not saved_row:
                raise RuntimeError("creator upsert returned no row")
            action = "ADDED" if saved_row[1] else "UPDATED"
            print(
                f"MONITORED CREATOR {action} | "
                f"login={twitch_data['channel']}"
            )
        except Exception as error:
            print(f"MONITORED CREATOR UPDATE FAILED | error={error!r}")
            raise HTTPException(
                status_code=503,
                detail="Creator could not be saved.",
            ) from error
    else:
        saved_creators.append(
            {
                "name": twitch_data["display_name"] or clean_name,
                "channel": twitch_data["channel"],
            }
        )
        save_creators(saved_creators)

    return twitch_data


@app.delete("/creators/{channel_name}")
def delete_creator(channel_name: str):
    clean_channel = clean_channel_name(channel_name)
    if DATABASE_URL:
        try:
            import psycopg

            with psycopg.connect(DATABASE_URL) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE monitored_creators
                        SET enabled = FALSE, updated_at = NOW()
                        WHERE login = %s AND enabled = TRUE
                        RETURNING id
                        """,
                        (clean_channel,),
                    )
                    disabled = cursor.fetchone()
                connection.commit()
            if not disabled:
                raise HTTPException(
                    status_code=404,
                    detail="That creator is not currently being monitored.",
                )
            print(f"MONITORED CREATOR DISABLED | login={clean_channel}")
            return {"message": f"{clean_channel} was removed from monitoring."}
        except HTTPException:
            raise
        except Exception as error:
            print(f"MONITORED CREATOR UPDATE FAILED | error={error!r}")
            raise HTTPException(
                status_code=503,
                detail="Creator could not be disabled.",
            ) from error
    saved_creators = load_creators()

    updated_creators = [
        creator
        for creator in saved_creators
        if creator["channel"] != clean_channel
    ]

    if len(updated_creators) == len(saved_creators):
        raise HTTPException(
            status_code=404,
            detail="That creator is not currently being monitored.",
        )

    save_creators(updated_creators)

    return {
        "message": f"{clean_channel} was removed from monitoring.",
    }
def _queue_row_to_dict(row: tuple[object, ...]) -> dict[str, object]:
    keys = (
        "id", "twitch_clip_id", "public_url", "creator", "title", "game",
        "score", "status", "video_path", "raw_video_path", "object_key",
        "durable_url", "ai_post_caption", "ai_hashtags",
        "ai_tiktok_description", "caption_generation_version",
        "duration_profile", "requested_duration", "actual_duration",
        "scheduled_for", "generated_at", "tiktok_publish_id",
        "tiktok_publish_mode", "tiktok_post_status", "tiktok_failure_reason",
        "rights_status",
    )
    return dict(zip(keys, row))


def _paginate_items(
    items: list[dict[str, object]],
    page: int,
    limit: int,
) -> dict[str, object]:
    start = (page - 1) * limit
    return {
        "items": items[start:start + limit],
        "total": len(items),
        "page": page,
        "limit": limit,
        "has_more": start + limit < len(items),
    }


UNPUBLISHED_CLIP_STATUSES = (
    "ready_for_review",
    "approved",
    "scheduled",
    "publish_failed",
)


def _clip_status_filter(
    status_filter: str,
) -> tuple[str, list[object]]:
    requested = str(status_filter or "").strip().lower()
    if not requested or requested == "all":
        return "", []
    if requested == "unpublished":
        return "status = ANY(%s)", [list(UNPUBLISHED_CLIP_STATUSES)]
    if requested == "published":
        return "status = %s", ["published"]
    statuses = [
        item.strip().lower()
        for item in requested.split(",")
        if item.strip()
    ]
    return ("status = ANY(%s)", [statuses]) if statuses else ("", [])


def _log_queue_persistence_recovery(
    clip_id: str,
    object_key: object,
    error: object,
) -> None:
    print(
        "CLIP QUEUE PERSISTENCE FAILED - R2 MEDIA RETAINED | "
        f"clip_id={clip_id or 'unknown'} | "
        f"object_key={str(object_key or '') or 'none'} | "
        f"error={error!r}"
    )


def _persist_generated_clip_record(
    clip: dict[str, object],
) -> dict[str, object] | None:
    if not DATABASE_URL:
        return {"clip_id": "", "status": "ready_for_review"}
    clip_id, clip_url = _normalized_twitch_identifiers(clip)
    if not clip_id:
        _log_queue_persistence_recovery(
            "",
            clip.get("object_key"),
            "invalid or mismatched Twitch clip ID/URL",
        )
        return None
    try:
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO twitch_clip_history (
                        provider, clip_id, clip_url, creator_id, creator_name,
                        created_at, status, viral_score, generated_clip_id,
                        display_title, category, local_video_path, raw_video_path,
                        object_key, durable_url, ai_post_caption, ai_hashtags,
                        ai_tiktok_description, caption_generation_version,
                        transcript, duration_profile, requested_duration,
                        actual_duration, longform_eligible_reason,
                        longform_rejection_reason, generated_at, title_version,
                        source_creator_id, source_platform, rights_status
                        , visual_layout_mode, visual_layout_confidence,
                        visual_layout_reason, visual_layout_version,
                        reaction_region, content_region,
                        generated_title, title_event_summary,
                        title_relevance_score, title_generation_version,
                        title_fallback_used
                    ) VALUES (
                        'twitch', %s, %s, %s, %s, %s,
                        'ready_for_review', %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s,
                        NOW(), 'title-v3', %s, 'twitch', %s,
                        %s, %s, %s, %s, %s::jsonb, %s::jsonb,
                        %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (provider, clip_id) DO UPDATE SET
                        clip_url = COALESCE(
                            EXCLUDED.clip_url, twitch_clip_history.clip_url
                        ),
                        creator_id = COALESCE(
                            twitch_clip_history.creator_id, EXCLUDED.creator_id
                        ),
                        creator_name = COALESCE(
                            twitch_clip_history.creator_name, EXCLUDED.creator_name
                        ),
                        created_at = COALESCE(
                            twitch_clip_history.created_at, EXCLUDED.created_at
                        ),
                        status = CASE
                            WHEN twitch_clip_history.status IN (
                                'publishing', 'uploaded_to_inbox',
                                'published', 'archived'
                            ) THEN twitch_clip_history.status
                            ELSE 'ready_for_review'
                        END,
                        viral_score = COALESCE(
                            EXCLUDED.viral_score,
                            twitch_clip_history.viral_score
                        ),
                        generated_clip_id = EXCLUDED.generated_clip_id,
                        display_title = COALESCE(
                            NULLIF(EXCLUDED.display_title, ''),
                            twitch_clip_history.display_title
                        ),
                        category = COALESCE(
                            NULLIF(EXCLUDED.category, ''),
                            twitch_clip_history.category
                        ),
                        local_video_path = COALESCE(
                            NULLIF(EXCLUDED.local_video_path, ''),
                            twitch_clip_history.local_video_path
                        ),
                        raw_video_path = COALESCE(
                            NULLIF(EXCLUDED.raw_video_path, ''),
                            twitch_clip_history.raw_video_path
                        ),
                        object_key = COALESCE(
                            NULLIF(EXCLUDED.object_key, ''),
                            twitch_clip_history.object_key
                        ),
                        durable_url = COALESCE(
                            NULLIF(EXCLUDED.durable_url, ''),
                            twitch_clip_history.durable_url
                        ),
                        ai_post_caption = COALESCE(
                            EXCLUDED.ai_post_caption,
                            twitch_clip_history.ai_post_caption
                        ),
                        ai_hashtags = COALESCE(
                            EXCLUDED.ai_hashtags,
                            twitch_clip_history.ai_hashtags
                        ),
                        ai_tiktok_description = COALESCE(
                            EXCLUDED.ai_tiktok_description,
                            twitch_clip_history.ai_tiktok_description
                        ),
                        caption_generation_version = COALESCE(
                            NULLIF(EXCLUDED.caption_generation_version, ''),
                            twitch_clip_history.caption_generation_version
                        ),
                        transcript = COALESCE(
                            NULLIF(EXCLUDED.transcript, ''),
                            twitch_clip_history.transcript
                        ),
                        duration_profile = COALESCE(
                            NULLIF(EXCLUDED.duration_profile, ''),
                            twitch_clip_history.duration_profile
                        ),
                        requested_duration = COALESCE(
                            EXCLUDED.requested_duration,
                            twitch_clip_history.requested_duration
                        ),
                        actual_duration = COALESCE(
                            EXCLUDED.actual_duration,
                            twitch_clip_history.actual_duration
                        ),
                        longform_eligible_reason = COALESCE(
                            NULLIF(EXCLUDED.longform_eligible_reason, ''),
                            twitch_clip_history.longform_eligible_reason
                        ),
                        longform_rejection_reason = COALESCE(
                            NULLIF(EXCLUDED.longform_rejection_reason, ''),
                            twitch_clip_history.longform_rejection_reason
                        ),
                        generated_at = COALESCE(
                            twitch_clip_history.generated_at,
                            EXCLUDED.generated_at
                        ),
                        title_version = COALESCE(
                            twitch_clip_history.title_version,
                            EXCLUDED.title_version
                        ),
                        source_creator_id = COALESCE(
                            twitch_clip_history.source_creator_id,
                            EXCLUDED.source_creator_id,
                            twitch_clip_history.creator_id
                        ),
                        source_platform = COALESCE(
                            twitch_clip_history.source_platform,
                            EXCLUDED.source_platform
                        ),
                        rights_status = COALESCE(
                            twitch_clip_history.rights_status,
                            EXCLUDED.rights_status
                        ),
                        visual_layout_mode = COALESCE(
                            EXCLUDED.visual_layout_mode,
                            twitch_clip_history.visual_layout_mode
                        ),
                        visual_layout_confidence = COALESCE(
                            EXCLUDED.visual_layout_confidence,
                            twitch_clip_history.visual_layout_confidence
                        ),
                        visual_layout_reason = COALESCE(
                            EXCLUDED.visual_layout_reason,
                            twitch_clip_history.visual_layout_reason
                        ),
                        visual_layout_version = COALESCE(
                            EXCLUDED.visual_layout_version,
                            twitch_clip_history.visual_layout_version
                        ),
                        reaction_region = COALESCE(
                            EXCLUDED.reaction_region,
                            twitch_clip_history.reaction_region
                        ),
                        content_region = COALESCE(
                            EXCLUDED.content_region,
                            twitch_clip_history.content_region
                        ),
                        generated_title = COALESCE(
                            EXCLUDED.generated_title,
                            twitch_clip_history.generated_title
                        ),
                        title_event_summary = COALESCE(
                            EXCLUDED.title_event_summary,
                            twitch_clip_history.title_event_summary
                        ),
                        title_relevance_score = COALESCE(
                            EXCLUDED.title_relevance_score,
                            twitch_clip_history.title_relevance_score
                        ),
                        title_generation_version = COALESCE(
                            EXCLUDED.title_generation_version,
                            twitch_clip_history.title_generation_version
                        ),
                        title_fallback_used = COALESCE(
                            EXCLUDED.title_fallback_used,
                            twitch_clip_history.title_fallback_used
                        )
                    RETURNING clip_id, clip_url, generated_clip_id, status,
                              object_key, durable_url, generated_at
                    """,
                    (
                        clip_id, clip_url,
                        clip.get("creator_id") or clip.get("broadcaster_id"),
                        clip.get("creator") or clip.get("creator_name"),
                        clip.get("created_at"), clip.get("score"),
                        clip.get("id"), clip.get("ai_title") or clip.get("title"),
                        clip.get("game"),
                        clip.get("video_path"), clip.get("raw_video_path"),
                        clip.get("object_key"), clip.get("durable_url"),
                        clip.get("ai_post_caption"),
                        json.dumps(clip.get("ai_hashtags") or []),
                        clip.get("ai_tiktok_description"),
                        clip.get("caption_generation_version"),
                        clip.get("transcript"),
                        clip.get("duration_profile"), clip.get("requested_duration"),
                        clip.get("actual_duration"),
                        clip.get("longform_eligible_reason"),
                        clip.get("longform_rejection_reason"),
                        (
                            clip.get("source_creator_id")
                            or clip.get("creator_id")
                            or clip.get("broadcaster_id")
                        ),
                        clip.get("rights_status") or "unknown",
                        clip.get("visual_layout_mode"),
                        clip.get("visual_layout_confidence"),
                        clip.get("visual_layout_reason"),
                        clip.get("visual_layout_version"),
                        (
                            json.dumps(clip.get("reaction_region"))
                            if clip.get("reaction_region")
                            else None
                        ),
                        (
                            json.dumps(clip.get("content_region"))
                            if clip.get("content_region")
                            else None
                        ),
                        clip.get("generated_title") or clip.get("ai_title"),
                        clip.get("title_event_summary"),
                        clip.get("title_relevance_score"),
                        clip.get("title_generation_version"),
                        clip.get("title_fallback_used"),
                    ),
                )
                saved = cursor.fetchone()
                if saved is None:
                    raise RuntimeError("queue upsert returned no saved row")
            connection.commit()
        saved_result = dict(saved)
        print(
            "CLIP QUEUE PERSISTENCE COMPLETE | "
            f"clip_id={saved_result['clip_id']} | "
            f"status={saved_result['status']} | "
            f"object_key={saved_result.get('object_key') or 'none'}"
        )
        return saved_result
    except Exception as error:
        print(
            "CLIP HISTORY DB ERROR | operation=generated_metadata | "
            f"clip_id={clip_id} | error={error!r}"
        )
        _log_queue_persistence_recovery(
            clip_id,
            clip.get("object_key"),
            error,
        )
        return None


@app.get("/api/clips")
async def get_clips(
    limit: int = Query(10, ge=1, le=100),
    page: int = Query(1, ge=1),
    status_filter: str = Query("", alias="status"),
    creator: str = Query(""),
):
    if not DATABASE_URL:
        clips_file = BASE_DIR / "clips.json"
        try:
            clips = json.loads(clips_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            clips = []
        status_clause, status_parameters = _clip_status_filter(status_filter)
        del status_clause
        requested_statuses = (
            status_parameters[0]
            if status_parameters and isinstance(status_parameters[0], list)
            else status_parameters
        )
        filtered = []
        for clip in clips:
            normalized_status = str(clip.get("status", "")).strip().lower()
            normalized_status = normalized_status.replace(" ", "_")
            if requested_statuses and normalized_status not in requested_statuses:
                continue
            if (
                creator
                and str(clip.get("creator", "")).lower() != creator.lower()
            ):
                continue
            filtered.append(clip)
        filtered.sort(
            key=lambda clip: (
                str(
                    clip.get("created_at")
                    or clip.get("started_at")
                    or clip.get("generated_at")
                    or ""
                ),
                str(clip.get("id") or clip.get("twitch_clip_id") or ""),
            ),
            reverse=True,
        )
        return _paginate_items(filtered, page, limit)
    if not getattr(app.state, "clip_history_ready", False):
        raise HTTPException(status_code=503, detail="Clip history is unavailable.")
    clauses = ["provider = 'twitch'", "generated_clip_id IS NOT NULL"]
    parameters: list[object] = []
    status_clause, status_parameters = _clip_status_filter(status_filter)
    if status_clause:
        clauses.append(status_clause)
        parameters.extend(status_parameters)
    if creator:
        clauses.append("LOWER(creator_name) = LOWER(%s)")
        parameters.append(creator)
    where_sql = " AND ".join(clauses)
    try:
        import psycopg
        from psycopg import sql

        with psycopg.connect(DATABASE_URL) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("SELECT COUNT(*) FROM twitch_clip_history WHERE ")
                    + sql.SQL(where_sql),
                    parameters,
                )
                total = cursor.fetchone()[0]
                cursor.execute(
                    sql.SQL(
                        """
                        SELECT generated_clip_id, clip_id, clip_url, creator_name,
                               display_title, category, viral_score, status,
                               local_video_path, raw_video_path, object_key, durable_url,
                               ai_post_caption, ai_hashtags, ai_tiktok_description,
                               caption_generation_version, duration_profile,
                               requested_duration, actual_duration, scheduled_for,
                               generated_at, provider_publish_id, tiktok_publish_mode,
                               tiktok_post_status, tiktok_failure_reason, rights_status
                        FROM twitch_clip_history WHERE
                        """
                    )
                    + sql.SQL(where_sql)
                    + sql.SQL(
                        " ORDER BY "
                        "created_at DESC NULLS LAST, clip_id DESC "
                        "LIMIT %s OFFSET %s"
                    ),
                    [*parameters, limit, (page - 1) * limit],
                )
                rows = cursor.fetchall()
        return {
            "items": [_queue_row_to_dict(row) for row in rows],
            "total": total,
            "page": page,
            "limit": limit,
            "has_more": page * limit < total,
        }
    except Exception as error:
        print(f"CLIP QUEUE LOAD FAILED | error={error!r}")
        raise HTTPException(status_code=503, detail="Clip queue is unavailable.")


@app.patch("/api/clips/{clip_id}")
async def update_clip_queue_item(clip_id: str, payload: dict):
    allowed_statuses = {
        "ready_for_review", "approved", "scheduled", "rejected", "archived",
        "publish_failed",
    }
    requested_status = str(payload.get("status") or "").strip().lower()
    if requested_status and requested_status not in allowed_statuses:
        raise HTTPException(status_code=400, detail="Unsupported clip status.")
    hashtags = payload.get("ai_hashtags")
    if hashtags is not None and not isinstance(hashtags, list):
        raise HTTPException(status_code=400, detail="ai_hashtags must be a list.")
    if not DATABASE_URL:
        raise HTTPException(
            status_code=503,
            detail="Durable queue actions require PostgreSQL.",
        )
    try:
        import psycopg

        with psycopg.connect(DATABASE_URL) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE twitch_clip_history SET
                        status = COALESCE(NULLIF(%s, ''), status),
                        ai_post_caption = COALESCE(%s, ai_post_caption),
                        ai_hashtags = COALESCE(%s::jsonb, ai_hashtags),
                        ai_tiktok_description = COALESCE(
                            %s, ai_tiktok_description
                        ),
                        scheduled_for = CASE
                            WHEN %s = 'scheduled' THEN %s::timestamptz
                            WHEN %s = 'ready_for_review' THEN NULL
                            ELSE scheduled_for
                        END,
                        approved_at = CASE WHEN %s = 'approved' THEN NOW()
                            ELSE approved_at END,
                        archived_at = CASE WHEN %s = 'archived' THEN NOW()
                            WHEN %s = 'ready_for_review' THEN NULL
                            ELSE archived_at END
                    WHERE generated_clip_id = %s
                    RETURNING status
                    """,
                    (
                        requested_status,
                        payload.get("ai_post_caption"),
                        json.dumps(hashtags) if hashtags is not None else None,
                        payload.get("ai_tiktok_description"),
                        requested_status, payload.get("scheduled_for"),
                        requested_status, requested_status, requested_status,
                        requested_status, clip_id,
                    ),
                )
                row = cursor.fetchone()
            connection.commit()
        if not row:
            raise HTTPException(status_code=404, detail="Clip not found.")
        return {"success": True, "id": clip_id, "status": row[0]}
    except HTTPException:
        raise
    except Exception as error:
        print(f"CLIP QUEUE UPDATE FAILED | clip_id={clip_id} | error={error!r}")
        raise HTTPException(status_code=503, detail="Clip update could not be saved.")


@app.post("/api/clips/{clip_id}/caption/regenerate")
async def regenerate_clip_caption(clip_id: str):
    if not DATABASE_URL:
        raise HTTPException(status_code=503, detail="PostgreSQL is required.")
    import psycopg

    with psycopg.connect(DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT creator_name, category, generated_clip_id, transcript
                FROM twitch_clip_history WHERE generated_clip_id = %s
                """,
                (clip_id,),
            )
            row = cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Clip not found.")
    transcript = str(row[3] or "")
    started_at = time.perf_counter()
    package = generate_tiktok_caption_package(transcript, str(row[0] or ""), str(row[1] or ""))
    _log_performance_timing(
        stage="caption_hashtag_generation",
        elapsed_seconds=time.perf_counter() - started_at,
    )
    package["ai_tiktok_description"] = package.get("ai_tiktok_description", "")
    await update_clip_queue_item(clip_id, package)
    return package


@app.get("/api/analytics")
async def get_clip_analytics():
    if not DATABASE_URL:
        return {
            "generated_count": 0, "unpublished_count": 0,
            "published_count": 0, "publish_failures": 0,
            "short_count": 0, "long_count": 0,
            "average_viral_score": 0, "top_creators": [],
        }
    import psycopg

    with psycopg.connect(DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) FILTER (WHERE generated_clip_id IS NOT NULL),
                       COUNT(*) FILTER (WHERE status IN (
                           'ready_for_review','approved','scheduled','publish_failed'
                       )),
                       COUNT(*) FILTER (WHERE status = 'published'),
                       COUNT(*) FILTER (WHERE status = 'publish_failed'),
                       COUNT(*) FILTER (WHERE duration_profile = 'short'),
                       COUNT(*) FILTER (WHERE duration_profile = 'long'),
                       COALESCE(AVG(viral_score) FILTER (
                           WHERE generated_clip_id IS NOT NULL
                       ), 0)
                FROM twitch_clip_history WHERE provider = 'twitch'
                """
            )
            counts = cursor.fetchone()
            cursor.execute(
                """
                SELECT creator_name, COUNT(*) FROM twitch_clip_history
                WHERE generated_clip_id IS NOT NULL
                GROUP BY creator_name ORDER BY COUNT(*) DESC LIMIT 10
                """
            )
            creators = cursor.fetchall()
    return {
        "generated_count": counts[0], "unpublished_count": counts[1],
        "published_count": counts[2], "publish_failures": counts[3],
        "short_count": counts[4], "long_count": counts[5],
        "average_viral_score": float(counts[6]),
        "top_creators": [{"creator": row[0], "count": row[1]} for row in creators],
    }


def _default_publish_settings() -> dict[str, object]:
    configured_mode = os.getenv("TIKTOK_POST_MODE", "draft").lower()
    return {
        "post_mode": "draft" if configured_mode != "draft" else configured_mode,
        "auto_publish_enabled": False,
        "auto_publish_approved": False,
        "daily_limit": _bounded_environment_int(
            "AUTO_PUBLISH_DAILY_LIMIT", 2, 0
        ),
        "min_gap_minutes": _bounded_environment_int(
            "AUTO_PUBLISH_MIN_GAP_MINUTES", 180, 0
        ),
        "timezone": os.getenv("AUTO_PUBLISH_TIMEZONE", "UTC"),
        "privacy_level": "",
        "allow_comments": True,
        "allow_duet": True,
        "allow_stitch": True,
        "longform_target_percent": _bounded_environment_int(
            "AUTO_CLIP_LONGFORM_TARGET_PERCENT", 30, 0, 100
        ),
        "direct_post_available": False,
        "published_media_retention_days": _published_media_retention_days(),
        "object_storage_enabled": object_storage_enabled(),
    }


@app.get("/api/settings/publishing")
async def get_publish_settings():
    defaults = _default_publish_settings()
    if not DATABASE_URL:
        return defaults
    import psycopg

    with psycopg.connect(DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT post_mode, auto_publish_enabled, auto_publish_approved,
                       daily_limit, min_gap_minutes, timezone, privacy_level,
                       allow_comments, allow_duet, allow_stitch,
                       longform_target_percent
                FROM pulseai_settings WHERE singleton = TRUE
                """
            )
            row = cursor.fetchone()
    if not row:
        return defaults
    keys = list(defaults)[:11]
    return {
        **defaults,
        **dict(zip(keys, row)),
        "direct_post_available": False,
    }


@app.put("/api/settings/publishing")
async def save_publish_settings(payload: dict):
    if not DATABASE_URL:
        raise HTTPException(status_code=503, detail="PostgreSQL is required.")
    post_mode = str(payload.get("post_mode") or "draft").lower()
    if post_mode not in {"draft", "direct"}:
        raise HTTPException(status_code=400, detail="Invalid post mode.")
    if post_mode == "direct":
        raise HTTPException(
            status_code=409,
            detail="Direct Post is unavailable until video.publish is approved.",
        )
    auto_enabled = bool(payload.get("auto_publish_enabled", False))
    auto_approved = bool(payload.get("auto_publish_approved", False))
    if auto_enabled and not auto_approved:
        raise HTTPException(
            status_code=400,
            detail="Explicit auto-publish approval is required.",
        )
    import psycopg

    with psycopg.connect(DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO pulseai_settings (
                    singleton, post_mode, auto_publish_enabled,
                    auto_publish_approved, daily_limit, min_gap_minutes,
                    timezone, privacy_level, allow_comments, allow_duet,
                    allow_stitch, longform_target_percent
                ) VALUES (
                    TRUE, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (singleton) DO UPDATE SET
                    post_mode = EXCLUDED.post_mode,
                    auto_publish_enabled = EXCLUDED.auto_publish_enabled,
                    auto_publish_approved = EXCLUDED.auto_publish_approved,
                    daily_limit = EXCLUDED.daily_limit,
                    min_gap_minutes = EXCLUDED.min_gap_minutes,
                    timezone = EXCLUDED.timezone,
                    privacy_level = EXCLUDED.privacy_level,
                    allow_comments = EXCLUDED.allow_comments,
                    allow_duet = EXCLUDED.allow_duet,
                    allow_stitch = EXCLUDED.allow_stitch,
                    longform_target_percent = EXCLUDED.longform_target_percent,
                    updated_at = NOW()
                """,
                (
                    post_mode, auto_enabled, auto_approved,
                    max(0, int(payload.get("daily_limit", 2))),
                    max(0, int(payload.get("min_gap_minutes", 180))),
                    str(payload.get("timezone") or "UTC"),
                    payload.get("privacy_level"),
                    bool(payload.get("allow_comments", True)),
                    bool(payload.get("allow_duet", True)),
                    bool(payload.get("allow_stitch", True)),
                    max(0, min(100, int(payload.get("longform_target_percent", 30)))),
                ),
            )
        connection.commit()
    return await get_publish_settings()


@app.get("/api/clips/{clip_id}/video")
async def get_clip_video(clip_id: str, download: int = 0):
    if DATABASE_URL:
        try:
            import psycopg

            with psycopg.connect(DATABASE_URL) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT durable_url, local_video_path
                        FROM twitch_clip_history
                        WHERE generated_clip_id = %s
                        """,
                        (clip_id,),
                    )
                    media_row = cursor.fetchone()
            if media_row:
                durable_url, local_path = media_row
                if durable_url:
                    return RedirectResponse(str(durable_url))
                if local_path:
                    resolved_video_path = _resolve_allowed_video_path(local_path)
                    return FileResponse(
                        path=str(resolved_video_path),
                        media_type="video/mp4",
                        filename=f"{clip_id}.mp4" if download == 1 else None,
                    )
        except HTTPException:
            raise
        except Exception as error:
            print(f"CLIP MEDIA LOOKUP FAILED | clip_id={clip_id} | error={error!r}")
    clips_file = BASE_DIR / "clips.json"

    try:
        with clips_file.open("r", encoding="utf-8") as file:
            clips = json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        clips = []

    clip = next(
        (item for item in clips if str(item.get("id", "")) == clip_id),
        None,
    )
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found.")

    resolved_video_path = _resolve_allowed_video_path(clip.get("video_path", ""))

    if download == 1:
        return FileResponse(
            path=str(resolved_video_path),
            media_type="video/mp4",
            filename=_build_safe_download_filename(clip, clip_id),
        )

    return FileResponse(path=str(resolved_video_path), media_type="video/mp4")


@app.post("/api/publish")
async def publish_clip_to_tiktok(clip: dict):
    clips_file = Path(__file__).resolve().parent / "clips.json"

    try:
        with clips_file.open("r", encoding="utf-8") as file:
            clips = json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        clips = []

    matching_clip_index = _find_clip_index_by_stable_identifier(clips, clip)
    if matching_clip_index is None:
        matching_clip = None
        if DATABASE_URL and clip.get("id"):
            import psycopg

            with psycopg.connect(DATABASE_URL) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT generated_clip_id, clip_id, clip_url, creator_name,
                               display_title, local_video_path, durable_url,
                               ai_tiktok_description, status, actual_duration
                        FROM twitch_clip_history
                        WHERE generated_clip_id = %s
                        """,
                        (clip.get("id"),),
                    )
                    row = cursor.fetchone()
            if row:
                matching_clip = {
                    "id": row[0], "twitch_clip_id": row[1],
                    "public_url": row[2], "creator": row[3],
                    "title": row[4], "video_path": row[5],
                    "durable_url": row[6], "ai_tiktok_description": row[7],
                    "status": row[8], "actual_duration": row[9],
                }
        if matching_clip is None:
            raise HTTPException(
                status_code=404,
                detail="Clip not found in durable queue.",
            )
    else:
        matching_clip = clips[matching_clip_index]
    if _is_clip_published(matching_clip):
        raise HTTPException(
            status_code=409,
            detail="Clip has already been published.",
        )

    published = load_published()
    if _contains_clip_with_same_identifier(published, matching_clip):
        raise HTTPException(
            status_code=409,
            detail="Clip has already been published.",
        )

    video_path = str(matching_clip.get("video_path") or "")
    durable_url = str(matching_clip.get("durable_url") or "")
    if not video_path and not durable_url:
        raise HTTPException(
            status_code=400,
            detail="Stored clip has no local or durable media.",
        )

    if video_path and not Path(video_path).is_file() and not durable_url:
        raise HTTPException(
            status_code=404,
            detail=f"Video file not found: {video_path}",
        )

    publish_settings = await get_publish_settings()
    post_mode = str(publish_settings.get("post_mode") or "draft").lower()
    if post_mode not in {"draft", "direct"}:
        raise HTTPException(
            status_code=400,
            detail="Invalid TikTok publishing mode.",
        )
    if post_mode == "direct":
        creator_info = await _get_tiktok_creator_info()
        allowed_duration = int(
            creator_info.get("max_video_post_duration_sec") or 0
        )
        actual_duration = float(matching_clip.get("actual_duration") or 0)
        if allowed_duration and actual_duration > allowed_duration:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"TikTok allows up to {allowed_duration} seconds, but this "
                    f"clip is {actual_duration:.1f} seconds."
                ),
            )
        raise HTTPException(
            status_code=409,
            detail="Direct Post is unavailable until video.publish is approved.",
        )

    if not _claim_clip_for_publishing(matching_clip):
        raise HTTPException(
            status_code=409,
            detail="Clip publishing is already in progress or is not eligible.",
        )

    print("TIKTOK DRAFT UPLOAD START | clip_id=" + str(matching_clip.get("id")))
    temporary_publish_path: Path | None = None
    try:
        if not video_path or not Path(video_path).is_file():
            downloads_dir = (BASE_DIR / "downloads").resolve()
            downloads_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                prefix="durable_publish_",
                suffix=".mp4",
                dir=downloads_dir,
                delete=False,
            ) as temporary_media:
                temporary_publish_path = Path(temporary_media.name)
            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream("GET", durable_url) as response:
                    response.raise_for_status()
                    with temporary_publish_path.open("wb") as media_file:
                        async for chunk in response.aiter_bytes(1024 * 1024):
                            media_file.write(chunk)
            if temporary_publish_path.stat().st_size <= 0:
                raise RuntimeError("Durable media download was empty.")
            video_path = str(temporary_publish_path)
        tiktok_result = await upload_tiktok_draft(video_path)
    except Exception as error:
        restored = _restore_clip_after_publish_failure(matching_clip)
        print(
            "TIKTOK DRAFT UPLOAD FAILED | "
            f"clip_id={matching_clip.get('id')} | "
            f"history_restored={restored} | error={error!r}"
        )
        print(
            "TIKTOK PUBLISH FAILED - STATUS UNCHANGED | "
            f"clip_id={matching_clip.get('id')} | "
            f"history_restored={restored} | error={error!r}"
        )
        raise
    finally:
        if temporary_publish_path is not None:
            try:
                temporary_publish_path.unlink(missing_ok=True)
            except OSError as cleanup_error:
                print(
                    "DURABLE PUBLISH TEMP CLEANUP FAILED | "
                    f"path={temporary_publish_path} | error={cleanup_error!r}"
                )

    upload_result = tiktok_result.get("upload_result")
    provider_publish_id = str(
        tiktok_result.get("publish_id")
        or (
            upload_result.get("publish_id")
            if isinstance(upload_result, dict)
            else ""
        )
        or f"confirmed:{datetime.now(timezone.utc).isoformat()}"
    )
    if not _mark_clip_uploaded_to_inbox(matching_clip, provider_publish_id):
        print(
            "TIKTOK PUBLISH SUCCEEDED BUT LOCAL STATE FAILED | "
            f"clip_id={matching_clip.get('id')} | "
            "stage=postgres_publish_completion"
        )
        raise HTTPException(
            status_code=503,
            detail="TikTok accepted the draft, but upload state could not be saved.",
        )
    print(
        "TIKTOK DRAFT UPLOAD COMPLETE | "
        f"clip_id={matching_clip.get('id')} | "
        f"publish_id={provider_publish_id}"
    )

    try:
        updated_clip_index = _find_clip_index_by_stable_identifier(
            clips,
            matching_clip,
        )
        if updated_clip_index is not None:
            clips[updated_clip_index]["status"] = "Uploaded to inbox"
            with clips_file.open("w", encoding="utf-8") as file:
                json.dump(clips, file, indent=2)
            matching_clip = clips[updated_clip_index]

    except Exception as error:
        print(
            "TIKTOK PUBLISH SUCCEEDED BUT LOCAL STATE FAILED | "
            f"clip_id={matching_clip.get('id')} | "
            f"provider_publish_id={provider_publish_id} | error={error!r}"
        )
        raise

    return {
        "success": True,
        "message": f"Uploaded '{matching_clip.get('title', 'clip')}' to TikTok inbox.",
        "published_count": len(published),
        "publish_id": tiktok_result.get("publish_id"),
        "upload_result": tiktok_result.get("upload_result"),
    }
published_count = 0
@app.get("/api/performance")
async def get_performance():
    published = load_published()

    return {
        "views": 1247381,
        "followers": 4392,
        "revenue": 327.84,
        "published": len(published),
    }
@app.get("/api/published")
async def get_published():
    return load_published()

def generate_demo_clip():
    import random

    titles = [
        "Insane 1v5 clutch",
        "Streamer loses it",
        "Impossible comeback",
        "Funniest Twitch moment",
        "Perfect timing",
        "Unexpected victory",
    ]

    creators = [
        "KaiCenat",
        "xQc",
        "pokimane",
        "shroud",
        "tarik",
    ]

    return {
        "title": random.choice(titles),
        "creator": random.choice(creators),
        "score": random.randint(AUTO_CLIP_MIN_SCORE, 99),
        "status": "Ready to review",
    }

def _build_generated_clip_record(clip: dict) -> dict:
    return {
    "id": str(uuid.uuid4()),
    "title": clip.get("title", "Untitled clip"),
    "creator": clip.get("creator", "Unknown creator"),
    "score": clip.get("score", 0),
    "status": clip.get("status", "Ready to review"),
    "viewer_count": clip.get("viewer_count"),
    "game": clip.get("game"),
    "started_at": clip.get("started_at"),
    "thumbnail_url": clip.get("thumbnail_url"),
    "timestamp": clip.get("timestamp"),
    "duration": clip.get("duration", 30),
    "twitch_clip_id": clip.get("twitch_clip_id"),
    "twitch_edit_url": clip.get("twitch_edit_url"),
    "public_url": clip.get("public_url"),
    "video_path": clip.get("video_path"),
    "raw_video_path": clip.get("raw_video_path"),
    "transcript": clip.get("transcript", ""),
    "ai_title": clip.get("ai_title", ""),
    "ai_description": clip.get("ai_description", ""),
    "ai_post_caption": clip.get("ai_post_caption", ""),
    "ai_hashtags": clip.get("ai_hashtags", []),
    "ai_tiktok_description": clip.get("ai_tiktok_description", ""),
    "caption_generation_version": clip.get("caption_generation_version", ""),
    "duration_profile": clip.get("duration_profile", "short"),
    "requested_duration": clip.get("requested_duration"),
    "actual_duration": clip.get("actual_duration", clip.get("duration")),
    "longform_eligible_reason": clip.get("longform_eligible_reason", ""),
    "longform_rejection_reason": clip.get("longform_rejection_reason", ""),
    "object_key": clip.get("object_key", ""),
    "durable_url": clip.get("durable_url", ""),
    "visual_layout_mode": clip.get("visual_layout_mode", "single_subject"),
    "visual_layout_confidence": clip.get("visual_layout_confidence", 0),
    "visual_layout_reason": clip.get("visual_layout_reason", ""),
    "visual_layout_version": clip.get("visual_layout_version", "layout-v1"),
    "reaction_region": clip.get("reaction_region"),
    "content_region": clip.get("content_region"),
    "generated_title": clip.get("generated_title", clip.get("ai_title", "")),
    "title_event_summary": clip.get("title_event_summary", ""),
    "title_relevance_score": clip.get("title_relevance_score", 0),
    "title_generation_version": clip.get("title_generation_version", "title-v3"),
    "title_fallback_used": bool(clip.get("title_fallback_used", False)),
}


@app.post("/api/clips")
async def create_clip(clip: dict):
    clips_file = Path(__file__).resolve().parent / "clips.json"

    try:
        with clips_file.open("r", encoding="utf-8") as file:
            clips = json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        clips = []

    new_clip = _build_generated_clip_record(clip)
    clips.append(new_clip)

    with clips_file.open("w", encoding="utf-8") as file:
        json.dump(clips, file, indent=2)

    return {
        "success": True,
        "clip": new_clip,
        "clip_count": len(clips),
    }

@app.post("/api/clips/generate")
async def generate_clip():
    clip = generate_demo_clip()

    clips_file = Path(__file__).resolve().parent / "clips.json"

    try:
        with clips_file.open("r", encoding="utf-8") as file:
            clips = json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        clips = []

    clips.append(clip)

    with clips_file.open("w", encoding="utf-8") as file:
        json.dump(clips, file, indent=2)

    return clip

@app.post("/api/clips/auto")
async def auto_generate_clip():
    try:
        job, created = enqueue_generation_job("manual")
    except Exception as error:
        print(f"GENERATION JOB ENQUEUE FAILED | error={error!r}")
        raise HTTPException(
            status_code=503,
            detail="Clip generation queue is unavailable.",
        ) from error
    payload = serialize_generation_job(job)
    payload["reused"] = not created
    return JSONResponse(status_code=202, content=payload)


@app.get("/api/clip-generation-jobs/{job_id}")
async def get_clip_generation_job(job_id: str):
    try:
        job = get_generation_job(job_id)
    except Exception as error:
        print(f"GENERATION JOB LOAD FAILED | job_id={job_id} | error={error!r}")
        raise HTTPException(
            status_code=503,
            detail="Clip generation status is unavailable.",
        ) from error
    if job is None:
        raise HTTPException(status_code=404, detail="Generation job not found.")
    return serialize_generation_job(job)


def _set_generation_job_stage(
    generation_job_id: str | None,
    generation_worker_id: str | None,
    stage: str,
) -> None:
    if not generation_job_id or not generation_worker_id:
        return
    if not update_generation_job_stage(
        generation_job_id,
        generation_worker_id,
        stage,
    ):
        raise RuntimeError(
            f"Generation job lease was lost before stage {stage}."
        )


async def _run_auto_generate_clip_pipeline(
    generation_job_id: str | None = None,
    generation_worker_id: str | None = None,
    _historical_only: bool = False,
    _forced_creator: dict[str, Any] | None = None,
    _forced_channel_data: dict[str, Any] | None = None,
    _streams_examined: int = 0,
    _prior_candidates_examined: int = 0,
    _prior_candidates_rejected: int = 0,
    _job_skipped_stream_ids: frozenset[str] = frozenset(),
    _job_had_partial_stream: bool = False,
    _historical_cursor_blocked: bool = False,
):
    if not getattr(app.state, "clip_history_ready", False):
        raise HTTPException(
            status_code=503,
            detail="Clip history is unavailable; Twitch generation is disabled.",
        )
    generation_started_at = time.perf_counter()
    processed_candidates_count = 0
    fully_evaluated_candidates_count = 0
    candidates_deferred_before_download = 0
    candidates_rejected_after_download = 0
    candidates_evaluated = 0
    candidates_examined_count = 0
    candidates_rejected_count = 0
    deferred_available_memory_mb: float | None = None
    deferred_required_memory_mb: float | None = None

    def pipeline_result(
        message: str,
        *,
        best_score: object = 0,
        outcome_reason: str,
    ) -> dict[str, object]:
        return {
            "message": message,
            "best_score": best_score,
            "outcome_reason": outcome_reason,
            "candidates_deferred_before_download": (
                candidates_deferred_before_download
            ),
            "candidates_rejected_after_download": (
                candidates_rejected_after_download
            ),
            "candidates_evaluated": candidates_evaluated,
            "_job_candidates_examined": (
                _prior_candidates_examined + candidates_examined_count
            ),
            "_job_candidates_rejected": (
                _prior_candidates_rejected + candidates_rejected_count
            ),
            "available_memory_mb": deferred_available_memory_mb,
            "required_memory_mb": deferred_required_memory_mb,
        }
    creators = load_creators()
    creator_count = len(creators)
    start_index = _load_creator_cursor(creator_count)
    selected_creator = None
    selected_stream = None
    selected_index = None

    if _forced_creator is not None and _forced_channel_data is not None:
        selected_creator = _forced_creator
        selected_stream = _forced_channel_data
        selected_index = start_index
    else:
        for offset in range(creator_count):
            creator_index = (start_index + offset) % creator_count
            creator = creators[creator_index]
            try:
                stream = await get_twitch_channel_data(creator["channel"])
            except Exception as error:
                print(
                    f"TWITCH CHECK FAILED for {creator['channel']}:",
                    repr(error),
                )
                continue

            if (
                not stream.get("is_live")
                and not stream.get("newest_completed_stream")
            ):
                continue

            selected_creator = creator
            selected_stream = stream
            selected_index = creator_index
            break

    if selected_creator is None or selected_stream is None or selected_index is None:
        print(
            "ROUND ROBIN | selected_creator=none | selected_index=-1 | "
            f"next_index={start_index} | reason=no_live_creators"
        )
        total_elapsed = time.perf_counter() - generation_started_at
        _log_performance_timing(
            stage="generation_total",
            elapsed_seconds=total_elapsed,
        )
        print(
            "PERFORMANCE TIMING SUMMARY | "
            f"total_elapsed_seconds={total_elapsed:.3f} | "
            f"processed_candidates={processed_candidates_count} | "
            f"fully_evaluated_candidates={fully_evaluated_candidates_count}"
        )
        print(
            "STREAM SEARCH COMPLETE | "
            "streams_checked=0 | clip_created=false | "
            "reason=no_more_streams"
        )
        return pipeline_result(
            "No live or completed Twitch streams were available.",
            outcome_reason="no_available_streams",
        )

    next_index = (selected_index + 1) % creator_count
    if _forced_creator is None:
        _save_creator_cursor(next_index, creator_count)
    print(
        "ROUND ROBIN | "
        f"selected_creator={selected_creator['name']} | "
        f"selected_index={selected_index} | "
        f"next_index={next_index}"
    )

    creator = selected_creator
    stream = selected_stream
    broadcaster_id = stream.get("user_id")
    stream_target = _select_stream_search_target(
        str(broadcaster_id or ""),
        stream,
        historical_only=_historical_only,
        job_skipped_stream_ids=_job_skipped_stream_ids,
    )
    if stream_target is None:
        print(
            "STREAM SEARCH COMPLETE | "
            f"streams_checked={_streams_examined} | "
            "clip_created=false | "
            f"reason={'partial_streams_remaining' if _job_had_partial_stream else 'no_more_streams'}"
        )
        return pipeline_result(
            (
                "Clip search paused after retryable stream failures."
                if _job_had_partial_stream
                else "No suitable clip was found. Try again later."
            ),
            outcome_reason=(
                "partial_streams_remaining"
                if _job_had_partial_stream
                else "no_more_streams"
            ),
        )
    streams_examined = _streams_examined + 1
    stream_state = stream_target.get("_stream_state") or {}
    stream_id = str(stream_target["stream_id"])
    stream_range_start = stream_target["started_at"]
    stream_range_end = (
        stream_target.get("ended_at")
        or datetime.now(timezone.utc)
    )
    stream_retryable_failure = False
    stream_discovery_complete = False
    streamer_label = str(
        creator.get("channel") or creator.get("name") or broadcaster_id or ""
    )
    if stream_target.get("_is_newest"):
        print(
            "STREAM SEARCH START | "
            f"streamer={streamer_label} | mode=current | "
            f"stream_id={stream_id} | "
            f"started_at={stream_range_start.isoformat()}"
        )
    else:
        print(
            "STREAM SEARCH START | "
            f"streamer={streamer_label} | mode=historical | "
            f"stream_id={stream_id} | "
            f"started_at={stream_range_start.isoformat()}"
        )

    async def continue_historical_search(
        *,
        outcome_reason: str,
        retryable_failure: bool,
        discovery_complete: bool,
        message: str = "No suitable clip was found. Try again later.",
    ) -> dict[str, object]:
        is_newest = bool(stream_target.get("_is_newest"))
        next_skipped_stream_ids = _job_skipped_stream_ids
        next_had_partial_stream = _job_had_partial_stream
        next_cursor_blocked = _historical_cursor_blocked
        if retryable_failure or not discovery_complete:
            update_stream_progress(
                str(broadcaster_id),
                stream_id,
                processing_state="partial",
                range_start=stream_range_start,
                range_end=None,
                retryable_failure_state=outcome_reason,
            )
            print(
                "STREAM SEARCH RESULT | "
                f"stream_id={stream_id} | result=partial"
            )
            next_had_partial_stream = True
            if not is_newest:
                next_skipped_stream_ids = frozenset(
                    {*_job_skipped_stream_ids, stream_id}
                )
                next_cursor_blocked = True
            if outcome_reason in {
                "history_unavailable",
                "memory_deferred",
                "memory_deferred_before_download",
                "memory_deferred_after_download",
            }:
                print(
                    "STREAM SEARCH COMPLETE | "
                    f"streams_checked={streams_examined} | "
                    "clip_created=false | "
                    f"reason={outcome_reason}"
                )
                return pipeline_result(
                    message,
                    outcome_reason=outcome_reason,
                )
        elif is_newest:
            update_stream_progress(
                str(broadcaster_id),
                stream_id,
                processing_state="partial",
                range_start=stream_range_start,
                range_end=stream_range_end,
                retryable_failure_state=None,
                checked_complete=True,
            )
            print(
                "STREAM SEARCH RESULT | "
                f"stream_id={stream_id} | result=no_clip"
            )
        else:
            update_stream_progress(
                str(broadcaster_id),
                stream_id,
                processing_state="exhausted",
                range_start=stream_range_start,
                range_end=stream_range_end,
                retryable_failure_state=None,
                checked_complete=True,
            )
            print(
                "STREAM SEARCH RESULT | "
                f"stream_id={stream_id} | result=exhausted"
            )
            print(
                "AUTO CLIP STREAM PERMANENTLY EXHAUSTED | "
                f"creator_id={broadcaster_id} | stream_id={stream_id}"
            )
            if not _historical_cursor_blocked:
                save_historical_cursor(
                    str(broadcaster_id),
                    next_before_timestamp=stream_range_start,
                    last_stream_id=stream_id,
                )
                print(
                    "AUTO CLIP HISTORICAL CURSOR SAVED | "
                    f"creator_id={broadcaster_id} | stream_id={stream_id} | "
                    f"next_before={stream_range_start.isoformat()}"
                )
            else:
                print(
                    "AUTO CLIP HISTORICAL CURSOR HELD | "
                    f"creator_id={broadcaster_id} | stream_id={stream_id} | "
                    "reason=retryable_newer_stream"
                )
        return await _run_auto_generate_clip_pipeline(
            generation_job_id=generation_job_id,
            generation_worker_id=generation_worker_id,
            _historical_only=True,
            _forced_creator=creator,
            _forced_channel_data=stream,
            _streams_examined=streams_examined,
            _prior_candidates_examined=(
                _prior_candidates_examined + candidates_examined_count
            ),
            _prior_candidates_rejected=(
                _prior_candidates_rejected + candidates_rejected_count
            ),
            _job_skipped_stream_ids=next_skipped_stream_ids,
            _job_had_partial_stream=next_had_partial_stream,
            _historical_cursor_blocked=next_cursor_blocked,
        )

    viewer_count = stream.get("viewer_count", 0)
    stream_title = (
        stream_target.get("title")
        or stream.get("title")
        or f"{creator['name']} Live Moment"
    )
    evaluation_stream = {
        **stream,
        "started_at": stream_target.get("started_at"),
        "title": stream_title,
        "game_name": (
            stream_target.get("game_name") or stream.get("game_name")
        ),
    }

    clips_file = Path(__file__).resolve().parent / "clips.json"

    try:
        with clips_file.open("r", encoding="utf-8") as file:
            existing_clips = json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        existing_clips = []

    cached_clip_ids, cached_clip_urls, history_available = (
        _load_clip_history_exclusions()
    )
    if not history_available:
        return await continue_historical_search(
            outcome_reason="history_unavailable",
            retryable_failure=True,
            discovery_complete=False,
            message="Clip search paused because history is unavailable.",
        )

    attempted_clip_ids: set[str] = set()
    attempted_clip_urls: set[str] = set()
    candidates = []
    target_candidate_count = AUTO_CLIP_CANDIDATE_COUNT

    for batch_attempt in range(1, 3):
        ignored_ids = cached_clip_ids | attempted_clip_ids
        ignored_urls = cached_clip_urls | attempted_clip_urls

        _log_memory_check(
            stage="before_twitch_clip_fetch",
            candidate_number=0,
            total_candidates=target_candidate_count,
        )
        fetch_started_at = time.perf_counter()
        fresh_batch, batch_discovery_complete, discovered_cursor = (
            await fetch_twitch_clips_for_stream(
                broadcaster_id=broadcaster_id,
                stream_target=stream_target,
                ignored_clip_ids=ignored_ids,
                ignored_clip_urls=ignored_urls,
                limit=AUTO_CLIP_CANDIDATE_COUNT,
            )
        )
        stream_discovery_complete = (
            stream_discovery_complete or batch_discovery_complete
        )
        _log_performance_timing(
            stage="twitch_clip_fetch",
            elapsed_seconds=time.perf_counter() - fetch_started_at,
        )
        _log_memory_check(
            stage="after_twitch_clip_fetch",
            candidate_number=0,
            total_candidates=max(len(fresh_batch), target_candidate_count),
        )

        if not fresh_batch:
            if (
                stream_target.get("is_live")
                and getattr(app.state, "current_stream_grace_active", False)
            ):
                return await continue_historical_search(
                    outcome_reason="current_stream_grace",
                    retryable_failure=True,
                    discovery_complete=False,
                    message="No fresh current-stream clips yet.",
                )
            print(
                f"No fresh Twitch clips available for {creator['channel']} "
                f"(batch {batch_attempt}/2)."
            )
            if batch_discovery_complete:
                if stream_target.get("_is_newest"):
                    print(
                        "AUTO CLIP NEWEST STREAM UNCHANGED | "
                        f"creator_id={broadcaster_id} | stream_id={stream_id}"
                    )
                break
            continue

        print(
            "AUTO CLIP NEW MATERIAL FOUND | "
            f"creator_id={broadcaster_id} | stream_id={stream_id} | "
            f"candidate_count={len(fresh_batch)}"
        )
        update_stream_progress(
            str(broadcaster_id),
            stream_id,
            processing_state="partial",
            candidate_cursor=discovered_cursor or None,
            retryable_failure_state=None,
        )

        total_candidates = len(fresh_batch)
        batch_memory_admitted, available_memory_mb, required_memory_mb = (
            _admit_candidate_batch_memory(
                total_candidates,
                batch_attempt,
            )
        )
        if not batch_memory_admitted:
            deferred_available_memory_mb = available_memory_mb
            deferred_required_memory_mb = required_memory_mb
            candidates_deferred_before_download += total_candidates
            for deferred_candidate in fresh_batch:
                candidates_examined_count += 1
                candidates_rejected_count += 1
                _log_candidate_rejection(
                    deferred_candidate,
                    "memory_deferral_before_download",
                    streamer=str(creator.get("channel") or creator.get("name") or ""),
                )
            return await continue_historical_search(
                outcome_reason="memory_deferred_before_download",
                retryable_failure=True,
                discovery_complete=False,
                message=(
                    "Clip generation deferred because worker memory did not "
                    "recover."
                ),
            )
        stage_one_started_at = time.perf_counter()
        preranked_candidates, has_sufficient_prerank_metadata = _fast_prerank_candidates(
            fresh_batch,
            cached_clip_ids,
            cached_clip_urls,
        )
        full_evaluation_count = min(_get_full_evaluation_count(), total_candidates)
        if has_sufficient_prerank_metadata:
            advanced_candidates = preranked_candidates[:1]
            if full_evaluation_count > 1:
                remaining_candidates = preranked_candidates[1:]
                best_remaining_tier = min(
                    int(candidate["search_tier"])
                    for candidate in remaining_candidates
                )
                diversity_candidate = max(
                    (
                        candidate
                        for candidate in remaining_candidates
                        if int(candidate["search_tier"]) == best_remaining_tier
                    ),
                    key=lambda candidate: (
                        float(candidate["title_score"]),
                        float(candidate["score"]),
                    ),
                )
                advanced_candidates.append(diversity_candidate)
                if full_evaluation_count > len(advanced_candidates):
                    advanced_ids = {
                        int(candidate["candidate_index"])
                        for candidate in advanced_candidates
                    }
                    advanced_candidates.extend(
                        candidate
                        for candidate in remaining_candidates
                        if int(candidate["candidate_index"]) not in advanced_ids
                    )
                    advanced_candidates = advanced_candidates[:full_evaluation_count]
        else:
            advanced_candidates = preranked_candidates
            print(
                "FAST PRE-RANK FALLBACK | "
                "reason=insufficient_metadata | "
                "action=sequential_full_evaluation"
            )

        advanced_candidate_numbers = [
            int(candidate["candidate_index"])
            for candidate in advanced_candidates
        ]
        advanced_candidate_set = set(advanced_candidate_numbers)
        rescue_candidate_numbers = [
            int(candidate["candidate_index"])
            for candidate in preranked_candidates
            if int(candidate["candidate_index"]) not in advanced_candidate_set
        ]

        for preranked_candidate in sorted(
            preranked_candidates,
            key=lambda item: int(item["candidate_index"]),
        ):
            print(
                "FAST PRE-RANK | "
                f"candidate={preranked_candidate['candidate_index']}/{total_candidates} | "
                f"score={float(preranked_candidate['score']):.2f} | "
                f"reasons={preranked_candidate['reasons']}"
            )
        print(
            "FAST PRE-RANK ADVANCED | "
            f"candidates={advanced_candidate_numbers} | "
            f"configured_full_evaluation_count={_get_full_evaluation_count()} | "
            f"fallback={not has_sufficient_prerank_metadata}"
        )
        for candidate_number in rescue_candidate_numbers:
            print(
                "FAST PRE-RANK SKIPPED | "
                f"candidate={candidate_number}/{total_candidates} | "
                "reason=reserved_for_rescue"
            )
        _log_performance_timing(
            stage="candidate_prerank_stage_1",
            elapsed_seconds=time.perf_counter() - stage_one_started_at,
        )

        batch_candidates: list[dict[str, object]] = []
        batch_full_evaluated_candidates = 0
        evaluated_candidate_numbers: set[int] = set()
        stage_two_started_at = time.perf_counter()
        evaluation_phases = [
            ("initial", advanced_candidate_numbers),
            ("rescue", rescue_candidate_numbers),
        ]
        for phase, candidate_numbers in evaluation_phases:
            if phase == "rescue":
                if batch_candidates or not candidate_numbers:
                    break
                print(
                    "FAST PRE-RANK RESCUE START | "
                    f"batch={batch_attempt}/2 | "
                    f"candidates={candidate_numbers}"
                )

            for candidate_index in candidate_numbers:
                twitch_clip = fresh_batch[candidate_index - 1]
                candidates_examined_count += 1
                twitch_clip_id, public_url = _normalized_twitch_identifiers(twitch_clip)
                if not twitch_clip_id:
                    candidates_rejected_count += 1
                    _log_candidate_rejection(
                        twitch_clip,
                        "invalid_candidate_identifier",
                        streamer=str(creator.get("channel") or creator.get("name") or ""),
                    )
                    continue
                attempted_clip_ids.add(twitch_clip_id)
                attempted_clip_urls.add(public_url)
                evaluated_candidate_numbers.add(candidate_index)
                if not _claim_clip_for_processing(twitch_clip):
                    candidates_rejected_count += 1
                    _log_candidate_rejection(
                        twitch_clip,
                        "duplicate_or_processing_claim_unavailable",
                        streamer=str(creator.get("channel") or creator.get("name") or ""),
                    )
                    continue
                processed_candidates_count += 1

                if phase == "rescue":
                    print(
                        "FAST PRE-RANK RESCUE CANDIDATE | "
                        f"candidate={candidate_index}/{total_candidates}"
                    )

                evaluation_result = _fully_evaluate_candidate(
                    twitch_clip=twitch_clip,
                    candidate_number=candidate_index,
                    total_candidates=total_candidates,
                    creator=creator,
                    stream=evaluation_stream,
                    stream_title=stream_title,
                    viewer_count=viewer_count,
                    persisted_clips=existing_clips,
                    generation_job_id=generation_job_id,
                    generation_worker_id=generation_worker_id,
                )
                if evaluation_result.get("memory_deferred_before_download"):
                    candidates_deferred_before_download += 1
                if evaluation_result.get("memory_rejected_after_download"):
                    candidates_rejected_after_download += 1
                if evaluation_result.get("expensive_evaluation_started"):
                    candidates_evaluated += 1
                if not evaluation_result["success"]:
                    stream_retryable_failure = True
                    if evaluation_result.get("memory_rejected_after_download"):
                        deferred_available_memory_mb = evaluation_result.get(
                            "available_memory_mb"
                        )
                        deferred_required_memory_mb = evaluation_result.get(
                            "required_memory_mb"
                        )
                    candidates_rejected_count += 1
                    failure_stage = str(
                        evaluation_result.get("failure_stage") or "unknown"
                    )
                    rejection_reason = {
                        "download": "download_failure",
                        "whisper_admission": "memory_deferral_after_download",
                        "whisper": "transcription_failure",
                        "multimodal_scoring": "multimodal_scoring_failure",
                        "model_release": "model_release_failure",
                        "candidate_construction": "candidate_construction_failure",
                    }.get(failure_stage, f"{failure_stage}_failure")
                    _log_candidate_rejection(
                        twitch_clip,
                        rejection_reason,
                        streamer=str(creator.get("channel") or creator.get("name") or ""),
                    )
                    _write_terminal_clip_history(
                        twitch_clip,
                        "failed",
                        failure_stage=evaluation_result["failure_stage"],
                        increment_retry=True,
                    )
                    print(
                        f"Candidate {candidate_index} full evaluation failed:",
                        evaluation_result["error"],
                    )
                    if (
                        evaluation_result.get(
                            "memory_rejected_after_download"
                        )
                        and not evaluation_result.get(
                            "rescue_allowed",
                            False,
                        )
                    ):
                        print(
                            "MEMORY BASELINE UNRECOVERED | "
                            f"batch={batch_attempt}/2 | "
                            "stage=after_download_cleanup"
                        )
                        return await continue_historical_search(
                            outcome_reason="memory_deferred_after_download",
                            retryable_failure=True,
                            discovery_complete=False,
                            message=(
                                "Clip generation deferred because worker "
                                "memory did not recover."
                            ),
                        )
                    continue

                clip = evaluation_result["clip"]
                if not isinstance(clip, dict):
                    candidates_rejected_count += 1
                    _log_candidate_rejection(
                        twitch_clip,
                        "candidate_construction_failure",
                        streamer=str(creator.get("channel") or creator.get("name") or ""),
                    )
                    continue
                if not _write_terminal_clip_history(clip, "fully_evaluated"):
                    candidates_rejected_count += 1
                    _log_candidate_rejection(
                        clip,
                        "history_persistence_failure",
                        streamer=str(creator.get("channel") or creator.get("name") or ""),
                    )
                    _cleanup_candidate_video(
                        clip.get("video_path"),
                        candidate_index,
                        existing_clips,
                    )
                    continue
                candidates.append(clip)
                batch_candidates.append(clip)
                batch_full_evaluated_candidates += 1
                fully_evaluated_candidates_count += 1
                if phase == "rescue":
                    print(
                        "FAST PRE-RANK RESCUE SUCCESS | "
                        f"candidate={candidate_index}/{total_candidates}"
                    )
                    break

            if phase == "rescue" and not batch_candidates:
                print(
                    "FAST PRE-RANK RESCUE EXHAUSTED | "
                    f"batch={batch_attempt}/2"
                )

        if batch_candidates:
            for candidate_index, twitch_clip in enumerate(fresh_batch, start=1):
                if candidate_index in evaluated_candidate_numbers:
                    continue
                twitch_clip_id, public_url = _normalized_twitch_identifiers(twitch_clip)
                if not twitch_clip_id:
                    continue
                attempted_clip_ids.add(twitch_clip_id)
                attempted_clip_urls.add(public_url)
                _write_terminal_clip_history(twitch_clip, "intentionally_skipped")
                candidates_examined_count += 1
                candidates_rejected_count += 1
                _log_candidate_rejection(
                    twitch_clip,
                    "pre_rank_not_advanced_after_success",
                    streamer=str(creator.get("channel") or creator.get("name") or ""),
                )

        _log_performance_timing(
            stage="candidate_full_evaluation_stage_2",
            elapsed_seconds=time.perf_counter() - stage_two_started_at,
        )
        print(
            "FULL EVALUATION SUMMARY | "
            f"batch={batch_attempt}/2 | "
            f"batch_full_evaluated_candidates={batch_full_evaluated_candidates} | "
            f"total_fully_evaluated_candidates={fully_evaluated_candidates_count} | "
            f"candidates_deferred_before_download={candidates_deferred_before_download} | "
            f"candidates_rejected_after_download={candidates_rejected_after_download} | "
            f"candidates_evaluated={candidates_evaluated}"
        )

        if candidates:
            break

        print(
            f"All clips in batch {batch_attempt} failed to download/score for "
            f"{creator['channel']}."
        )

    if not candidates:
        all_processed_candidates_deferred_for_memory = (
            processed_candidates_count > 0
            and candidates_evaluated == 0
            and (
                candidates_deferred_before_download
                + candidates_rejected_after_download
            )
            == processed_candidates_count
        )
        return await continue_historical_search(
            outcome_reason=(
                "memory_deferred"
                if all_processed_candidates_deferred_for_memory
                else "no_fully_evaluated_candidates"
            ),
            retryable_failure=(
                all_processed_candidates_deferred_for_memory
                or stream_retryable_failure
            ),
            discovery_complete=stream_discovery_complete,
            message=(
                "Clip generation deferred because worker memory did not "
                "recover."
                if all_processed_candidates_deferred_for_memory
                else "No suitable clip was found. Try again later."
            ),
        )

    print("------------------------")
    for candidate in candidates:
        print(f"Candidate {candidate['candidate_number']}: {candidate['score']}")

    winner_selection_started_at = time.perf_counter()
    best_available_tier = min(
        int(candidate.get("_search_tier", 1))
        for candidate in candidates
    )
    tier_candidates = [
        candidate
        for candidate in candidates
        if int(candidate.get("_search_tier", 1)) == best_available_tier
    ]
    best_clip = max(tier_candidates, key=lambda c: c["score"])
    print(
        "FINAL WINNER TIER | "
        f"tier={best_available_tier} | eligible_candidates={len(tier_candidates)}"
    )
    _log_performance_timing(
        stage="winner_selection",
        elapsed_seconds=time.perf_counter() - winner_selection_started_at,
    )
    for candidate in candidates:
        if candidate is best_clip:
            continue
        candidates_rejected_count += 1
        _log_candidate_rejection(
            candidate,
            "lower_score_than_selected_winner",
            streamer=str(candidate.get("creator") or ""),
        )
        _cleanup_candidate_video(
            candidate.get("video_path"),
            int(candidate.get("candidate_number", 0) or 0),
            existing_clips,
        )
    print("")
    print(f"Best Clip: #{best_clip['candidate_number']}")
    print(f"Final Score: {best_clip['score']}")
    print("------------------------")

    if best_clip["decision"] == "reject" or best_clip["score"] < AUTO_CLIP_MIN_SCORE:
        candidates_rejected_count += 1
        _log_candidate_rejection(
            best_clip,
            "score_threshold",
            streamer=str(best_clip.get("creator") or ""),
            viral_score=best_clip.get("score"),
        )
        rejection_recorded = _write_terminal_clip_history(
            best_clip,
            "rejected_low_score",
        )
        _cleanup_candidate_video(
            best_clip.get("video_path"),
            int(best_clip.get("candidate_number", 0) or 0),
            existing_clips,
        )
        if DATABASE_URL and not rejection_recorded:
            raise HTTPException(
                status_code=503,
                detail="Clip rejection could not be saved safely.",
            )
        total_elapsed = time.perf_counter() - generation_started_at
        _log_performance_timing(
            stage="generation_total",
            elapsed_seconds=total_elapsed,
        )
        print(
            "PERFORMANCE TIMING SUMMARY | "
            f"total_elapsed_seconds={total_elapsed:.3f} | "
            f"processed_candidates={processed_candidates_count} | "
            f"fully_evaluated_candidates={fully_evaluated_candidates_count}"
        )
        return await continue_historical_search(
            outcome_reason="score_threshold",
            retryable_failure=stream_retryable_failure,
            discovery_complete=stream_discovery_complete,
        )

    is_duplicate = any(
        existing.get("twitch_clip_id") == best_clip["twitch_clip_id"]
        or existing.get("public_url") == best_clip["public_url"]
        for existing in existing_clips
    )

    if is_duplicate:
        candidates_rejected_count += 1
        _log_candidate_rejection(
            best_clip,
            "duplicate",
            streamer=str(best_clip.get("creator") or ""),
            viral_score=best_clip.get("score"),
        )
        _cleanup_candidate_video(
            best_clip.get("video_path"),
            int(best_clip.get("candidate_number", 0) or 0),
            existing_clips,
        )
        total_elapsed = time.perf_counter() - generation_started_at
        _log_performance_timing(
            stage="generation_total",
            elapsed_seconds=total_elapsed,
        )
        print(
            "PERFORMANCE TIMING SUMMARY | "
            f"total_elapsed_seconds={total_elapsed:.3f} | "
            f"processed_candidates={processed_candidates_count} | "
            f"fully_evaluated_candidates={fully_evaluated_candidates_count}"
        )
        return await continue_historical_search(
            outcome_reason="duplicate",
            retryable_failure=stream_retryable_failure,
            discovery_complete=stream_discovery_complete,
        )

    best_clip.update(_select_duration_profile(best_clip))
    title_context = {
        "visual_score": best_clip.get("visual_score", 0),
        "transcript_score": best_clip.get("transcript_score", 0),
        "context_score": best_clip.get("context_score", 0),
        "score_hook": best_clip.get("score_hook", ""),
        "score_reason": best_clip.get("score_reason", ""),
        "stream_title": best_clip.get("title", ""),
        "game": best_clip.get("game", ""),
        "creator": best_clip.get("creator", ""),
        "clip_start_seconds": 0,
        "clip_end_seconds": best_clip.get("requested_duration")
        or best_clip.get("duration"),
    }
    title_generation_started_at = time.perf_counter()
    title_package = generate_ai_title_package(
        best_clip["transcript"],
        context=title_context,
    )
    best_clip.update(title_package)
    best_clip["ai_title"] = title_package["generated_title"]
    _log_performance_timing(
        stage="title_generation",
        elapsed_seconds=time.perf_counter() - title_generation_started_at,
    )
    description_generation_started_at = time.perf_counter()
    best_clip["ai_description"] = generate_ai_description(best_clip["transcript"])
    _log_performance_timing(
        stage="description_generation",
        elapsed_seconds=time.perf_counter() - description_generation_started_at,
    )
    caption_generation_started_at = time.perf_counter()
    best_clip.update(
        generate_tiktok_caption_package(
            str(best_clip.get("transcript") or ""),
            str(best_clip.get("creator") or ""),
            str(best_clip.get("game") or ""),
        )
    )
    _log_performance_timing(
        stage="caption_hashtag_generation",
        elapsed_seconds=time.perf_counter() - caption_generation_started_at,
    )
    best_clip["raw_video_path"] = best_clip.get("video_path")
    best_candidate_number = int(best_clip.get("candidate_number", 0) or 0)
    total_candidates = len(candidates)
    _set_generation_job_stage(
        generation_job_id,
        generation_worker_id,
        "rendering",
    )
    _log_memory_check(
        stage="before_visual_layout_detection",
        candidate_number=best_candidate_number,
        total_candidates=total_candidates,
    )
    layout_detection_started_at = time.perf_counter()
    visual_layout = await asyncio.to_thread(
        _detect_visual_layout_subprocess,
        str(best_clip["raw_video_path"]),
    )
    _log_memory_check(
        stage="after_visual_layout_detection",
        candidate_number=best_candidate_number,
        total_candidates=total_candidates,
    )
    _log_performance_timing(
        stage="visual_layout_detection",
        elapsed_seconds=time.perf_counter() - layout_detection_started_at,
    )
    best_clip["visual_layout_mode"] = visual_layout["mode"]
    best_clip["visual_layout_confidence"] = visual_layout["confidence"]
    best_clip["visual_layout_reason"] = visual_layout["reason"]
    best_clip["visual_layout_version"] = visual_layout["version"]
    best_clip["reaction_region"] = visual_layout.get("reaction_region")
    best_clip["content_region"] = visual_layout.get("content_region")

    try:
        title_for_overlay = best_clip.get("ai_title") or best_clip.get("title", "")
        caption_segments = best_clip.get("segments", [])
        emphasis_moments = _derive_emphasis_moments(
            transcript_segments=caption_segments,
            duration_seconds=float(best_clip.get("duration", 0) or 0),
        )

        async with app.state.video_edit_lock:
            visual_layout = _apply_visual_layout_memory_fallback(
                visual_layout,
                _get_available_memory_mb(),
            )
            best_clip["visual_layout_mode"] = visual_layout["mode"]
            best_clip["visual_layout_confidence"] = visual_layout["confidence"]
            best_clip["visual_layout_reason"] = visual_layout["reason"]
            best_clip["visual_layout_version"] = visual_layout["version"]
            best_clip["reaction_region"] = visual_layout.get("reaction_region")
            best_clip["content_region"] = visual_layout.get("content_region")
            _log_memory_check(
                stage="before_ffmpeg_edit",
                candidate_number=best_candidate_number,
                total_candidates=total_candidates,
            )
            ffmpeg_started_at = time.perf_counter()
            visual_layout_render_started_at = time.perf_counter()
            edited_video_path = await asyncio.to_thread(
                create_tiktok_edited_video,
                best_clip["raw_video_path"],
                title_for_overlay,
                caption_segments,
                emphasis_moments=emphasis_moments,
                selected_duration_seconds=float(
                    best_clip.get("requested_duration") or 0
                ),
                visual_layout=visual_layout,
            )
            _log_performance_timing(
                stage="visual_layout_render",
                elapsed_seconds=time.perf_counter() - visual_layout_render_started_at,
            )
            _log_performance_timing(
                stage="ffmpeg_editing",
                candidate_number=best_candidate_number,
                total_candidates=total_candidates,
                elapsed_seconds=time.perf_counter() - ffmpeg_started_at,
            )
            _log_memory_check(
                stage="after_ffmpeg_edit",
                candidate_number=best_candidate_number,
                total_candidates=total_candidates,
            )

        best_clip["video_path"] = edited_video_path
    except Exception as error:
        print("TIKTOK VIDEO EDIT FAILED:", repr(error))
        print(traceback.format_exc())
        _log_candidate_rejection(
            best_clip,
            "editing_failure_raw_fallback",
            streamer=str(best_clip.get("creator") or ""),
            viral_score=best_clip.get("score"),
        )
        best_clip["video_path"] = best_clip.get("raw_video_path")
    finally:
        gc.collect()
        _trim_native_memory("ffmpeg_completion")

    if object_storage_enabled():
        _set_generation_job_stage(
            generation_job_id,
            generation_worker_id,
            "uploading",
        )
        try:
            storage_result = await asyncio.to_thread(
                upload_video,
                str(best_clip.get("video_path") or ""),
                str(best_clip.get("twitch_clip_id") or uuid.uuid4()),
            )
            best_clip.update(storage_result)
        except Exception:
            _log_candidate_rejection(
                best_clip,
                "object_storage_upload_failure",
                streamer=str(best_clip.get("creator") or ""),
                viral_score=best_clip.get("score"),
            )
            # Keep local media and fail persistence closed when durable storage
            # was explicitly enabled but could not confirm the upload.
            raise HTTPException(
                status_code=503,
                detail="Rendered clip could not be uploaded to durable storage.",
            )

    _log_memory_check(
        stage="before_clip_persistence",
        candidate_number=int(best_clip.get("candidate_number", 0) or 0),
        total_candidates=len(candidates),
    )
    persistence_started_at = time.perf_counter()
    try:
        # Background workers persist generated records in PostgreSQL. Avoid
        # writing worker-local legacy JSON, which is neither shared nor durable
        # across the web and worker services.
        result = {
            "success": True,
            "clip": _build_generated_clip_record(best_clip),
        }
    except Exception as error:
        print(
            "CLIP PERSISTENCE FAILED - MEDIA RETAINED FOR RECOVERY | "
            f"raw_video_path={best_clip.get('raw_video_path')} | "
            f"video_path={best_clip.get('video_path')}"
        )
        if best_clip.get("object_key"):
            canonical_clip_id, _ = _normalized_twitch_identifiers(best_clip)
            _log_queue_persistence_recovery(
                canonical_clip_id,
                best_clip.get("object_key"),
                error,
            )
        raise
    persisted_clip = result["clip"]
    queue_clip = {**best_clip, **persisted_clip}
    if not _persist_generated_clip_record(queue_clip):
        raise HTTPException(
            status_code=503,
            detail="Rendered clip could not be saved to the durable queue.",
        )
    _log_performance_timing(
        stage="persistence",
        elapsed_seconds=time.perf_counter() - persistence_started_at,
    )
    _log_memory_check(
        stage="after_clip_persistence",
        candidate_number=int(best_clip.get("candidate_number", 0) or 0),
        total_candidates=len(candidates),
    )

    total_elapsed = time.perf_counter() - generation_started_at
    _log_performance_timing(
        stage="generation_total",
        elapsed_seconds=total_elapsed,
    )
    print(
        "PERFORMANCE TIMING SUMMARY | "
        f"total_elapsed_seconds={total_elapsed:.3f} | "
        f"processed_candidates={processed_candidates_count} | "
        f"fully_evaluated_candidates={fully_evaluated_candidates_count}"
    )
    update_stream_progress(
        str(broadcaster_id),
        stream_id,
        processing_state="succeeded",
        range_start=stream_range_start,
        range_end=stream_range_end,
        retryable_failure_state=None,
        checked_complete=stream_discovery_complete,
    )
    result["clip"]["_job_candidates_examined"] = (
        _prior_candidates_examined + candidates_examined_count
    )
    result["clip"]["_job_candidates_rejected"] = (
        _prior_candidates_rejected + candidates_rejected_count
    )
    print(
        "STREAM SEARCH RESULT | "
        f"stream_id={stream_id} | result=clip_created"
    )
    print(
        "STREAM SEARCH COMPLETE | "
        f"streams_checked={streams_examined} | clip_created=true"
    )
    return result["clip"]

@app.post("/api/clips/{clip_id}/publish")
async def publish_clip_by_id(clip_id: str):
    return await publish_clip_to_tiktok({"id": clip_id})

@app.get("/auth/twitch")
async def twitch_login():
    params = {
        "client_id": TWITCH_CLIENT_ID,
        "redirect_uri": TWITCH_REDIRECT_URI,
        "response_type": "code",
        "scope": "clips:edit",
    }

    return RedirectResponse(
        "https://id.twitch.tv/oauth2/authorize?" + urlencode(params)
    )

@app.get("/auth/twitch/callback")
async def twitch_callback(code: str):
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            "https://id.twitch.tv/oauth2/token",
            data={
                "client_id": TWITCH_CLIENT_ID,
                "client_secret": TWITCH_CLIENT_SECRET,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": TWITCH_REDIRECT_URI,
            },
        )

    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Twitch token exchange failed: {response.text}",
        )

    token_data = response.json()

    normalized_token_data = {
        "access_token": token_data.get("access_token"),
        "refresh_token": token_data.get("refresh_token"),
        "expires_in": token_data.get("expires_in"),
        "scope": token_data.get("scope", []),
    }
    _save_provider_token_data("twitch", TWITCH_USER_TOKEN_FILE, normalized_token_data)

    return {
        "success": True,
        "message": "Twitch account connected. You may close this tab.",
    }

@app.get("/api/tiktok/login")
async def tiktok_login():
    state = secrets.token_urlsafe(24)
    verifier, challenge = _generate_pkce_pair()
    app.state.tiktok_pkce_verifiers[state] = verifier

    authorization_url = _get_tiktok_authorization_url(
        state=state,
        code_challenge=challenge,
    )

    return RedirectResponse(url=authorization_url)

@app.get("/api/tiktok/callback")
async def tiktok_callback(code: str, state: str):
    client_key = os.getenv("TIKTOK_CLIENT_KEY")
    client_secret = os.getenv("TIKTOK_CLIENT_SECRET")
    redirect_uri = os.getenv("TIKTOK_REDIRECT_URI")

    if not client_key or not client_secret or not redirect_uri:
        raise HTTPException(
            status_code=500,
            detail="TikTok OAuth configuration is incomplete.",
        )

    code_verifier = app.state.tiktok_pkce_verifiers.pop(state, None)
    if not code_verifier:
        raise HTTPException(
            status_code=400,
            detail="Invalid or expired TikTok OAuth state.",
        )

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            "https://open.tiktokapis.com/v2/oauth/token/",
            data={
                "client_key": client_key,
                "client_secret": client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            },
        )

    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail="TikTok token exchange failed.",
        )

    token_data = response.json()
    if not isinstance(token_data, dict):
        raise HTTPException(
            status_code=502,
            detail="TikTok token exchange returned an invalid payload.",
        )

    normalized_token_data = _normalize_tiktok_token_data(token_data)
    if not _extract_tiktok_token_fields(normalized_token_data).get("access_token"):
        raise HTTPException(
            status_code=502,
            detail="TikTok token exchange returned an access token error.",
        )

    _save_provider_token_data("tiktok", TIKTOK_USER_TOKEN_FILE, normalized_token_data)

    return {
        "success": True,
        "message": "TikTok account connected successfully.",
    }
