from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
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

        output_path = DOWNLOADS_DIR / f"{output_name}.mp4"

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

        except subprocess.CalledProcessError as error:
            print(f"TWITCH CLIP DOWNLOAD FAILED for {clip_url}:", repr(error))
            return ""