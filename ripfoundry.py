from __future__ import annotations

import csv
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

APP_NAME = "RipFoundry for Windows"
APP_VERSION = "1.1.0"
REPOSITORY_URL = "https://github.com/kylereddoch/RipFoundry-for-Windows"
CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / "RipFoundry"
CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULT_STAGING = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Videos" / "RipFoundry Staging"
DEFAULT_MOVIES = ""
INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

MODE_ORIGINAL = "Original DVD only"
MODE_ENHANCED = "Original DVD + Enhanced DVD"
MODE_UPSCALE = "Original DVD + 1080p"

MODE_DESCRIPTIONS = {
    MODE_ENHANCED: (
        "Keeps the untouched MakeMKV original and creates a second, playback-friendly H.264 copy "
        "at the DVD's native resolution. HandBrakeCLI deinterlaces when needed. This takes longer "
        "and uses additional storage."
    ),
    MODE_UPSCALE: (
        "Keeps the untouched MakeMKV original and creates a second H.264 copy scaled to 1080p. "
        "FFmpeg deinterlaces when needed. Scaling can improve playback compatibility, but it cannot "
        "restore HD detail that was not on the DVD."
    ),
    MODE_ORIGINAL: (
        "Keeps only the untouched MakeMKV remux at the DVD's native resolution. This is the fastest "
        "choice, preserves the disc's original video quality, and uses no additional encoding space."
    ),
}

DOWNLOAD_URLS = {
    "makemkv": "https://www.makemkv.com/download/",
    "ffmpeg": "https://ffmpeg.org/download.html#build-windows",
    "handbrake": "https://handbrake.fr/downloads2.php",
}


def resource_path(*parts: str) -> Path:
    """Return an asset path in source checkouts and PyInstaller builds."""
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return root.joinpath(*parts)


def configure_windows_app_id() -> None:
    """Give Windows a stable identity for the taskbar and pinned shortcuts."""
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "CybersecKyle.RipFoundry"
        )
    except Exception:
        pass


@dataclass
class DiscTitle:
    title_id: int
    name: str = ""
    chapters: str = ""
    duration: str = ""
    duration_seconds: int = 0
    size: str = ""
    size_bytes: int = 0
    source: str = ""
    output_name: str = ""


@dataclass
class MovieMetadata:
    title: str
    year: str
    tmdb_id: int


@dataclass
class MediaInfo:
    codec: str
    duration: float
    width: int
    height: int
    dar: str
    audio_tracks: int
    subtitle_tracks: int


def parse_duration(value: str) -> int:
    try:
        parts = [int(x) for x in value.split(":")]
    except ValueError:
        return 0
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return 0


def clean_disc_label(label: str) -> str:
    if not label:
        return ""
    value = label.replace("_", " ").replace(".", " ")
    value = re.sub(r"\s+", " ", value).strip()
    for pat in [r"\b4X3\b", r"\b16X9\b", r"\bWIDESCREEN\b", r"\bFULLSCREEN\b",
                r"\bFULL SCREEN\b", r"\bDISC\s*\d+\b", r"\bDISK\s*\d+\b", r"\bDVD\s*\d*\b"]:
        value = re.sub(pat, " ", value, flags=re.I)
    value = re.sub(r"\s+", " ", value).strip(" -_")
    return value.title() if value.isupper() else value


def sanitize_title(value: str) -> str:
    value = INVALID_FILENAME_CHARS.sub("-", value)
    value = re.sub(r"\s+", " ", value).strip().rstrip(". ")
    return value or "Untitled"


def resolution_label(info: MediaInfo) -> str:
    if info.height <= 500: return "480p"
    if info.height <= 600: return "576p"
    if info.height <= 760: return "720p"
    if info.height <= 1100: return "1080p"
    if info.height <= 2200: return "2160p"
    return f"{info.height}p"


def detect_optical_drives() -> list[tuple[str, str]]:
    """Return friendly Windows optical-drive labels and MakeMKV disc sources."""
    if os.name != "nt":
        return []
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        drive_mask = kernel32.GetLogicalDrives()
        found = []
        for index in range(26):
            if not drive_mask & (1 << index):
                continue
            letter = chr(ord("A") + index)
            root = f"{letter}:\\"
            if kernel32.GetDriveTypeW(root) != 5:  # DRIVE_CDROM
                continue
            volume = ctypes.create_unicode_buffer(261)
            has_label = kernel32.GetVolumeInformationW(
                root, volume, len(volume), None, None, None, None, 0
            )
            description = f"{letter}: - DVD/CD drive"
            if has_label and volume.value:
                description += f" ({volume.value})"
            disc_source = f"disc:{len(found)}"
            found.append((f"{description} - MakeMKV {disc_source}", disc_source))
        return found
    except Exception:
        return []


def sha256_file(path: Path, callback=None) -> str:
    h = hashlib.sha256()
    total = path.stat().st_size
    done = 0
    with path.open("rb") as f:
        while True:
            chunk = f.read(8 * 1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
            done += len(chunk)
            if callback and total:
                callback(done, total)
    return h.hexdigest()


def copy_verified(source: Path, destination: Path, log, progress=None) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".partial")
    if partial.exists():
        partial.unlink()
    log(f"Copying to media library: {destination}")
    total = source.stat().st_size
    done = 0
    with source.open("rb") as src, partial.open("wb") as dst:
        while True:
            chunk = src.read(8 * 1024 * 1024)
            if not chunk:
                break
            dst.write(chunk)
            done += len(chunk)
            if progress and total:
                progress(done / total * 100)
    log("Computing SHA-256 on local and destination copies...")
    local_hash = sha256_file(source)
    remote_hash = sha256_file(partial)
    log(f"Local SHA-256: {local_hash}")
    log(f"Dest. SHA-256: {remote_hash}")
    if local_hash != remote_hash:
        partial.unlink(missing_ok=True)
        raise RuntimeError("Checksum mismatch. Local staging file was preserved.")
    os.replace(partial, destination)
    log("Checksum verified; destination copy finalized.")


