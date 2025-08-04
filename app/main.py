import os
import re
import subprocess
import tempfile
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, Response
from pydantic import BaseModel
from azure.storage.blob import BlobServiceClient
from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound
from openai import OpenAI

app = FastAPI()


class ConvertRequest(BaseModel):
    url: str
    title: str = None  # Optional custom title for the files


class SearchRequest(BaseModel):
    query: str
    max_results: int = 10  # Default to 10 results


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
        # Fetch transcript using YouTubeTranscriptApi
        transcript_list = YouTubeTranscriptApi().fetch(video_id)
    except NoTranscriptFound:
        raise HTTPException(status_code=404, detail="No transcript found for this video")
    except Exception as e:
        # Other errors
        raise HTTPException(status_code=500, detail=f"Transcript error: {str(e)}")
    # Combine transcript text into single string
    # The youtube_transcript_api may return a list of FetchedTranscriptSnippet
    # objects rather than plain dictionaries. These objects expose the text
    # content via the ``text`` attribute. The previous implementation attempted
    # to access the items like dictionaries (``item["text"]``) which raises
    # ``TypeError: 'FetchedTranscriptSnippet' object is not subscriptable``.
    # Access the attribute instead so the transcript is combined correctly.
    return " ".join(item.text for item in transcript_list)


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


def _sanitize_filename(filename):
    """Sanitize filename for safe storage"""
    if not filename:
        return None
    # Remove or replace invalid characters
    import re
    sanitized = re.sub(r'[<>:"/\\|?*]', '_', filename)
    # Limit length
    return sanitized[:100]


