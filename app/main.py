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


SPOTIFY_EPISODE_RE = re.compile(r"^https://open\.spotify\.com/episode/([a-zA-Z0-9]+)")


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
