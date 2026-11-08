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

## 2. Managing Cookies in the Web UI

You do not need to edit `docker-compose.yml` or mount extra files to use cookies.

1. Open the Web UI and navigate to **Settings** (`/settings`).
2. Under **YouTube Authentication (Cookies)**:
   * **Option A:** Upload your exported `cookies.txt` file directly.
   * **Option B:** Open your `cookies.txt`, copy its contents, and paste them into the textarea.
3. Click **Save Cookies**.

The application validates the Netscape format and saves the cookies directly to `/data/cookies.txt` with restricted permissions (`0600`). Because `./data:/data` is already mounted as a persistent volume in Docker Compose, the cookies persist across container updates and restarts automatically.

To remove saved cookies, click **Delete saved cookies** on the same settings page.

---

## 3. Security Recommendations

- Cookies are stored with `0600` permissions (read/write only by the container user).
- Do not commit any `cookies.txt` into git (it is included in `.gitignore` by default).
- Periodically refresh cookies when YouTube session tokens expire or downloads fail with authentication errors.
