from functools import lru_cache
from pathlib import Path
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import textwrap
import time
import unicodedata
from typing import Dict, List, Optional

import cv2


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


def _build_overlay_ass_content(
    title_text: str,
    title_font_size: int,
    transcript_segments: List[Dict[str, object]],
    speech_captions_enabled: bool = False,
) -> str:
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
        "Style: Caption,Arial,40,&H00FFFFFF,&H000000FF,&H00000000,&H99000000,1,0,0,0,100,100,0,0,3,2.8,0,2,52,52,210,1",
        "",
        "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]
    if not speech_captions_enabled:
        header = [
            line for line in header
            if not line.startswith("Style: Caption")
        ]

    events = []

    cleaned_title = _escape_ass_text(title_text)
    if cleaned_title:
        events.append(
            "Dialogue: "
            "0,0:00:00.00,9:59:59.00,TopTitle,,0,0,0,,"
            f"{{\\q2}}{cleaned_title}"
        )

    for segment in transcript_segments[:120] if speech_captions_enabled else []:
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


def _fraction_to_float(value: str) -> float:
    text = (value or "").strip()
    if not text or text == "0/0":
        return 0.0
    if "/" in text:
        numerator, denominator = text.split("/", 1)
        try:
            denominator_value = float(denominator)
            if denominator_value == 0:
                return 0.0
            return float(numerator) / denominator_value
        except (TypeError, ValueError):
            return 0.0
    try:
        return float(text)
    except (TypeError, ValueError):
        return 0.0


def _probe_media_metadata(ffprobe_path: str, media_path: Path) -> dict[str, object]:
    command = [
        ffprobe_path,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,avg_frame_rate,r_frame_rate",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(media_path),
    ]

    result = subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    payload = json.loads(result.stdout)
    streams = payload.get("streams") or []
    format_data = payload.get("format") or {}
    stream = streams[0] if streams else {}

    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    avg_frame_rate = _fraction_to_float(str(stream.get("avg_frame_rate") or "0"))
    r_frame_rate = _fraction_to_float(str(stream.get("r_frame_rate") or "0"))
    duration = _fraction_to_float(str(format_data.get("duration") or "0"))

    return {
        "width": width,
        "height": height,
        "avg_frame_rate": avg_frame_rate,
        "r_frame_rate": r_frame_rate,
        "duration": duration,
    }


def _format_ffmpeg_command(command: List[str]) -> str:
    return shlex.join(command)


@lru_cache(maxsize=1)
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
    overlay_ass_file_path: Path,
    emphasis_moments: Optional[List[Dict[str, object]]] = None,
    visual_layout: Optional[Dict[str, object]] = None,
) -> str:
    layout = visual_layout or {}
    layout_mode = str(layout.get("mode") or "single_subject")
    regions = layout.get("regions") if isinstance(layout.get("regions"), list) else []
    safe_overlay_ass_file = _escape_filter_path(overlay_ass_file_path)

    if layout_mode in {"multi_person_split", "reaction_split"} and len(regions) >= 2:
        crop_filters = []
        output_labels = []
        for index, region in enumerate(regions[:2]):
            normalized = _normalized_layout_region(region)
            output_label = f"layout_region_{index}"
            output_labels.append(output_label)
            crop_filters.append(
                f"[layout_input_{index}]"
                f"crop=w='iw*{normalized['width']:.5f}':"
                f"h='ih*{normalized['height']:.5f}':"
                f"x='iw*{normalized['x']:.5f}':"
                f"y='ih*{normalized['y']:.5f}',"
                f"scale={CANVAS_WIDTH}:{VIDEO_REGION_HEIGHT // 2}:"
                "force_original_aspect_ratio=increase,"
                f"crop={CANVAS_WIDTH}:{VIDEO_REGION_HEIGHT // 2}"
                f"[{output_label}]"
            )
        filters = [
            "fps=24,split=2[layout_input_0][layout_input_1]",
            *crop_filters,
            f"[{output_labels[0]}][{output_labels[1]}]"
            "vstack=inputs=2[video_region]",
            f"color=c=white:s={CANVAS_WIDTH}x{CANVAS_HEIGHT}[base]",
            f"[base][video_region]overlay=x=0:y={VIDEO_REGION_TOP}:"
            "shortest=1:eof_action=endall[composed]",
            f"[composed]subtitles=filename='{safe_overlay_ass_file}'",
        ]
        return ";".join(filters)

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
        "x='(in_w-out_w)/2 + ((in_w-out_w)/8)*sin(t*0.45)':"
        "y='(in_h-out_h)/2',"
        f"scale={CANVAS_WIDTH}:{VIDEO_REGION_HEIGHT}"
        "[video_region]",
        f"color=c=white:s={CANVAS_WIDTH}x{CANVAS_HEIGHT}[base]",
        f"[base][video_region]overlay=x=0:y={VIDEO_REGION_TOP}:shortest=1:eof_action=endall[composed]",
    ]

    filters.append(f"[composed]subtitles=filename='{safe_overlay_ass_file}'")

    return ";".join(filters)


