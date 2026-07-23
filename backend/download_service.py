from pathlib import Path
import subprocess


DOWNLOADS_DIR = Path(__file__).resolve().parent / "downloads"
DOWNLOADS_DIR.mkdir(exist_ok=True)


class DownloadService:
   def download_with_ytdlp(self, clip_url: str, output_name: str) -> str:
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

    except subprocess.CalledProcessError:
        return ""