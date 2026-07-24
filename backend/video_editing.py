from pathlib import Path
import shutil
import subprocess
from typing import Dict, List, Optional


def _escape_drawtext_text(value: str) -> str:
    text = value or ""
    text = text.replace("\\", r"\\")
    text = text.replace("'", r"\'")
    text = text.replace(":", r"\:")
    text = text.replace("%", r"\%")
    text = text.replace("\n", " ")
    return text


def _build_drawtext_filter(title: str, transcript_segments: List[Dict[str, object]]) -> str:
    filters = [
        "scale=1080:1920:force_original_aspect_ratio=increase",
        "crop=1080:1920",
    ]

    safe_title = _escape_drawtext_text((title or "").strip())
    if safe_title:
        filters.append(
            "drawtext="
            "fontcolor=white:fontsize=64:line_spacing=8:"
            "box=1:boxcolor=black@0.45:boxborderw=24:"
            f"text='{safe_title}':"
            "x=(w-text_w)/2:y=120"
        )

    # Keep captions readable and bounded so the ffmpeg filter does not grow too large.
    for segment in transcript_segments[:80]:
        text = str(segment.get("text", "")).strip()
        if not text:
            continue

        try:
            start = float(segment.get("start", 0.0) or 0.0)
            end = float(segment.get("end", start) or start)
        except (TypeError, ValueError):
            continue

        if end <= start:
            end = start + 0.8

        safe_text = _escape_drawtext_text(text)
        filters.append(
            "drawtext="
            "fontcolor=white:fontsize=46:line_spacing=6:"
            "box=1:boxcolor=black@0.55:boxborderw=16:"
            f"text='{safe_text}':"
            "x=(w-text_w)/2:y=(h*0.72):"
            f"enable='between(t,{start:.2f},{end:.2f})'"
        )

    return ",".join(filters)


def create_tiktok_edited_video(
    raw_video_path: str,
    title: str,
    transcript_segments: List[Dict[str, object]],
    output_path: Optional[str] = None,
) -> str:
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        raise RuntimeError("ffmpeg is not installed or not available in PATH.")

    raw_path = Path(raw_video_path)
    if not raw_path.is_file():
        raise RuntimeError(f"Raw video file not found: {raw_video_path}")

    if output_path:
        edited_path = Path(output_path)
    else:
        edited_dir = raw_path.parent / "edited"
        edited_dir.mkdir(parents=True, exist_ok=True)
        edited_path = edited_dir / f"{raw_path.stem}_tiktok.mp4"

    filter_chain = _build_drawtext_filter(title=title, transcript_segments=transcript_segments)

    command = [
        ffmpeg_path,
        "-y",
        "-i",
        str(raw_path),
        "-vf",
        filter_chain,
        "-r",
        "30",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "21",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(edited_path),
    ]

    try:
        subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.CalledProcessError as error:
        stderr = (error.stderr or "").strip()
        raise RuntimeError(f"ffmpeg editing failed: {stderr}") from error

    if not edited_path.is_file() or edited_path.stat().st_size == 0:
        raise RuntimeError(f"Edited video was not created: {edited_path}")

    return str(edited_path)
