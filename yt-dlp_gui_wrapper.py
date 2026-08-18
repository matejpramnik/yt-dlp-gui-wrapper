import os
import re
import sys
import queue
import threading
from enum import Enum, auto
from io import BytesIO
from pathlib import Path

import tkinter as tk
from tkinter import scrolledtext, messagebox

import requests
import yt_dlp
from PIL import Image, ImageOps
from mutagen.flac import FLAC, Picture
from mutagen.mp4 import MP4, MP4Cover


class DownloadType(Enum):
    MP4 = auto()
    AUDIO = auto()
    AUDIO_MP3 = auto()


class QueueWriter:
    # file-like

    def __init__(self, q: queue.Queue):
        self.q = q

    def write(self, text: str):
        if text:
            self.q.put(text.replace("\r", "\n"))

    def flush(self):
        pass



######################################################################################

def sanitize_filename(name: str) -> str:
    illegal = r'\/:*?"<>|'
    for ch in illegal:
        name = name.replace(ch, "_")
    return name.strip()


NOISE_WORDS = re.compile(
    r"(official|music|video|audio|visualizer|lyric|lyrics|"
    r"remaster(?:ed)?|upgrade(?:ed)?|"
    r"hd|hq|\d{3,4}p|karaoke|extended|4k|[4k]|"
    r"radio\s*edit|explicit|clean)",
    re.I,
)


def clean_youtube_title(title):
    title = re.sub(
        r"[\(\[]([^\]\)]*)[\)\]]",
        lambda m: "" if NOISE_WORDS.search(m.group(1)) else m.group(0),
        title,
    )

    title = re.sub(r"\s+", " ", title).strip(" -–—|")

    sep = re.split(r"\s+[-–—]\s+", title, maxsplit=1)
    if len(sep) == 2:
        return sep[0].strip(), sep[1].strip()

    return None, title



######################################################################################
## Metadata API
#

AUDIO_DB_BASE = "https://www.theaudiodb.com/api/v1/json/123"


def lookup_audiodb(artist_hint: str, title_hint: str) -> dict:
    """
    Look up a song using TheAudioDB.
    """

    result = {
        "title": "",
        "artist": "",
        "album": "",
        "year": "",
        "track": "",
        "genre": "",
        "cover_url": "",
    }

    if not artist_hint or not title_hint:
        return result

    try:
        print(f"TheAudioDB lookup: {artist_hint} - {title_hint}")

        resp = requests.get(
            f"{AUDIO_DB_BASE}/searchtrack.php",
            params={
                "s": artist_hint,
                "t": title_hint,
            },
            timeout=10,
        )
        resp.raise_for_status()

        data = resp.json()
        tracks = data.get("track")

        if not tracks:
            return result

        track = tracks[0]

        result["title"] = track.get("strTrack") or ""
        result["artist"] = track.get("strArtist") or ""
        result["album"] = track.get("strAlbum") or ""

        # Extract year from release date
        released = track.get("intYearReleased") or track.get("strReleaseDate") or ""
        if released:
            result["year"] = str(released)[:4]

        result["track"] = (
            track.get("intTrackNumber")
            or track.get("intTrack")
            or ""
        )

        result["genre"] = track.get("strGenre") or ""

        # Prefer album artwork over track artwork
        result["cover_url"] = (
            track.get("strAlbumThumb")
            or track.get("strTrackThumb")
            or ""
        )

    except Exception as exc:
        print(f"TheAudioDB lookup failed: {exc}")

    return result

######################################################################################
## cover
#

def fetch_url_bytes(url: str) -> bytes | None:
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        return resp.content
    except Exception as exc:
        print(f"Could not download image: {exc}")
        return None


def mime_from_bytes(data: bytes) -> str:
    if data[:4] == b"\x89PNG":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def make_square_image(image_bytes: bytes) -> bytes:
    img = Image.open(BytesIO(image_bytes)).convert("RGB")

    size = min(img.size)
    img = ImageOps.fit(
        img,
        (size, size),
        method=Image.Resampling.LANCZOS
    )

    output = BytesIO()
    img.save(output, format="JPEG", quality=95)
    return output.getvalue()


######################################################################################
## Metadata embedding
#

