# Podcast & Video Service

This service provides a comprehensive solution for downloading podcast episodes as MP3 files, extracting transcriptions, and generating AI-powered summaries. All content is uploaded to Azure Blob Storage with a beautiful web interface.

**Features:**
- 🔍 **Search YouTube** for podcasts and videos by title
- 📥 **Download audio** in MP3 format with custom naming
- 📝 **Extract transcriptions** in VTT format when available
- 🤖 **AI-powered summaries** with bullet points and company mentions
- ☁️ **Azure Blob Storage** integration for file hosting
- 🎨 **Modern web interface** with Bootstrap and real-time status
- 🔗 **REST API** compatible with Flask and other applications

**Supported platforms:**
- YouTube (podcasts and videos)
- YouTube Music (podcasts and music)
- Other platforms supported by yt-dlp

**Note:** Spotify episodes cannot be downloaded due to DRM protection and platform restrictions.

## Building and Running

```powershell
docker rm podcastsummarizer ; docker build -t podcastsummarizer . ; if ($LASTEXITCODE -eq 0) { docker run --env-file .env -p 8080:8080 podcastsummarizer }
```

## API Endpoints

### Search Endpoint
`POST /search`

Search YouTube for podcasts/videos by title before downloading.

**Request Body:**
```json
{
  "query": "search terms for podcast",
  "max_results": 10
}
```

**Response:**
```json
{
  "query": "search terms for podcast",
  "results_count": 5,
  "results": [
    {
      "title": "Podcast Episode Title",
      "url": "https://www.youtube.com/watch?v=...",
      "duration": "1:23:45",
      "channel": "Channel Name",
      "view_count": 12345,
      "upload_date": "20241201",
      "description": "Episode description..."
    }
  ]
}
```

### Summarize Endpoint
`POST /summarize`

Generate AI-powered summaries of YouTube videos with bullet points and company mentions.

**Request Body:**
```json
{
  "url": "https://www.youtube.com/watch?v=..."
}
```

**Response:**
```json
{
  "bullet_points": [
    "Key point 1 from the video",
    "Key point 2 from the video"
  ],
  "companies": [
    {
      "name": "Company Name",
      "summary": "Brief description of company's involvement"
    }
  ]
}
```

### Convert Endpoint
`POST /convert`

**Request Body:**
```json
{
  "url": "https://www.youtube.com/watch?v=...",
  "title": "optional custom filename"
}
```

**Response:**
```json
{
  "audio_url": "https://yourstorageaccount.blob.core.windows.net/container/filename.mp3",
  "transcript_url": "https://yourstorageaccount.blob.core.windows.net/container/filename.vtt"
}
```

### Health Check
`GET /health`

Returns a simple health check response.

## Example Requests

**Search for podcasts:**
```bash
curl -X POST http://localhost:8080/search \
     -H "Content-Type: application/json" \
     -d '{"query": "Latent Space podcast", "max_results": 5}'
```

**Generate AI summary:**
```bash
curl -X POST http://localhost:8080/summarize \
     -H "Content-Type: application/json" \
     -d '{"url": "https://www.youtube.com/watch?v=FLQVlA_DNFU"}'
```

**Basic usage with auto-generated filename:**
```bash
curl -X POST http://localhost:8080/convert \
     -H "Content-Type: application/json" \
     -d '{"url": "https://www.youtube.com/watch?v=FLQVlA_DNFU"}'
```

**With custom filename:**
```bash
curl -X POST http://localhost:8080/convert \
     -H "Content-Type: application/json" \
     -d '{"url": "https://www.youtube.com/watch?v=FhQqBfNCQZo", "title": "Latent Space:   Gemini in 2025 and Real Time Voice"}'
```

**Example response:**
```json
{
  "audio_url": "https://yourstorageaccount.blob.core.windows.net/container/My_Podcast_Episode.mp3",
  "transcript_url": "https://yourstorageaccount.blob.core.windows.net/container/My_Podcast_Episode.vtt"
}
```

**Note:** `transcript_url` will only be included if transcription/subtitles are available for the video.

## Flask Integration

This service is fully compatible with Flask applications. You can call it from any Flask route:

```python
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

@app.route('/search-podcasts', methods=['POST'])
def search_podcasts():
    data = request.get_json()
    
    # Search for podcasts
    response = requests.post('http://localhost:8080/search', json={
        'query': data['query'],
        'max_results': data.get('max_results', 10)
    })
    
    if response.status_code == 200:
        return jsonify(response.json())
    else:
        return jsonify({'error': 'Search failed'}), 500

@app.route('/download-podcast', methods=['POST'])
def download_podcast():
    data = request.get_json()
    
    # Call the podcast service
    response = requests.post('http://localhost:8080/convert', json={
        'url': data['url'],
        'title': data.get('title', None)  # Optional custom title
    })
    
    if response.status_code == 200:
        return jsonify(response.json())
    else:
        return jsonify({'error': 'Download failed'}), 500
```

## Additional Examples

**YouTube Music podcast:**
```bash
curl -X POST http://localhost:8080/convert \
     -H "Content-Type: application/json" \
     -d '{"url": "https://music.youtube.com/podcast/FLQVlA_DNFU", "title": "Music Podcast Episode"}'
```

**Spotify URLs will return an error:**
```bash
curl -X POST http://localhost:8080/convert \
     -H "Content-Type: application/json" \
     -d '{"url": "https://open.spotify.com/episode/1mzj7PRdo6Xr4hCxLqf0JK"}'
# Returns: {"detail":"Spotify episodes cannot be downloaded due to DRM protection..."}
```
