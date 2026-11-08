# Jellyfin TV Show Library Setup Guide

This guide walks you through configuring **Jellyfin** to natively recognize and display content downloaded by **yt-to-jellyfin** with posters, channel descriptions, episode titles, air dates, and thumbnails — without installing any extra Jellyfin plugins.

---

## 1. How yt-to-jellyfin Organizes Media

`yt-to-jellyfin` follows Jellyfin's recommended TV Show folder naming standard:

```text
/media/
├── default/                         <-- Library folder (or 'kids', 'dokus', 'tech')
│   └── Kurzgesagt [UCsXVk37bltUxDu47Pq8wUDA]/
│       ├── tvshow.nfo               <-- Channel metadata (title, plot, YouTube uniqueid, lockdata)
│       ├── poster.jpg               <-- Channel avatar / poster
│       └── Season 2024/             <-- Yearly season folder
│           ├── 2024-03-12 - Can We Fix Climate Change [dQw4w9WgXcQ].mp4
│           ├── 2024-03-12 - Can We Fix Climate Change [dQw4w9WgXcQ].nfo
│           └── 2024-03-12 - Can We Fix Climate Change [dQw4w9WgXcQ]-thumb.jpg
```

Every generated NFO file includes `<lockdata>true</lockdata>`, which prevents Jellyfin from accidentally overwriting YouTube metadata with false matches from online television databases like TheTVDB or AniList.

---

## 2. Step-by-Step Jellyfin Configuration

### Step 1: Add a New Media Library
1. Log into your Jellyfin web interface with an administrator account.
2. Go to **Dashboard** -> **Libraries** -> **Add Media Library**.

### Step 2: Set Content Type and Display Name
1. **Content Type**: Choose **Shows** (or "Serien / TV Shows").
   * *Important:* Do **not** select "Music Videos" or "Home Videos", as these types do not support season/episode hierarchy and Kodi XML NFO files as reliably as TV Shows.
2. **Display Name**: e.g., `YouTube`, `YouTube Dokus`, or `Kinder YouTube`.

### Step 3: Add Media Folder Path
1. Click the **+** button under **Folders**.
2. Navigate to and select the mount path where `yt-to-jellyfin` saves media (e.g., `/media/default` or `/media/kids`).

### Step 4: Configure Metadata Readers (Crucial!)
1. In the **Metadata Readers** section:
   * **Check (Enable)**: **Nfo**
   * Move **Nfo** to the top of the reader priority list using the up arrow.
2. In the **Metadata Downloaders** section (TheTVDB, TheMovieDb, etc.):
   * **Uncheck all online downloaders**. Since YouTube channels and videos are not traditional broadcast television shows, online scrapers will either find no match or mismatch.
   * `yt-to-jellyfin` provides all title, plot, release date, and studio tags natively via local NFO files.
3. In the **Image Fetchers** section:
   * **Uncheck online image downloaders**. `yt-to-jellyfin` automatically generates `<video_name>-thumb.jpg` and channel `poster.jpg`.

### Step 5: Save & Initial Library Scan
1. Click **Ok** to save the library.
2. Jellyfin will immediately start a local library scan. Within moments, your YouTube channels will appear as series with full season breakdowns, episode titles, thumbnails, and descriptions.

---

## 3. Multi-Library Setup & Parental Controls

`yt-to-jellyfin` has built-in multi-library support (`all`, `default`, `kids`, `dokus`, `tech` or any custom name):

1. **Kids / Family Profiles:**
   * In `yt-to-jellyfin`, assign channels or quick downloads to the `kids` library.
   * In Jellyfin, create a separate library pointing specifically to `/media/kids`.
   * Under Jellyfin **Dashboard** -> **Users** -> edit your children's profiles to only grant access to the `kids` library.
2. **Documentaries & Learning:**
   * Direct science/documentary channels to `dokus`.
   * Mount `/media/dokus` into a "Dokumentationen" library in Jellyfin.