def embed_metadata_flac(flac_path: str, meta: dict, cover_bytes: bytes | None) -> None:
    audio = FLAC(flac_path)
    audio.clear()
    audio.clear_pictures()

    tag_map = {
        "TITLE": meta.get("title", ""),
        "ARTIST": meta.get("artist", ""),
        "ALBUMARTIST": meta.get("album_artist") or meta.get("artist", ""),
        "ALBUM": meta.get("album", ""),
        "DATE": str(meta.get("year", "")),
        "TRACKNUMBER": str(meta.get("track", "")),
        "GENRE": meta.get("genre", ""),
        "COMMENT": meta.get("comment", ""),
    }
    for tag, value in tag_map.items():
        if value and value.strip():
            audio[tag] = value

    if cover_bytes:
        pic = Picture()
        pic.type = 3  # Front Cover
        pic.mime = mime_from_bytes(cover_bytes)
        pic.desc = "Cover"
        pic.data = cover_bytes
        audio.add_picture(pic)
        print("Album cover embedded")

    audio.save()
    print("Vorbis Comment (FLAC) tags saved")


def embed_metadata_m4a(m4a_path: str, meta: dict, cover_bytes: bytes | None) -> None:
    """
    m4a iba ak sa neda flac
    """

    audio = MP4(m4a_path)

    tag_map = {
        "\xa9nam": meta.get("title", ""),
        "\xa9ART": meta.get("artist", ""),
        "aART":    meta.get("album_artist") or meta.get("artist", ""),
        "\xa9alb": meta.get("album", ""),
        "\xa9day": str(meta.get("year", "")),
        "\xa9gen": meta.get("genre", ""),
        "\xa9cmt": meta.get("comment", ""),
    }
    for tag, value in tag_map.items():
        if value and str(value).strip():
            audio[tag] = [str(value)]

    track = meta.get("track", "")
    if track:
        try:
            audio["trkn"] = [(int(track), 0)]
        except (ValueError, TypeError):
            pass

    if cover_bytes:
        fmt = MP4Cover.FORMAT_PNG if mime_from_bytes(cover_bytes) == "image/png" else MP4Cover.FORMAT_JPEG
        audio["covr"] = [MP4Cover(cover_bytes, imageformat=fmt)]
        print("Album cover embedded")

    audio.save()
    print("MP4 (M4A) tags saved")


def gather_metadata(url: str, overrides: dict) -> tuple[dict, dict, str]:

    print(f"\nFetching YouTube info for: {url}")
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
        info = ydl.extract_info(url, download=False)

    yt_title = info.get("title", "Unknown Title")
    yt_artist = info.get("artist") or info.get("uploader", "")
    yt_album = info.get("album", "")
    yt_year = info.get("release_year") or (info.get("upload_date") or "")[:4]
    yt_genre = info.get("genre", "")
    yt_thumbnail = info.get("thumbnail", "")
    yt_desc = (info.get("description") or "")[:200]

    parsed_artist, parsed_title = clean_youtube_title(yt_title)

    search_artist = overrides.get("artist") or parsed_artist or yt_artist
    search_title = overrides.get("title") or parsed_title or yt_title

    mb = {}
    if not all(overrides.get(k) for k in ("title", "artist", "album")):
        print("\nLooking up clean metadata on AudioDB ...")
        mb = lookup_audiodb(search_artist, search_title)
        if mb.get("title"):
            print(f"Found: \"{mb['title']}\" by {mb['artist']}"
                  + (f" -- {mb['album']}" if mb.get("album") else ""))
        else:
            print("No AudioDB match -- falling back to YouTube metadata")

    meta = {
        "title": overrides.get("title") or mb.get("title") or parsed_title or yt_title,
        "artist": overrides.get("artist") or mb.get("artist") or parsed_artist or yt_artist,
        "album": overrides.get("album") or mb.get("album") or yt_album,
        "album_artist": overrides.get("artist") or mb.get("artist") or parsed_artist or yt_artist,
        "year": overrides.get("year") or mb.get("year") or yt_year,
        "track": mb.get("track", ""),
        "genre": overrides.get("genre") or mb.get("genre") or yt_genre,
        "comment": yt_desc,
    }

    print("\nFinal metadata:")
    for k, v in meta.items():
        if v and k != "comment":
            print(f"      {k:<14}: {v}")

    return meta, mb, yt_thumbnail


def fetch_cover(overrides: dict, adb: dict, yt_thumbnail: str) -> bytes | None:
    cover_bytes = None

    if overrides.get("cover_url"):
        print("\nUsing provided cover URL ...")
        cover_bytes = fetch_url_bytes(overrides["cover_url"])

    elif adb.get("cover_url"):
        print("\nFetching artwork from TheAudioDB ...")
        cover_bytes = fetch_url_bytes(adb["cover_url"])

    elif yt_thumbnail:
        print("\nFalling back to YouTube thumbnail ...")
        cover_bytes = fetch_url_bytes(yt_thumbnail)

    if cover_bytes:
        cover_bytes = make_square_image(cover_bytes)

    return cover_bytes

