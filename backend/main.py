import base64
import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
from pathlib import Path
from typing import Any
import uuid
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from pathlib import Path
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from urllib.parse import urlencode
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
    client,
    generate_ai_title,
    generate_ai_description,
    release_whisper_model,
    score_multimodal_clip,
)
from video_editing import create_tiktok_edited_video

load_dotenv()


app = FastAPI(title="PulseAI Backend")
AUTO_CLIP_INTERVAL_SECONDS = 300
AUTO_CLIP_MIN_SCORE = int(os.getenv("AUTO_CLIP_MIN_SCORE", "45"))
app.state.tiktok_pkce_verifiers = {}


def _get_current_rss_mb() -> float:
    if psutil is not None:
        process = psutil.Process(os.getpid())
        return process.memory_info().rss / (1024 * 1024)

    rss_value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return rss_value / (1024 * 1024)
    return rss_value / 1024


def _log_memory_check(stage: str, candidate_number: int, total_candidates: int) -> None:
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    rss_mb = _get_current_rss_mb()
    print(
        f"MEMORY CHECK | stage={stage} | candidate={candidate_number}/{total_candidates} | "
        f"rss_mb={rss_mb:.1f} | ts={timestamp}"
    )


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
        subprocess.run(
            command,
            check=True,
            timeout=180,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
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
app.state.clip_generation_admission_lock = asyncio.Lock()
app.state.clip_generation_busy = False
app.state.video_edit_lock = asyncio.Lock()


async def try_begin_clip_generation() -> bool:
    async with app.state.clip_generation_admission_lock:
        if app.state.clip_generation_busy:
            return False
        app.state.clip_generation_busy = True
        return True


async def end_clip_generation() -> None:
    async with app.state.clip_generation_admission_lock:
        app.state.clip_generation_busy = False


async def _auto_clip_loop():
    await asyncio.sleep(AUTO_CLIP_INTERVAL_SECONDS)
    print("AUTO MODE STARTED")

    while True:
        print("AUTO CYCLE START")
        started = await try_begin_clip_generation()
        if not started:
            print("AUTO CYCLE SKIPPED: generation already in progress")
        else:
            try:
                result = await _run_auto_generate_clip_pipeline()
                print("AUTO RESULT:", result)
            except Exception as error:
                print("AUTO ERROR:", repr(error))
            finally:
                await end_clip_generation()
        print("AUTO CYCLE COMPLETE")
        await asyncio.sleep(AUTO_CLIP_INTERVAL_SECONDS)


@app.on_event("startup")
async def _start_auto_clip_task():
    if app.state.auto_clip_task is None or app.state.auto_clip_task.done():
        app.state.auto_clip_task = asyncio.create_task(_auto_clip_loop())


@app.on_event("shutdown")
async def _stop_auto_clip_task():
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


def load_creators() -> list[dict[str, str]]:
    if not CREATORS_FILE.exists():
        save_creators(DEFAULT_CREATORS)
        return DEFAULT_CREATORS.copy()

    try:
        with CREATORS_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)

        if not isinstance(data, list):
            raise ValueError("Creator data must be a list.")

        creators = []

        for item in data:
            if not isinstance(item, dict):
                continue

            name = str(item.get("name", "")).strip()
            channel = clean_channel_name(str(item.get("channel", "")))

            if name and channel:
                creators.append(
                    {
                        "name": name,
                        "channel": channel,
                    }
                )

        return creators

    except (json.JSONDecodeError, OSError, ValueError) as error:
        raise HTTPException(
            status_code=500,
            detail=f"Unable to read creators.json: {error}",
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
def verify_twitch_credentials() -> None:
    if not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
        raise HTTPException(
            status_code=500,
            detail="Twitch credentials are missing from backend/.env",
        )

def get_twitch_user_access_token() -> str:
    token_file = Path(__file__).resolve().parent / "twitch_user_token.json"

    if not token_file.exists():
        raise HTTPException(
            status_code=401,
            detail="Twitch account is not connected. Visit /auth/twitch first.",
        )

    with token_file.open("r", encoding="utf-8") as file:
        token_data = json.load(file)

    access_token = token_data.get("access_token")

    if not access_token:
        raise HTTPException(
            status_code=401,
            detail="Twitch user access token is missing.",
        )

    return access_token

def refresh_twitch_user_access_token() -> str:
    token_file = Path(__file__).resolve().parent / "twitch_user_token.json"

    with token_file.open("r", encoding="utf-8") as file:
        token_data = json.load(file)

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

    with token_file.open("w", encoding="utf-8") as file:
        json.dump(token_data, file, indent=2)

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

        streams = stream_response.json().get("data", [])

        print("Checking channel:", clean_channel)
        print("Streams returned:", streams)

        if not streams:
            return {
                "channel": user["login"],
                "user_id": user["id"],
                "display_name": user["display_name"],
                "profile_image_url": user["profile_image_url"],
                "is_live": False,
                "status": "OFFLINE",
                "title": None,
                "game_name": None,
                "viewer_count": 0,
                "started_at": None,
                "thumbnail_url": None,
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
            "status": "LIVE",
            "title": stream.get("title"),
            "game_name": stream.get("game_name"),
            "viewer_count": stream.get("viewer_count", 0),
            "started_at": stream.get("started_at"),
            "thumbnail_url": thumbnail_url,
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


async def fetch_fresh_twitch_clips(
    broadcaster_id: str,
    ignored_clip_ids: set[str],
    ignored_clip_urls: set[str],
    limit: int = 5,
) -> list[dict[str, Any]]:
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

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            response = await client.get(
                "https://api.twitch.tv/helix/clips",
                headers=headers,
                params={"broadcaster_id": broadcaster_id, "first": 20},
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

    fresh_clips = []
    for twitch_clip in response.json().get("data", []):
        clip_id = str(twitch_clip.get("id", "")).strip()
        if not clip_id or clip_id in ignored_clip_ids:
            continue

        public_url = twitch_clip.get("url") or f"https://clips.twitch.tv/{clip_id}"
        if public_url in ignored_clip_urls:
            continue

        fresh_clips.append(twitch_clip)
        if len(fresh_clips) >= limit:
            break

    return fresh_clips

def download_twitch_clip(clip_url: str, output_name: str) -> str:
    output_path = f"downloads/{output_name}.mp4"

    try:
        subprocess.run(
            [
                "yt-dlp",
                "-o",
                output_path,
                clip_url,
            ],
            check=True,
        )
        return output_path
    except subprocess.CalledProcessError as error:
        print(f"TWITCH CLIP DOWNLOAD FAILED for {clip_url}:", repr(error))
        return None


async def upload_tiktok_draft(video_path: str) -> dict:
    token_file = Path(__file__).resolve().parent / "tiktok_user_token.json"

    if not token_file.exists():
        raise HTTPException(
            status_code=401,
            detail="TikTok account is not connected.",
        )

    try:
        with token_file.open("r", encoding="utf-8") as file:
            token_response = json.load(file)
    except (OSError, json.JSONDecodeError) as error:
        raise HTTPException(
            status_code=500,
            detail=f"Unable to read TikTok token storage: {error}",
        ) from error

    access_token = token_response.get("data", {}).get("access_token")
    if not access_token:
        access_token = token_response.get("access_token")

    if not access_token:
        raise HTTPException(
            status_code=401,
            detail="TikTok access token is missing.",
        )

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

        if init_response.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"TikTok draft upload initialization failed: {init_response.text}",
            )

        init_result = init_response.json()
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
            detail=f"TikTok draft video upload failed: {upload_response.text}",
        )

    return {
        "publish_id": publish_id,
        "upload_result": upload_response.json()
        if upload_response.content
        else None,
    }


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
@app.get("/api/clips")
async def get_clips():
    clips_file = Path(__file__).resolve().parent / "clips.json"

    try:
        with clips_file.open("r", encoding="utf-8") as file:
            return json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


@app.get("/api/clips/{clip_id}/video")
async def get_clip_video(clip_id: str, download: int = 0):
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
async def publish_clip(clip: dict):
    video_path = clip.get("video_path")
    if not video_path:
        raise HTTPException(
            status_code=400,
            detail="Clip is missing video_path.",
        )

    if not Path(video_path).is_file():
        raise HTTPException(
            status_code=404,
            detail=f"Video file not found: {video_path}",
        )

    tiktok_result = await upload_tiktok_draft(video_path)
    published = load_published()

    # Don't save duplicates
    if not any(item["title"] == clip["title"] for item in published):
        published.append(clip)
        save_published(published)

    return {
        "success": True,
        "message": f"Published '{clip['title']}' successfully!",
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

@app.post("/api/clips")
async def create_clip(clip: dict):
    clips_file = Path(__file__).resolve().parent / "clips.json"

    try:
        with clips_file.open("r", encoding="utf-8") as file:
            clips = json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        clips = []

    new_clip = {
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
}

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
    started = await try_begin_clip_generation()
    if not started:
        return JSONResponse(
            status_code=409,
            content={"message": "Clip generation already in progress."},
        )

    try:
        return await _run_auto_generate_clip_pipeline()
    finally:
        await end_clip_generation()


async def _run_auto_generate_clip_pipeline():
    generation_started_at = time.perf_counter()
    processed_candidates_count = 0
    creators = load_creators()

    for creator in creators:
        try:
            stream = await get_twitch_channel_data(creator["channel"])
            broadcaster_id = stream.get("user_id")

        except Exception as error:
            print(
                f"TWITCH CHECK FAILED for {creator['channel']}:",
                repr(error),
            )
            continue

        if not stream.get("is_live"):
            continue

        viewer_count = stream.get("viewer_count", 0)
        stream_title = (
            stream.get("title")
            or f"{creator['name']} Live Moment"
        )

        clips_file = Path(__file__).resolve().parent / "clips.json"

        try:
            with clips_file.open("r", encoding="utf-8") as file:
                existing_clips = json.load(file)
        except (FileNotFoundError, json.JSONDecodeError):
            existing_clips = []

        cached_clip_ids = {
            str(existing.get("twitch_clip_id", "")).strip()
            for existing in existing_clips
            if existing.get("twitch_clip_id")
        }
        cached_clip_urls = {
            str(existing.get("public_url", "")).strip()
            for existing in existing_clips
            if existing.get("public_url")
        }

        attempted_clip_ids: set[str] = set()
        attempted_clip_urls: set[str] = set()
        candidates = []
        target_candidate_count = 5

        for batch_attempt in range(1, 3):
            ignored_ids = cached_clip_ids | attempted_clip_ids
            ignored_urls = cached_clip_urls | attempted_clip_urls

            _log_memory_check(
                stage="before_twitch_clip_fetch",
                candidate_number=0,
                total_candidates=target_candidate_count,
            )
            fetch_started_at = time.perf_counter()
            fresh_batch = await fetch_fresh_twitch_clips(
                broadcaster_id=broadcaster_id,
                ignored_clip_ids=ignored_ids,
                ignored_clip_urls=ignored_urls,
                limit=5,
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
                print(
                    f"No fresh Twitch clips available for {creator['channel']} "
                    f"(batch {batch_attempt}/2)."
                )
                continue

            total_candidates = len(fresh_batch)
            for candidate_index, twitch_clip in enumerate(fresh_batch, start=1):
                twitch_clip_id = str(twitch_clip.get("id", "")).strip()
                if not twitch_clip_id:
                    continue

                processed_candidates_count += 1
                candidate_started_at = time.perf_counter()

                public_url = (
                    twitch_clip.get("url")
                    or f"https://clips.twitch.tv/{twitch_clip_id}"
                )

                attempted_clip_ids.add(twitch_clip_id)
                attempted_clip_urls.add(public_url)

                clip = {
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
                    "candidate_number": candidate_index,
                }

                _log_memory_check(
                    stage="before_ytdlp_download",
                    candidate_number=candidate_index,
                    total_candidates=total_candidates,
                )
                download_started_at = time.perf_counter()
                video_path = download_twitch_clip(
                    clip["public_url"],
                    clip["twitch_clip_id"],
                )
                _log_performance_timing(
                    stage="ytdlp_download",
                    candidate_number=candidate_index,
                    total_candidates=total_candidates,
                    elapsed_seconds=time.perf_counter() - download_started_at,
                )
                _log_memory_check(
                    stage="after_ytdlp_download",
                    candidate_number=candidate_index,
                    total_candidates=total_candidates,
                )

                if not video_path:
                    _log_performance_timing(
                        stage="candidate_processing_total",
                        candidate_number=candidate_index,
                        total_candidates=total_candidates,
                        elapsed_seconds=time.perf_counter() - candidate_started_at,
                    )
                    print(f"Candidate {candidate_index} skipped: download failed.")
                    continue

                clip["video_path"] = video_path
                try:
                    _log_memory_check(
                        stage="before_whisper_transcription",
                        candidate_number=candidate_index,
                        total_candidates=total_candidates,
                    )
                    transcription_started_at = time.perf_counter()
                    transcription = _transcribe_video_with_segments_subprocess(video_path)
                    _log_performance_timing(
                        stage="whisper_transcription",
                        candidate_number=candidate_index,
                        total_candidates=total_candidates,
                        elapsed_seconds=time.perf_counter() - transcription_started_at,
                    )
                    _log_memory_check(
                        stage="after_whisper_transcription",
                        candidate_number=candidate_index,
                        total_candidates=total_candidates,
                    )
                    clip["transcript"] = transcription.get("transcript", "")
                    clip["segments"] = transcription.get("segments", [])
                    _log_memory_check(
                        stage="before_multimodal_scoring",
                        candidate_number=candidate_index,
                        total_candidates=total_candidates,
                    )
                    scoring_started_at = time.perf_counter()
                    multimodal = score_multimodal_clip(
                        video_path=video_path,
                        transcript=clip["transcript"],
                        creator=clip["creator"],
                        game=clip.get("game") or "",
                        stream_title=clip["title"],
                        viewer_count=clip["viewer_count"],
                        duration=clip.get("duration", 0),
                    )
                    _log_performance_timing(
                        stage="multimodal_scoring",
                        candidate_number=candidate_index,
                        total_candidates=total_candidates,
                        elapsed_seconds=time.perf_counter() - scoring_started_at,
                    )
                    _log_memory_check(
                        stage="after_multimodal_scoring",
                        candidate_number=candidate_index,
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
                finally:
                    _log_memory_check(
                        stage="before_whisper_release",
                        candidate_number=candidate_index,
                        total_candidates=total_candidates,
                    )
                    release_whisper_model()
                    _log_memory_check(
                        stage="after_whisper_release",
                        candidate_number=candidate_index,
                        total_candidates=total_candidates,
                    )

                _log_performance_timing(
                    stage="candidate_processing_total",
                    candidate_number=candidate_index,
                    total_candidates=total_candidates,
                    elapsed_seconds=time.perf_counter() - candidate_started_at,
                )

                candidates.append(clip)

            if candidates:
                break

            print(
                f"All clips in batch {batch_attempt} failed to download/score for "
                f"{creator['channel']}."
            )

        if not candidates:
            return {
                "message": "No viral clips found.",
                "best_score": 0,
            }

        print("------------------------")
        for candidate in candidates:
            print(f"Candidate {candidate['candidate_number']}: {candidate['score']}")

        winner_selection_started_at = time.perf_counter()
        best_clip = max(candidates, key=lambda c: c["score"])
        _log_performance_timing(
            stage="winner_selection",
            elapsed_seconds=time.perf_counter() - winner_selection_started_at,
        )
        print("")
        print(f"Best Clip: #{best_clip['candidate_number']}")
        print(f"Final Score: {best_clip['score']}")
        print("------------------------")

        if best_clip["decision"] == "reject" or best_clip["score"] < AUTO_CLIP_MIN_SCORE:
            total_elapsed = time.perf_counter() - generation_started_at
            _log_performance_timing(
                stage="generation_total",
                elapsed_seconds=total_elapsed,
            )
            print(
                "PERFORMANCE TIMING SUMMARY | "
                f"total_elapsed_seconds={total_elapsed:.3f} | "
                f"processed_candidates={processed_candidates_count}"
            )
            return {
                "message": "No viral clips found.",
                "best_score": best_clip["score"],
            }

        is_duplicate = any(
            existing.get("twitch_clip_id") == best_clip["twitch_clip_id"]
            or existing.get("public_url") == best_clip["public_url"]
            for existing in existing_clips
        )

        if is_duplicate:
            total_elapsed = time.perf_counter() - generation_started_at
            _log_performance_timing(
                stage="generation_total",
                elapsed_seconds=total_elapsed,
            )
            print(
                "PERFORMANCE TIMING SUMMARY | "
                f"total_elapsed_seconds={total_elapsed:.3f} | "
                f"processed_candidates={processed_candidates_count}"
            )
            return {
                "message": "No viral clips found.",
                "best_score": best_clip["score"],
            }

        best_clip["ai_title"] = generate_ai_title(best_clip["transcript"])
        best_clip["ai_description"] = generate_ai_description(best_clip["transcript"])
        best_clip["raw_video_path"] = best_clip.get("video_path")

        try:
            title_for_overlay = best_clip.get("ai_title") or best_clip.get("title", "")
            caption_segments = best_clip.get("segments", [])
            best_candidate_number = int(best_clip.get("candidate_number", 0) or 0)
            total_candidates = len(candidates)

            async with app.state.video_edit_lock:
                _log_memory_check(
                    stage="before_ffmpeg_video_editing",
                    candidate_number=best_candidate_number,
                    total_candidates=total_candidates,
                )
                ffmpeg_started_at = time.perf_counter()
                edited_video_path = await asyncio.to_thread(
                    create_tiktok_edited_video,
                    best_clip["raw_video_path"],
                    title_for_overlay,
                    caption_segments,
                )
                _log_performance_timing(
                    stage="ffmpeg_editing",
                    candidate_number=best_candidate_number,
                    total_candidates=total_candidates,
                    elapsed_seconds=time.perf_counter() - ffmpeg_started_at,
                )
                _log_memory_check(
                    stage="after_ffmpeg_video_editing",
                    candidate_number=best_candidate_number,
                    total_candidates=total_candidates,
                )

            best_clip["video_path"] = edited_video_path
        except Exception as error:
            print("TIKTOK VIDEO EDIT FAILED:", repr(error))
            print(traceback.format_exc())
            best_clip["video_path"] = best_clip.get("raw_video_path")

        _log_memory_check(
            stage="before_clip_persistence",
            candidate_number=int(best_clip.get("candidate_number", 0) or 0),
            total_candidates=len(candidates),
        )
        persistence_started_at = time.perf_counter()
        result = await create_clip(best_clip)
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
            f"processed_candidates={processed_candidates_count}"
        )
        return result["clip"]

    total_elapsed = time.perf_counter() - generation_started_at
    _log_performance_timing(
        stage="generation_total",
        elapsed_seconds=total_elapsed,
    )
    print(
        "PERFORMANCE TIMING SUMMARY | "
        f"total_elapsed_seconds={total_elapsed:.3f} | "
        f"processed_candidates={processed_candidates_count}"
    )
    return {
        "message": "No monitored creators are currently live."
    }

@app.post("/api/clips/{clip_id}/publish")
async def publish_clip(clip_id: str):
    clips_file = Path(__file__).resolve().parent / "clips.json"

    try:
        with clips_file.open("r", encoding="utf-8") as file:
            clips = json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        clips = []

    for clip in clips:
        if clip.get("id") == clip_id:
            clip["status"] = "Published"

            with clips_file.open("w", encoding="utf-8") as file:
                json.dump(clips, file, indent=2)

            return {"success": True, "clip": clip}

    raise HTTPException(status_code=404, detail="Clip not found")

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

    token_file = Path(__file__).resolve().parent / "twitch_user_token.json"

    with token_file.open("w", encoding="utf-8") as file:
        json.dump(
            {
                "access_token": token_data.get("access_token"),
                "refresh_token": token_data.get("refresh_token"),
                "expires_in": token_data.get("expires_in"),
                "scope": token_data.get("scope", []),
            },
            file,
            indent=2,
        )

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
            detail=f"TikTok token exchange failed: {response.text}",
        )

    token_file = Path(__file__).resolve().parent / "tiktok_user_token.json"
    with token_file.open("w", encoding="utf-8") as file:
        json.dump(response.json(), file, indent=2)

    return {
        "success": True,
        "message": "TikTok account connected successfully.",
    }