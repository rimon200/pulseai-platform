from pathlib import Path
import os
import shutil
import subprocess
import tempfile
from typing import Dict, List, Optional


def _escape_filter_path(path: Path) -> str:
    value = str(path)
    value = value.replace("\\", r"\\")
    value = value.replace("'", r"\'")
    value = value.replace(":", r"\:")
    value = value.replace(",", r"\,")
    return value


def _seconds_to_ass_time(seconds: float) -> str:
    total = max(0.0, float(seconds))
    hours = int(total // 3600)
    minutes = int((total % 3600) // 60)
    whole_seconds = int(total % 60)
    centiseconds = int(round((total - int(total)) * 100))
    if centiseconds >= 100:
        centiseconds = 0
        whole_seconds += 1
    if whole_seconds >= 60:
        whole_seconds = 0
        minutes += 1
    if minutes >= 60:
        minutes = 0
        hours += 1
    return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{centiseconds:02d}"


def _escape_ass_text(value: str) -> str:
    text = value or ""
    text = text.replace("\\", r"\\")
    text = text.replace("{", r"\{")
    text = text.replace("}", r"\}")
    text = text.replace("\n", r"\N")
    return text


def _build_ass_subtitles_content(transcript_segments: List[Dict[str, object]]) -> str:
    header = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "PlayResX: 1080",
        "PlayResY: 1920",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
        "Style: Caption,Arial,46,&H00FFFFFF,&H000000FF,&H00000000,&H70000000,1,0,0,0,100,100,0,0,1,2,0,2,60,60,420,1",
        "",
        "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]

    events = []
    for segment in transcript_segments[:120]:
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

        ass_text = _escape_ass_text(text)
        events.append(
            "Dialogue: "
            f"0,{_seconds_to_ass_time(start)},{_seconds_to_ass_time(end)},"
            f"Caption,,0,0,0,,{ass_text}"
        )

    return "\n".join(header + events) + "\n"


def _build_filter_chain(title_file_path: Path, subtitle_file_path: Path) -> str:
    filters = [
        "scale=1080:1920:force_original_aspect_ratio=increase",
        "crop=1080:1920",
    ]

    if title_file_path and title_file_path.is_file() and title_file_path.stat().st_size > 0:
        safe_title_file = _escape_filter_path(title_file_path)
        filters.append(
            "drawtext="
            "fontcolor=white:fontsize=64:line_spacing=8:"
            "box=1:boxcolor=black@0.45:boxborderw=24:"
            f"textfile='{safe_title_file}':"
            "x=(w-text_w)/2:y=120"
        )

    safe_subtitle_file = _escape_filter_path(subtitle_file_path)
    filters.append(f"subtitles='{safe_subtitle_file}'")

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

    title_temp_path = None
    subtitle_temp_path = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".txt",
            prefix="title_",
            dir=str(edited_path.parent),
            delete=False,
        ) as title_temp:
            title_temp.write((title or "").strip())
            title_temp_path = Path(title_temp.name)

        subtitles_content = _build_ass_subtitles_content(transcript_segments)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".ass",
            prefix="captions_",
            dir=str(edited_path.parent),
            delete=False,
        ) as subtitle_temp:
            subtitle_temp.write(subtitles_content)
            subtitle_temp_path = Path(subtitle_temp.name)

        filter_chain = _build_filter_chain(
            title_file_path=title_temp_path,
            subtitle_file_path=subtitle_temp_path,
        )

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
    finally:
        for temporary_path in (title_temp_path, subtitle_temp_path):
            if temporary_path and temporary_path.exists():
                try:
                    os.remove(temporary_path)
                except OSError:
                    pass
