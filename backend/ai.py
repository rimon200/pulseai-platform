import argparse
from dotenv import load_dotenv
from openai import OpenAI
import base64
import cv2
import gc
import json
import os
import re
import tempfile
import textwrap
from typing import Optional

load_dotenv()
AUTO_CLIP_MIN_SCORE = int(os.getenv("AUTO_CLIP_MIN_SCORE", "45"))

client = OpenAI()
_WHISPER_MODEL: Optional[object] = None
_TITLE_EMOJI_PATTERN = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]")
_TITLE_BANNED_PHRASES = {
    "this got crazy",
    "out of pocket chaos",
    "you won't believe this",
    "you won’t believe this",
    "this moment went viral",
    "yesterday's clip",
    "yesterday’s clip",
}
_TITLE_MAX_BODY_CHARS = 70
_TITLE_MAX_LINE_WIDTH = 30
_TITLE_MAX_LINES = 2
_TITLE_ALLOWED_EMOJIS = ["😂", "😭", "💀", "😱", "😳", "🔥"]


def _collapse_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _remove_quotes(value: str) -> str:
    return (
        value.replace('"', "")
        .replace("'", "")
        .replace("“", "")
        .replace("”", "")
        .replace("‘", "")
        .replace("’", "")
    )


def _strip_emojis(value: str) -> str:
    return _TITLE_EMOJI_PATTERN.sub("", value or "")


def _pick_relevant_emoji(source_text: str) -> str:
    text = (source_text or "").lower()
    if any(word in text for word in ("laugh", "lol", "lmao", "funny", "joke")):
        return "😂"
    if any(word in text for word in ("cry", "sad", "regret", "sorry", "pain")):
        return "😭"
    if any(word in text for word in ("dead", "clipped", "obliterated", "destroyed", "cooked")):
        return "💀"
    if any(word in text for word in ("what", "no way", "wtf", "insane", "shock", "unexpected")):
        return "😱"
    if any(word in text for word in ("clutch", "win", "comeback", "ace", "crazy")):
        return "🔥"
    return "😳"


def _limit_title_lines(title_body: str) -> str:
    wrapped = textwrap.wrap(
        title_body,
        width=_TITLE_MAX_LINE_WIDTH,
        break_long_words=False,
        break_on_hyphens=False,
    )
    if len(wrapped) <= _TITLE_MAX_LINES:
        return " ".join(wrapped)

    kept = wrapped[:_TITLE_MAX_LINES]
    merged = " ".join(kept)
    if len(merged) > _TITLE_MAX_BODY_CHARS:
        merged = merged[:_TITLE_MAX_BODY_CHARS].rstrip()
    return merged


def _build_transcript_fallback_title(transcript: str) -> str:
    cleaned = _collapse_whitespace(_strip_emojis(_remove_quotes(transcript)))
    cleaned = re.sub(r"#[^\s]+", "", cleaned)
    words = cleaned.split()
    if not words:
        return "Unexpected Moment Caught On Stream 😳"

    snippet_words = words[:8]
    snippet = " ".join(snippet_words)
    snippet = snippet[0].upper() + snippet[1:] if snippet else ""
    snippet = snippet.rstrip(".,;:!?")
    if not snippet:
        return "Unexpected Moment Caught On Stream 😳"
    if len(snippet) > _TITLE_MAX_BODY_CHARS:
        snippet = snippet[:_TITLE_MAX_BODY_CHARS].rstrip()
    snippet = _limit_title_lines(snippet)
    return f"{snippet} {_pick_relevant_emoji(transcript)}"


def _sanitize_generated_title(raw_title: str, transcript: str) -> str:
    candidate = _collapse_whitespace(raw_title)
    candidate = _remove_quotes(candidate)
    candidate = re.sub(r"#[^\s]+", "", candidate)
    candidate = _collapse_whitespace(candidate)

    if not candidate:
        return _build_transcript_fallback_title(transcript)

    lower_candidate = candidate.lower()
    if any(phrase in lower_candidate for phrase in _TITLE_BANNED_PHRASES):
        return _build_transcript_fallback_title(transcript)

    body = _strip_emojis(candidate)
    body = _collapse_whitespace(body)
    if len(body) > _TITLE_MAX_BODY_CHARS:
        body = body[:_TITLE_MAX_BODY_CHARS].rstrip()
    body = _limit_title_lines(body)
    body = body.rstrip(".,;:!? ")

    if len(body) < 12:
        return _build_transcript_fallback_title(transcript)

    emoji_source = f"{raw_title} {transcript}"
    trailing_emoji = _pick_relevant_emoji(emoji_source)
    return f"{body} {trailing_emoji}"


