from dotenv import load_dotenv
from openai import OpenAI
from faster_whisper import WhisperModel
import base64
import cv2
import json
import re

load_dotenv()

client = OpenAI()
def generate_ai_title(transcript: str) -> str:
        if not transcript.strip():
             return ""

        response = client.responses.create(
            model="gpt-5-nano",
            input=f"""
    Create ONE short, catchy, viral title for this gaming clip.

    Transcript:
{transcript}

Return only the title.
"""
    )

        return response.output_text.strip()

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
    model = WhisperModel("tiny", device="cpu", compute_type="int8")
    segments, _ = model.transcribe(video_path, language="en")

    transcript = " ".join(segment.text.strip() for segment in segments)
    return transcript

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
        "75-84: Good clip. Return \"accept\".\n"
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
        decision = "accept" if score >= 75 else "reject"

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

    if decision == "accept" and score < 75:
        result["decision"] = "reject"

    print("MULTIMODAL SCORE:", result["score"])
    print("DECISION:", result["decision"])
    print("REASON:", result["reason"])
    print("VISUAL SCORE:", result["visual_score"])
    print("TRANSCRIPT SCORE:", result["transcript_score"])
    print("CONTEXT SCORE:", result["context_score"])
    print("CONFIDENCE:", result["confidence"])

    return result
