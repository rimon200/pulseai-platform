from __future__ import annotations

import asyncio
import gc
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from youtube_uploads import (
    YouTubeSourceError,
    authorized_hosts_for_upload,
    claim_youtube_upload,
    get_youtube_upload,
    resolve_upload_source,
    select_clip_candidates,
    set_upload_processing_result,
    validate_authorized_media_url,
    youtube_config,
)


def _safe_download_name(video_id: object) -> str:
    normalized = "".join(
        character for character in str(video_id or "")
        if character.isalnum() or character in {"_", "-"}
    )
    if not normalized:
        raise YouTubeSourceError("source_unavailable", "YouTube video ID is invalid.")
    return f"youtube_{normalized}.mp4"


async def _download_authorized_source(
    url: str, destination: Path, allowed_hosts: set[str],
) -> int:
    current_url = url
    transferred = 0
    timeout = httpx.Timeout(30.0, read=120.0)
    maximum_bytes = max(
        100 * 1024 * 1024,
        int(os.getenv("YOUTUBE_MAX_SOURCE_BYTES", str(8 * 1024**3))),
    )
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for _ in range(4):
            validate_authorized_media_url(current_url, allowed_hosts=allowed_hosts)
            async with client.stream("GET", current_url) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise YouTubeSourceError(
                            "source_unavailable", "Approved source returned an invalid redirect."
                        )
                    current_url = urljoin(current_url, location)
                    continue
                if response.status_code >= 400:
                    raise YouTubeSourceError(
                        "source_unavailable", "Approved source download was rejected."
                    )
                content_length = int(response.headers.get("content-length") or 0)
                if content_length > maximum_bytes:
                    raise YouTubeSourceError(
                        "source_unavailable", "Approved source exceeds the configured size limit."
                    )
                with destination.open("xb") as output:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        transferred += len(chunk)
                        if transferred > maximum_bytes:
                            raise YouTubeSourceError(
                                "source_unavailable",
                                "Approved source exceeds the configured size limit.",
                            )
                        output.write(chunk)
                return transferred
    raise YouTubeSourceError("source_host_rejected", "Too many source redirects.")