def _get_whisper_model() -> object:
    global _WHISPER_MODEL
    if _WHISPER_MODEL is None:
        from faster_whisper import WhisperModel

        _WHISPER_MODEL = WhisperModel("tiny", device="cpu", compute_type="int8")
    return _WHISPER_MODEL


def release_whisper_model() -> None:
    global _WHISPER_MODEL
    _WHISPER_MODEL = None
    gc.collect()


def _transcribe_video_data(video_path: str) -> tuple[str, list[dict[str, object]]]:
    model = _get_whisper_model()
    segments, _ = model.transcribe(video_path, language="en")

    transcript_parts = []
    transcript_segments = []
    for segment in segments:
        text = segment.text.strip()
        if not text:
            continue

        start = float(getattr(segment, "start", 0.0) or 0.0)
        end = float(getattr(segment, "end", start) or start)
        if end < start:
            end = start

        transcript_parts.append(text)
        transcript_segments.append(
            {
                "start": start,
                "end": end,
                "text": text,
            }
        )

    transcript = " ".join(transcript_parts)
    return transcript, transcript_segments


def generate_ai_title(
    transcript: str,
    context: Optional[dict[str, object]] = None,
) -> str:
    if not transcript.strip():
        return _build_transcript_fallback_title(transcript)

    context = context or {}
    context_lines = [
        f"visual_score: {context.get('visual_score', '')}",
        f"transcript_score: {context.get('transcript_score', '')}",
        f"context_score: {context.get('context_score', '')}",
        f"score_hook: {context.get('score_hook', '')}",
        f"score_reason: {context.get('score_reason', '')}",
        f"stream_title: {context.get('stream_title', '')}",
        f"game: {context.get('game', '')}",
    ]
    context_text = "\n".join(context_lines)

    response = client.responses.create(
        model="gpt-5-nano",
        input=(
            "Create exactly one curiosity-driven title for this short-form gaming clip.\n"
            "Rules:\n"
            "- Truthful to the clip and transcript\n"
            "- Concrete language, not generic hype\n"
            "- 5 to 10 words preferred\n"
            "- No hashtags\n"
            "- No creator name unless absolutely necessary\n"
            "- End with exactly one relevant emoji\n"
            "- Avoid these exact phrases: This got crazy; Out of pocket chaos; You won't believe this; This moment went viral; Yesterday's clip\n"
            "Return only the title and nothing else.\n\n"
            "Clip transcript:\n"
            f"{transcript}\n\n"
            "Visual and scoring context:\n"
            f"{context_text}"
        ),
    )

    return _sanitize_generated_title(response.output_text.strip(), transcript)

def generate_ai_description(transcript: str) -> str:
    if not transcript.strip():
        return ""

    response = client.responses.create(
        model="gpt-5-nano",
        input=f"""
Write a short, engaging social media description (2-3 sentences max)
for this gaming clip.

Transcript:
{transcript}

Return only the description.
"""
    )

    return response.output_text.strip()

def transcribe_video(video_path: str) -> str:
    transcript, _ = _transcribe_video_data(video_path)
    return transcript


def transcribe_video_with_segments(video_path: str) -> dict[str, object]:
    transcript, segments = _transcribe_video_data(video_path)
    return {
        "transcript": transcript,
        "segments": segments,
    }


def run_transcription_worker(video_path: str, output_json_path: str) -> int:
    payload = transcribe_video_with_segments(video_path)

    output_path = os.path.abspath(output_json_path)
    output_dir = os.path.dirname(output_path) or "."

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".json",
        prefix="transcription_",
        dir=output_dir,
        delete=False,
    ) as temp_file:
        json.dump(payload, temp_file)
        temp_file.flush()
        os.fsync(temp_file.fileno())
        temporary_output_path = temp_file.name

    os.replace(temporary_output_path, output_path)
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--transcribe-worker", action="store_true")
    parser.add_argument("--video-path")
    parser.add_argument("--output-json")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if not args.transcribe_worker:
        return 0
    if not args.video_path or not args.output_json:
        raise ValueError("--video-path and --output-json are required.")
    return run_transcription_worker(args.video_path, args.output_json)

