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
