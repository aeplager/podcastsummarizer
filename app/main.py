import os
import re
import subprocess
import tempfile
import json
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from azure.storage.blob import BlobServiceClient
from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound
from openai import OpenAI

app = FastAPI()


class ConvertRequest(BaseModel):
    url: str


class SummarizeRequest(BaseModel):
    url: str


SPOTIFY_EPISODE_RE = re.compile(r"^https://open\.spotify\.com/episode/([a-zA-Z0-9]+)")
YOUTUBE_VIDEO_RE = re.compile(r"(?:v=|be/)([\w-]{11})")


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


def _extract_video_id(url: str) -> str:
    match = YOUTUBE_VIDEO_RE.search(url)
    if not match:
        raise HTTPException(status_code=400, detail="Invalid YouTube URL")
    return match.group(1)


def _fetch_transcript(video_id: str) -> str:
    try:
        transcript = YouTubeTranscriptApi.get_transcript(video_id)
    except NoTranscriptFound:
        raise HTTPException(status_code=404, detail="Transcript not found")
    return " ".join(t["text"] for t in transcript)


def _summarize_text(text: str) -> dict:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="Missing OpenAI API key")
    client = OpenAI(api_key=api_key)
    prompt = (
        "Summarize the following transcript. Return JSON with keys 'bullet_points' "
        "(list of bullet point strings) and 'companies' (list of objects with 'name' "
        "and 'summary'). Transcript:\n"
    )
    completion = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": prompt + text},
        ],
        temperature=0.3,
    )
    try:
        return json.loads(completion.choices[0].message.content)
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Failed to parse summary response")


@app.get("/", response_class=HTMLResponse)
def index():
    return """
    <html>
      <body>
        <h1>YouTube Summarizer</h1>
        <form id='form'>
          <input type='text' id='url' placeholder='YouTube URL' size='50'/>
          <button type='submit'>Summarize</button>
        </form>
        <div id='result'></div>
        <script>
        const form=document.getElementById('form');
        form.addEventListener('submit', async (e)=>{
          e.preventDefault();
          const url=document.getElementById('url').value;
          const resp=await fetch('/summarize', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({url})});
          const data=await resp.json();
          let html='<h2>Summary</h2><ul>';
          for(const bp of data.bullet_points){html+=`<li>${bp}</li>`;}
          html+='</ul>';
          if(data.companies && data.companies.length){
            html+='<h2>Companies</h2><ul>';
            for(const c of data.companies){html+=`<li><strong>${c.name}</strong>: ${c.summary}</li>`;}
            html+='</ul>';
          }
          document.getElementById('result').innerHTML=html;
        });
        </script>
      </body>
    </html>
    """


@app.post("/summarize")
def summarize(req: SummarizeRequest):
    video_id = _extract_video_id(req.url)
    transcript = _fetch_transcript(video_id)
    result = _summarize_text(transcript)
    return result


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