def analyze_video_frames(video_path: str) -> int:
    capture = cv2.VideoCapture(video_path)
    if not capture.isOpened():
        return 0

    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = capture.get(cv2.CAP_PROP_FPS)

    if frame_count <= 0 or fps <= 0:
        capture.release()
        return 0

    sample_count = 5
    if frame_count <= sample_count:
        sample_indices = list(range(frame_count))
    else:
        sample_indices = [
            int(round(i * (frame_count - 1) / (sample_count - 1)))
            for i in range(sample_count)
        ]

    image_contents = []
    for index in sample_indices:
        capture.set(cv2.CAP_PROP_POS_FRAMES, index)
        success, frame = capture.read()
        if not success or frame is None:
            continue

        height, width = frame.shape[:2]
        if width > 768:
            scale = 768.0 / width
            frame = cv2.resize(
                frame,
                (768, int(round(height * scale))),
                interpolation=cv2.INTER_AREA,
            )

        success, jpeg = cv2.imencode('.jpg', frame)
        if not success:
            continue

        image_b64 = base64.b64encode(jpeg.tobytes()).decode('utf-8')
        image_contents.append(
            {
                "type": "input_image",
                "image_url": f"data:image/jpeg;base64,{image_b64}",
            }
        )

    capture.release()

    if not image_contents:
        return 0

    prompt_text = (
        "You are a viral short-form video editor. Score this clip from 0 to 100 based on how likely the visuals are to stop someone scrolling within the first 2 seconds.\n"
        "Evaluate facial expressions, emotion, action intensity, surprising moments, webcam reactions, explosions or effects, visible humor or conflict, captions or memes, visual clarity, and overall entertainment value.\n"
        "Return only a whole number from 0 to 100."
    )

    response = client.responses.create(
        model="gpt-5-mini",
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt_text},
                    *image_contents,
                ],
            }
        ],
    )

    try:
        score = int(response.output_text.strip())
    except (ValueError, AttributeError):
        return 0

    return max(0, min(score, 100))


def _safe_json_parse(response_text: str) -> dict:
    if not isinstance(response_text, str):
        return {}
    cleaned = re.sub(r"```(?:json)?\n?|```", "", response_text).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return {}


def _clamp_score(value: object) -> int:
    try:
        score = int(float(value))
    except (TypeError, ValueError):
        return 0
    return max(0, min(score, 100))


def _default_clip_score() -> dict:
    return {
        "score": 0,
        "decision": "reject",
        "reason": "Scoring failed",
        "hook": "",
        "visual_score": 0,
        "transcript_score": 0,
        "context_score": 0,
        "confidence": 0,
    }


