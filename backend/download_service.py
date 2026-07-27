from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import re
import subprocess


DOWNLOADS_DIR = Path(__file__).resolve().parent / "downloads"
DOWNLOADS_DIR.mkdir(exist_ok=True)

_YTDLP_VERSION_LOGGED = False


def _log_ytdlp_version_once() -> None:
    global _YTDLP_VERSION_LOGGED

    if _YTDLP_VERSION_LOGGED:
        return

    _YTDLP_VERSION_LOGGED = True
    try:
        installed_version = version("yt-dlp")
    except PackageNotFoundError:
        installed_version = "not installed"

    print(f"YT-DLP VERSION: {installed_version}")


class DownloadService:
    def __init__(self) -> None:
        _log_ytdlp_version_once()

    def download_with_ytdlp(self, clip_url: str, output_name: str) -> str:
        _log_ytdlp_version_once()

        safe_output_name = str(output_name or "").strip()
        if (
            not safe_output_name
            or safe_output_name in {".", ".."}
            or "/" in safe_output_name
            or "\\" in safe_output_name
            or Path(safe_output_name).is_absolute()
            or re.fullmatch(r"[A-Za-z0-9_-]+", safe_output_name) is None
        ):
            print(
                "TWITCH CLIP DOWNLOAD FAILED | "
                f"reason=unsafe_output_name | output_name={safe_output_name!r}"
            )
            return ""

        downloads_root = DOWNLOADS_DIR.resolve()
        output_path = (downloads_root / f"{safe_output_name}.mp4").resolve()
        if output_path.parent != downloads_root:
            print(
                "TWITCH CLIP DOWNLOAD FAILED | "
                f"reason=output_path_outside_downloads | output_name={safe_output_name!r}"
            )
            return ""

        preexisting_artifacts = {
            artifact.resolve(strict=False)
            for artifact in (
                [
                    output_path,
                    Path(f"{output_path}.part"),
                    Path(f"{output_path}.ytdl"),
                ]
                + list(output_path.parent.glob(f"{output_path.name}.part-*"))
                + list(
                    output_path.parent.glob(
                        f"{output_path.stem}.f*{output_path.suffix}"
                    )
                )
            )
            if artifact.exists()
        }
        if output_path.is_file():
            return str(output_path)

        try:
            subprocess.run(
                [
                    "yt-dlp",
                    "-o",
                    str(output_path),
                    clip_url,
                ],
                check=True,
            )
            return str(output_path)

        except (subprocess.CalledProcessError, OSError) as error:
            print(f"TWITCH CLIP DOWNLOAD FAILED for {clip_url}:", repr(error))
            failed_artifacts = [
                output_path,
                Path(f"{output_path}.part"),
                Path(f"{output_path}.ytdl"),
            ]
            failed_artifacts.extend(
                output_path.parent.glob(f"{output_path.name}.part-*")
            )
            failed_artifacts.extend(
                output_path.parent.glob(
                    f"{output_path.stem}.f*{output_path.suffix}"
                )
            )
            for artifact_path in failed_artifacts:
                try:
                    if (
                        artifact_path.resolve(strict=False)
                        not in preexisting_artifacts
                        and artifact_path.is_file()
                    ):
                        artifact_path.unlink()
                        print(
                            "CANDIDATE VIDEO CLEANUP | "
                            f"output_name={safe_output_name} | path={artifact_path}"
                        )
                except OSError as cleanup_error:
                    print(
                        "CANDIDATE VIDEO CLEANUP FAILED | "
                        f"output_name={safe_output_name} | "
                        f"path={artifact_path} | error={cleanup_error!r}"
                    )
            return ""