@app.get("/")
def read_root():
    return HTMLResponse(content="""
    <!DOCTYPE html>
    <html lang="en">
      <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Podcast & Video Service</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
        <style>
          .feature-card { transition: transform 0.2s; }
          .feature-card:hover { transform: translateY(-5px); }
          .status-indicator { width: 10px; height: 10px; border-radius: 50%; display: inline-block; margin-right: 8px; }
          .status-success { background-color: #28a745; }
          .status-processing { background-color: #ffc107; }
          .status-error { background-color: #dc3545; }
        </style>
      </head>
      <body class="bg-light">
        <div class="container py-5">
          <div class="row justify-content-center">
            <div class="col-lg-10">
              <div class="text-center mb-5">
                <h1 class="display-4 text-primary mb-3">
                  <i class="fas fa-podcast me-3"></i>Podcast & Video Service
                </h1>
                <p class="lead text-muted">
                  Search, download, and summarize YouTube videos and podcasts
                </p>
                <div class="badge bg-success fs-6">
                  <span class="status-indicator status-success"></span>Service Online
                </div>
              </div>

              <!-- Service Cards -->
              <div class="row g-4 mb-5">
                <div class="col-md-4">
                  <div class="card feature-card h-100 border-0 shadow-sm">
                    <div class="card-body text-center">
                      <i class="fas fa-search fa-3x text-info mb-3"></i>
                      <h5 class="card-title">Search</h5>
                      <p class="card-text text-muted">Find podcasts and videos by title</p>
                    </div>
                  </div>
                </div>
                <div class="col-md-4">
                  <div class="card feature-card h-100 border-0 shadow-sm">
                    <div class="card-body text-center">
                      <i class="fas fa-download fa-3x text-success mb-3"></i>
                      <h5 class="card-title">Download</h5>
                      <p class="card-text text-muted">Get MP3 audio and VTT transcripts</p>
                    </div>
                  </div>
                </div>
                <div class="col-md-4">
                  <div class="card feature-card h-100 border-0 shadow-sm">
                    <div class="card-body text-center">
                      <i class="fas fa-file-alt fa-3x text-warning mb-3"></i>
                      <h5 class="card-title">Summarize</h5>
                      <p class="card-text text-muted">AI-powered content summaries</p>
                    </div>
                  </div>
                </div>
              </div>

              <!-- Main Interface -->
              <div class="card border-0 shadow">
                <div class="card-header bg-white">
                  <ul class="nav nav-tabs card-header-tabs" id="serviceTabs" role="tablist">
                    <li class="nav-item" role="presentation">
                      <button class="nav-link active" id="search-tab" data-bs-toggle="tab" data-bs-target="#search" type="button">
                        <i class="fas fa-search me-2"></i>Search
                      </button>
                    </li>
                    <li class="nav-item" role="presentation">
                      <button class="nav-link" id="download-tab" data-bs-toggle="tab" data-bs-target="#download" type="button">
                        <i class="fas fa-download me-2"></i>Download
                      </button>
                    </li>
                    <li class="nav-item" role="presentation">
                      <button class="nav-link" id="summarize-tab" data-bs-toggle="tab" data-bs-target="#summarize" type="button">
                        <i class="fas fa-file-alt me-2"></i>Summarize
                      </button>
                    </li>
                  </ul>
                </div>
                <div class="card-body">
                  <div class="tab-content" id="serviceTabsContent">
                    
                    <!-- Search Tab -->
                    <div class="tab-pane fade show active" id="search" role="tabpanel">
                      <form id="searchForm">
                        <div class="row g-3">
                          <div class="col-md-8">
                            <label for="searchQuery" class="form-label">Search YouTube</label>
                            <input type="text" class="form-control" id="searchQuery" placeholder="Enter podcast or video title...">
                          </div>
                          <div class="col-md-2">
                            <label for="maxResults" class="form-label">Results</label>
                            <select class="form-select" id="maxResults">
                              <option value="5">5</option>
                              <option value="10" selected>10</option>
                              <option value="15">15</option>
                            </select>
                          </div>
                          <div class="col-md-2 d-flex align-items-end">
                            <button type="submit" class="btn btn-primary w-100">
                              <i class="fas fa-search me-2"></i>Search
                            </button>
                          </div>
                        </div>
                      </form>
                      <div id="searchResults" class="mt-4"></div>
                    </div>

                    <!-- Download Tab -->
                    <div class="tab-pane fade" id="download" role="tabpanel">
                      <form id="downloadForm">
                        <div class="row g-3">
                          <div class="col-md-8">
                            <label for="downloadUrl" class="form-label">YouTube URL</label>
                            <input type="url" class="form-control" id="downloadUrl" placeholder="https://www.youtube.com/watch?v=...">
                          </div>
                          <div class="col-md-4">
                            <label for="customTitle" class="form-label">Custom Filename (optional)</label>
                            <input type="text" class="form-control" id="customTitle" placeholder="My Podcast Episode">
                          </div>
                          <div class="col-12">
                            <button type="submit" class="btn btn-success">
                              <i class="fas fa-download me-2"></i>Download Audio & Transcript
                            </button>
                          </div>
                        </div>
                      </form>
                      <div id="downloadResults" class="mt-4"></div>
                    </div>

                    <!-- Summarize Tab -->
                    <div class="tab-pane fade" id="summarize" role="tabpanel">
                      <form id="summarizeForm">
                        <div class="row g-3">
                          <div class="col-md-10">
                            <label for="summarizeUrl" class="form-label">YouTube URL</label>
                            <input type="url" class="form-control" id="summarizeUrl" placeholder="https://www.youtube.com/watch?v=...">
                          </div>
                          <div class="col-md-2 d-flex align-items-end">
                            <button type="submit" class="btn btn-warning w-100">
                              <i class="fas fa-file-alt me-2"></i>Summarize
                            </button>
                          </div>
                        </div>
                      </form>
                      <div id="summarizeResults" class="mt-4"></div>
                    </div>

                  </div>
                </div>
              </div>

              <!-- Status Section -->
              <div class="mt-4 text-center">
                <small class="text-muted">
                  <i class="fas fa-info-circle me-1"></i>
                  Supports YouTube, YouTube Music | Azure Blob Storage Integration
                </small>
              </div>
            </div>
          </div>
        </div>

        <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
        <script>
        // Helper functions
        function showLoading(elementId, message = 'Processing...') {
          document.getElementById(elementId).innerHTML = `
            <div class="text-center p-4">
              <div class="spinner-border text-primary" role="status">
                <span class="visually-hidden">Loading...</span>
              </div>
              <div class="mt-2">${message}</div>
            </div>`;
        }

        function showError(elementId, error) {
          document.getElementById(elementId).innerHTML = `
            <div class="alert alert-danger" role="alert">
              <i class="fas fa-exclamation-triangle me-2"></i>
              <strong>Error:</strong> ${error}
            </div>`;
        }

        // Search functionality
        document.getElementById('searchForm').addEventListener('submit', async (e) => {
          e.preventDefault();
          const query = document.getElementById('searchQuery').value;
          const maxResults = document.getElementById('maxResults').value;
          
          showLoading('searchResults', 'Searching YouTube...');
          
          try {
            const response = await fetch('/search', {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({query, max_results: parseInt(maxResults)})
            });
            
            const data = await response.json();
            
            if (response.ok) {
              let html = `<div class="alert alert-info"><i class="fas fa-search me-2"></i>Found ${data.results_count} results for "${data.query}"</div>`;
              
              if (data.results && data.results.length > 0) {
                html += '<div class="row g-3">';
                data.results.forEach(result => {
                  html += `
                    <div class="col-12">
                      <div class="card">
                        <div class="card-body">
                          <h6 class="card-title">${result.title}</h6>
                          <p class="card-text text-muted small">
                            <i class="fas fa-user me-1"></i>${result.channel} • 
                            <i class="fas fa-clock me-1"></i>${result.duration} • 
                            <i class="fas fa-eye me-1"></i>${result.view_count.toLocaleString()} views
                          </p>
                          <p class="card-text small">${result.description}</p>
                          <div class="btn-group btn-group-sm">
                            <button class="btn btn-outline-success" onclick="useForDownload('${result.url}', '${result.title.replace(/'/g, "\\'")}')">
                              <i class="fas fa-download me-1"></i>Download
                            </button>
                            <button class="btn btn-outline-warning" onclick="useForSummary('${result.url}')">
                              <i class="fas fa-file-alt me-1"></i>Summarize
                            </button>
                          </div>
                        </div>
                      </div>
                    </div>`;
                });
                html += '</div>';
              }
              
              document.getElementById('searchResults').innerHTML = html;
            } else {
              showError('searchResults', data.detail || 'Search failed');
            }
          } catch (error) {
            showError('searchResults', 'Network error: ' + error.message);
          }
        });

        // Download functionality
        document.getElementById('downloadForm').addEventListener('submit', async (e) => {
          e.preventDefault();
          const url = document.getElementById('downloadUrl').value;
          const title = document.getElementById('customTitle').value;
          
          showLoading('downloadResults', 'Downloading and processing...');
          
          try {
            const response = await fetch('/convert', {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({url, title: title || null})
            });
            
            const data = await response.json();
            
            if (response.ok) {
              let html = `
                <div class="alert alert-success">
                  <i class="fas fa-check-circle me-2"></i>
                  <strong>Download Complete!</strong>
                </div>
                <div class="row g-3">
                  <div class="col-md-6">
                    <div class="card">
                      <div class="card-body">
                        <h6 class="card-title"><i class="fas fa-music me-2"></i>Audio File</h6>
                        <a href="${data.audio_url}" class="btn btn-primary btn-sm" target="_blank">
                          <i class="fas fa-download me-1"></i>Download MP3
                        </a>
                      </div>
                    </div>
                  </div>`;
              
              if (data.transcript_url) {
                html += `
                  <div class="col-md-6">
                    <div class="card">
                      <div class="card-body">
                        <h6 class="card-title"><i class="fas fa-file-text me-2"></i>Transcript</h6>
                        <a href="${data.transcript_url}" class="btn btn-info btn-sm" target="_blank">
                          <i class="fas fa-download me-1"></i>Download VTT
                        </a>
                      </div>
                    </div>
                  </div>`;
              }
              html += '</div>';
              
              document.getElementById('downloadResults').innerHTML = html;
            } else {
              showError('downloadResults', data.detail || 'Download failed');
            }
          } catch (error) {
            showError('downloadResults', 'Network error: ' + error.message);
          }
        });

        // Summarize functionality
        document.getElementById('summarizeForm').addEventListener('submit', async (e) => {
          e.preventDefault();
          const url = document.getElementById('summarizeUrl').value;
          
          showLoading('summarizeResults', 'Generating summary...');
          
          try {
            const response = await fetch('/summarize', {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({url})
            });
            
            const data = await response.json();
            
            if (response.ok) {
              let html = '<div class="alert alert-success"><i class="fas fa-check-circle me-2"></i><strong>Summary Complete!</strong></div>';
              
              if (data.bullet_points && data.bullet_points.length > 0) {
                html += '<div class="card mb-3"><div class="card-header"><h6 class="mb-0"><i class="fas fa-list me-2"></i>Key Points</h6></div><div class="card-body"><ul class="mb-0">';
                data.bullet_points.forEach(point => {
                  html += `<li>${point}</li>`;
                });
                html += '</ul></div></div>';
              }
              
              if (data.companies && data.companies.length > 0) {
                html += '<div class="card"><div class="card-header"><h6 class="mb-0"><i class="fas fa-building me-2"></i>Companies Mentioned</h6></div><div class="card-body"><ul class="mb-0">';
                data.companies.forEach(company => {
                  html += `<li><strong>${company.name}</strong>: ${company.summary}</li>`;
                });
                html += '</ul></div></div>';
              }
              
              document.getElementById('summarizeResults').innerHTML = html;
            } else {
              showError('summarizeResults', data.detail || 'Summary failed');
            }
          } catch (error) {
            showError('summarizeResults', 'Network error: ' + error.message);
          }
        });

        // Helper functions for search results
        function useForDownload(url, title) {
          document.getElementById('downloadUrl').value = url;
          document.getElementById('customTitle').value = title;
          new bootstrap.Tab(document.getElementById('download-tab')).show();
        }

        function useForSummary(url) {
          document.getElementById('summarizeUrl').value = url;
          new bootstrap.Tab(document.getElementById('summarize-tab')).show();
        }
        </script>
      </body>
    </html>
    """)


