import json
import os
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from pathlib import Path

load_dotenv()

app = FastAPI(title="PulseAI Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")

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

    access_token = await get_twitch_access_token()

    headers = {
        "Client-Id": TWITCH_CLIENT_ID,
        "Authorization": f"Bearer {access_token}",
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
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
    

    if not streams:
        return {
            "channel": user["login"],
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
    "title": clip.get("title", "Untitled clip"),
    "creator": clip.get("creator", "Unknown creator"),
    "score": clip.get("score", 0),
    "status": clip.get("status", "Ready to review"),
    "viewer_count": clip.get("viewer_count"),
    "game": clip.get("game"),
    "started_at": clip.get("started_at"),
    "thumbnail_url": clip.get("thumbnail_url"),
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
        except Exception:
            continue

        if stream.get("is_live"):
            viewer_count = stream.get("viewer_count", 0)
            stream_title = stream.get("title") or f"{creator['name']} Live Moment"

            clip = {
                "title": stream_title,
                "creator": creator["name"],
                "score": min(99, 80 + viewer_count // 50000),
                "status": "Ready to review",
                "viewer_count": viewer_count,
                "game": stream.get("game_name"),
                "started_at": stream.get("started_at"),
                "thumbnail_url": stream.get("thumbnail_url"),
            }

            await create_clip(clip)
            return clip

    return {
        "message": "No monitored creators are currently live."
    }