def _normalized_layout_region(region: object) -> Dict[str, float]:
    source = region if isinstance(region, dict) else {}
    try:
        x = min(0.95, max(0.0, float(source.get("x", 0.0))))
        y = min(0.95, max(0.0, float(source.get("y", 0.0))))
        width = min(1.0 - x, max(0.05, float(source.get("width", 1.0))))
        height = min(1.0 - y, max(0.05, float(source.get("height", 1.0))))
    except (TypeError, ValueError):
        return {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}
    return {"x": x, "y": y, "width": width, "height": height}


def _detect_face_like_regions(frame: object) -> List[Dict[str, float]]:
    height, width = frame.shape[:2]
    ycrcb = cv2.cvtColor(frame, cv2.COLOR_BGR2YCrCb)
    skin_mask = cv2.inRange(
        ycrcb,
        (0, 133, 77),
        (255, 173, 127),
    )
    contours_result = cv2.findContours(
        skin_mask,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE,
    )
    contours = contours_result[-2]
    regions = []
    minimum_area = width * height * 0.003
    for contour in contours:
        x, y, region_width, region_height = cv2.boundingRect(contour)
        area = cv2.contourArea(contour)
        aspect_ratio = region_width / max(1, region_height)
        if (
            area < minimum_area
            or not 0.35 <= aspect_ratio <= 1.8
            or region_height < height * 0.08
        ):
            continue
        regions.append(
            {
                "x": x / width,
                "y": y / height,
                "width": region_width / width,
                "height": region_height / height,
                "center_x": (x + region_width / 2) / width,
            }
        )
    regions.sort(
        key=lambda region: region["width"] * region["height"],
        reverse=True,
    )
    return regions[:4]