######################################################################################

def download_video(url: str, output_dir: str) -> str:
    """
    Downloads as an MP4
    """

    output_dir = os.path.expanduser(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    print("\nDownloading video ...")
    dl_opts = {
        "format": "bestvideo[height<=1080][ext=mp4]+(bestaudio[ext=m4a]/bestaudio)/best[height<=1080][ext=mp4]/best",
        "merge_output_format": "mp4",
        "outtmpl": os.path.join(output_dir, "%(title)s.%(ext)s"),
        "quiet": False,
        "no_warnings": False,
        "noprogress": False,
        "js_runtimes": {"deno": {}},
    }

    try:
        with yt_dlp.YoutubeDL(dl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            file_path = str(Path(ydl.prepare_filename(info)).with_suffix(".mp4"))
    except Exception as exc:
        print(f"\nyt-dlp error: {exc}")
        raise

    if not os.path.exists(file_path):
        candidates = list(Path(output_dir).glob("*.mp4"))
        if candidates:
            file_path = str(max(candidates, key=lambda p: p.stat().st_mtime))
        else:
            raise FileNotFoundError(f"MP4 not found after download. Looked for: {file_path}")

    print(f"\nVideo saved: {file_path}")
    return file_path


def download_audio(url: str, output_dir: str, overrides: dict) -> str:
    """
    Downloads as FLAC (or M4A)
    """

    output_dir = os.path.expanduser(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    overrides = overrides or {}

    meta, mb, yt_thumbnail = gather_metadata(url, overrides)
    safe_name = sanitize_filename(meta["title"]) or "audio"

    # FLAC
    flac_tpl = os.path.join(output_dir, f"{safe_name}.%(ext)s")
    flac_path = os.path.join(output_dir, f"{safe_name}.flac")

    print("\nAttempting FLAC download ...")
    flac_opts = {
        "format": "bestaudio/best",
        "outtmpl": flac_tpl,
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "flac"}],
        "quiet": False,
        "no_warnings": True,
    }

    flac_ok = False
    try:
        with yt_dlp.YoutubeDL(flac_opts) as ydl:
            ydl.download([url])
        if not os.path.exists(flac_path):
            candidates = list(Path(output_dir).glob(f"{safe_name}*.flac"))
            if candidates:
                flac_path = str(candidates[0])
        flac_ok = os.path.exists(flac_path)
    except Exception as exc:
        print(f"\nFLAC download failed ({exc}); will fall back to M4A")
        flac_ok = False

    if flac_ok:
        print(f"\nAudio saved: {flac_path}")
        cover_bytes = fetch_cover(overrides, mb, yt_thumbnail)
        print("\nEmbedding metadata ...")
        embed_metadata_flac(flac_path, meta, cover_bytes)
        return flac_path

    # M4A (if FLAC fails)
    print("\nFalling back to M4A ...")
    m4a_tpl = os.path.join(output_dir, f"{safe_name}.%(ext)s")
    m4a_path = os.path.join(output_dir, f"{safe_name}.m4a")

    m4a_opts = {
        "format": "bestaudio[ext=m4a]/bestaudio",
        "outtmpl": m4a_tpl,
        "quiet": False,
        "no_warnings": True,
    }

    with yt_dlp.YoutubeDL(m4a_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        m4a_path = str(Path(ydl.prepare_filename(info)).with_suffix(".m4a"))

    if not os.path.exists(m4a_path):
        candidates = list(Path(output_dir).glob(f"{safe_name}*.m4a"))
        if candidates:
            m4a_path = str(candidates[0])
        else:
            raise FileNotFoundError(f"M4A not found after download. Looked for: {m4a_path}")

    print(f"\nAudio saved: {m4a_path}")
    cover_bytes = fetch_cover(overrides, mb, yt_thumbnail)
    print("\nEmbedding metadata ...")
    embed_metadata_m4a(m4a_path, meta, cover_bytes)
    return m4a_path

def download_audio_mp3(url: str, output_dir: str) -> str:
    """
    Downloads as an MP3
    """

    output_dir = os.path.expanduser(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    print("\nAttempting MP3 download ...")
    mp3_opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(output_dir, "%(title)s.%(ext)s"),
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}],
        "quiet": False,
        "no_warnings": True,
        "js_runtimes": {"deno": {}}
    }

    try:
        with yt_dlp.YoutubeDL(mp3_opts) as ydl:
            ydl.download([url])
    except Exception as exc:
        print(f"\nMP3 download failed ({exc}).")

    print(f"\nAudio saved")
    return


