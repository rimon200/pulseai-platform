import base64
import hashlib
import json
import os
import secrets
from pathlib import Path
from typing import Any
import uuid
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from pathlib import Path
from fastapi.responses import RedirectResponse
from urllib.parse import urlencode
import subprocess
from download_service import DownloadService
import asyncio
import time
from ai import (
    client,
    generate_ai_title,
    generate_ai_description,
    transcribe_video,
    score_multimodal_clip,
)

load_dotenv()


app = FastAPI(title="PulseAI Backend")
AUTO_CLIP_INTERVAL_SECONDS = 300
AUTO_CLIP_MIN_SCORE = 75
app.state.tiktok_pkce_verifiers = {}


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
app.state.auto_clip_lock = asyncio.Lock()


async def _auto_clip_loop():
    await asyncio.sleep(AUTO_CLIP_INTERVAL_SECONDS)
    print("AUTO MODE STARTED")

    while True:
        print("AUTO CYCLE START")
        if app.state.auto_clip_lock.locked():
            print("AUTO CYCLE SKIPPED: previous cycle still running")
        else:
            async with app.state.auto_clip_lock:
                try:
                    result = await auto_generate_clip()
                    print("AUTO RESULT:", result)
                except Exception as error:
                    print("AUTO ERROR:", repr(error))
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

    await asyncio.sleep(30)

    clip["public_url"] = get_twitch_clip_url(clip["id"])

    return clip

def download_twitch_clip(clip_url: str, output_name: str) -> str:
    output_path = f"downloads/{output_name}.mp4"

    for _ in range(12):
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

        except subprocess.CalledProcessError:
            time.sleep(5)

    return ""

    return output_path


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

    video_bytes = video_file.read_bytes()
    video_size = len(video_bytes)
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
            content=video_bytes,
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
@app.post("/api/publish")
async def publish_clip(clip: dict):
    published = load_published()

    # Don't save duplicates
    if not any(item["title"] == clip["title"] for item in published):
        published.append(clip)
        save_published(published)

    return {
        "success": True,
        "message": f"Published '{clip['title']}' successfully!",
        "published_count": len(published),
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
        "score": random.randint(75, 99),
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

        candidates = []
        for candidate_index in range(1, 6):
            try:
                twitch_clip = await create_twitch_clip(broadcaster_id)
            except Exception as error:
                print(
                    f"TWITCH CLIP CREATION FAILED for candidate {candidate_index}:",
                    repr(error),
                )
                continue

            clip = {
                "title": stream_title,
                "creator": creator["name"],
                "status": "Ready to review",
                "viewer_count": viewer_count,
                "game": stream.get("game_name"),
                "started_at": stream.get("started_at"),
                "thumbnail_url": stream.get("thumbnail_url"),
                "twitch_clip_id": twitch_clip.get("id"),
                "twitch_edit_url": twitch_clip.get("edit_url"),
                "public_url": twitch_clip.get("public_url"),
                "candidate_number": candidate_index,
            }

            video_path = download_twitch_clip(
                clip["public_url"],
                clip["twitch_clip_id"],
            )

            print("RETURNED:", video_path)
            print(
                "VIDEO PATH DEBUG:",
                repr(video_path),
                type(video_path),
            )

            if not video_path:
                print(f"Candidate {candidate_index} skipped: download failed.")
                continue

            clip["video_path"] = video_path
            clip["transcript"] = transcribe_video(video_path)
            multimodal = score_multimodal_clip(
                video_path=video_path,
                transcript=clip["transcript"],
                creator=clip["creator"],
                game=clip.get("game") or "",
                stream_title=clip["title"],
                viewer_count=clip["viewer_count"],
                duration=clip.get("duration", 0),
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

            candidates.append(clip)

        if not candidates:
            return {
                "message": "No viral clips found.",
                "best_score": 0,
            }

        print("------------------------")
        for candidate in candidates:
            print(f"Candidate {candidate['candidate_number']}: {candidate['score']}")

        best_clip = max(candidates, key=lambda c: c["score"])
        print("")
        print(f"Best Clip: #{best_clip['candidate_number']}")
        print(f"Final Score: {best_clip['score']}")
        print("------------------------")

        if best_clip["decision"] == "reject" or best_clip["score"] < AUTO_CLIP_MIN_SCORE:
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
            return {
                "message": "No viral clips found.",
                "best_score": best_clip["score"],
            }

        best_clip["ai_title"] = generate_ai_title(best_clip["transcript"])
        best_clip["ai_description"] = generate_ai_description(best_clip["transcript"])

        

        result = await create_clip(best_clip)
        return result["clip"]

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