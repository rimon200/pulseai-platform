import hashlib
import os
from pathlib import Path
from urllib.parse import quote


def object_storage_enabled() -> bool:
    return os.getenv("OBJECT_STORAGE_ENABLED", "false").strip().lower() == "true"


def upload_video(video_path: str, clip_id: str) -> dict[str, str]:
    path = Path(video_path).resolve()
    if not path.is_file() or path.stat().st_size <= 0:
        raise ValueError("Object storage upload requires a non-empty video file.")
    if not object_storage_enabled():
        return {"object_key": "", "durable_url": ""}

    required = {
        "endpoint_url": os.getenv("OBJECT_STORAGE_ENDPOINT", "").strip(),
        "region_name": os.getenv("OBJECT_STORAGE_REGION", "").strip() or "auto",
        "bucket": os.getenv("OBJECT_STORAGE_BUCKET", "").strip(),
        "aws_access_key_id": os.getenv("OBJECT_STORAGE_ACCESS_KEY_ID", "").strip(),
        "aws_secret_access_key": os.getenv(
            "OBJECT_STORAGE_SECRET_ACCESS_KEY", ""
        ).strip(),
    }
    if not all(required.values()):
        raise RuntimeError("Object storage is enabled but configuration is incomplete.")

    import boto3

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
        client = boto3.client(
            "s3",
            endpoint_url=required["endpoint_url"],
            region_name=required["region_name"],
            aws_access_key_id=required["aws_access_key_id"],
            aws_secret_access_key=required["aws_secret_access_key"],
        )
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
    return {"object_key": object_key, "durable_url": durable_url}


def delete_video_object(object_key: str) -> bool:
    """Delete one owned clip object when a future retention job requests it."""
    safe_key = str(object_key or "").strip()
    if not object_storage_enabled():
        return False
    if (
        not safe_key.startswith("clips/")
        or safe_key.startswith("/")
        or ".." in Path(safe_key).parts
    ):
        print(
            "OBJECT STORAGE DELETE FAILED | "
            f"key={safe_key!r} | reason=unsafe_key"
        )
        return False

    required = {
        "endpoint_url": os.getenv("OBJECT_STORAGE_ENDPOINT", "").strip(),
        "region_name": os.getenv("OBJECT_STORAGE_REGION", "").strip() or "auto",
        "bucket": os.getenv("OBJECT_STORAGE_BUCKET", "").strip(),
        "aws_access_key_id": os.getenv("OBJECT_STORAGE_ACCESS_KEY_ID", "").strip(),
        "aws_secret_access_key": os.getenv(
            "OBJECT_STORAGE_SECRET_ACCESS_KEY", ""
        ).strip(),
    }
    if not all(required.values()):
        print(
            "OBJECT STORAGE DELETE FAILED | "
            f"key={safe_key} | reason=incomplete_configuration"
        )
        return False
    try:
        import boto3

        client = boto3.client(
            "s3",
            endpoint_url=required["endpoint_url"],
            region_name=required["region_name"],
            aws_access_key_id=required["aws_access_key_id"],
            aws_secret_access_key=required["aws_secret_access_key"],
        )
        client.delete_object(Bucket=required["bucket"], Key=safe_key)
        print(f"OBJECT STORAGE DELETE | key={safe_key}")
        return True
    except Exception as error:
        print(
            "OBJECT STORAGE DELETE FAILED | "
            f"key={safe_key} | error={error!r}"
        )
        return False