def detect_visual_layout(video_path: str) -> Dict[str, object]:
    if os.getenv("VIDEO_LAYOUT_DETECTION_ENABLED", "true").lower() != "true":
        return _single_subject_layout("layout detection disabled")
    try:
        requested_samples = int(os.getenv("VIDEO_LAYOUT_SAMPLE_COUNT", "5"))
    except ValueError:
        requested_samples = 5
    sample_count = max(1, min(8, requested_samples))
    try:
        minimum_confidence = min(
            1.0,
            max(0.0, float(os.getenv("VIDEO_LAYOUT_MIN_CONFIDENCE", "0.70"))),
        )
    except ValueError:
        minimum_confidence = 0.70

    capture = cv2.VideoCapture(video_path)
    if not capture.isOpened():
        return _single_subject_layout("video could not be sampled")
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    source_width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    source_height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if frame_count <= 0 or source_width <= 0 or source_height <= 0:
        capture.release()
        return _single_subject_layout("video metadata unavailable")
    indices = [
        int(round(index * (frame_count - 1) / max(1, sample_count - 1)))
        for index in range(sample_count)
    ]
    sampled_faces: List[List[Dict[str, float]]] = []
    for frame_index in indices:
        capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        success, frame = capture.read()
        if not success or frame is None:
            continue
        height, width = frame.shape[:2]
        if width > 480:
            ratio = 480.0 / width
            frame = cv2.resize(
                frame,
                (480, max(1, int(height * ratio))),
                interpolation=cv2.INTER_AREA,
            )
        sampled_faces.append(_detect_face_like_regions(frame))
        del frame
    capture.release()

    usable_samples = len(sampled_faces)
    if not usable_samples:
        return _single_subject_layout("no usable samples")
    two_face_samples = [
        faces for faces in sampled_faces
        if len(faces) >= 2
        and max(face["center_x"] for face in faces)
        - min(face["center_x"] for face in faces) >= 0.25
    ]
    two_face_confidence = len(two_face_samples) / usable_samples
    if two_face_confidence >= minimum_confidence:
        result = {
            "mode": "multi_person_split",
            "confidence": round(two_face_confidence, 3),
            "reason": "multiple separated faces remained visible across samples",
            "version": "layout-v1",
            "sample_count": usable_samples,
            "regions": [
                {"x": 0.0, "y": 0.0, "width": 0.52, "height": 1.0},
                {"x": 0.48, "y": 0.0, "width": 0.52, "height": 1.0},
            ],
        }
        print(f"VISUAL LAYOUT SELECTED | mode=multi_person_split | confidence={two_face_confidence:.3f}")
        print(f"VISUAL LAYOUT REGIONS | regions={result['regions']}")
        return result

    wide_source = source_width / source_height >= 1.45
    edge_face_samples = [
        faces[0]
        for faces in sampled_faces
        if len(faces) == 1
        and (faces[0]["center_x"] <= 0.32 or faces[0]["center_x"] >= 0.68)
    ]
    reaction_confidence = len(edge_face_samples) / usable_samples
    if wide_source and reaction_confidence >= minimum_confidence:
        face_on_left = sum(face["center_x"] < 0.5 for face in edge_face_samples) >= (
            len(edge_face_samples) / 2
        )
        face_region = (
            {"x": 0.0, "y": 0.0, "width": 0.42, "height": 1.0}
            if face_on_left
            else {"x": 0.58, "y": 0.0, "width": 0.42, "height": 1.0}
        )
        content_region = (
            {"x": 0.32, "y": 0.0, "width": 0.68, "height": 1.0}
            if face_on_left
            else {"x": 0.0, "y": 0.0, "width": 0.68, "height": 1.0}
        )
        result = {
            "mode": "reaction_split",
            "confidence": round(reaction_confidence, 3),
            "reason": "persistent edge webcam face with wide reacted-content area",
            "version": "layout-v1",
            "sample_count": usable_samples,
            "regions": [face_region, content_region],
        }
        print(f"VISUAL LAYOUT SELECTED | mode=reaction_split | confidence={reaction_confidence:.3f}")
        print(f"VISUAL LAYOUT REGIONS | regions={result['regions']}")
        return result
    return _single_subject_layout(
        "layout evidence below confidence threshold",
        max(two_face_confidence, reaction_confidence),
        usable_samples,
    )


def _single_subject_layout(
    reason: str,
    confidence: float = 1.0,
    sample_count: int = 0,
) -> Dict[str, object]:
    result = {
        "mode": "single_subject",
        "confidence": round(float(confidence), 3),
        "reason": reason,
        "version": "layout-v1",
        "sample_count": sample_count,
        "regions": [],
    }
    print(
        "VISUAL LAYOUT FALLBACK | "
        f"mode=single_subject | confidence={confidence:.3f} | reason={reason}"
    )
    return result


