import os
import re
import subprocess
import tempfile
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
    match = SPOTIFY_EPISODE_RE.match(req.url)
    if not match:
        raise HTTPException(status_code=400, detail="Invalid Spotify episode URL")
    episode_id = match.group(1)

    try:
        container_client = _get_container_client()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    with tempfile.TemporaryDirectory() as tmpdir:
        cmd = ["spotdl", req.url, "--output", tmpdir, "--format", "mp3"]
        try:
            subprocess.run(cmd, check=True, capture_output=True)
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.decode() if exc.stderr else ""
            raise HTTPException(status_code=500, detail=f"Download failed: {stderr}")

        mp3_files = list(Path(tmpdir).glob("*.mp3"))
        if not mp3_files:
            raise HTTPException(status_code=500, detail="MP3 file not found after download")
        downloaded = mp3_files[0]
        final_path = Path(tmpdir) / f"{episode_id}.mp3"
        downloaded.rename(final_path)

        blob_name = f"{episode_id}.mp3"
        try:
            with open(final_path, "rb") as data:
                container_client.upload_blob(name=blob_name, data=data, overwrite=True)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Upload failed: {exc}")

    blob_url = (
        f"https://{os.getenv('AZURE_STORAGE_ACCOUNT')}.blob.core.windows.net/"
        f"{os.getenv('AZURE_CONTAINER_NAME')}/{blob_name}"
    )
    return {"url": blob_url}