def _extract_segment(
    source_path: Path, destination: Path, start: float, duration: float,
    *, copy_streams: bool,
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is unavailable.")
    command = [
        ffmpeg, "-y", "-threads", "1", "-ss", f"{start:.3f}",
        "-i", str(source_path), "-t", f"{duration:.3f}",
    ]
    if copy_streams:
        command.extend(["-map", "0:v:0", "-map", "0:a:0?", "-c", "copy"])
    else:
        command.extend([
            "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264",
            "-preset", "ultrafast", "-crf", "24", "-c:a", "aac", "-b:a", "128k",
        ])
    command.append(str(destination))
    completed = subprocess.run(
        command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True, timeout=max(120, int(duration * 3)),
    )
    if completed.returncode != 0 or not destination.is_file():
        raise RuntimeError(
            "YouTube source segment extraction failed: "
            f"{completed.stderr[-1000:].strip()}"
        )


def _analysis_offsets(duration: float) -> list[float]:
    sample_duration = min(300.0, duration)
    if duration <= sample_duration:
        return [0.0]
    sample_count = min(8, max(3, int(duration // 900) + 1))
    usable_start = min(60.0, duration * 0.05)
    usable_end = max(usable_start, duration - sample_duration - 60.0)
    if sample_count == 1 or usable_end <= usable_start:
        return [usable_start]
    return [
        usable_start + (usable_end - usable_start) * index / (sample_count - 1)
        for index in range(sample_count)
    ]


async def process_youtube_job(
    job: dict[str, Any], worker_id: str,
) -> dict[str, Any]:
    import main
    from storage_service import object_storage_enabled, upload_video
    from video_editing import create_tiktok_edited_video

    database_url = os.getenv("DATABASE_URL", "").strip()
    upload_id = str(job.get("source_upload_id") or "")
    upload = get_youtube_upload(database_url, upload_id)
    if not upload:
        return {"status": "failed", "error_message": "YouTube upload was not found."}
    claimed = claim_youtube_upload(database_url, upload_id, worker_id)
    if not claimed:
        if str(upload.get("processing_status")) == "completed":
            return {
                "status": "completed", "outcome": "no_clip_found",
                "error_message": "YouTube upload was already processed.",
            }
        return {"status": "failed", "error_message": "YouTube upload claim failed."}
    upload = get_youtube_upload(database_url, upload_id) or upload
    source_path: Path | None = None
    downloaded_source = False
    generated_paths: list[Path] = []
    analysis_paths: list[Path] = []
    created_ids: list[str] = []
    bytes_uploaded = 0
    started = time.perf_counter()
    config = youtube_config()
    requested = min(config["clips_per_video"], config["max_clips_per_video"])
    try:
        source_type, reference = resolve_upload_source(upload)
        set_upload_processing_result(database_url, upload_id, status="downloading")
        if source_type == "manual_upload":
            source_path = Path(reference).resolve()
        else:
            download_root = Path(__file__).resolve().parent / "downloads"
            download_root.mkdir(parents=True, exist_ok=True)
            source_path = download_root / _safe_download_name(upload["platform_video_id"])
            if source_path.exists():
                source_path.unlink()
            await _download_authorized_source(
                reference, source_path, authorized_hosts_for_upload(upload),
            )
            downloaded_source = True
        set_upload_processing_result(database_url, upload_id, status="analyzing")
        absolute_segments: list[dict[str, Any]] = []
        duration = float(upload.get("duration_seconds") or 0)
        for sample_number, offset in enumerate(_analysis_offsets(duration), start=1):
            descriptor, sample_name = tempfile.mkstemp(
                prefix=f"youtube_analysis_{sample_number}_", suffix=".mp4"
            )
            os.close(descriptor)
            os.unlink(sample_name)
            sample_path = Path(sample_name)
            analysis_paths.append(sample_path)
            sample_duration = min(300.0, duration - offset)
            await asyncio.to_thread(
                _extract_segment, source_path, sample_path, offset, sample_duration,
                copy_streams=True,
            )
            transcription = await asyncio.to_thread(
                main._transcribe_video_with_segments_subprocess, str(sample_path)
            )
            for segment in transcription.get("segments") or []:
                absolute_segments.append({
                    **segment,
                    "start": float(segment.get("start") or 0) + offset,
                    "end": float(segment.get("end") or 0) + offset,
                })
            sample_path.unlink(missing_ok=True)
        candidates = select_clip_candidates(
            absolute_segments, duration, requested=requested,
        )
        print(
            "YOUTUBE ANALYSIS SUMMARY | "
            f"video_id={upload['platform_video_id']} | "
            f"duration_seconds={int(duration)} | candidates={len(candidates)} | "
            f"selected={min(requested, len(candidates))} | "
            f"analysis_seconds={time.perf_counter() - started:.3f}"
        )
        set_upload_processing_result(
            database_url, upload_id, status="generating",
            clips_requested=requested, diagnostics=candidates,
        )
        for index, candidate in enumerate(candidates[:requested], start=1):
            descriptor, raw_name = tempfile.mkstemp(
                prefix=f"youtube_candidate_{index}_", suffix=".mp4"
            )
            os.close(descriptor)
            os.unlink(raw_name)
            raw_path = Path(raw_name)
            generated_paths.append(raw_path)
            await asyncio.to_thread(
                _extract_segment, source_path, raw_path,
                float(candidate["start_seconds"]),
                float(candidate["duration_seconds"]), copy_streams=False,
            )
            transcription = await asyncio.to_thread(
                main._transcribe_video_with_segments_subprocess, str(raw_path)
            )
            transcript = str(transcription.get("transcript") or candidate["transcript"])
            score = await asyncio.to_thread(
                main._score_multimodal_clip_subprocess,
                str(raw_path), transcript,
                str(upload.get("platform_display_name") or ""), "YouTube",
                str(upload.get("title") or ""), 0, candidate["duration_seconds"],
            )
            if score.get("decision") == "reject" or int(score.get("score") or 0) < main.AUTO_CLIP_MIN_SCORE:
                continue
            title_package = main.generate_ai_title_package(
                transcript,
                context={
                    "creator": upload.get("platform_display_name"),
                    "stream_title": upload.get("title"),
                    "clip_start_seconds": candidate["start_seconds"],
                    "clip_end_seconds": candidate["end_seconds"],
                    "score_reason": score.get("reason"),
                },
            )
            caption_package = main.generate_tiktok_caption_package(
                transcript, str(upload.get("platform_display_name") or ""), "YouTube"
            )
            layout = await asyncio.to_thread(
                main._detect_visual_layout_subprocess, str(raw_path)
            )
            output_path = raw_path.with_name(f"{raw_path.stem}_edited.mp4")
            generated_paths.append(output_path)
            edited = await asyncio.to_thread(
                create_tiktok_edited_video,
                str(raw_path), title_package["generated_title"],
                transcription.get("segments") or [], str(output_path),
                emphasis_moments=main._derive_emphasis_moments(
                    transcription.get("segments") or [], candidate["duration_seconds"]
                ),
                selected_duration_seconds=float(candidate["duration_seconds"]),
                visual_layout=layout,
                creator=str(upload.get("platform_display_name") or ""),
                topic=str(upload.get("title") or "")[:60],
            )
            storage = {}
            object_identity = (
                f"youtube-{upload['platform_video_id']}-"
                f"{int(candidate['start_seconds'] * 1000)}-"
                f"{int(candidate['end_seconds'] * 1000)}"
            )
            if object_storage_enabled():
                storage = await asyncio.to_thread(upload_video, edited, object_identity)
                bytes_uploaded += int(storage.get("transferred_bytes") or 0)
                main.record_generation_job_outbound_bytes(
                    str(job.get("id") or ""),
                    int(storage.get("transferred_bytes") or 0),
                    "r2",
                )
            record = {
                "id": str(__import__("uuid").uuid4()),
                "provider": "youtube", "source_platform": "youtube",
                "youtube_video_id": upload["platform_video_id"],
                "clip_start_seconds": candidate["start_seconds"],
                "clip_end_seconds": candidate["end_seconds"],
                "creator_id": upload["creator_id"],
                "source_creator_id": upload.get("platform_user_id"),
                "creator": upload.get("platform_display_name"),
                "title": upload.get("title"), "game": "YouTube",
                "created_at": upload.get("published_at"),
                "thumbnail_url": upload.get("thumbnail_url"),
                "score": int(score.get("score") or 0),
                "viral_score": int(score.get("score") or 0),
                "transcript": transcript,
                "segments": transcription.get("segments") or [],
                "duration": candidate["duration_seconds"],
                "actual_duration": candidate["duration_seconds"],
                "requested_duration": candidate["duration_seconds"],
                "duration_profile": "long",
                "raw_video_path": str(raw_path), "video_path": edited,
                "ai_title": title_package["generated_title"],
                **title_package, **caption_package, **storage,
                "visual_layout_mode": layout.get("mode"),
                "visual_layout_confidence": layout.get("confidence"),
                "visual_layout_reason": layout.get("reason"),
                "visual_layout_version": layout.get("version"),
                "reaction_region": layout.get("reaction_region"),
                "content_region": layout.get("content_region"),
                "rights_status": "operator_authorized",
            }
            saved = main._persist_generated_clip_record(record)
            generated_id = str((saved or {}).get("generated_clip_id") or "")
            if not generated_id or not main._verify_generated_clip_retrieval(generated_id):
                raise RuntimeError("Generated YouTube clip persistence verification failed.")
            created_ids.append(generated_id)
        final_status = "completed" if created_ids else "skipped"
        set_upload_processing_result(
            database_url, upload_id, status=final_status,
            clips_requested=requested, clips_created=len(created_ids),
            diagnostics=candidates,
        )
        print(
            "YOUTUBE GENERATION SUMMARY | "
            f"video_id={upload['platform_video_id']} | requested={requested} | "
            f"created={len(created_ids)} | failed={max(0, requested-len(created_ids))} | "
            f"bytes_uploaded={bytes_uploaded}"
        )
        return {
            "status": "completed",
            "outcome": "clip_created" if created_ids else "no_clip_found",
            "result_clip_id": created_ids[0] if created_ids else None,
            "result_clip_ids": created_ids,
            "candidates_examined": len(candidates),
            "candidates_rejected": max(0, len(candidates) - len(created_ids)),
        }
    except YouTubeSourceError as error:
        set_upload_processing_result(
            database_url, upload_id, status="failed", error=error.code,
        )
        print(
            "YOUTUBE SOURCE STATUS | "
            f"creator_id={upload.get('creator_id')} | "
            f"video_id={upload.get('platform_video_id')} | status="
            f"{'rejected' if error.code == 'source_host_rejected' else 'unavailable'}"
        )
        return {"status": "failed", "error_message": str(error), "pipeline_reason": error.code}
    except Exception as error:
        set_upload_processing_result(
            database_url, upload_id, status="failed",
            error=error.__class__.__name__,
        )
        print(
            "YOUTUBE GENERATION SUMMARY | "
            f"video_id={upload.get('platform_video_id')} | requested={requested} | "
            f"created={len(created_ids)} | failed={requested} | bytes_uploaded={bytes_uploaded} | "
            f"error_type={error.__class__.__name__}"
        )
        return {
            "status": "failed",
            "error_message": "YouTube generation failed during authorized media processing.",
            "pipeline_reason": error.__class__.__name__,
        }
    finally:
        for path in [*analysis_paths, *generated_paths]:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        if downloaded_source and source_path is not None:
            try:
                source_path.unlink(missing_ok=True)
            except OSError:
                pass
        gc.collect()
