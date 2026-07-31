from __future__ import annotations

import asyncio
import os
import re
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import httpx


class StreamProviderError(RuntimeError):
    def __init__(self, code: str, message: str, *, retry_after: int | None = None):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


class StreamProvider(ABC):
    provider: str

    @abstractmethod
    def normalize_channel_identifier(self, value: object) -> str:
        raise NotImplementedError

    @abstractmethod
    async def resolve_creator(self, identifier: object) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    async def get_live_status(self, creator: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    async def validate_connection(self) -> dict[str, Any]:
        raise NotImplementedError


def _normalized_stream(
    *, provider: str, creator: dict[str, Any], livestream: dict[str, Any] | None,
) -> dict[str, Any]:
    live = livestream or {}
    category = live.get("category") or {}
    user = live.get("broadcaster_user") or {}
    channel = live.get("channel") or {}
    username = str(
        creator.get("platform_username") or user.get("username")
        or creator.get("platform_channel_slug") or channel.get("slug") or ""
    )
    return {
        "provider": provider,
        "creator_id": creator.get("id"),
        "platform_user_id": str(
            creator.get("platform_user_id") or user.get("id") or ""
        ),
        "username": username,
        "channel": str(
            creator.get("platform_channel_slug") or channel.get("slug")
            or username
        ),
        "display_name": str(
            creator.get("platform_display_name") or username
        ),
        "is_live": bool(livestream),
        "status": "LIVE" if livestream else "OFFLINE",
        "stream_id": str(live.get("id") or ""),
        "title": live.get("title"),
        "category": category.get("name"),
        "game_name": category.get("name"),
        "language": live.get("language_code"),
        "started_at": live.get("started_at"),
        "viewer_count": int(live.get("viewer_count") or 0),
        "thumbnail_url": live.get("thumbnail"),
        "profile_image_url": (
            creator.get("platform_avatar_url") or user.get("profile_picture")
        ),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "generation_available": provider == "twitch",
        "generation_unavailable_code": (
            "" if provider == "twitch" else "kick_playback_unavailable"
        ),
    }


class TwitchStreamProvider(StreamProvider):
    provider = "twitch"

    def __init__(self, channel_loader: Callable[[str], Awaitable[dict[str, Any]]]):
        self._channel_loader = channel_loader

    def normalize_channel_identifier(self, value: object) -> str:
        return str(value or "").strip().removeprefix("@").lower()

    async def resolve_creator(self, identifier: object) -> dict[str, Any]:
        return await self._channel_loader(self.normalize_channel_identifier(identifier))

    async def get_live_status(self, creator: dict[str, Any]) -> dict[str, Any]:
        return await self.resolve_creator(
            creator.get("platform_channel_slug") or creator.get("channel")
        )

    async def validate_connection(self) -> dict[str, Any]:
        return {"provider": self.provider, "valid": True}


class KickStreamProvider(StreamProvider):
    provider = "kick"
    api_base_url = "https://api.kick.com"
    oauth_base_url = "https://id.kick.com"

    def __init__(self, *, client: httpx.AsyncClient | None = None):
        self._client = client
        self._app_token = ""
        self._app_token_expires_at = 0.0
        self.last_successful_request: str | None = None
        self.last_polling_error: str | None = None
        self.rate_limit_status: str | None = None

    @property
    def enabled(self) -> bool:
        return os.getenv("KICK_INTEGRATION_ENABLED", "false").strip().lower() in {
            "1", "true", "yes", "on",
        }

    @property
    def configured(self) -> bool:
        return bool(
            os.getenv("KICK_CLIENT_ID", "").strip()
            and os.getenv("KICK_CLIENT_SECRET", "").strip()
            and os.getenv("KICK_REDIRECT_URI", "").strip()
        )

    def normalize_channel_identifier(self, value: object) -> str:
        raw = str(value or "").strip().lower()
        raw = re.sub(r"^https?://(?:www\.)?kick\.com/", "", raw)
        slug = raw.removeprefix("@").split("/", 1)[0]
        if not slug or len(slug) > 25 or not re.fullmatch(r"[a-z0-9_-]+", slug):
            raise StreamProviderError("invalid_channel", "Invalid Kick channel slug.")
        return slug

    def _timeout(self) -> float:
        try:
            return max(1.0, min(float(os.getenv("KICK_REQUEST_TIMEOUT_SECONDS", "15")), 60.0))
        except ValueError:
            return 15.0

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        if self._client is not None:
            response = await self._client.request(method, url, **kwargs)
        else:
            async with httpx.AsyncClient(timeout=self._timeout()) as client:
                response = await client.request(method, url, **kwargs)
        if response.status_code == 429:
            retry_after_text = response.headers.get("Retry-After", "")
            try:
                retry_after = max(1, int(retry_after_text))
            except ValueError:
                retry_after = int(os.getenv("KICK_POLL_INTERVAL_SECONDS", "60"))
            self.rate_limit_status = f"limited:{retry_after}"
            raise StreamProviderError(
                "rate_limited", "Kick API rate limit reached.",
                retry_after=retry_after,
            )
        if response.status_code in {401, 403}:
            raise StreamProviderError("auth_error", "Kick authorization failed.")
        if response.status_code == 404:
            raise StreamProviderError("not_found", "Kick creator was not found.")
        if response.status_code >= 400:
            raise StreamProviderError("api_error", "Kick API request failed.")
        self.last_successful_request = datetime.now(timezone.utc).isoformat()
        self.last_polling_error = None
        self.rate_limit_status = "ok"
        return response

    async def _app_access_token(self) -> str:
        if not self.enabled:
            raise StreamProviderError("disabled", "Kick integration is disabled.")
        if not self.configured:
            raise StreamProviderError("auth_error", "Kick integration is not configured.")
        if self._app_token and self._app_token_expires_at > time.time() + 30:
            return self._app_token
        response = await self._request(
            "POST",
            f"{self.oauth_base_url}/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": os.getenv("KICK_CLIENT_ID", "").strip(),
                "client_secret": os.getenv("KICK_CLIENT_SECRET", "").strip(),
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        payload = response.json()
        token = str(payload.get("access_token") or "")
        if not token:
            raise StreamProviderError("auth_error", "Kick token response was invalid.")
        self._app_token = token
        self._app_token_expires_at = time.time() + int(payload.get("expires_in") or 3600)
        return token

    async def resolve_creator(self, identifier: object) -> dict[str, Any]:
        slug = self.normalize_channel_identifier(identifier)
        token = await self._app_access_token()
        response = await self._request(
            "GET", f"{self.api_base_url}/public/v1/channels",
            params=[("slug", slug)],
            headers={"Authorization": f"Bearer {token}"},
        )
        rows = response.json().get("data") or []
        if not rows:
            raise StreamProviderError("not_found", "Kick creator was not found.")
        channel = rows[0]
        return {
            "provider": "kick",
            "platform_user_id": str(channel.get("broadcaster_user_id") or ""),
            "platform_username": str(channel.get("slug") or slug),
            "platform_channel_slug": str(channel.get("slug") or slug),
            "platform_display_name": str(channel.get("slug") or slug),
            "platform_avatar_url": "",
            "channel": str(channel.get("slug") or slug),
            "display_name": str(channel.get("slug") or slug),
        }

    async def get_live_status(self, creator: dict[str, Any]) -> dict[str, Any]:
        token = await self._app_access_token()
        user_id = str(creator.get("platform_user_id") or "")
        if not user_id:
            resolved = await self.resolve_creator(
                creator.get("platform_channel_slug") or creator.get("channel")
            )
            creator = {**creator, **resolved}
            user_id = str(resolved["platform_user_id"])
        response = await self._request(
            "GET", f"{self.api_base_url}/public/v1/users/livestreams",
            params=[("user_id", user_id)],
            headers={"Authorization": f"Bearer {token}"},
        )
        rows = response.json().get("data") or []
        return _normalized_stream(
            provider="kick", creator=creator,
            livestream=rows[0] if rows else None,
        )

    async def validate_connection(self) -> dict[str, Any]:
        try:
            await self._app_access_token()
            return {"provider": "kick", "valid": True}
        except StreamProviderError as error:
            return {"provider": "kick", "valid": False, "code": error.code}


def _iso8601_duration_seconds(value: object) -> int | None:
    text = str(value or "").strip().upper()
    match = re.fullmatch(
        r"P(?:(?P<days>\d+)D)?T(?:(?P<hours>\d+)H)?"
        r"(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?",
        text,
    )
    if not match:
        return None
    values = {key: int(number or 0) for key, number in match.groupdict().items()}
    return (
        values["days"] * 86400 + values["hours"] * 3600
        + values["minutes"] * 60 + values["seconds"]
    )


class YouTubeUploadProvider(StreamProvider):
    provider = "youtube"
    api_base_url = "https://www.googleapis.com/youtube/v3"

    def __init__(self, *, client: httpx.AsyncClient | None = None):
        self._client = client
        self.last_successful_request: str | None = None
        self.last_polling_error: str | None = None

    @property
    def enabled(self) -> bool:
        return os.getenv("YOUTUBE_INTEGRATION_ENABLED", "false").strip().lower() in {
            "1", "true", "yes", "on",
        }

    @property
    def configured(self) -> bool:
        return bool(os.getenv("YOUTUBE_API_KEY", "").strip())

    def _timeout(self) -> float:
        try:
            return max(
                1.0,
                min(float(os.getenv("YOUTUBE_REQUEST_TIMEOUT_SECONDS", "15")), 60.0),
            )
        except ValueError:
            return 15.0

    def normalize_channel_identifier(self, value: object) -> str:
        raw = str(value or "").strip()
        raw = re.sub(
            r"^https?://(?:www\.)?youtube\.com/(?:channel/|@)", "", raw,
            flags=re.IGNORECASE,
        ).split("/", 1)[0]
        if raw.startswith("UC") and re.fullmatch(r"UC[A-Za-z0-9_-]{20,30}", raw):
            return raw
        handle = raw.removeprefix("@").lower()
        if not handle or len(handle) > 30 or not re.fullmatch(r"[a-z0-9._-]+", handle):
            raise StreamProviderError(
                "invalid_channel", "Invalid YouTube channel ID or handle."
            )
        return f"@{handle}"

    async def _request(self, resource: str, params: dict[str, object]) -> dict[str, Any]:
        if not self.enabled:
            raise StreamProviderError("disabled", "YouTube integration is disabled.")
        api_key = os.getenv("YOUTUBE_API_KEY", "").strip()
        if not api_key:
            raise StreamProviderError("auth_error", "YouTube API key is unavailable.")
        request_params = {**params, "key": api_key}
        if self._client is not None:
            response = await self._client.get(
                f"{self.api_base_url}/{resource}", params=request_params,
            )
        else:
            async with httpx.AsyncClient(timeout=self._timeout()) as client:
                response = await client.get(
                    f"{self.api_base_url}/{resource}", params=request_params,
                )
        if response.status_code == 429:
            self.last_polling_error = "rate_limited"
            raise StreamProviderError("rate_limited", "YouTube API rate limit reached.")
        if response.status_code in {401, 403}:
            reason = "quota_exceeded"
            try:
                errors = response.json().get("error", {}).get("errors", [])
                api_reason = str((errors or [{}])[0].get("reason") or "")
                if api_reason not in {"quotaExceeded", "dailyLimitExceeded"}:
                    reason = "auth_error"
            except (AttributeError, IndexError, ValueError):
                reason = "auth_error"
            self.last_polling_error = reason
            raise StreamProviderError(reason, "YouTube API request was rejected.")
        if response.status_code >= 400:
            self.last_polling_error = "api_error"
            raise StreamProviderError("api_error", "YouTube API request failed.")
        self.last_successful_request = datetime.now(timezone.utc).isoformat()
        self.last_polling_error = None
        return response.json()

    async def resolve_channel(self, identifier: object) -> dict[str, Any]:
        normalized = self.normalize_channel_identifier(identifier)
        selector = (
            {"id": normalized}
            if normalized.startswith("UC")
            else {"forHandle": normalized}
        )
        payload = await self._request(
            "channels", {"part": "snippet,contentDetails", **selector},
        )
        rows = payload.get("items") or []
        if not rows:
            raise StreamProviderError("not_found", "YouTube channel was not found.")
        channel = rows[0]
        snippet = channel.get("snippet") or {}
        thumbnails = snippet.get("thumbnails") or {}
        avatar = (thumbnails.get("high") or thumbnails.get("default") or {}).get("url")
        uploads = (
            (channel.get("contentDetails") or {}).get("relatedPlaylists") or {}
        ).get("uploads")
        if not uploads:
            raise StreamProviderError("api_error", "YouTube uploads playlist is unavailable.")
        custom_url = str(snippet.get("customUrl") or "").removeprefix("@")
        return {
            "provider": "youtube",
            "platform_user_id": str(channel.get("id") or ""),
            "platform_username": custom_url,
            "platform_channel_slug": custom_url or str(channel.get("id") or ""),
            "platform_display_name": str(snippet.get("title") or custom_url),
            "platform_avatar_url": str(avatar or ""),
            "uploads_playlist_id": str(uploads),
            "channel": custom_url or str(channel.get("id") or ""),
            "display_name": str(snippet.get("title") or custom_url),
        }

    async def resolve_creator(self, identifier: object) -> dict[str, Any]:
        return await self.resolve_channel(identifier)

    async def get_uploads_playlist(self, creator: dict[str, Any]) -> str:
        playlist = str(creator.get("uploads_playlist_id") or "").strip()
        if playlist:
            return playlist
        resolved = await self.resolve_channel(
            creator.get("platform_user_id")
            or creator.get("platform_channel_slug")
            or creator.get("channel")
        )
        return str(resolved["uploads_playlist_id"])

    async def get_video_metadata(self, video_ids: list[str]) -> list[dict[str, Any]]:
        identifiers = [str(item).strip() for item in video_ids if str(item).strip()]
        if not identifiers:
            return []
        payload = await self._request(
            "videos",
            {
                "part": "snippet,contentDetails,status,liveStreamingDetails",
                "id": ",".join(identifiers[:50]),
                "maxResults": 50,
            },
        )
        results = []
        for video in payload.get("items") or []:
            snippet = video.get("snippet") or {}
            status = video.get("status") or {}
            thumbnails = snippet.get("thumbnails") or {}
            thumbnail = (
                thumbnails.get("maxres") or thumbnails.get("high")
                or thumbnails.get("medium") or thumbnails.get("default") or {}
            ).get("url")
            duration = _iso8601_duration_seconds(
                (video.get("contentDetails") or {}).get("duration")
            )
            live_details = video.get("liveStreamingDetails") or {}
            results.append({
                "provider": "youtube",
                "video_id": str(video.get("id") or ""),
                "title": str(snippet.get("title") or ""),
                "description": str(snippet.get("description") or ""),
                "published_at": snippet.get("publishedAt"),
                "duration_seconds": duration,
                "thumbnail_url": str(thumbnail or ""),
                "channel_title": str(snippet.get("channelTitle") or ""),
                "privacy_status": str(status.get("privacyStatus") or ""),
                "upload_status": str(status.get("uploadStatus") or ""),
                "live_broadcast_content": str(snippet.get("liveBroadcastContent") or "none"),
                "actual_start_time": live_details.get("actualStartTime"),
                "actual_end_time": live_details.get("actualEndTime"),
            })
        returned_ids = {str(item.get("video_id") or "") for item in results}
        for missing_id in identifiers:
            if missing_id not in returned_ids:
                results.append({
                    "provider": "youtube", "video_id": missing_id,
                    "title": "Unavailable YouTube upload", "description": "",
                    "published_at": None, "duration_seconds": None,
                    "thumbnail_url": "", "channel_title": "",
                    "privacy_status": "unavailable", "upload_status": "unavailable",
                    "live_broadcast_content": "none",
                    "actual_start_time": None, "actual_end_time": None,
                })
        return results

    async def check_new_uploads(
        self, creator: dict[str, Any], *, max_results: int = 10,
    ) -> list[dict[str, Any]]:
        playlist_id = await self.get_uploads_playlist(creator)
        payload = await self._request(
            "playlistItems",
            {
                "part": "contentDetails,snippet,status",
                "playlistId": playlist_id,
                "maxResults": max(1, min(int(max_results), 50)),
            },
        )
        video_ids = []
        for item in payload.get("items") or []:
            video_id = str((item.get("contentDetails") or {}).get("videoId") or "")
            if video_id and video_id not in video_ids:
                video_ids.append(video_id)
        return await self.get_video_metadata(video_ids)

    async def get_live_status(self, creator: dict[str, Any]) -> dict[str, Any]:
        uploads = await self.check_new_uploads(creator, max_results=1)
        return {
            **creator, "provider": "youtube", "is_live": False,
            "status": "UPLOAD_MONITORING", "latest_upload": uploads[0] if uploads else None,
            "generation_available": bool(creator.get("authorized_media_source_type")),
        }

    async def validate_connection(self) -> dict[str, Any]:
        if not self.enabled:
            return {"provider": "youtube", "valid": False, "code": "disabled"}
        if not self.configured:
            return {"provider": "youtube", "valid": False, "code": "auth_error"}
        return {"provider": "youtube", "valid": True}


async def poll_creators_independently(
    creators: list[dict[str, Any]],
    providers: dict[str, StreamProvider],
) -> list[dict[str, Any]]:
    results = []
    for creator in creators:
        provider_name = str(creator.get("provider") or "twitch").lower()
        provider = providers.get(provider_name)
        if provider is None:
            continue
        try:
            results.append(await provider.get_live_status(creator))
        except Exception as error:
            results.append({
                **creator, "provider": provider_name, "is_live": False,
                "status": "ERROR", "error": str(error),
            })
        await asyncio.sleep(0)
    return results
