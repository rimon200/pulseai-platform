from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
from typing import Dict, List, Optional


CANVAS_WIDTH = 720
CANVAS_HEIGHT = 1280
TITLE_PANEL_HEIGHT = 240
VIDEO_REGION_TOP = TITLE_PANEL_HEIGHT
VIDEO_REGION_HEIGHT = CANVAS_HEIGHT - VIDEO_REGION_TOP
TITLE_HORIZONTAL_PADDING = 48
TITLE_MAX_LINES = 2
TITLE_PREFERRED_MAX_FONT_SIZE = 54
TITLE_MIN_FONT_SIZE = 38
TITLE_DEFAULT_EMOJI = "😳"
TITLE_EMOJI_PATTERN = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]")


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


def _wrap_caption_text(value: str, max_chars_per_line: int = 26) -> str:
    cleaned = " ".join((value or "").split())
    if not cleaned:
        return ""

    lines = textwrap.wrap(
        cleaned,
        width=max_chars_per_line,
        break_long_words=False,
        break_on_hyphens=False,
    )
    if len(lines) > 2:
        lines = lines[:2]
        lines[-1] = lines[-1].rstrip(" .,!?:;") + "..."
    return "\\N".join(lines)


def _collapse_whitespace(value: str) -> str:
    return " ".join((value or "").split())


def _extract_trailing_emoji(value: str) -> str:
    emoji_matches = TITLE_EMOJI_PATTERN.findall(value or "")
    if not emoji_matches:
        return TITLE_DEFAULT_EMOJI
    return emoji_matches[-1]


def _remove_all_emojis(value: str) -> str:
    return TITLE_EMOJI_PATTERN.sub("", value or "")


def _build_ass_subtitles_content(transcript_segments: List[Dict[str, object]]) -> str:
    header = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "PlayResX: 720",
        "PlayResY: 1280",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
        "Style: Caption,Arial,40,&H00FFFFFF,&H000000FF,&H00000000,&H99000000,1,0,0,0,100,100,0,0,3,2.8,0,2,52,52,210,1",
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

        wrapped_text = _wrap_caption_text(text)
        if not wrapped_text:
            continue
        ass_text = _escape_ass_text(wrapped_text)
        events.append(
            "Dialogue: "
            f"0,{_seconds_to_ass_time(start)},{_seconds_to_ass_time(end)},"
            f"Caption,,0,0,0,,{{\\q2}}{ass_text}"
        )

    return "\n".join(header + events) + "\n"


def _build_title_ass_content(title_text: str, title_font_size: int) -> str:
    header = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {CANVAS_WIDTH}",
        f"PlayResY: {CANVAS_HEIGHT}",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
        f"Style: TopTitle,Arial,{title_font_size},&H00000000,&H00000000,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,0,0,8,{TITLE_HORIZONTAL_PADDING},{TITLE_HORIZONTAL_PADDING},84,1",
        "",
        "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]

    cleaned_title = _escape_ass_text(title_text)
    if cleaned_title:
        event = (
            "Dialogue: "
            "0,0:00:00.00,9:59:59.00,TopTitle,,0,0,0,,"
            f"{{\\q2}}{cleaned_title}"
        )
        return "\n".join(header + [event]) + "\n"

    return "\n".join(header) + "\n"


def _normalize_emphasis_moments(
    emphasis_moments: Optional[List[Dict[str, object]]],
) -> List[Dict[str, float]]:
    normalized: List[Dict[str, float]] = []
    if not emphasis_moments:
        return [{"start": 0.0, "end": 2.0, "zoom": 1.10}]

    for moment in emphasis_moments:
        if not isinstance(moment, dict):
            continue
        try:
            start = float(moment.get("start", 0.0) or 0.0)
            end = float(moment.get("end", start + 1.2) or (start + 1.2))
            zoom = float(moment.get("zoom", 1.06) or 1.06)
        except (TypeError, ValueError):
            continue

        if end <= start:
            end = start + 1.0
        zoom = min(1.10, max(1.03, zoom))
        normalized.append(
            {
                "start": max(0.0, start),
                "end": max(0.0, end),
                "zoom": zoom,
            }
        )

    if not normalized:
        return [{"start": 0.0, "end": 2.0, "zoom": 1.10}]

    normalized.sort(key=lambda item: item["start"])
    return normalized[:4]


def _build_zoom_expression(emphasis_moments: List[Dict[str, float]]) -> str:
    expression = "1.0"
    for moment in reversed(emphasis_moments):
        start = max(0.0, float(moment["start"]))
        end = max(start, float(moment["end"]))
        zoom = min(1.10, max(1.03, float(moment["zoom"])))
        expression = f"if(between(t,{start:.3f},{end:.3f}),{zoom:.3f},{expression})"
    return expression


def _ensure_subtitles_filter_available(ffmpeg_path: str) -> None:
    try:
        result = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-filters"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.CalledProcessError as error:
        raise RuntimeError(
            "Unable to verify ffmpeg subtitle capability. "
            "The subtitles filter is required for title and caption burn-in."
        ) from error

    filters_output = f"{result.stdout}\n{result.stderr}".lower()
    if " subtitles " not in filters_output and "\nsubtitles" not in filters_output:
        raise RuntimeError(
            "ffmpeg subtitles filter is unavailable. "
            "Install/build ffmpeg with libass to render required title and captions."
        )