def _prepare_title_text(value: str) -> tuple[str, int, int]:
    title_text = _collapse_whitespace(_remove_all_emojis(value))
    title_body = unicodedata.normalize("NFKD", title_text)
    title_body = title_body.encode("ascii", "ignore").decode("ascii")
    title_body = _collapse_whitespace(title_body).strip(".,;:!?")
    if not title_body:
        title_body = "Unexpected Stream Moment"

    selected_lines = [title_body]
    selected_font_size = TITLE_MIN_FONT_SIZE

    for font_size in range(TITLE_PREFERRED_MAX_FONT_SIZE, TITLE_MIN_FONT_SIZE - 1, -1):
        approx_char_width = max(8.0, font_size * 0.60)
        max_chars_per_line = max(
            14,
            int((CANVAS_WIDTH - (TITLE_HORIZONTAL_PADDING * 2)) / approx_char_width),
        )

        candidate_lines = textwrap.wrap(
            title_body,
            width=max_chars_per_line,
            break_long_words=False,
            break_on_hyphens=False,
        )
        if len(candidate_lines) <= TITLE_MAX_LINES:
            selected_lines = candidate_lines
            selected_font_size = font_size
            break
    else:
        min_font_char_width = max(8.0, TITLE_MIN_FONT_SIZE * 0.60)
        min_font_line_width = max(
            14,
            int(
                (CANVAS_WIDTH - (TITLE_HORIZONTAL_PADDING * 2))
                / min_font_char_width
            ),
        )
        fitted_words: List[str] = []
        for word in title_body.split():
            candidate = " ".join(fitted_words + [word])
            candidate_lines = textwrap.wrap(
                candidate,
                width=min_font_line_width,
                break_long_words=False,
                break_on_hyphens=False,
            )
            if len(candidate_lines) > TITLE_MAX_LINES:
                break
            fitted_words.append(word)

        if len(fitted_words) < len(title_body.split()) and fitted_words:
            truncated_text = " ".join(fitted_words).rstrip(".,;:!?") + "..."
            truncated_lines = textwrap.wrap(
                truncated_text,
                width=min_font_line_width,
                break_long_words=False,
                break_on_hyphens=False,
            )
            while len(truncated_lines) > TITLE_MAX_LINES and len(fitted_words) > 1:
                fitted_words.pop()
                truncated_text = " ".join(fitted_words).rstrip(".,;:!?") + "..."
                truncated_lines = textwrap.wrap(
                    truncated_text,
                    width=min_font_line_width,
                    break_long_words=False,
                    break_on_hyphens=False,
                )
            selected_lines = truncated_lines
        else:
            selected_lines = textwrap.wrap(
                title_body,
                width=min_font_line_width,
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
    selected_duration_seconds: Optional[float] = None,
    visual_layout: Optional[Dict[str, object]] = None,
) -> str:
    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_path = shutil.which("ffprobe")
    if not ffmpeg_path:
        raise RuntimeError("ffmpeg is not installed or not available in PATH.")
    if not ffprobe_path:
        raise RuntimeError("ffprobe is not installed or not available in PATH.")
    _ensure_subtitles_filter_available(ffmpeg_path)

    raw_path = Path(raw_video_path)
    if not raw_path.is_file():
        raise RuntimeError(f"Raw video file not found: {raw_video_path}")

    input_metadata = _probe_media_metadata(ffprobe_path, raw_path)
    input_duration_seconds = float(input_metadata["duration"] or 0.0)
    input_width = int(input_metadata["width"] or 0)
    input_height = int(input_metadata["height"] or 0)
    input_fps = float(input_metadata["avg_frame_rate"] or input_metadata["r_frame_rate"] or 0.0)
    input_was_already_24_fps = abs(input_fps - 24.0) < 0.01

    if output_path:
        edited_path = Path(output_path)
    else:
        edited_dir = raw_path.parent / "edited"
        edited_dir.mkdir(parents=True, exist_ok=True)
        edited_path = edited_dir / f"{raw_path.stem}_tiktok.mp4"

    edited_output_existed_before = False
    edited_output_before_signature = None
    try:
        if edited_path.is_file():
            edited_output_existed_before = True
            existing_stat = edited_path.stat()
            edited_output_before_signature = (
                existing_stat.st_ino,
                existing_stat.st_size,
                existing_stat.st_mtime_ns,
            )
    except OSError:
        edited_output_before_signature = None

    overlay_temp_path = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".ass",
            prefix="overlay_",
            dir=str(edited_path.parent),
            delete=False,
        ) as overlay_temp:
            prepared_title, title_font_size, _ = _prepare_title_text(title)
            overlay_temp.write(
                _build_overlay_ass_content(
                    prepared_title,
                    title_font_size,
                    transcript_segments,
                    speech_captions_enabled=(
                        os.getenv(
                            "VIDEO_SPEECH_CAPTIONS_ENABLED",
                            "false",
                        ).lower() == "true"
                    ),
                )
            )
            overlay_temp_path = Path(overlay_temp.name)

        filter_chain = _build_filter_chain(
            overlay_ass_file_path=overlay_temp_path,
            emphasis_moments=emphasis_moments,
            visual_layout=visual_layout,
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
        ]
        if selected_duration_seconds and selected_duration_seconds > 0:
            command.extend(["-t", f"{selected_duration_seconds:.3f}"])
        command.append(str(edited_path))

        print(
            "FFMPEG EDIT START | "
            f"input_duration_seconds={input_duration_seconds:.3f} | "
            f"input_resolution={input_width}x{input_height} | "
            f"input_fps={input_fps:.3f} | "
            f"input_was_already_24_fps={input_was_already_24_fps} | "
            f"ffmpeg_command={_format_ffmpeg_command(command)}"
        )

        ffmpeg_started_at = time.perf_counter()

        try:
            subprocess.run(
                command,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except (subprocess.CalledProcessError, OSError) as error:
            stderr = (
                (error.stderr or "").strip()
                if isinstance(error, subprocess.CalledProcessError)
                else repr(error)
            )
            try:
                if (
                    edited_path.resolve(strict=False)
                    != raw_path.resolve(strict=False)
                    and edited_path.is_file()
                ):
                    failed_stat = edited_path.stat()
                    failed_signature = (
                        failed_stat.st_ino,
                        failed_stat.st_size,
                        failed_stat.st_mtime_ns,
                    )
                    if (
                        not edited_output_existed_before
                        or (
                            edited_output_before_signature is not None
                            and failed_signature != edited_output_before_signature
                        )
                    ):
                        edited_path.unlink()
                        print(
                            "FFMPEG PARTIAL OUTPUT CLEANUP | "
                            f"path={edited_path}"
                        )
            except Exception as cleanup_error:
                print(
                    "FFMPEG PARTIAL OUTPUT CLEANUP FAILED | "
                    f"path={edited_path} | error={cleanup_error!r}"
                )
            raise RuntimeError(f"ffmpeg editing failed: {stderr}") from error

        ffmpeg_elapsed_seconds = time.perf_counter() - ffmpeg_started_at

        output_metadata = _probe_media_metadata(ffprobe_path, edited_path)
        output_duration_seconds = float(output_metadata["duration"] or 0.0)
        output_width = int(output_metadata["width"] or 0)
        output_height = int(output_metadata["height"] or 0)
        output_fps = float(output_metadata["avg_frame_rate"] or output_metadata["r_frame_rate"] or 0.0)
        render_ratio = (
            ffmpeg_elapsed_seconds / input_duration_seconds
            if input_duration_seconds > 0
            else 0.0
        )

        print(
            "FFMPEG EDIT METRICS | "
            f"input_duration_seconds={input_duration_seconds:.3f} | "
            f"output_duration_seconds={output_duration_seconds:.3f} | "
            f"ffmpeg_elapsed_seconds={ffmpeg_elapsed_seconds:.3f} | "
            f"render_ratio={render_ratio:.3f} | "
            f"input_resolution={input_width}x{input_height} | "
            f"input_fps={input_fps:.3f} | "
            f"output_resolution={output_width}x{output_height} | "
            f"output_fps={output_fps:.3f} | "
            f"input_was_already_24_fps={input_was_already_24_fps} | "
            f"frame_rate_conversion_required={not input_was_already_24_fps}"
        )

        if not edited_path.is_file() or edited_path.stat().st_size == 0:
            raise RuntimeError(f"Edited video was not created: {edited_path}")

        return str(edited_path)
    finally:
        if overlay_temp_path and overlay_temp_path.exists():
            try:
                os.remove(overlay_temp_path)
            except OSError:
                pass
