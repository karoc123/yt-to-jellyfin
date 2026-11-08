# YouTube Cookies Configuration Guide

Certain YouTube videos require authentication to download:
- Age-restricted videos
- Private or unlisted videos in your account
- Member-only content
- Videos served in regions enforcing bot-detection puzzles

`yt-to-jellyfin` natively supports standard Netscape `cookies.txt` files for `yt-dlp`.

---

## 1. Exporting Your Cookies

1. Open your web browser (Chrome, Firefox, Edge, etc.) and ensure you are logged into YouTube.
2. Install a reputable cookie export extension:
   * **[Get cookies.txt LOCALLY](https://github.com/kairi003/Get-cookies.txt-LOCALLY)** (Chrome / Firefox - open source, runs locally without uploading data)
3. Navigate to [youtube.com](https://www.youtube.com).
4. Click the extension icon and export the cookies for `youtube.com` as a Netscape formatted `cookies.txt` file.

---

## 2. Mounting Cookies in Docker Compose

Place the exported `cookies.txt` into your project directory or a secure configuration folder:

```yaml
services:
  yt-to-jellyfin:
    image: yt-to-jellyfin:latest
    container_name: yt-to-jellyfin
    restart: unless-stopped
    ports:
      - "8099:8000"
    environment:
      - DATABASE_PATH=/data/app.db
      - MEDIA_PATH=/media
      - COOKIES_PATH=/cookies/cookies.txt
      - TZ=Europe/Berlin
    volumes:
      - ./data:/data
      - ./media:/media
      - ./cookies.txt:/cookies/cookies.txt:ro
```

Restart the container:
```bash
docker compose up -d
```

---

## 3. Security Recommendations

- Mount `cookies.txt` as read-only (`:ro`).
- Do not commit `cookies.txt` into git (it is included in `.gitignore` by default).
- Periodically refresh `cookies.txt` when YouTube session tokens expire.