def score_multimodal_clip(
    video_path: str,
    transcript: str,
    creator: str = "",
    game: str = "",
    stream_title: str = "",
    viewer_count: int = 0,
    duration: int = 0,
) -> dict:
    capture = cv2.VideoCapture(video_path)
    if not capture.isOpened():
        return _default_clip_score()

    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = capture.get(cv2.CAP_PROP_FPS)

    if frame_count <= 0 or fps <= 0:
        capture.release()
        return _default_clip_score()

    sample_count = 5
    if frame_count <= sample_count:
        sample_indices = list(range(frame_count))
    else:
        sample_indices = [
            int(round(i * (frame_count - 1) / (sample_count - 1)))
            for i in range(sample_count)
        ]

    image_contents = []
    for index in sample_indices:
        capture.set(cv2.CAP_PROP_POS_FRAMES, index)
        success, frame = capture.read()
        if not success or frame is None:
            continue

        height, width = frame.shape[:2]
        if width > 768:
            scale = 768.0 / width
            frame = cv2.resize(
                frame,
                (768, int(round(height * scale))),
                interpolation=cv2.INTER_AREA,
            )

        success, jpeg = cv2.imencode('.jpg', frame)
        if not success:
            continue

        image_b64 = base64.b64encode(jpeg.tobytes()).decode('utf-8')
        image_contents.append(
            {
                "type": "input_image",
                "image_url": f"data:image/jpeg;base64,{image_b64}",
            }
        )

    capture.release()

    if not image_contents:
        return _default_clip_score()

    prompt_text = (
        "You are the head content reviewer for TikTok, YouTube Shorts, Instagram Reels, and Kick clips. Be EXTREMELY selective.\n"
        "Score this clip from 0 to 100 based on VIRAL POTENTIAL, not production quality.\n"
        "Reward clips that viewers would stop scrolling to watch, send to friends, or keep watching out of curiosity and retention.\n"
        "Strong positive signals include insane clutch plays, near deaths, close calls, unexpected outcomes, funny fails, streamer screaming or laughing, emotional reactions, arguments or drama, shock value, huge wins or huge losses, rare moments, impressive skill, accidental comedy, chat-spam worthy moments, memes, and an instant hook within the first 2 seconds.\n"
        "Negative signals include long explanations, slow pacing, no payoff, repetitive gameplay, dead air, menus, inventory management, walking around, nothing interesting happening, and clips that require stream context to understand.\n"
        "Scoring guide:\n"
        "95-100: Exceptional viral clip, would likely perform extremely well.\n"
        "85-94: Very strong clip, definitely worth publishing.\n"
        f"{AUTO_CLIP_MIN_SCORE}-84: Good clip. Return \"accept\".\n"
        "60-74: Borderline, some entertainment but lacks a major viral moment.\n"
        "40-59: Weak, little reason to publish.\n"
        "0-39: Do not publish.\n"
        "Return STRICT JSON only in this exact format:\n"
        "{\n"
        "  \"score\": number,\n"
        "  \"decision\": \"accept\" or \"reject\",\n"
        "  \"reason\": \"...\",\n"
        "  \"hook\": \"...\",\n"
        "  \"visual_score\": number,\n"
        "  \"transcript_score\": number,\n"
        "  \"context_score\": number,\n"
        "  \"confidence\": number\n"
        "}\n"
        "Use integers 0-100 for score, visual_score, transcript_score, context_score, and confidence.\n"
        "Set decision to \"accept\" for publishable clips and \"reject\" for clips that should not be published.\n"
        "Return only the JSON object and nothing else."
    )

    metadata = {
        "creator": creator or "",
        "game": game or "",
        "stream_title": stream_title or "",
        "viewer_count": viewer_count,
        "duration": duration,
        "transcript": transcript or "",
    }

    response = client.responses.create(
        model="gpt-5-mini",
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": json.dumps(metadata, indent=2)},
                    {"type": "input_text", "text": prompt_text},
                    *image_contents,
                ],
            }
        ],
    )

    response_text = getattr(response, "output_text", "") or ""
    parsed = _safe_json_parse(response_text)

    if not isinstance(parsed, dict):
        return _default_clip_score()

    score = _clamp_score(parsed.get("score"))
    visual_score = _clamp_score(parsed.get("visual_score"))
    transcript_score = _clamp_score(parsed.get("transcript_score"))
    context_score = _clamp_score(parsed.get("context_score"))
    confidence = _clamp_score(parsed.get("confidence"))
    decision = parsed.get("decision", "reject")
    if decision not in {"accept", "reject"}:
        decision = (
            "accept"
            if score >= AUTO_CLIP_MIN_SCORE
            else "reject"
        )

    result = {
        "score": score,
        "decision": decision,
        "reason": str(parsed.get("reason", "")).strip(),
        "hook": str(parsed.get("hook", "")).strip(),
        "visual_score": visual_score,
        "transcript_score": transcript_score,
        "context_score": context_score,
        "confidence": confidence,
    }

    if decision == "accept" and score < AUTO_CLIP_MIN_SCORE:
        result["decision"] = "reject"

    print("MULTIMODAL SCORE:", result["score"])
    print("DECISION:", result["decision"])
    print("REASON:", result["reason"])
    print("VISUAL SCORE:", result["visual_score"])
    print("TRANSCRIPT SCORE:", result["transcript_score"])
    print("CONTEXT SCORE:", result["context_score"])
    print("CONFIDENCE:", result["confidence"])

    return result


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Transcription worker failed: {error}", file=os.sys.stderr)
        raise SystemExit(1)
