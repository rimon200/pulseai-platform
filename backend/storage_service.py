from __future__ import annotations

import hashlib
import os
from pathlib import Path
from urllib.parse import quote


def _safe_object_key(object_key: object) -> str:
    key = str(object_key or "").strip()
    parts = Path(key).parts
    if (
        not key
        or key.startswith(("/", "\\"))
        or "\\" in key
        or not parts
        or parts[0] != "clips"
        or ".." in parts
    ):
        return ""
    return key


def _storage_configuration() -> dict[str, str]:
    return {
        "endpoint_url": os.getenv("OBJECT_STORAGE_ENDPOINT", "").strip(),
        "region_name": os.getenv("OBJECT_STORAGE_REGION", "").strip() or "auto",
        "bucket": os.getenv("OBJECT_STORAGE_BUCKET", "").strip(),
        "aws_access_key_id": os.getenv("OBJECT_STORAGE_ACCESS_KEY_ID", "").strip(),
        "aws_secret_access_key": os.getenv(
            "OBJECT_STORAGE_SECRET_ACCESS_KEY", ""
        ).strip(),
    }


def _storage_client(configuration: dict[str, str]):
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=configuration["endpoint_url"],
        region_name=configuration["region_name"],
        aws_access_key_id=configuration["aws_access_key_id"],
        aws_secret_access_key=configuration["aws_secret_access_key"],
    )


def get_video_preview_url(
    object_key: object,
    clip_id: object,
) -> dict[str, object]:
    """Return a direct public or presigned R2 URL without proxying media."""
    safe_key = _safe_object_key(object_key)
    safe_clip_id = str(clip_id or "").strip() or "unknown"
    if not safe_key:
        print(
            "CLIP PREVIEW UNAVAILABLE | "
            f"clip_id={safe_clip_id} | reason=missing_object_key"
        )
        return {
            "durable_url": "",
            "preview_url": "",
            "preview_available": False,
            "source": "",
            "expires_in_seconds": 0,
        }

    public_base_url = os.getenv("OBJECT_STORAGE_PUBLIC_BASE_URL", "").rstrip("/")
    if public_base_url:
        public_url = f"{public_base_url}/{quote(safe_key, safe='/')}"
        print(
            "CLIP PREVIEW URL GENERATED | "
            f"clip_id={safe_clip_id} | source=public_url | "
            "expires_in_seconds=0"
        )
        return {
            "durable_url": public_url,
            "preview_url": public_url,
            "preview_available": True,
            "source": "public_url",
            "expires_in_seconds": 0,
        }

    configuration = _storage_configuration()
    if not object_storage_enabled() or not all(configuration.values()):
        print(
            "CLIP PREVIEW UNAVAILABLE | "
            f"clip_id={safe_clip_id} | reason=signing_failed"
        )
        return {
            "durable_url": "",
            "preview_url": "",
            "preview_available": False,
            "source": "",
            "expires_in_seconds": 0,
        }

    try:
        expires_in = max(
            60,
            min(
                int(os.getenv(
                    "OBJECT_STORAGE_PREVIEW_URL_EXPIRATION_SECONDS",
                    "3600",
                )),
                86400,
            ),
        )
    except (TypeError, ValueError):
        expires_in = 3600
    try:
        client = _storage_client(configuration)
        client.head_object(
            Bucket=configuration["bucket"],
            Key=safe_key,
        )
        preview_url = client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": configuration["bucket"],
                "Key": safe_key,
                "ResponseContentType": "video/mp4",
            },
            ExpiresIn=expires_in,
        )
    except Exception as error:
        error_response = getattr(error, "response", {})
        status_code = int(
            error_response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
            or 0
        )
        error_code = str(error_response.get("Error", {}).get("Code") or "")
        reason = (
            "object_missing"
            if status_code == 404
            or error_code in {"404", "NoSuchKey", "NotFound"}
            else "signing_failed"
        )
        print(
            "CLIP PREVIEW UNAVAILABLE | "
            f"clip_id={safe_clip_id} | reason={reason}"
        )
        return {
            "durable_url": "",
            "preview_url": "",
            "preview_available": False,
            "source": "",
            "expires_in_seconds": 0,
        }
    print(
        "CLIP PREVIEW URL GENERATED | "
        f"clip_id={safe_clip_id} | source=presigned_r2 | "
        f"expires_in_seconds={expires_in}"
    )
    return {
        "durable_url": "",
        "preview_url": preview_url,
        "preview_available": True,
        "source": "presigned_r2",
        "expires_in_seconds": expires_in,
    }


def object_storage_enabled() -> bool:
    return os.getenv("OBJECT_STORAGE_ENABLED", "false").strip().lower() == "true"