class Settings:
    def __init__(self):
        self.movies = DEFAULT_MOVIES
        self.staging = str(DEFAULT_STAGING)
        self.dvd_source = "disc:0"
        self.tmdb_token = ""
        self.makemkv = ""
        self.ffmpeg = ""
        self.ffprobe = ""
        self.handbrake = ""
        self.scan_min_length = 60
        self.collection_min_length = 2700
        self.crf = 18
        self.preset = "slow"
        self.hb_preset = "medium"
        self.load()
        self.autodetect()

    def load(self):
        if CONFIG_FILE.exists():
            try:
                data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                for k, v in data.items():
                    if hasattr(self, k): setattr(self, k, v)
            except Exception:
                pass

    def save(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(self.__dict__, indent=2), encoding="utf-8")

    @staticmethod
    def _first(candidates):
        for c in candidates:
            if not c: continue
            found = shutil.which(c)
            if found: return found
            if Path(c).exists(): return str(Path(c))
        return ""

    def autodetect(self):
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pfx86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        local = os.environ.get("LOCALAPPDATA", "")
        program_data = os.environ.get("ProgramData", r"C:\ProgramData")
        if not self.makemkv or not Path(self.makemkv).is_file():
            self.makemkv = self._first([
                "makemkvcon64.exe", "makemkvcon.exe",
                str(Path(pfx86) / "MakeMKV" / "makemkvcon64.exe"),
                str(Path(pf) / "MakeMKV" / "makemkvcon64.exe"),
                str(Path(pfx86) / "MakeMKV" / "makemkvcon.exe"),
            ])
        if not self.ffmpeg or not Path(self.ffmpeg).is_file():
            self.ffmpeg = self._first([
                "ffmpeg.exe", "ffmpeg",
                str(Path(local) / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe") if local else "",
                str(Path(program_data) / "chocolatey" / "bin" / "ffmpeg.exe"),
            ])
        if not self.ffprobe or not Path(self.ffprobe).is_file():
            adjacent = str(Path(self.ffmpeg).with_name("ffprobe.exe")) if self.ffmpeg else ""
            self.ffprobe = self._first([
                "ffprobe.exe", "ffprobe", adjacent,
                str(Path(local) / "Microsoft" / "WinGet" / "Links" / "ffprobe.exe") if local else "",
                str(Path(program_data) / "chocolatey" / "bin" / "ffprobe.exe"),
            ])
        if not self.handbrake or not Path(self.handbrake).is_file():
            self.handbrake = self._first([
                "HandBrakeCLI.exe", "HandBrakeCLI",
                str(Path(pf) / "HandBrake" / "HandBrakeCLI.exe"),
                str(Path(pfx86) / "HandBrake" / "HandBrakeCLI.exe"),
                str(Path(local) / "Programs" / "HandBrake" / "HandBrakeCLI.exe") if local else "",
            ])


class Runner:
    def __init__(self, settings: Settings, log, progress):
        self.s = settings
        self.log = log
        self.progress = progress

    def run(self, args, capture=False, check=True):
        self.log("$ " + subprocess.list2cmdline([str(x) for x in args]))
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" and capture else 0
        if capture:
            p = subprocess.run([str(x) for x in args], text=True, capture_output=True,
                               check=False, creationflags=flags)
            if check and p.returncode != 0:
                raise RuntimeError((p.stderr or p.stdout or "Command failed").strip())
            return p
        p = subprocess.Popen([str(x) for x in args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, bufsize=1, universal_newlines=True,
                             creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
        assert p.stdout
        for line in p.stdout:
            self.log(line.rstrip())
        rc = p.wait()
        if check and rc != 0:
            raise RuntimeError(f"Command exited with code {rc}")
        return rc

    def require(self, *names):
        missing = []
        mapping = {"makemkv": self.s.makemkv, "ffmpeg": self.s.ffmpeg,
                   "ffprobe": self.s.ffprobe, "handbrake": self.s.handbrake}
        for n in names:
            p = mapping[n]
            if not p or not Path(p).exists(): missing.append(n)
        if missing:
            raise RuntimeError("Missing tool path(s): " + ", ".join(missing) + ". Configure them in Settings.")

    def scan_disc(self):
        self.require("makemkv")
        p = self.run([self.s.makemkv, "-r", f"--minlength={self.s.scan_min_length}",
                      "info", self.s.dvd_source], capture=True, check=False)
        output = (p.stdout or "") + "\n" + (p.stderr or "")
        if p.returncode != 0:
            raise RuntimeError("MakeMKV could not scan the DVD.\n" + output[-2000:])
        attrs = {}
        labels = []
        for raw in output.splitlines():
            line = raw.strip()
            if line.startswith("TINFO:"):
                try:
                    row = next(csv.reader([line[len("TINFO:"):]], escapechar="\\"))
                    tid, aid = int(row[0]), int(row[1])
                    val = row[3] if len(row) > 3 else ""
                    attrs.setdefault(tid, {})[aid] = val
                except Exception: pass
            elif line.startswith("CINFO:"):
                try:
                    row = next(csv.reader([line[len("CINFO:"):]], escapechar="\\"))
                    aid = int(row[0]); val = row[2] if len(row) > 2 else ""
                    if aid in {2, 30, 32} and val.strip(): labels.append(val.strip())
                except Exception: pass
        titles = []
        for tid, info in sorted(attrs.items()):
            duration = info.get(9, "")
            if not duration: continue
            try: size_bytes = int(info.get(11, "0") or "0")
            except ValueError: size_bytes = 0
            titles.append(DiscTitle(tid, info.get(2, ""), info.get(8, ""), duration,
                                    parse_duration(duration), info.get(10, ""), size_bytes,
                                    info.get(16, ""), info.get(27, "")))
        if not titles: raise RuntimeError("No usable DVD titles were found.")
        label = next((x for x in labels if x.lower() not in {"dvd", "dvd disc"}), "")
        return titles, clean_disc_label(label)

    def tmdb_search(self, query):
        if not self.s.tmdb_token:
            return []
        params = urllib.parse.urlencode({"query": query, "include_adult": "false", "language": "en-US", "page": "1"})
        req = urllib.request.Request("https://api.themoviedb.org/3/search/movie?" + params,
                                     headers={"Authorization": f"Bearer {self.s.tmdb_token}",
                                              "accept": "application/json", "User-Agent": f"RipFoundry-Windows/{APP_VERSION}"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8")).get("results", [])[:10]
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"TMDb search failed (HTTP {e.code}).")
        except Exception as e:
            raise RuntimeError(f"TMDb search failed: {e}")

    def eject_disc(self):
        if os.name != "nt":
            return
        script = (
            "$d=(Get-CimInstance Win32_CDROMDrive | Where-Object Drive | Select-Object -First 1 -ExpandProperty Drive);"
            "if($d){(New-Object -ComObject Shell.Application).Namespace(17).ParseName($d).InvokeVerb('Eject')}"
        )
        try:
            subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                           capture_output=True, text=True, check=False, creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception:
            pass

    def ffprobe(self, path: Path):
        self.require("ffprobe")
        p = self.run([self.s.ffprobe, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)], capture=True)
        data = json.loads(p.stdout)
        streams = data.get("streams") or []
        video = next((x for x in streams if x.get("codec_type") == "video"), None)
        if not video: raise RuntimeError(f"No video stream found in {path}")
        duration = float((data.get("format") or {}).get("duration", "0") or 0)
        return MediaInfo(str(video.get("codec_name") or ""), duration,
                         int(video.get("width") or 0), int(video.get("height") or 0),
                         str(video.get("display_aspect_ratio") or ""),
                         sum(1 for x in streams if x.get("codec_type") == "audio"),
                         sum(1 for x in streams if x.get("codec_type") == "subtitle"))

    def enhanced(self, source: Path, output: Path):
        self.require("handbrake")
        output.unlink(missing_ok=True)
        args = [self.s.handbrake, "-i", str(source), "-o", str(output),
                "-f", "av_mkv", "-e", "x264", "-q", str(self.s.crf), "--encoder-preset", self.s.hb_preset,
                "--comb-detect", "--decomb", "--vfr",
                "--all-audio", "--aencoder", "copy",
                "--audio-copy-mask", "aac,ac3,eac3,truehd,dts,dtshd,mp2,mp3,flac",
                "--audio-fallback", "ac3",
                "--all-subtitles", "--markers", "--non-anamorphic"]
        self.run(args)
        if not output.exists(): raise RuntimeError("HandBrake did not create the Enhanced DVD output.")

    def upscale(self, source: Path, output: Path):
        self.require("ffmpeg")
        output.unlink(missing_ok=True)
        vf = "bwdif=mode=send_frame:parity=auto:deint=interlaced,scale=w='trunc(1080*dar/2)*2':h=1080:flags=lanczos,setsar=1"
        args = [self.s.ffmpeg, "-hide_banner", "-y", "-i", str(source),
                "-map", "0:v:0", "-map", "0:a?", "-map", "0:s?", "-map_metadata", "0", "-map_chapters", "0",
                "-vf", vf, "-c:v", "libx264", "-preset", self.s.preset, "-crf", str(self.s.crf),
                "-pix_fmt", "yuv420p", "-c:a", "copy", "-c:s", "copy", "-max_muxing_queue_size", "4096", str(output)]
        rc = self.run(args, check=False)
        if rc != 0:
            # Retry with yadif on builds lacking bwdif.
            vf = "yadif=mode=send_frame:parity=auto:deint=interlaced,scale=w='trunc(1080*dar/2)*2':h=1080:flags=lanczos,setsar=1"
            args[args.index("-vf") + 1] = vf
            self.log("Retrying with yadif deinterlacing...")
            self.run(args)
        if not output.exists(): raise RuntimeError("FFmpeg did not create the 1080p output.")

    def validate_processed(self, source_info, output, target="1080"):
        out = self.ffprobe(output)
        if out.codec != "h264": raise RuntimeError(f"Processed file codec is {out.codec}, expected H.264.")
        if target == "1080" and not (1000 <= out.height <= 1100):
            raise RuntimeError(f"Processed output is {out.width}x{out.height}, not 1080-height.")
        if abs(source_info.duration - out.duration) > 10:
            raise RuntimeError("Processed movie duration differs from source by more than 10 seconds.")
        if out.audio_tracks < source_info.audio_tracks:
            self.log(f"WARNING: audio tracks decreased {source_info.audio_tracks} -> {out.audio_tracks}")
        if out.subtitle_tracks < source_info.subtitle_tracks:
            self.log(f"WARNING: subtitle tracks decreased {source_info.subtitle_tracks} -> {out.subtitle_tracks}")
        return out

    def rip_title(self, disc_title: DiscTitle, meta: MovieMetadata, mode: str):
        self.require("makemkv", "ffprobe")
        if mode == MODE_ENHANCED: self.require("handbrake")
        if mode == MODE_UPSCALE: self.require("ffmpeg")
        staging_root = Path(self.s.staging)
        movies_root = Path(self.s.movies)
        staging_root.mkdir(parents=True, exist_ok=True)
        if not self.s.movies.strip(): raise RuntimeError("Media Library Destination is not configured. Open Settings and choose a destination folder.")
        if not movies_root.exists(): raise RuntimeError(f"Media Library Destination is unavailable: {movies_root}")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        base = f"{sanitize_title(meta.title)} ({meta.year}) [tmdbid-{meta.tmdb_id}]"
        job = staging_root / "rip-jobs" / f"{stamp}-title-{disc_title.title_id}"
        job.mkdir(parents=True, exist_ok=True)
        self.log(f"Ripping DVD title {disc_title.title_id}: {base}")
        self.run([self.s.makemkv, "mkv", self.s.dvd_source, str(disc_title.title_id), str(job)])
        mkvs = list(job.glob("*.mkv"))
        if len(mkvs) != 1: raise RuntimeError(f"Expected one MKV from MakeMKV; found {len(mkvs)}.")
        raw = mkvs[0]
        info = self.ffprobe(raw)
        if info.duration <= 0: raise RuntimeError("Ripped MKV failed duration validation.")
        native = resolution_label(info)
        raw_named = job / f"{base} - {native}.mkv"
        raw.rename(raw_named)
        processed = None
        processed_name = None
        if mode == MODE_ENHANCED:
            processed = job / f"{base} - {native} Enhanced.mkv"
            self.enhanced(raw_named, processed)
            self.validate_processed(info, processed, target="native")
            processed_name = processed.name
        elif mode == MODE_UPSCALE:
            processed = job / f"{base} - 1080p.mkv"
            self.upscale(raw_named, processed)
            self.validate_processed(info, processed, target="1080")
            processed_name = processed.name
        dest_dir = movies_root / base
        if (dest_dir / raw_named.name).exists(): raise RuntimeError(f"Destination already exists: {dest_dir / raw_named.name}")
        if processed and (dest_dir / processed.name).exists(): raise RuntimeError(f"Destination already exists: {dest_dir / processed.name}")
        copy_verified(raw_named, dest_dir / raw_named.name, self.log, self.progress)
        if processed:
            copy_verified(processed, dest_dir / processed.name, self.log, self.progress)
        raw_named.unlink(missing_ok=True)
        if processed: processed.unlink(missing_ok=True)
        try: job.rmdir()
        except OSError: pass
        self.log(f"COMPLETE: {dest_dir}")
        self.log(f"  {raw_named.name}")
        if processed_name: self.log(f"  {processed_name}")

    def add_1080(self, source: Path):
        self.require("ffmpeg", "ffprobe")
        if not source.exists(): raise RuntimeError("Source movie no longer exists.")
        folder = source.parent
        base = folder.name
        if not source.name.startswith(base):
            raise RuntimeError("Selected file does not use the Jellyfin movie-folder prefix naming convention.")
        source_info = self.ffprobe(source)
        native = resolution_label(source_info)
        original_dest = folder / f"{base} - {native}.mkv" if source.name == f"{base}.mkv" else source
        target = folder / f"{base} - 1080p.mkv"
        if target.exists() and target != source:
            raise RuntimeError(f"A 1080p version already exists: {target}")
        stage = Path(self.s.staging) / "upscale-jobs" / datetime.now().strftime("%Y%m%d-%H%M%S")
        stage.mkdir(parents=True, exist_ok=True)
        encoded = stage / f"{sanitize_title(base)} - 1080p.mkv"
        self.upscale(source, encoded)
        encoded_info = self.validate_processed(source_info, encoded, target="1080")
        # Rename old single-version file only after successful encode.
        if original_dest != source:
            if original_dest.exists(): raise RuntimeError(f"Cannot rename original; destination exists: {original_dest}")
            source.rename(original_dest)
            source = original_dest
        copy_verified(encoded, target, self.log, self.progress)
        final = self.ffprobe(target)
        if final.height != encoded_info.height or abs(final.duration - encoded_info.duration) > 2:
            target.unlink(missing_ok=True)
            raise RuntimeError("1080p file failed verification after finalization; NAS copy removed.")
        encoded.unlink(missing_ok=True)
        try: stage.rmdir()
        except OSError: pass
        self.log("1080p VERSION ADDED")
        self.log(f"Original: {source.name}")
        self.log(f"1080p:    {target.name}")


class MetadataDialog(tk.Toplevel):
    def __init__(self, parent, runner: Runner, suggested=""):
        super().__init__(parent)
        self.title("Select TMDb Movie")
        self.geometry("760x450")
        self.resizable(True, True)
        self.runner = runner
        self.result = None
        self.transient(parent)
        self.grab_set()
        top = ttk.Frame(self, padding=10); top.pack(fill="x")
        ttk.Label(top, text="Movie search:").pack(side="left")
        self.q = tk.StringVar(value=suggested)
        ent = ttk.Entry(top, textvariable=self.q); ent.pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(top, text="Search TMDb", command=self.search).pack(side="left")
        self.tree = ttk.Treeview(self, columns=("title","year","id"), show="headings", selectmode="browse")
        for col, title, w in [("title","Title",430),("year","Year",90),("id","TMDb ID",110)]:
            self.tree.heading(col, text=title); self.tree.column(col, width=w, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=10, pady=5)
        bottom = ttk.Frame(self, padding=10); bottom.pack(fill="x")
        ttk.Button(bottom, text="Manual Entry", command=self.manual).pack(side="left")
        ttk.Button(bottom, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(bottom, text="Use Selected", command=self.use_selected).pack(side="right", padx=8)
        if suggested: self.after(100, self.search)
        ent.focus_set()

    def search(self):
        for x in self.tree.get_children(): self.tree.delete(x)
        q = self.q.get().strip()
        if not q: return
        try: results = self.runner.tmdb_search(q)
        except Exception as e:
            messagebox.showerror("TMDb", str(e), parent=self); return
        if not results:
            messagebox.showinfo("TMDb", "No results, or no TMDb token is configured. Use Manual Entry if needed.", parent=self); return
        for r in results:
            year = str(r.get("release_date") or "")[:4]
            self.tree.insert("", "end", values=(r.get("title") or "", year, r.get("id") or ""))

    def use_selected(self):
        sel = self.tree.selection()
        if not sel: return
        title, year, tid = self.tree.item(sel[0], "values")
        if not year:
            year = simpledialog.askstring("Year", "Release year:", parent=self) or ""
        if year and str(tid).isdigit():
            self.result = MovieMetadata(str(title), str(year), int(tid)); self.destroy()

    def manual(self):
        title = simpledialog.askstring("Movie", "Movie title:", parent=self)
        if not title: return
        year = simpledialog.askstring("Movie", "Release year (YYYY):", parent=self)
        if not year or not re.fullmatch(r"\d{4}", year): return
        tid = simpledialog.askstring("Movie", "TMDb movie ID:", parent=self)
        if not tid or not tid.isdigit(): return
        self.result = MovieMetadata(title, year, int(tid)); self.destroy()


class ToolTip:
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.window = None
        widget.bind("<Enter>", self.show, add="+")
        widget.bind("<Leave>", self.hide, add="+")

    def show(self, _event=None):
        if self.window or not self.text:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.window = tk.Toplevel(self.widget)
        self.window.wm_overrideredirect(True)
        self.window.wm_geometry(f"+{x}+{y}")
        tk.Label(
            self.window, text=self.text, justify="left", background="#fffbe6",
            relief="solid", borderwidth=1, padx=7, pady=4, wraplength=440,
        ).pack()

    def hide(self, _event=None):
        if self.window:
            self.window.destroy()
            self.window = None


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self._apply_app_icon()
        self.title(f"{APP_NAME} {APP_VERSION}")
        self.geometry("1040x780")
        self.minsize(900, 650)
        self.settings = Settings()
        self.q = queue.Queue()
        self.disc_titles = []
        self.disc_hint = ""
        self.status_labels = {}
        self.dvd_choices = {}
        self.optical_drives = []
        self._build()
        self.after(150, self.refresh_setup_status)
        self.after(100, self._drain)

    def _apply_app_icon(self):
        """Apply the RipFoundry mark to the window and Windows taskbar."""
        icon_path = resource_path("assets", "RipFoundry.ico")
        png_path = resource_path("assets", "RipFoundry.png")
        try:
            if os.name == "nt" and icon_path.is_file():
                self.iconbitmap(default=str(icon_path))
        except tk.TclError:
            pass
        try:
            if png_path.is_file():
                self.app_icon_image = tk.PhotoImage(file=str(png_path))
                self.iconphoto(True, self.app_icon_image)
        except tk.TclError:
            self.app_icon_image = None

    def _build(self):
        nb = ttk.Notebook(self); nb.pack(fill="both", expand=True, padx=10, pady=10)
        self.rip_tab = ttk.Frame(nb, padding=10); nb.add(self.rip_tab, text="Rip DVD")
        self.up_tab = ttk.Frame(nb, padding=10); nb.add(self.up_tab, text="Add 1080p Version")
        self.set_tab = ttk.Frame(nb, padding=10); nb.add(self.set_tab, text="Settings")
        self.about_tab = ttk.Frame(nb, padding=16); nb.add(self.about_tab, text="About")
        self._build_rip(); self._build_up(); self._build_settings(); self._build_about()
        lf = ttk.LabelFrame(self, text="Activity", padding=6); lf.pack(fill="both", expand=False, padx=10, pady=(0,10))
        self.logbox = tk.Text(lf, height=8, wrap="word", state="disabled"); self.logbox.pack(fill="both", expand=True)
        self.progress_var = tk.DoubleVar(value=0)
        ttk.Progressbar(lf, variable=self.progress_var, maximum=100).pack(fill="x", pady=(6,0))

    def _build_rip(self):
        top = ttk.Frame(self.rip_tab); top.pack(fill="x")
        ttk.Label(top, text="Insert a DVD, then scan it. Select one or more titles to rip.").pack(side="left")
        ttk.Button(top, text="Scan DVD", command=self.scan).pack(side="right")
        self.disc_label = ttk.Label(self.rip_tab, text="No disc scanned."); self.disc_label.pack(anchor="w", pady=8)
        self.title_tree = ttk.Treeview(self.rip_tab, columns=("id","duration","size","chapters","name"), show="headings", selectmode="extended", height=12)
        for col,title,w in [("id","ID",45),("duration","Duration",90),("size","Size",90),("chapters","Ch",50),("name","Source / Name",560)]:
            self.title_tree.heading(col,text=title); self.title_tree.column(col,width=w,anchor="w")
        self.title_tree.pack(fill="both", expand=True)
        opts = ttk.Frame(self.rip_tab); opts.pack(fill="x", pady=10)
        ttk.Label(opts,text="Processing:").pack(side="left")
        self.mode = tk.StringVar(value=MODE_ENHANCED)
        self.mode_combo = ttk.Combobox(opts,textvariable=self.mode,values=[MODE_ENHANCED,MODE_UPSCALE,MODE_ORIGINAL],state="readonly",width=32)
        self.mode_combo.pack(side="left",padx=8)
        self.mode_combo.bind("<<ComboboxSelected>>", self._update_mode_help)
        ttk.Button(opts,text="Rip Selected Title(s)",command=self.rip_selected).pack(side="right")
        mode_help = ttk.LabelFrame(self.rip_tab, text="What this option creates", padding=(10, 7))
        mode_help.pack(fill="x", pady=(0, 7))
        self.mode_help = ttk.Label(mode_help, wraplength=880, justify="left")
        self.mode_help.pack(anchor="w", fill="x")
        self._update_mode_help()
        ttk.Label(self.rip_tab, text="Each selected DVD title gets its own TMDb match before ripping.").pack(anchor="w")

    def _update_mode_help(self, _event=None):
        self.mode_help.configure(text=MODE_DESCRIPTIONS.get(self.mode.get(), ""))

    def _build_up(self):
        ttk.Label(self.up_tab, text="Create a non-destructive 1080-height H.264 version beside an existing Jellyfin movie.").pack(anchor="w", pady=(0,12))
        row=ttk.Frame(self.up_tab); row.pack(fill="x")
        self.movie_query=tk.StringVar()
        ttk.Entry(row,textvariable=self.movie_query).pack(side="left",fill="x",expand=True)
        ttk.Button(row,text="Search Library",command=self.search_movies).pack(side="left",padx=(8,0))
        self.movie_tree=ttk.Treeview(self.up_tab,columns=("path",),show="headings",selectmode="browse",height=15)
        self.movie_tree.heading("path",text="Matching MKV files"); self.movie_tree.column("path",width=780)
        self.movie_tree.pack(fill="both",expand=True,pady=10)
        ttk.Button(self.up_tab,text="Add 1080p Version",command=self.start_upscale).pack(anchor="e")

    def _helper(self, parent, text, row, column=0, columnspan=4):
        label = ttk.Label(parent, text=text, foreground="#5f6368", wraplength=760, justify="left")
        label.grid(row=row, column=column, columnspan=columnspan, sticky="w", pady=(0, 7))
        return label

    def _browse_for(self, key, folder=False):
        var = getattr(self, "var_" + key)
        if folder:
            value = filedialog.askdirectory(initialdir=var.get() or None)
        else:
            initial = str(Path(var.get()).parent) if var.get() and Path(var.get()).parent.exists() else None
            value = filedialog.askopenfilename(
                initialdir=initial,
                filetypes=[("Windows executables", "*.exe"), ("All files", "*.*")],
            )
        if value:
            var.set(value)
            self.refresh_setup_status()

    def _tool_row(self, parent, row, key, title, description, download_key, optional=False):
        label = ttk.Label(parent, text=title, font=("Segoe UI", 9, "bold"))
        label.grid(row=row, column=0, sticky="w", pady=(6, 2))
        ToolTip(label, description)
        status = ttk.Label(parent, text="Checking...", width=18)
        status.grid(row=row, column=1, sticky="w", padx=(10, 4), pady=(6, 2))
        self.status_labels[key] = status
        var = tk.StringVar(value=getattr(self.settings, key))
        setattr(self, "var_" + key, var)
        entry = ttk.Entry(parent, textvariable=var)
        entry.grid(row=row, column=2, sticky="ew", padx=(4, 6), pady=(6, 2))
        ToolTip(entry, description)
        ttk.Button(parent, text="Locate...", command=lambda: self._browse_for(key)).grid(
            row=row, column=3, padx=(0, 5), pady=(6, 2)
        )
        download = ttk.Button(parent, text="Download", command=lambda: webbrowser.open(DOWNLOAD_URLS[download_key]))
        download.grid(row=row, column=4, pady=(6, 2))
        ToolTip(download, "Open the official download page in your browser.")
        suffix = " Required when you choose Enhanced DVD mode." if optional else ""
        self._helper(parent, description + suffix, row + 1, column=2, columnspan=3)

    def _path_setting(self, parent, row, key, title, description):
        label = ttk.Label(parent, text=title, font=("Segoe UI", 9, "bold"))
        label.grid(row=row, column=0, sticky="w", pady=(6, 2))
        ToolTip(label, description)
        var = tk.StringVar(value=getattr(self.settings, key))
        setattr(self, "var_" + key, var)
        entry = ttk.Entry(parent, textvariable=var)
        entry.grid(row=row, column=1, sticky="ew", padx=8, pady=(6, 2))
        ToolTip(entry, description)
        ttk.Button(parent, text="Browse...", command=lambda: self._browse_for(key, True)).grid(
            row=row, column=2, pady=(6, 2)
        )
        self._helper(parent, description, row + 1, column=1, columnspan=2)

    def _build_settings(self):
        canvas = tk.Canvas(self.set_tab, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self.set_tab, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        content = ttk.Frame(canvas, padding=(4, 2, 12, 12))
        window = canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))

        def wheel(event):
            canvas.yview_scroll(int(-event.delta / 120), "units")
        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", wheel))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))

        ttk.Label(content, text="Setup & Required Software", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Label(
            content,
            text="RipFoundry checks the programs and locations it needs. A green Ready status means the saved path is usable.",
            wraplength=820, justify="left",
        ).pack(anchor="w", pady=(2, 10))

        deps = ttk.LabelFrame(content, text="Required software", padding=10)
        deps.pack(fill="x", pady=(0, 10))
        deps.columnconfigure(2, weight=1)
        self._tool_row(
            deps, 0, "makemkv", "MakeMKV console",
            "makemkvcon64.exe reads and rips the DVD. It is installed with MakeMKV; do not select MakeMKV.exe.",
            "makemkv",
        )
        self._tool_row(
            deps, 2, "ffmpeg", "FFmpeg",
            "ffmpeg.exe creates the optional 1080p version. Download a Windows build from a provider linked by FFmpeg.org.",
            "ffmpeg",
        )
        self._tool_row(
            deps, 4, "ffprobe", "FFprobe",
            "ffprobe.exe validates duration, resolution, streams, and codecs. It normally comes in the same folder as FFmpeg.",
            "ffmpeg",
        )
        self._tool_row(
            deps, 6, "handbrake", "HandBrakeCLI",
            "HandBrakeCLI.exe creates the optional Enhanced DVD version. Use the command-line download, not only the desktop app.",
            "handbrake", optional=True,
        )
        ttk.Button(deps, text="Auto-detect / Check Again", command=self.autodetect_tools).grid(
            row=8, column=2, sticky="e", pady=(8, 0)
        )

        dvd = ttk.LabelFrame(content, text="DVD drive", padding=10)
        dvd.pack(fill="x", pady=(0, 10))
        dvd.columnconfigure(1, weight=1)
        ttk.Label(dvd, text="Windows optical drive:", font=("Segoe UI", 9, "bold")).grid(row=0, column=0, sticky="w")
        self.var_dvd_source = tk.StringVar()
        self.dvd_combo = ttk.Combobox(dvd, textvariable=self.var_dvd_source, state="normal")
        self.dvd_combo.grid(row=0, column=1, sticky="ew", padx=8)
        ToolTip(self.dvd_combo, "Choose the Windows DVD drive. RipFoundry translates it to MakeMKV's disc:N source internally.")
        ttk.Button(dvd, text="Refresh Drives", command=self.refresh_dvd_drives).grid(row=0, column=2)
        self.status_labels["dvd"] = ttk.Label(dvd, text="Checking...")
        self.status_labels["dvd"].grid(row=1, column=1, sticky="w", padx=8, pady=(4, 0))
        self._helper(
            dvd,
            "MakeMKV calls the first detected optical drive disc:0, the second disc:1, and so on. The selector shows the familiar Windows drive letter while saving that MakeMKV value.",
            2, column=1, columnspan=2,
        )
        self.refresh_dvd_drives()

        storage = ttk.LabelFrame(content, text="Storage", padding=10)
        storage.pack(fill="x", pady=(0, 10))
        storage.columnconfigure(1, weight=1)
        self._path_setting(
            storage, 0, "movies", "Media Library Destination",
            r"Where finished movies are saved. Supports local folders, mapped drives, and UNC paths such as \\NAS\Media\Movies.",
        )
        self.status_labels["movies"] = ttk.Label(storage, text="Not checked")
        self.status_labels["movies"].grid(row=2, column=1, sticky="w", padx=8)
        ttk.Button(storage, text="Test Destination", command=self.test_destination).grid(row=2, column=2, sticky="e")
        self._path_setting(
            storage, 3, "staging", "Local staging",
            "Temporary working space on this Windows PC. Ripping and encoding happen here before a verified file is copied to the media library.",
        )

        options = ttk.LabelFrame(content, text="Movie matching & encoding", padding=10)
        options.pack(fill="x", pady=(0, 10))
        options.columnconfigure(1, weight=1)
        ttk.Label(options, text="TMDb Read Access Token", font=("Segoe UI", 9, "bold")).grid(row=0, column=0, sticky="w")
        self.var_tmdb_token = tk.StringVar(value=self.settings.tmdb_token)
        token_entry = ttk.Entry(options, textvariable=self.var_tmdb_token, show="*")
        token_entry.grid(row=0, column=1, sticky="ew", padx=8)
        ToolTip(token_entry, "Used only to search for a movie and add Jellyfin's [tmdbid-####] naming hint.")
        self._helper(
            options,
            "Used only for movie identification and Jellyfin [tmdbid-####] folder/file naming. Get a read token from your TMDb account API settings.",
            1, column=1, columnspan=2,
        )
        ttk.Label(options, text="CRF", font=("Segoe UI", 9, "bold")).grid(row=2, column=0, sticky="w")
        self.var_crf = tk.StringVar(value=str(self.settings.crf))
        crf_entry = ttk.Entry(options, textvariable=self.var_crf, width=10)
        crf_entry.grid(row=2, column=1, sticky="w", padx=8)
        ToolTip(crf_entry, "CRF controls x264 quality. 18 is high quality; higher values make smaller, lower-quality files.")
        self._helper(options, "CRF 18 is high quality with larger files. Higher numbers reduce file size and quality.", 3, column=1, columnspan=2)
        ttk.Label(options, text="1080p x264 preset", font=("Segoe UI", 9, "bold")).grid(row=4, column=0, sticky="w")
        self.var_preset = tk.StringVar(value=self.settings.preset)
        preset = ttk.Combobox(options, textvariable=self.var_preset, values=["medium", "slow", "slower"], state="readonly", width=15)
        preset.grid(row=4, column=1, sticky="w", padx=8)
        ToolTip(preset, "Controls encoding speed versus compression efficiency. Slow is the recommended balance.")
        self._helper(options, 'Preset controls encoding speed versus compression efficiency. "slow" is recommended.', 5, column=1, columnspan=2)

        buttons = ttk.Frame(content)
        buttons.pack(fill="x", pady=(0, 6))
        ttk.Button(buttons, text="Check Setup", command=self.refresh_setup_status).pack(side="right")
        ttk.Button(buttons, text="Save Settings", command=self.save_settings).pack(side="right", padx=(0, 8))
        self.after_idle(lambda: canvas.yview_moveto(0))

    def _set_status(self, key, text, ready=False, warning=False):
        label = self.status_labels.get(key)
        if not label:
            return
        color = "#177245" if ready else ("#9a6700" if warning else "#b42318")
        label.configure(text=text, foreground=color)

    def autodetect_tools(self):
        for key in ["makemkv", "ffmpeg", "ffprobe", "handbrake"]:
            setattr(self.settings, key, getattr(self, "var_" + key).get().strip())
        self.settings.autodetect()
        for key in ["makemkv", "ffmpeg", "ffprobe", "handbrake"]:
            getattr(self, "var_" + key).set(getattr(self.settings, key))
        self.refresh_setup_status()

    def refresh_dvd_drives(self):
        current_source = self._resolved_dvd_source() if hasattr(self, "dvd_combo") else self.settings.dvd_source
        self.optical_drives = detect_optical_drives()
        self.dvd_choices = {display: source for display, source in self.optical_drives}
        if self.optical_drives:
            values = list(self.dvd_choices)
            selected = next((display for display, source in self.optical_drives if source == current_source), values[0])
        else:
            selected = f"No Windows optical drive detected - use MakeMKV {current_source or 'disc:0'}"
            self.dvd_choices[selected] = current_source or "disc:0"
            values = [selected]
        self.dvd_combo.configure(values=values)
        self.var_dvd_source.set(selected)
        self.refresh_setup_status()

    def _resolved_dvd_source(self):
        if not hasattr(self, "var_dvd_source"):
            return self.settings.dvd_source or "disc:0"
        value = self.var_dvd_source.get().strip()
        if value in self.dvd_choices:
            return self.dvd_choices[value]
        match = re.search(r"\bdisc:\d+\b", value, flags=re.I)
        return match.group(0).lower() if match else (value or "disc:0")

    def refresh_setup_status(self):
        if not hasattr(self, "var_makemkv"):
            return
        for key in ["makemkv", "ffmpeg", "ffprobe", "handbrake"]:
            value = getattr(self, "var_" + key).get().strip()
            ready = bool(value and Path(value).is_file())
            self._set_status(key, "Ready" if ready else "Not found", ready=ready)
        if self.optical_drives:
            self._set_status("dvd", f"Ready - uses {self._resolved_dvd_source()}", ready=True)
        else:
            self._set_status("dvd", "No Windows DVD drive detected", warning=True)
        movie_value = self.var_movies.get().strip() if hasattr(self, "var_movies") else ""
        movie_path = Path(movie_value) if movie_value else None
        self._set_status(
            "movies",
            "Ready" if movie_path and movie_path.is_dir() else ("Not configured" if not movie_value else "Unavailable"),
            ready=bool(movie_path and movie_path.is_dir()), warning=not movie_value,
        )

    def _build_about(self):
        header = ttk.Frame(self.about_tab)
        header.pack(fill="x", anchor="w", pady=(0, 18))

        if getattr(self, "app_icon_image", None):
            scale = max(1, self.app_icon_image.width() // 96)
            self.about_logo = self.app_icon_image.subsample(scale, scale)
            ttk.Label(header, image=self.about_logo).pack(side="left", anchor="n", padx=(0, 14))

        heading = ttk.Frame(header)
        heading.pack(side="left", anchor="n")
        ttk.Label(heading, text="RipFoundry", font=("Segoe UI", 18, "bold")).pack(anchor="w")
        ttk.Label(
            heading,
            text="Physical media in. Digital library out.",
            font=("Segoe UI", 10, "italic"),
        ).pack(anchor="w", pady=(2, 0))

        info = ttk.LabelFrame(self.about_tab, text="Built By", padding=14)
        info.pack(fill="x", anchor="w")
        ttk.Label(info, text="Kyle Reddoch", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        ttk.Label(info, text="CybersecKyle").pack(anchor="w", pady=(2, 0))
        ttk.Label(info, text="https://www.kylereddoch.me").pack(anchor="w", pady=(2, 0))

        ttk.Label(
            self.about_tab,
            text=(
                "RipFoundry was created to simplify a real-world DVD-to-Jellyfin workflow: "
                "rip locally, preserve the source, create optional playback-friendly versions, "
                "validate the results, and safely move completed media into a Jellyfin library."
            ),
            wraplength=760,
            justify="left",
        ).pack(anchor="w", pady=(18, 8))

        version_link = ttk.Label(
            self.about_tab,
            text=f"{APP_NAME} {APP_VERSION}",
            foreground="#0563c1",
            cursor="hand2",
            font=("Segoe UI", 9, "underline"),
        )
        version_link.pack(anchor="w", pady=(12, 0))
        version_link.bind("<Button-1>", lambda _event: webbrowser.open(REPOSITORY_URL))
        ToolTip(version_link, "Open the RipFoundry for Windows repository on GitHub.")

    def test_destination(self):
        value = self.var_movies.get().strip()
        if not value:
            messagebox.showwarning(APP_NAME, "Choose or enter a Media Library Destination first.")
            return
        path = Path(value)
        try:
            if not path.exists():
                self._set_status("movies", "Unavailable")
                messagebox.showerror(APP_NAME, f"Destination is not available:\n\n{path}")
                return
            if not path.is_dir():
                self._set_status("movies", "Not a folder")
                messagebox.showerror(APP_NAME, f"Destination is not a folder:\n\n{path}")
                return
            probe = path / f".ripfoundry-write-test-{os.getpid()}"
            probe.write_text("RipFoundry write test", encoding="utf-8")
            probe.unlink()
            free = shutil.disk_usage(path).free
            free_text = f"{free / (1024 ** 4):.2f} TB" if free >= 1024 ** 4 else f"{free / (1024 ** 3):.1f} GB"
            self._set_status("movies", f"Ready - {free_text} free", ready=True)
            messagebox.showinfo(APP_NAME, f"Destination is reachable and writable:\n\n{path}\n\nFree space: {free_text}")
        except Exception as exc:
            self._set_status("movies", "Write test failed")
            messagebox.showerror(APP_NAME, f"Destination could not be written to:\n\n{path}\n\n{exc}")

    def save_settings(self):
        for key in ["movies","staging","makemkv","ffmpeg","ffprobe","handbrake"]:
            setattr(self.settings,key,getattr(self,"var_"+key).get().strip())
        self.settings.dvd_source=self._resolved_dvd_source()
        self.settings.tmdb_token=self.var_tmdb_token.get().strip()
        try: self.settings.crf=int(self.var_crf.get())
        except ValueError: self.settings.crf=18
        self.settings.preset=self.var_preset.get()
        self.settings.save(); self.refresh_setup_status(); self.log("Settings saved.")
        messagebox.showinfo(APP_NAME,"Settings saved.")

    def runner(self): return Runner(self.settings,self.log,self.set_progress)
    def log(self,msg): self.q.put(("log",str(msg)))
    def set_progress(self,v): self.q.put(("progress",float(v)))
    def done(self,msg=None): self.q.put(("done",msg))
    def error(self,e): self.q.put(("error",str(e)))

    def _drain(self):
        try:
            while True:
                kind,val=self.q.get_nowait()
                if kind=="log":
                    self.logbox.configure(state="normal"); self.logbox.insert("end",val+"\n"); self.logbox.see("end"); self.logbox.configure(state="disabled")
                elif kind=="progress": self.progress_var.set(val)
                elif kind=="done":
                    self.progress_var.set(0)
                    if val: messagebox.showinfo(APP_NAME,val)
                elif kind=="error":
                    self.progress_var.set(0); messagebox.showerror(APP_NAME,val)
        except queue.Empty: pass
        self.after(100,self._drain)

    def threaded(self,fn):
        def work():
            try: fn()
            except Exception as e: self.error(e)
        threading.Thread(target=work,daemon=True).start()

    def scan(self):
        self.save_settings_silent()
        def work():
            titles,hint=self.runner().scan_disc(); self.disc_titles=titles; self.disc_hint=hint
            self.q.put(("scan_result",(titles,hint)))
        # special thread to push ui callback safely
        def w():
            try:
                titles,hint=self.runner().scan_disc(); self.after(0,lambda:self.show_scan(titles,hint))
            except Exception as e: self.error(e)
        threading.Thread(target=w,daemon=True).start()

    def show_scan(self,titles,hint):
        self.disc_titles=titles; self.disc_hint=hint
        for x in self.title_tree.get_children(): self.title_tree.delete(x)
        for t in titles:
            label=t.source or t.output_name or t.name
            self.title_tree.insert("","end",iid=str(t.title_id),values=(t.title_id,t.duration,t.size,t.chapters,label))
        if titles:
            suggested=max(titles,key=lambda x:(x.size_bytes,x.duration_seconds))
            self.title_tree.selection_set(str(suggested.title_id)); self.title_tree.focus(str(suggested.title_id))
        self.disc_label.configure(text=(f"Disc suggestion: {hint}" if hint else f"Found {len(titles)} title(s)."))
        self.log(f"DVD scan complete: {len(titles)} title(s) found.")

    def metadata_dialog(self,suggested):
        dlg=MetadataDialog(self,self.runner(),suggested); self.wait_window(dlg); return dlg.result

    def rip_selected(self):
        self.save_settings_silent()
        ids=[int(x) for x in self.title_tree.selection()]
        if not ids:
            messagebox.showwarning(APP_NAME,"Select at least one DVD title."); return
        chosen=[next(t for t in self.disc_titles if t.title_id==i) for i in ids]
        metas=[]
        for i,t in enumerate(chosen):
            suggestion=self.disc_hint if len(chosen)==1 else (t.name or "")
            meta=self.metadata_dialog(suggestion)
            if not meta: return
            metas.append(meta)
        mode=self.mode.get()
        plan="\n".join(f"DVD title {t.title_id} -> {m.title} ({m.year}) [tmdbid-{m.tmdb_id}]" for t,m in zip(chosen,metas))
        if not messagebox.askyesno(APP_NAME,f"Start this rip?\n\n{plan}\n\n{mode}"): return
        def work():
            r=self.runner()
            failures=[]
            for t,m in zip(chosen,metas):
                try: r.rip_title(t,m,mode)
                except Exception as e:
                    self.log(f"FAILED title {t.title_id}: {e}"); failures.append(f"Title {t.title_id}: {e}")
            if failures: raise RuntimeError("Some titles failed:\n"+"\n".join(failures))
            r.eject_disc()
            self.done("Rip completed and verified on the NAS. DVD ejected.")
        self.threaded(work)

    def search_movies(self):
        self.save_settings_silent()
        root=Path(self.settings.movies); q=self.movie_query.get().lower().strip()
        for x in self.movie_tree.get_children(): self.movie_tree.delete(x)
        if not self.settings.movies.strip(): messagebox.showerror(APP_NAME,"Media Library Destination is not configured. Open Settings first."); return
        if not root.exists(): messagebox.showerror(APP_NAME,f"Media Library Destination unavailable:\n{root}"); return
        try:
            matches=[]
            for p in root.rglob("*.mkv"):
                text=(p.parent.name+" "+p.name).lower()
                if not q or all(term in text for term in q.split()): matches.append(p)
                if len(matches)>=100: break
            for i,p in enumerate(matches): self.movie_tree.insert("","end",iid=str(i),values=(str(p),))
            self.log(f"Found {len(matches)} matching MKV file(s).")
        except Exception as e: messagebox.showerror(APP_NAME,str(e))

    def start_upscale(self):
        sel=self.movie_tree.selection()
        if not sel: messagebox.showwarning(APP_NAME,"Select an MKV first."); return
        source=Path(self.movie_tree.item(sel[0],"values")[0])
        if not messagebox.askyesno(APP_NAME,f"Create a 1080p version from:\n\n{source.name}\n\nThe original will be preserved."): return
        self.save_settings_silent()
        def work(): self.runner().add_1080(source); self.done("1080p Jellyfin version added and verified.")
        self.threaded(work)

    def save_settings_silent(self):
        for key in ["movies","staging","makemkv","ffmpeg","ffprobe","handbrake"]:
            if hasattr(self,"var_"+key): setattr(self.settings,key,getattr(self,"var_"+key).get().strip())
        if hasattr(self,"var_dvd_source"): self.settings.dvd_source=self._resolved_dvd_source()
        if hasattr(self,"var_tmdb_token"): self.settings.tmdb_token=self.var_tmdb_token.get().strip()
        if hasattr(self,"var_crf"):
            try:self.settings.crf=int(self.var_crf.get())
            except ValueError:self.settings.crf=18
        if hasattr(self,"var_preset"): self.settings.preset=self.var_preset.get()
        self.settings.save()


def main():
    if sys.version_info < (3,10):
        raise SystemExit("RipFoundry requires Python 3.10 or newer.")
    configure_windows_app_id()
    App().mainloop()

if __name__ == "__main__": main()