def _build_filter_chain(
    title_ass_file_path: Path,
    subtitle_file_path: Path,
    title_font_size: int,
    emphasis_moments: Optional[List[Dict[str, object]]] = None,
) -> str:
    normalized_moments = _normalize_emphasis_moments(emphasis_moments)
    zoom_expression = _build_zoom_expression(normalized_moments)

    # Add headroom for max zoom so dynamic crop can always sample without stretch.
    zoom_headroom = 1.12
    scaled_width = int(CANVAS_WIDTH * zoom_headroom)
    scaled_height = int(VIDEO_REGION_HEIGHT * zoom_headroom)
    crop_w_expr = f"{CANVAS_WIDTH}/({zoom_expression})"
    crop_h_expr = f"{VIDEO_REGION_HEIGHT}/({zoom_expression})"

    filters = [
        "fps=24,"
        f"scale={scaled_width}:{scaled_height}:force_original_aspect_ratio=increase,"
        f"crop=w='{crop_w_expr}':h='{crop_h_expr}':"
        "x='(in_w-w)/2 + ((in_w-w)/8)*sin(t*0.45)':"
        "y='(in_h-h)/2',"
        f"scale={CANVAS_WIDTH}:{VIDEO_REGION_HEIGHT}"
        "[video_region]",
        f"color=c=white:s={CANVAS_WIDTH}x{CANVAS_HEIGHT}[base]",
        f"[base][video_region]overlay=x=0:y={VIDEO_REGION_TOP}:shortest=1:eof_action=endall[composed]",
    ]

    safe_title_ass_file = _escape_filter_path(title_ass_file_path)
    filters.append(f"[composed]subtitles=filename='{safe_title_ass_file}'[titled]")

    safe_subtitle_file = _escape_filter_path(subtitle_file_path)
    filters.append(f"[titled]subtitles=filename='{safe_subtitle_file}'")

    return ";".join(filters)


def _prepare_title_text(value: str) -> tuple[str, int, int]:
    title_text = _collapse_whitespace(value)
    title_emoji = _extract_trailing_emoji(title_text)
    title_body = _collapse_whitespace(_remove_all_emojis(title_text)).strip(".,;:!?")
    if not title_body:
        title_body = "Unexpected Stream Moment"

    selected_lines = [f"{title_body} {title_emoji}"]
    selected_font_size = TITLE_MIN_FONT_SIZE

    for font_size in range(TITLE_PREFERRED_MAX_FONT_SIZE, TITLE_MIN_FONT_SIZE - 1, -1):
        approx_char_width = max(8.0, font_size * 0.60)
        max_chars_per_line = max(
            14,
            int((CANVAS_WIDTH - (TITLE_HORIZONTAL_PADDING * 2)) / approx_char_width),
        )

        fitted_body = title_body
        candidate_text = f"{fitted_body} {title_emoji}".strip()
        candidate_lines = textwrap.wrap(
            candidate_text,
            width=max_chars_per_line,
            break_long_words=False,
            break_on_hyphens=False,
        )

        if len(candidate_lines) > TITLE_MAX_LINES:
            body_limit = max(
                8,
                (max_chars_per_line * TITLE_MAX_LINES) - len(title_emoji) - 1,
            )
            truncated_body = fitted_body
            if len(truncated_body) > body_limit:
                truncated_body = truncated_body[:body_limit].rstrip()
                if " " in truncated_body:
                    truncated_body = truncated_body.rsplit(" ", 1)[0]
                truncated_body = truncated_body.rstrip(".,;:!?")
                if truncated_body:
                    truncated_body = f"{truncated_body}..."
                else:
                    truncated_body = "Stream Moment"

            candidate_text = f"{truncated_body} {title_emoji}".strip()
            candidate_lines = textwrap.wrap(
                candidate_text,
                width=max_chars_per_line,
                break_long_words=False,
                break_on_hyphens=False,
            )

        if len(candidate_lines) <= TITLE_MAX_LINES:
            selected_lines = candidate_lines
            selected_font_size = font_size
            break

    if len(selected_lines) > TITLE_MAX_LINES:
        merged = _collapse_whitespace(" ".join(selected_lines))
        max_chars = 42
        merged_body = _collapse_whitespace(_remove_all_emojis(merged)).strip(".,;:!?")
        if len(merged_body) > max_chars:
            merged_body = merged_body[:max_chars].rstrip()
            if " " in merged_body:
                merged_body = merged_body.rsplit(" ", 1)[0]
            merged_body = merged_body.rstrip(".,;:!?") + "..."
        merged = f"{merged_body} {title_emoji}".strip()
        selected_lines = textwrap.wrap(
            merged,
            width=24,
            break_long_words=False,
            break_on_hyphens=False,
        )[:TITLE_MAX_LINES]

    line_spacing = max(6, int(selected_font_size * 0.18))
    return "\n".join(selected_lines), selected_font_size, line_spacing


def create_tiktok_edited_video(
    raw_video_path: str,
    title: str,
    transcript_segments: List[Dict[str, object]],
    output_path: Optional[str] = None,
    emphasis_moments: Optional[List[Dict[str, object]]] = None,
) -> str:
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        raise RuntimeError("ffmpeg is not installed or not available in PATH.")
    _ensure_subtitles_filter_available(ffmpeg_path)

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
            suffix=".ass",
            prefix="title_overlay_",
            dir=str(edited_path.parent),
            delete=False,
        ) as title_temp:
            prepared_title, title_font_size, title_line_spacing = _prepare_title_text(title)
            title_temp.write(_build_title_ass_content(prepared_title, title_font_size))
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
            title_ass_file_path=title_temp_path,
            subtitle_file_path=subtitle_temp_path,
            title_font_size=title_font_size,
            emphasis_moments=emphasis_moments,
        )

        command = [
            ffmpeg_path,
            "-y",
            "-threads",
            "1",
            "-i",
            str(raw_path),
            "-vf",
            filter_chain,
            "-r",
            "24",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "24",
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