######################################################################################
# GUI

class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Download YouTube video / audio")
        root.minsize(700, 560)
        root.maxsize(700, 560)

        self.log_queue: queue.Queue = queue.Queue()
        self.download_type = tk.StringVar(value="AUDIO")

        row = 0
        tk.Label(root, text="URL").grid(row=row, column=0, pady=5, sticky="w")
        self.url_entry = tk.Entry(root, width=80)
        self.url_entry.grid(row=row, column=1, pady=5, sticky="w", columnspan=2)
        row += 1

        # Type selector
        type_frame = tk.Frame(root)
        type_frame.grid(row=row, column=0, columnspan=3, pady=(0, 5))
        tk.Radiobutton(
            type_frame, text="Video (MP4)", variable=self.download_type,
            value="MP4", command=self.on_type_change,
        ).pack(side="left", padx=10)

        tk.Radiobutton(
            type_frame, text="Audio (FLAC/M4A)", variable=self.download_type,
            value="AUDIO", command=self.on_type_change,
        ).pack(side="left", padx=10)

        tk.Radiobutton(
            type_frame, text="Audio (MP3)", variable=self.download_type,
            value="AUDIO_MP3", command=self.on_type_change,
        ).pack(side="left", padx=10)
        row += 1

        self.meta_frame = tk.LabelFrame(root, text="Optional metadata overrides (audio only)")
        self.meta_frame.grid(row=row, column=0, columnspan=3, padx=10, pady=5, sticky="we")
        row += 1

        self.meta_entries = {}
        fields = [("title", "Title"), ("artist", "Artist"), ("album", "Album"),
                  ("year", "Year"), ("genre", "Genre"), ("cover_url", "Cover URL")]
        for i, (key, label) in enumerate(fields):
            r, c = divmod(i, 2)
            tk.Label(self.meta_frame, text=label).grid(row=r, column=c * 2, padx=5, pady=3, sticky="e")
            entry = tk.Entry(self.meta_frame, width=28)
            entry.grid(row=r, column=c * 2 + 1, padx=5, pady=3, sticky="w")
            self.meta_entries[key] = entry

        self.download_btn = tk.Button(
            root, width=20, text="Download", command=self.start_download
        )
        self.download_btn.grid(row=row, column=0, columnspan=3, pady=8)
        row += 1

        # terminal
        self.term = scrolledtext.ScrolledText(
            root,
            height=20,
            width=95,
            bg="black",
            fg="#00FF00",
            insertbackground="#00FF00",
            font=("Consolas", 10),
            state="disabled",
        )
        self.term.grid(row=row, column=0, columnspan=3, padx=10, pady=5)

        self.root.after(100, self.poll_log_queue)

    def on_type_change(self):
        state = "normal" if (self.download_type.get() == "AUDIO") else "disabled"
        for entry in self.meta_entries.values():
            entry.config(state=state)

    def log(self, text: str):
        self.term.configure(state="normal")
        self.term.insert(tk.END, text)
        self.term.see(tk.END)
        self.term.configure(state="disabled")

    def poll_log_queue(self):
        try:
            while True:
                self.log(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        self.root.after(100, self.poll_log_queue)

    def start_download(self):
        url = self.url_entry.get().strip()
        if not url:
            messagebox.showwarning("Missing URL", "Please enter a URL first.")
            return

        dtype = DownloadType[self.download_type.get()]
        overrides = {k: e.get().strip() for k, e in self.meta_entries.items()}
        overrides = {k: v for k, v in overrides.items() if v}

        self.download_btn.config(state="disabled")
        self.log_queue.put(f"\n> Starting download: {url}\n")

        threading.Thread(
            target=self._run_download, args=(url, dtype, overrides), daemon=True
        ).start()

    def _run_download(self, url: str, dtype: DownloadType, overrides: dict):
        writer = QueueWriter(self.log_queue)
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout = writer
        sys.stderr = writer
        try:
            if dtype == DownloadType.MP4:
                download_video(url, "youtube_downloads")
            elif dtype == DownloadType.AUDIO:
                download_audio(url, "youtube_downloads", overrides)
            else:
                download_audio_mp3(url, "youtube_downloads")
        except Exception as exc:
            self.log_queue.put(f"\nError: {exc}\n")
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr
            self.log_queue.put("\n> Done.\n")
            self.root.after(0, lambda: self.download_btn.config(state="normal"))


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
