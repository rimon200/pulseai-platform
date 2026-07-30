from __future__ import annotations

import argparse
import sys
import time
from dotenv import load_dotenv
from openai import OpenAI
import base64
import gc
import json
import os
import re
import tempfile
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
    "crazy stream moment",
    "streamer goes viral",
    "unexpected stream moment",
    "this was insane",
}
_TITLE_MAX_BODY_CHARS = 70
_TITLE_PROFANITY_PATTERN = re.compile(
    r"\b(fuck(?:ing)?|shit|bitch|asshole|cunt|nigg(?:a|er))\b",
    re.IGNORECASE,
)
_TITLE_STOP_WORDS = {
    "about", "after", "again", "before", "could", "from", "have", "just",
    "moment", "really", "stream", "streamer", "that", "their", "there",
    "these", "they", "this", "those", "with", "would", "your",
}


def _log_timing(stage: str, elapsed_seconds: float) -> None:
    print(
        "PERFORMANCE TIMING | "
        f"stage={stage} | "
        f"elapsed_seconds={elapsed_seconds:.3f}",
        file=sys.stderr,
    )


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
    if any(word in text for word in ("awkward", "cringe", "embarrass")):
        return "😬"
    if any(word in text for word in ("heartbreak", "heartbroken", "breakup")):
        return "💔"
    if any(word in text for word in ("sad", "disappoint", "regret", "sorry")):
        return "😔"
    if any(word in text for word in ("dead", "clipped", "obliterated", "destroyed", "cooked")):
        return "💀"
    if any(word in text for word in ("mind blown", "unbelievable", "speechless")):
        return "🤯"
    if any(word in text for word in ("scream", "shock", "terrified")):
        return "😱"
    if any(word in text for word in ("confused", "what", "no way", "unexpected")):
        return "😳"
    if any(word in text for word in ("goat", "best ever", "legend")):
        return "🐐"
    if any(word in text for word in ("clutch", "win", "comeback", "ace", "impressive")):
        return "🔥"
    return "😳"


def _limit_title_lines(title_body: str) -> str:
    words = _collapse_whitespace(title_body).split()
    limited_words = words[:10]
    while (
        len(" ".join(limited_words)) > _TITLE_MAX_BODY_CHARS
        and len(limited_words) > 5
    ):
        limited_words.pop()
    return " ".join(limited_words)


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
    body = _TITLE_PROFANITY_PATTERN.sub("", body)
    body = _collapse_whitespace(body)
    body = _limit_title_lines(body)
    body = body.rstrip(".,;:!? ")

    if len(body.split()) < 5:
        return _build_transcript_fallback_title(transcript)

    emoji_source = f"{raw_title} {transcript}"
    trailing_emoji = _pick_relevant_emoji(emoji_source)
    return f"{body} {trailing_emoji}"


def _title_terms(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9']+", (value or "").lower())
        if len(token) >= 4 and token not in _TITLE_STOP_WORDS
    }


def _title_relevance_score(title: str, evidence: str) -> float:
    title_terms = _title_terms(_strip_emojis(title))
    evidence_terms = _title_terms(evidence)
    if not title_terms:
        return 0.0
    supported = len(title_terms & evidence_terms)
    return round(supported / len(title_terms), 3)


def _deterministic_title_fallback(transcript: str, event_summary: str) -> str:
    evidence = _collapse_whitespace(
        _TITLE_PROFANITY_PATTERN.sub("", event_summary or transcript)
    )
    meaningful = [
        word.strip(".,;:!?")
        for word in evidence.split()
        if word.strip(".,;:!?")
    ][:10]
    while len(meaningful) > 5 and len(" ".join(meaningful)) > _TITLE_MAX_BODY_CHARS:
        meaningful.pop()
    if len(meaningful) < 5:
        meaningful = ["Reaction", "Builds", "Into", "A", "Real", "Payoff"]
    body = " ".join(meaningful[:10]).strip(".,;:!?")
    body = body[0].upper() + body[1:] if body else "Reaction Builds Into A Real Payoff"
    return f"{body} {_pick_relevant_emoji(evidence)}"


