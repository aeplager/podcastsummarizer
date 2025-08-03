# Podcast Downloader Service

This service exposes an HTTP endpoint to download a Spotify podcast episode as an MP3 file and upload it to Azure Blob Storage.

## Building the Docker image

```bash
docker build -t podcast-downloader .
```

## Running the container

```bash
docker run -e AZURE_STORAGE_ACCOUNT=... \
           -e AZURE_STORAGE_KEY=... \
           -e AZURE_CONTAINER_NAME=... \
           -p 8080:8080 \
           podcast-downloader
```

## Example request

```bash
curl -X POST http://localhost:8080/convert \
     -H "Content-Type: application/json" \
     -d '{"url": "https://open.spotify.com/episode/123456"}'
```