def upload_video(video_path: str, clip_id: str) -> dict[str, object]:
    path = Path(video_path).resolve()
    if not path.is_file() or path.stat().st_size <= 0:
        raise ValueError("Object storage upload requires a non-empty video file.")
    if not object_storage_enabled():
        return {"object_key": "", "durable_url": ""}

    required = _storage_configuration()
    if not all(required.values()):
        raise RuntimeError("Object storage is enabled but configuration is incomplete.")

    hasher = hashlib.sha256()
    with path.open("rb") as video_file:
        for chunk in iter(lambda: video_file.read(1024 * 1024), b""):
            hasher.update(chunk)
    digest = hasher.hexdigest()[:16]
    safe_clip_id = "".join(
        character for character in clip_id if character.isalnum() or character in "_-"
    )
    if not safe_clip_id:
        raise ValueError("Object storage upload requires a safe clip identifier.")
    object_key = f"clips/{safe_clip_id}/{digest}.mp4"
    video_size = path.stat().st_size
    public_base_url = os.getenv("OBJECT_STORAGE_PUBLIC_BASE_URL", "").rstrip("/")
    durable_url = (
        f"{public_base_url}/{quote(object_key)}"
        if public_base_url
        else ""
    )
    try:
        client = _storage_client(required)
        try:
            existing = client.head_object(
                Bucket=required["bucket"],
                Key=object_key,
            )
            existing_size = int(existing.get("ContentLength") or 0)
            if existing_size == video_size:
                print(
                    "MEDIA TRANSFER SKIPPED | "
                    "reason=object_already_exists | "
                    f"clip_id={safe_clip_id} | bytes={video_size}"
                )
                return {
                    "object_key": object_key,
                    "durable_url": durable_url,
                    "transferred_bytes": 0,
                }
            raise RuntimeError(
                "Existing object size does not match the final clip."
            )
        except Exception as error:
            error_response = getattr(error, "response", {})
            status_code = int(
                error_response.get("ResponseMetadata", {}).get(
                    "HTTPStatusCode",
                    0,
                )
                or 0
            )
            error_code = str(
                error_response.get("Error", {}).get("Code") or ""
            )
            if status_code != 404 and error_code not in {
                "404",
                "NoSuchKey",
                "NotFound",
            }:
                raise
        print(
            "OBJECT STORAGE UPLOAD START | "
            f"clip_id={safe_clip_id} | key={object_key}"
        )
        client.upload_file(
            str(path),
            required["bucket"],
            object_key,
            ExtraArgs={"ContentType": "video/mp4"},
        )
        print(
            "MEDIA TRANSFER | "
            "direction=outbound | destination=r2 | "
            "purpose=final_clip_upload | "
            f"clip_id={safe_clip_id} | bytes={video_size} | duplicate=false"
        )
    except Exception as error:
        print(
            "OBJECT STORAGE UPLOAD FAILED | "
            f"clip_id={safe_clip_id} | key={object_key} | error={error!r}"
        )
        raise
    print(f"OBJECT STORAGE UPLOAD COMPLETE | clip_id={safe_clip_id} | key={object_key}")
    return {
        "object_key": object_key,
        "durable_url": durable_url,
        "transferred_bytes": video_size,
    }


def get_video_object_size(object_key: object) -> int | None:
    safe_key = _safe_object_key(object_key)
    configuration = _storage_configuration()
    if (
        not safe_key
        or not object_storage_enabled()
        or not all(configuration.values())
    ):
        return None
    try:
        response = _storage_client(configuration).head_object(
            Bucket=configuration["bucket"], Key=safe_key,
        )
        return int(response.get("ContentLength") or 0)
    except Exception:
        return None


def delete_video_object_with_result(object_key: object) -> dict[str, object]:
    """Delete and confirm one exact owned object, returning audit metadata."""
    safe_key = _safe_object_key(object_key)
    configuration = _storage_configuration()
    if not safe_key:
        return {"deleted": False, "bytes": 0, "error": "unsafe_key"}
    if not object_storage_enabled() or not all(configuration.values()):
        return {
            "deleted": False, "bytes": 0,
            "error": "object_storage_unavailable",
        }
    try:
        client = _storage_client(configuration)
        try:
            existing = client.head_object(
                Bucket=configuration["bucket"], Key=safe_key,
            )
        except Exception as error:
            response = getattr(error, "response", {})
            status_code = int(
                response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
                or 0
            )
            error_code = str(response.get("Error", {}).get("Code") or "")
            if status_code == 404 or error_code in {
                "404", "NoSuchKey", "NotFound",
            }:
                return {
                    "deleted": True, "bytes": 0, "error": "",
                    "already_missing": True,
                }
            raise
        object_bytes = int(existing.get("ContentLength") or 0)
        client.delete_object(Bucket=configuration["bucket"], Key=safe_key)
        try:
            client.head_object(Bucket=configuration["bucket"], Key=safe_key)
        except Exception as error:
            response = getattr(error, "response", {})
            status_code = int(
                response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
                or 0
            )
            error_code = str(response.get("Error", {}).get("Code") or "")
            if status_code == 404 or error_code in {
                "404", "NoSuchKey", "NotFound",
            }:
                return {"deleted": True, "bytes": object_bytes, "error": ""}
            raise
        return {
            "deleted": False, "bytes": 0,
            "error": "deletion_not_confirmed",
        }
    except Exception as error:
        return {
            "deleted": False, "bytes": 0,
            "error": error.__class__.__name__,
        }


def delete_video_object(object_key: str) -> bool:
    """Delete one owned clip object when a future retention job requests it."""
    result = delete_video_object_with_result(object_key)
    safe_key = str(object_key or "").strip()
    if result["deleted"]:
        print(f"OBJECT STORAGE DELETE | key={safe_key}")
        return True
    print(
        "OBJECT STORAGE DELETE FAILED | "
        f"key={safe_key!r} | reason={result.get('error') or 'unknown'}"
    )
    return False
