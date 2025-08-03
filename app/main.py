import os
import re
import subprocess
import tempfile
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from azure.storage.blob import BlobServiceClient

app = FastAPI()


class ConvertRequest(BaseModel):
    url: str
    title: str = None  # Optional custom title for the files


# Support multiple podcast platforms that allow downloads
SUPPORTED_URL_PATTERNS = [
    (re.compile(r"^https://www\.youtube\.com/watch\?v=([a-zA-Z0-9_-]+)"), "youtube"),
    (re.compile(r"^https://youtu\.be/([a-zA-Z0-9_-]+)"), "youtube"),
    (re.compile(r"^https://music\.youtube\.com/podcast/([a-zA-Z0-9_-]+)"), "youtube_music"),
    (re.compile(r"^https://music\.youtube\.com/watch\?v=([a-zA-Z0-9_-]+)"), "youtube_music"),
    (re.compile(r"^https://open\.spotify\.com/episode/([a-zA-Z0-9]+)"), "spotify"),
]


def _get_url_info(url):
    """Extract URL info and platform type"""
    for pattern, platform in SUPPORTED_URL_PATTERNS:
        match = pattern.match(url)
        if match:
            return match.group(1), platform
    return None, None


def _get_container_client():
    account = os.getenv("AZURE_STORAGE_ACCOUNT")
    key = os.getenv("AZURE_STORAGE_KEY")
    container = os.getenv("AZURE_CONTAINER_NAME")
    if not all([account, key, container]):
        raise HTTPException(status_code=500, detail="Missing Azure Storage configuration")
    service = BlobServiceClient(
        account_url=f"https://{account}.blob.core.windows.net", credential=key
    )
    return service.get_container_client(container)


def _sanitize_filename(title):
    """Convert a title to a safe filename"""
    if not title:
        return None
    # Remove invalid characters and replace spaces with underscores
    sanitized = re.sub(r'[<>:"/\\|?*]', '', title)
    sanitized = re.sub(r'\s+', '_', sanitized.strip())
    # Limit length to avoid filesystem issues
    return sanitized[:100] if sanitized else None


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.post("/convert")
def convert(req: ConvertRequest):
    episode_id, platform = _get_url_info(req.url)
    if not episode_id:
        raise HTTPException(status_code=400, detail="Unsupported URL format. Supported: YouTube, YouTube Music")
    
    # Warn about Spotify limitations
    if platform == "spotify":
        raise HTTPException(
            status_code=400, 
            detail="Spotify episodes cannot be downloaded due to DRM protection. Please use YouTube or other supported platforms."
        )

    # Determine filename base - use custom title if provided, otherwise video ID
    sanitized_title = _sanitize_filename(req.title)
    filename_base = sanitized_title if sanitized_title else episode_id

    try:
        container_client = _get_container_client()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    with tempfile.TemporaryDirectory() as tmpdir:
        # Handle different URL types with appropriate yt-dlp options
        if platform == "youtube_music":
            # For YouTube Music, try to get the latest episode from the podcast
            cmd = ["yt-dlp", req.url, "-o", f"{tmpdir}/%(title)s.%(ext)s", 
                   "--extract-audio", "--audio-format", "mp3", 
                   "--playlist-end", "1", "--yes-playlist",
                   "--write-subs", "--write-auto-subs", "--sub-lang", "en"]
        else:
            # For regular YouTube videos - only download the specific video, not the playlist
            cmd = ["yt-dlp", req.url, "-o", f"{tmpdir}/%(title)s.%(ext)s", 
                   "--extract-audio", "--audio-format", "mp3", "--no-playlist",
                   "--write-subs", "--write-auto-subs", "--sub-lang", "en"]
                   
        try:
            result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr if exc.stderr else ""
            stdout = exc.stdout if exc.stdout else ""
            raise HTTPException(status_code=500, detail=f"Download failed: {stderr}\nStdout: {stdout}")

        # List all files in the directory for debugging
        all_files = list(Path(tmpdir).iterdir())
        mp3_files = list(Path(tmpdir).glob("*.mp3"))
        
        # Look for transcription files (subtitles)
        subtitle_files = list(Path(tmpdir).glob("*.vtt")) + list(Path(tmpdir).glob("*.srt"))
        
        # Also capture what the downloader actually output for debugging
        stdout_output = result.stdout if result.stdout else "No stdout output"
        
        if not mp3_files:
            # If no MP3 files, check for other audio files
            audio_files = list(Path(tmpdir).glob("*.m4a")) + list(Path(tmpdir).glob("*.ogg")) + list(Path(tmpdir).glob("*.wav"))
            if audio_files:
                downloaded = audio_files[0]
            else:
                file_list = [f.name for f in all_files]
                raise HTTPException(status_code=500, detail=f"No audio files found after download. Files in directory: {file_list}. Downloader output: {stdout_output}")
        else:
            downloaded = mp3_files[0]
            
        # Prepare audio file
        final_audio_path = Path(tmpdir) / f"{filename_base}.mp3"
        downloaded.rename(final_audio_path)

        # Upload audio file
        audio_blob_name = f"{filename_base}.mp3"
        try:
            with open(final_audio_path, "rb") as data:
                container_client.upload_blob(name=audio_blob_name, data=data, overwrite=True)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Audio upload failed: {exc}")

        # Handle transcription file if available
        transcript_blob_name = None
        transcript_url = None
        
        if subtitle_files:
            subtitle_file = subtitle_files[0]  # Use the first subtitle file found
            final_transcript_path = Path(tmpdir) / f"{filename_base}.{subtitle_file.suffix}"
            subtitle_file.rename(final_transcript_path)
            
            transcript_blob_name = f"{filename_base}{subtitle_file.suffix}"
            try:
                with open(final_transcript_path, "rb") as data:
                    container_client.upload_blob(name=transcript_blob_name, data=data, overwrite=True)
                
                transcript_url = (
                    f"https://{os.getenv('AZURE_STORAGE_ACCOUNT')}.blob.core.windows.net/"
                    f"{os.getenv('AZURE_CONTAINER_NAME')}/{transcript_blob_name}"
                )
            except Exception as exc:
                # Don't fail the whole request if transcript upload fails
                transcript_url = f"Transcript upload failed: {exc}"

    audio_url = (
        f"https://{os.getenv('AZURE_STORAGE_ACCOUNT')}.blob.core.windows.net/"
        f"{os.getenv('AZURE_CONTAINER_NAME')}/{audio_blob_name}"
    )
    
    response = {"audio_url": audio_url}
    if transcript_url:
        response["transcript_url"] = transcript_url
    
    return response