def _get_whisper_model() -> object:
    global _WHISPER_MODEL
    if _WHISPER_MODEL is None:
        from faster_whisper import WhisperModel

        startup_started_at = time.perf_counter()
        _WHISPER_MODEL = WhisperModel("tiny", device="cpu", compute_type="int8")
        _log_timing(
            stage="whisper_startup",
            elapsed_seconds=time.perf_counter() - startup_started_at,
        )
    return _WHISPER_MODEL


def release_whisper_model() -> None:
    global _WHISPER_MODEL
    _WHISPER_MODEL = None
    gc.collect()


def _transcribe_video_data(video_path: str) -> tuple[str, list[dict[str, object]]]:
    model = _get_whisper_model()
    transcription_started_at = time.perf_counter()
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
    _log_timing(
        stage="audio_extraction_and_transcription",
        elapsed_seconds=time.perf_counter() - transcription_started_at,
    )
    return transcript, transcript_segments


def generate_ai_title(
    transcript: str,
    context: Optional[dict[str, object]] = None,
) -> str:
    return str(generate_ai_title_package(transcript, context)["generated_title"])


def generate_ai_title_package(
    transcript: str,
    context: Optional[dict[str, object]] = None,
) -> dict[str, object]:
    context = context or {}
    selected_transcript = _collapse_whitespace(transcript)
    event_summary = _collapse_whitespace(
        " ".join(
            str(context.get(key) or "")
            for key in ("score_hook", "score_reason", "visual_event_summary")
        )
    )
    if not event_summary:
        event_summary = " ".join(selected_transcript.split()[:40])
    if not selected_transcript:
        fallback = _deterministic_title_fallback("", event_summary)
        return {
            "generated_title": fallback,
            "title_event_summary": event_summary,
            "title_relevance_score": 1.0,
            "title_generation_version": "title-v3",
            "title_fallback_used": True,
        }

    context_lines = [
        f"visual_score: {context.get('visual_score', '')}",
        f"transcript_score: {context.get('transcript_score', '')}",
        f"context_score: {context.get('context_score', '')}",
        f"score_hook: {context.get('score_hook', '')}",
        f"score_reason: {context.get('score_reason', '')}",
        f"stream_title: {context.get('stream_title', '')}",
        f"game: {context.get('game', '')}",
        f"creator: {context.get('creator', '')}",
        f"clip_start_seconds: {context.get('clip_start_seconds', 0)}",
        f"clip_end_seconds: {context.get('clip_end_seconds', '')}",
        f"event_summary: {event_summary}",
    ]
    context_text = "\n".join(context_lines)
    evidence = f"{selected_transcript} {event_summary} {context_text}"
    prompt = (
        "Act as a professional reaction-video editor. Write exactly one truthful, "
        "curiosity-driven title using 4-10 natural English words. Lead with the "
        "creator when known and frame the visible reaction or concrete turning "
        "point (for example: 'Kai instantly regrets watching this'). Never return "
        "a transcript fragment or merely restate dialogue. Use at most one trailing "
        "emoji. Use only facts supported by the transcript and "
        "event summary. No hashtags, profanity, invented people, motives, outcomes, "
        "ALL CAPS, or unfinished ellipses. "
        "or generic phrases such as Crazy Stream Moment, You Won't Believe This, "
        "Streamer Goes Viral, Unexpected Stream Moment, or This Was Insane. "
        "Return only the title.\n\n"
        f"Selected transcript:\n{selected_transcript}\n\nContext:\n{context_text}"
    )
    fallback_used = False
    title = ""
    relevance = 0.0
    for attempt in range(2):
        retry_instruction = (
            "\nYour prior title was unsupported. Reuse concrete words and actions "
            "from the evidence; do not generalize."
            if attempt
            else ""
        )
        response = client.responses.create(
            model="gpt-5-nano",
            input=prompt + retry_instruction,
        )
        title = _sanitize_generated_title(response.output_text.strip(), evidence)
        relevance = _title_relevance_score(title, evidence)
        if relevance >= 0.45:
            break
    else:
        title = _deterministic_title_fallback(selected_transcript, event_summary)
        relevance = _title_relevance_score(title, evidence)
        fallback_used = True
    return {
        "generated_title": title,
        "title_event_summary": event_summary,
        "title_relevance_score": relevance,
        "title_generation_version": "title-v3",
        "title_fallback_used": fallback_used,
    }

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