@app.get("/health")
def health():
    return {"status": "healthy", "service": "Podcast & Video Service"}


@app.get("/download/{filename}")
def download_file(filename: str):
    """Download files directly from Azure storage and serve to user"""
    try:
        container_client = _get_container_client()
        
        # Download the blob to memory
        blob_client = container_client.get_blob_client(filename)
        blob_data = blob_client.download_blob().readall()
        
        # Determine content type based on file extension
        if filename.endswith('.mp3'):
            media_type = 'audio/mpeg'
            disposition = f'attachment; filename="{filename}"'
        elif filename.endswith('.vtt'):
            media_type = 'text/vtt'
            disposition = f'attachment; filename="{filename}"'
        elif filename.endswith('.srt'):
            media_type = 'text/plain'
            disposition = f'attachment; filename="{filename}"'
        else:
            media_type = 'application/octet-stream'
            disposition = f'attachment; filename="{filename}"'
        
        return Response(
            content=blob_data,
            media_type=media_type,
            headers={"Content-Disposition": disposition}
        )
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"File not found: {str(exc)}")


@app.post("/search")
def search_podcasts(req: SearchRequest):
    """Search YouTube for podcasts/videos by title"""
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            # Use yt-dlp to search YouTube and extract metadata without downloading
            cmd = [
                "yt-dlp", 
                f"ytsearch{req.max_results}:{req.query}",
                "--dump-json",
                "--no-download"
            ]
            
            result = subprocess.run(cmd, check=True, capture_output=True, text=True)
            
            # Parse the JSON output - each line is a separate JSON object
            search_results = []
            for line in result.stdout.strip().split('\n'):
                if line:
                    try:
                        video_info = json.loads(line)
                        search_results.append({
                            "title": video_info.get("title", "Unknown Title"),
                            "url": video_info.get("webpage_url", ""),
                            "duration": video_info.get("duration_string", "Unknown"),
                            "channel": video_info.get("uploader", "Unknown Channel"),
                            "view_count": video_info.get("view_count", 0),
                            "upload_date": video_info.get("upload_date", "Unknown"),
                            "description": video_info.get("description", "")[:200] + "..." if video_info.get("description", "") else ""
                        })
                    except json.JSONDecodeError:
                        continue
            
            return {
                "query": req.query,
                "results_count": len(search_results),
                "results": search_results
            }
            
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr if exc.stderr else ""
        raise HTTPException(status_code=500, detail=f"Search failed: {stderr}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Search error: {str(exc)}")


@app.post("/summarize") 
def summarize(req: SummarizeRequest):
    video_id = _extract_video_id(req.url)
    transcript = _fetch_transcript(video_id)
    result = _summarize_text(transcript)
    return result


@app.post("/convert")
def convert(req: ConvertRequest):
    try:
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
            raise HTTPException(status_code=500, detail=f"Azure Storage connection failed: {str(exc)}")

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
                    
                    transcript_url = f"/download/{transcript_blob_name}"
                except Exception as exc:
                    # Don't fail the whole request if transcript upload fails
                    transcript_url = None

        audio_url = f"/download/{audio_blob_name}"
        
        response = {"audio_url": audio_url}
        if transcript_url:
            response["transcript_url"] = transcript_url
        
        return response

    except Exception as exc:
        # Catch any unexpected errors
        raise HTTPException(status_code=500, detail=f"Unexpected error during conversion: {str(exc)}")