def generate_tiktok_caption_package(
    transcript: str,
    creator: str = "",
    game: str = "",
) -> dict[str, object]:
    if not transcript.strip():
        return {
            "ai_post_caption": "",
            "ai_hashtags": [],
            "ai_tiktok_description": "",
            "caption_generation_version": "caption-v1",
        }
    response = client.responses.create(
        model="gpt-5-nano",
        input=(
            "Return JSON with keys caption and hashtags. Caption must be one or two "
            "concise truthful sentences grounded only in the transcript/context. "
            "Use a strong hook without misleading clickbait. Do not invent context, "
            "defame the creator, or make sexual assumptions. Hashtags must be a JSON "
            "array of 3 to 6 relevant tags, each beginning with #. Prefer creator, "
            "game/category, event/theme, and Twitch/streamer tags. Avoid repeated "
            "#fyp or generic #viral tags.\n"
            f"Creator: {creator}\nGame: {game}\nTranscript:\n{transcript}"
        ),
    )
    raw = response.output_text.strip()
    try:
        payload = json.loads(raw.removeprefix("```json").removesuffix("```").strip())
    except (json.JSONDecodeError, AttributeError):
        payload = {"caption": raw, "hashtags": []}
    caption = _collapse_whitespace(str(payload.get("caption") or ""))
    hashtags = []
    for value in payload.get("hashtags", []) if isinstance(payload.get("hashtags"), list) else []:
        tag = re.sub(r"[^A-Za-z0-9_]", "", str(value).lstrip("#"))
        if tag and f"#{tag}" not in hashtags:
            hashtags.append(f"#{tag}")
    hashtags = hashtags[:6]
    combined = " ".join(part for part in (caption, " ".join(hashtags)) if part)
    while len(combined.encode("utf-16-le")) // 2 > 2200 and hashtags:
        hashtags.pop()
        combined = " ".join(part for part in (caption, " ".join(hashtags)) if part)
    if len(combined.encode("utf-16-le")) // 2 > 2200:
        combined = combined.encode("utf-16-le")[: 2198 * 2].decode(
            "utf-16-le", errors="ignore"
        ).rstrip()
        caption = combined
    return {
        "ai_post_caption": caption,
        "ai_hashtags": hashtags,
        "ai_tiktok_description": combined,
        "caption_generation_version": "caption-v1",
    }

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


def run_scoring_worker(input_json_path: str, output_json_path: str) -> int:
    with open(input_json_path, "r", encoding="utf-8") as input_file:
        request = json.load(input_file)
    result = score_multimodal_clip(
        video_path=str(request.get("video_path") or ""),
        transcript=str(request.get("transcript") or ""),
        creator=str(request.get("creator") or ""),
        game=str(request.get("game") or ""),
        stream_title=str(request.get("stream_title") or ""),
        viewer_count=int(request.get("viewer_count") or 0),
        duration=int(float(request.get("duration") or 0)),
    )
    output_path = os.path.abspath(output_json_path)
    output_dir = os.path.dirname(output_path) or "."
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".json",
        prefix="scoring_",
        dir=output_dir,
        delete=False,
    ) as temp_file:
        json.dump(result, temp_file)
        temp_file.flush()
        os.fsync(temp_file.fileno())
        temporary_output_path = temp_file.name
    os.replace(temporary_output_path, output_path)
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--transcribe-worker", action="store_true")
    parser.add_argument("--score-worker", action="store_true")
    parser.add_argument("--video-path")
    parser.add_argument("--input-json")
    parser.add_argument("--output-json")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.transcribe_worker:
        if not args.video_path or not args.output_json:
            raise ValueError("--video-path and --output-json are required.")
        return run_transcription_worker(args.video_path, args.output_json)
    if args.score_worker:
        if not args.input_json or not args.output_json:
            raise ValueError("--input-json and --output-json are required.")
        return run_scoring_worker(args.input_json, args.output_json)
    return 0

def analyze_video_frames(video_path: str) -> int:
    import cv2

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
    import cv2

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
