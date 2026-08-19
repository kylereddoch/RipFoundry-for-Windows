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
APP_VERSION = "1.3.0"
REPOSITORY_URL = "https://github.com/kylereddoch/RipFoundry-for-Windows"
CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / "RipFoundry"
CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULT_STAGING = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Videos" / "RipFoundry Staging"
DEFAULT_MOVIES = ""
INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

MODE_ORIGINAL = "Original DVD only"
MODE_ENHANCED = "Original DVD + Enhanced DVD"
MODE_UPSCALE = "Original DVD + 1080p"

PROCESS_ENHANCED = "Enhanced native-resolution only"
PROCESS_UPSCALE = "1080p only"
PROCESS_BOTH = "Enhanced + 1080p"
PROCESS_NONE = "No additional encode"
SUPPORTED_VIDEO_SUFFIXES = {".mkv", ".mp4", ".m4v"}
JELLYFIN_EXTRA_FOLDERS = (
    "featurettes",
    "behind the scenes",
    "deleted scenes",
    "interviews",
    "scenes",
    "shorts",
    "clips",
    "trailers",
    "other",
)


class JobCancelled(RuntimeError):
    """Raised when the user cancels the active RipFoundry job."""

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


def _choose_windows_folder(parent, initialdir=None, title="Choose a folder") -> str:
    """Show the modern Windows Explorer folder picker using IFileOpenDialog."""
    import ctypes
    import uuid
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

        @classmethod
        def from_string(cls, value):
            raw = uuid.UUID(value).bytes_le
            return cls.from_buffer_copy(raw)

    HRESULT = ctypes.c_long
    ole32 = ctypes.OleDLL("ole32")
    shell32 = ctypes.OleDLL("shell32")
    coinit_result = ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
    rpc_e_changed_mode = ctypes.c_long(0x80010106).value
    if coinit_result not in (0, 1, rpc_e_changed_mode):
        raise OSError(f"Windows could not initialize the folder picker (0x{coinit_result & 0xffffffff:08X}).")
    should_uninitialize = coinit_result in (0, 1)

    dialog = ctypes.c_void_p()
    initial_item = ctypes.c_void_p()
    result_item = ctypes.c_void_p()
    display_name = ctypes.c_void_p()

    def com_method(pointer, index, restype, *argtypes):
        vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtable[index])

    def release(pointer):
        if pointer and pointer.value:
            com_method(pointer, 2, wintypes.ULONG)(pointer)
            pointer.value = None

    def require_success(result, action):
        if result < 0:
            raise OSError(f"Windows could not {action} (0x{result & 0xffffffff:08X}).")

    try:
        clsid_file_open_dialog = GUID.from_string("DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7")
        iid_file_open_dialog = GUID.from_string("D57C7288-D4AD-4768-BE02-9D969532D960")
        iid_shell_item = GUID.from_string("43826D1E-E718-42EE-BC55-A1E261C37BFE")
        ole32.CoCreateInstance.argtypes = [
            ctypes.POINTER(GUID), ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p),
        ]
        ole32.CoCreateInstance.restype = HRESULT
        require_success(
            ole32.CoCreateInstance(
                ctypes.byref(clsid_file_open_dialog), None, 0x1,
                ctypes.byref(iid_file_open_dialog), ctypes.byref(dialog),
            ),
            "open the folder picker",
        )

        options = wintypes.DWORD()
        require_success(
            com_method(dialog, 10, HRESULT, ctypes.POINTER(wintypes.DWORD))(
                dialog, ctypes.byref(options)
            ),
            "read folder picker options",
        )
        # FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST | FOS_NOCHANGEDIR
        options.value |= 0x20 | 0x40 | 0x800 | 0x8
        require_success(
            com_method(dialog, 9, HRESULT, wintypes.DWORD)(dialog, options.value),
            "configure the folder picker",
        )
        require_success(
            com_method(dialog, 17, HRESULT, wintypes.LPCWSTR)(dialog, title),
            "set the folder picker title",
        )
        require_success(
            com_method(dialog, 18, HRESULT, wintypes.LPCWSTR)(dialog, "Select Folder"),
            "label the folder picker button",
        )

        initial_path = Path(initialdir).expanduser() if initialdir else None
        if initial_path and initial_path.is_dir():
            shell32.SHCreateItemFromParsingName.argtypes = [
                wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(GUID),
                ctypes.POINTER(ctypes.c_void_p),
            ]
            shell32.SHCreateItemFromParsingName.restype = HRESULT
            if shell32.SHCreateItemFromParsingName(
                str(initial_path), None, ctypes.byref(iid_shell_item), ctypes.byref(initial_item)
            ) >= 0:
                com_method(dialog, 12, HRESULT, ctypes.c_void_p)(dialog, initial_item)

        owner = parent.winfo_id() if parent is not None else 0
        result = com_method(dialog, 3, HRESULT, wintypes.HWND)(dialog, owner)
        if result == ctypes.c_long(0x800704C7).value:  # ERROR_CANCELLED
            return ""
        require_success(result, "show the folder picker")
        require_success(
            com_method(dialog, 20, HRESULT, ctypes.POINTER(ctypes.c_void_p))(
                dialog, ctypes.byref(result_item)
            ),
            "read the selected folder",
        )
        require_success(
            com_method(result_item, 5, HRESULT, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))(
                result_item, 0x80058000, ctypes.byref(display_name)
            ),
            "read the selected folder path",
        )
        return ctypes.wstring_at(display_name)
    finally:
        if display_name.value:
            ole32.CoTaskMemFree(display_name)
        release(result_item)
        release(initial_item)
        release(dialog)
        if should_uninitialize:
            ole32.CoUninitialize()


def choose_folder(parent=None, initialdir=None, title="Choose a folder") -> str:
    """Choose a folder with the modern Windows picker and a portable fallback."""
    if os.name == "nt":
        try:
            return _choose_windows_folder(parent, initialdir, title)
        except (OSError, ValueError):
            pass
    return filedialog.askdirectory(parent=parent, initialdir=initialdir, title=title)


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
class ExtraMetadata:
    name: str
    folder: str


@dataclass
class StagedExtra:
    disc_title: DiscTitle
    path: Path


@dataclass
class StagedExtrasBatch:
    root: Path
    items: list[StagedExtra]


@dataclass
class MediaInfo:
    codec: str
    duration: float
    width: int
    height: int
    dar: str
    audio_tracks: int
    subtitle_tracks: int
    field_order: str = "unknown"
    container: str = "unknown"


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


def parse_makemkv_scan_output(output: str):
    """Parse MakeMKV robot-mode disc and title records."""
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
            except (csv.Error, IndexError, StopIteration, TypeError, ValueError):
                continue
        elif line.startswith("CINFO:"):
            try:
                row = next(csv.reader([line[len("CINFO:"):]], escapechar="\\"))
                aid = int(row[0])
                val = row[2] if len(row) > 2 else ""
                if aid in {2, 30, 32} and val.strip():
                    labels.append(val.strip())
            except (csv.Error, IndexError, StopIteration, TypeError, ValueError):
                continue

    titles = []
    for tid, info in sorted(attrs.items()):
        duration = info.get(9, "")
        if not duration:
            continue
        try:
            size_bytes = int(info.get(11, "0") or "0")
        except ValueError:
            size_bytes = 0
        titles.append(DiscTitle(
            tid,
            info.get(2, ""),
            info.get(8, ""),
            duration,
            parse_duration(duration),
            info.get(10, ""),
            size_bytes,
            info.get(16, ""),
            info.get(27, ""),
        ))

    label = next((x for x in labels if x.lower() not in {"dvd", "dvd disc"}), "")
    return titles, clean_disc_label(label)


def sanitize_title(value: str) -> str:
    value = INVALID_FILENAME_CHARS.sub("-", value)
    value = re.sub(r"\s+", " ", value).strip().rstrip(". ")
    return value or "Untitled"


def movie_library_base(meta: MovieMetadata) -> str:
    return f"{sanitize_title(meta.title)} ({meta.year}) [tmdbid-{meta.tmdb_id}]"


def resolution_label(info: MediaInfo) -> str:
    if info.height <= 500: return "480p"
    if info.height <= 600: return "576p"
    if info.height <= 760: return "720p"
    if info.height <= 1100: return "1080p"
    if info.height <= 2200: return "2160p"
    return f"{info.height}p"


def is_supported_video(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_VIDEO_SUFFIXES


def existing_video_base(source: Path) -> str:
    folder_name = source.parent.name
    if folder_name and source.name.lower().startswith(folder_name.lower()):
        return folder_name
    stem = re.sub(r"\s+-\s+\d+p(?:\s+Enhanced)?$", "", source.stem, flags=re.I)
    return sanitize_title(stem)


def is_interlaced(info: MediaInfo) -> bool:
    return info.field_order.lower() in {"tt", "bb", "tb", "bt"}


def recommend_existing_processing(info: MediaInfo) -> tuple[str, str]:
    codec = info.codec.lower()
    if info.height <= 600:
        if is_interlaced(info) or codec != "h264":
            return (
                PROCESS_BOTH,
                "The source is SD and either interlaced or not H.264. Keep a cleaned native-resolution "
                "version and create a separate 1080p compatibility version.",
            )
        return (
            PROCESS_UPSCALE,
            "The source is progressive H.264 at SD resolution, so a separate native-resolution re-encode "
            "would add compression without a clear benefit.",
        )
    if info.height < 1000:
        return (
            PROCESS_UPSCALE,
            "The source is below 1080p and does not need a separate native-resolution cleanup encode.",
        )
    if info.height <= 1100:
        if is_interlaced(info):
            return (
                PROCESS_ENHANCED,
                "The source is already near 1080p but is flagged as interlaced. A native-resolution cleanup "
                "encode is the useful output.",
            )
        if codec != "h264":
            return (
                PROCESS_ENHANCED,
                f"The source is already near 1080p but uses {codec or 'an unknown codec'} video. A native-resolution "
                "H.264 version is the useful output.",
            )
        return (
            PROCESS_NONE,
            "The source is already progressive H.264 at approximately 1080p, so another encode would add "
            "compression without a clear playback benefit.",
        )
    return (
        PROCESS_UPSCALE,
        "The source is above 1080p. A separate 1080p version can reduce playback and bandwidth requirements "
        "while preserving the original.",
    )


def analysis_summary(source: Path, info: MediaInfo, recommendation: str, reason: str) -> str:
    return (
        f"File: {source.name}\n"
        f"Container: {info.container}\n"
        f"Video: {info.codec or 'unknown'}, {info.width}x{info.height} ({resolution_label(info)})\n"
        f"Display aspect ratio: {info.dar or 'unknown'}\n"
        f"Field order: {info.field_order or 'unknown'}\n"
        f"Audio tracks: {info.audio_tracks}\n"
        f"Subtitle tracks: {info.subtitle_tracks}\n\n"
        f"Recommendation: {recommendation}\n"
        f"Reason: {reason}"
    )


def parse_ffmpeg_progress(line: str, duration: float) -> float | None:
    """Convert FFmpeg -progress output to a source-duration percentage."""
    value = line.strip()
    if value == "progress=end":
        return 100.0
    microseconds = None
    for key in ("out_time_us=", "out_time_ms="):
        if value.startswith(key):
            try:
                microseconds = int(value[len(key):])
            except ValueError:
                return None
            break
    if microseconds is not None:
        if duration <= 0:
            return None
        return max(0.0, min(100.0, microseconds / 1_000_000 / duration * 100))
    if value.startswith("out_time="):
        try:
            hours, minutes, seconds = value[len("out_time="):].split(":")
            elapsed = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        except (TypeError, ValueError):
            return None
        if duration <= 0:
            return None
        return max(0.0, min(100.0, elapsed / duration * 100))
    return None


def is_ffmpeg_progress_line(line: str) -> bool:
    key = line.strip().partition("=")[0]
    return key in {
        "bitrate", "drop_frames", "dup_frames", "encoder", "fps", "frame",
        "out_time", "out_time_ms", "out_time_us", "progress", "speed",
        "stream_0_0_q", "total_size",
    }


def parse_handbrake_progress(line: str) -> float | None:
    match = re.search(r"Encoding:.*?([0-9]+(?:\.[0-9]+)?)\s*%", line, flags=re.I)
    if not match:
        return None
    return max(0.0, min(100.0, float(match.group(1))))


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


def sha256_file(path: Path, callback=None, cancel_check=None) -> str:
    h = hashlib.sha256()
    total = path.stat().st_size
    done = 0
    with path.open("rb") as f:
        while True:
            if cancel_check:
                cancel_check()
            chunk = f.read(8 * 1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
            done += len(chunk)
            if callback and total:
                callback(done, total)
    return h.hexdigest()


def copy_verified(source: Path, destination: Path, log, progress=None, phase=None, cancel_check=None) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".partial")
    if partial.exists():
        partial.unlink()
    if phase:
        phase(f"Copying to library: {destination.name}")
    if progress:
        progress(0)
    log(f"Copying to media library: {destination}")
    total = source.stat().st_size
    done = 0
    try:
        with source.open("rb") as src, partial.open("wb") as dst:
            while True:
                if cancel_check:
                    cancel_check()
                chunk = src.read(8 * 1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)
                done += len(chunk)
                if progress and total:
                    progress(done / total * 100)
        log("Computing SHA-256 on local and destination copies...")
        if phase:
            phase(f"Verifying checksums: {destination.name}")
        if progress:
            progress(0)
        local_hash = sha256_file(
            source,
            (lambda done, total: progress(done / total * 50)) if progress else None,
            cancel_check,
        )
        remote_hash = sha256_file(
            partial,
            (lambda done, total: progress(50 + done / total * 50)) if progress else None,
            cancel_check,
        )
    except JobCancelled:
        partial.unlink(missing_ok=True)
        raise
    log(f"Local SHA-256: {local_hash}")
    log(f"Dest. SHA-256: {remote_hash}")
    if local_hash != remote_hash:
        partial.unlink(missing_ok=True)
        raise RuntimeError("Checksum mismatch. Local staging file was preserved.")
    if cancel_check:
        try:
            cancel_check()
        except JobCancelled:
            partial.unlink(missing_ok=True)
            raise
    os.replace(partial, destination)
    if progress:
        progress(100)
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
    def __init__(self, settings: Settings, log, progress, phase=None, cancel_event=None):
        self.s = settings
        self.log = log
        self.progress = progress
        self.phase = phase or (lambda _message: None)
        self.cancel_event = cancel_event or threading.Event()
        self.current_process = None
        self.process_lock = threading.Lock()
        self.cancel_cleanup_paths = set()

    def check_cancelled(self):
        if self.cancel_event.is_set():
            raise JobCancelled("The active job was cancelled.")

    def cancel(self):
        self.cancel_event.set()
        with self.process_lock:
            process = self.current_process
        if process and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    def register_cancel_cleanup(self, path: Path):
        self.cancel_cleanup_paths.add(Path(path))

    def finish_cancel_cleanup(self, path: Path):
        self.cancel_cleanup_paths.discard(Path(path))

    def cleanup_cancelled_job(self):
        staging_root = Path(self.s.staging).resolve()
        for path in list(self.cancel_cleanup_paths):
            try:
                resolved = path.resolve()
                if resolved != staging_root and staging_root in resolved.parents:
                    shutil.rmtree(resolved, ignore_errors=True)
            finally:
                self.cancel_cleanup_paths.discard(path)

    def begin_phase(self, message, initial_progress=0):
        self.check_cancelled()
        self.phase(message)
        self.progress(initial_progress)

    def run(self, args, capture=False, check=True, progress_parser=None, progress_line_filter=None):
        self.check_cancelled()
        self.log("$ " + subprocess.list2cmdline([str(x) for x in args]))
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" and capture else 0
        if capture:
            command = [str(x) for x in args]
            p = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 creationflags=flags)
            with self.process_lock:
                self.current_process = p
            stdout = ""
            stderr = ""
            try:
                while True:
                    try:
                        stdout, stderr = p.communicate(timeout=0.2)
                        break
                    except subprocess.TimeoutExpired:
                        self.check_cancelled()
            finally:
                if self.cancel_event.is_set() and p.poll() is None:
                    try:
                        p.terminate()
                        p.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        p.kill()
                        p.wait()
                    except OSError:
                        pass
                with self.process_lock:
                    if self.current_process is p:
                        self.current_process = None
            self.check_cancelled()
            result = subprocess.CompletedProcess(command, p.returncode, stdout, stderr)
            if check and result.returncode != 0:
                raise RuntimeError((result.stderr or result.stdout or "Command failed").strip())
            return result
        p = subprocess.Popen([str(x) for x in args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, bufsize=1, universal_newlines=True,
                             creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
        with self.process_lock:
            self.current_process = p
        recent_output = []
        try:
            assert p.stdout
            for line in p.stdout:
                self.check_cancelled()
                text = line.rstrip()
                recent_output.append(text)
                if len(recent_output) > 20:
                    recent_output.pop(0)
                percent = progress_parser(text) if progress_parser else None
                if percent is not None:
                    self.progress(percent)
                if not progress_line_filter or not progress_line_filter(text):
                    self.log(text)
            rc = p.wait()
        finally:
            if p.stdout:
                p.stdout.close()
            if self.cancel_event.is_set() and p.poll() is None:
                try:
                    p.terminate()
                    p.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
                except OSError:
                    pass
            with self.process_lock:
                if self.current_process is p:
                    self.current_process = None
        self.check_cancelled()
        if check and rc != 0:
            details = "\n".join(recent_output[-8:]).strip()
            message = f"Command exited with code {rc}"
            if details:
                message += f"\n\n{details}"
            raise RuntimeError(message)
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

    def makemkv_title_command(self, title_id: int, output: Path) -> list[str]:
        # MakeMKV assigns title indexes after applying its minimum-length filter.
        # Use the scan threshold again so the selected index still identifies the
        # same title, including extras shorter than MakeMKV's 120-second default.
        return [
            self.s.makemkv,
            f"--minlength={self.s.scan_min_length}",
            "mkv",
            self.s.dvd_source,
            str(title_id),
            str(output),
        ]

    def scan_disc(self):
        self.require("makemkv")
        p = self.run([self.s.makemkv, "-r", f"--minlength={self.s.scan_min_length}",
                      "info", self.s.dvd_source], capture=True, check=False)
        output = (p.stdout or "") + "\n" + (p.stderr or "")
        if p.returncode != 0:
            raise RuntimeError("MakeMKV could not scan the DVD.\n" + output[-2000:])
        titles, label = parse_makemkv_scan_output(output)
        if not titles: raise RuntimeError("No usable DVD titles were found.")
        return titles, label

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
        container = str((data.get("format") or {}).get("format_name") or "unknown")
        return MediaInfo(str(video.get("codec_name") or ""), duration,
                         int(video.get("width") or 0), int(video.get("height") or 0),
                         str(video.get("display_aspect_ratio") or ""),
                         sum(1 for x in streams if x.get("codec_type") == "audio"),
                         sum(1 for x in streams if x.get("codec_type") == "subtitle"),
                         str(video.get("field_order") or "unknown"), container)

    def enhanced(self, source: Path, output: Path, duration=None):
        self.require("handbrake")
        output.unlink(missing_ok=True)
        self.begin_phase(f"Encoding Enhanced version: {output.name}")
        args = [self.s.handbrake, "-i", str(source), "-o", str(output),
                "-f", "av_mkv", "-e", "x264", "-q", str(self.s.crf), "--encoder-preset", self.s.hb_preset,
                "--comb-detect", "--decomb", "--vfr",
                "--all-audio", "--aencoder", "copy",
                "--audio-copy-mask", "aac,ac3,eac3,truehd,dts,dtshd,mp2,mp3,flac",
                "--audio-fallback", "ac3",
                "--all-subtitles", "--markers", "--non-anamorphic"]
        self.run(args, progress_parser=parse_handbrake_progress,
                 progress_line_filter=lambda line: parse_handbrake_progress(line) is not None)
        if not output.exists(): raise RuntimeError("HandBrake did not create the Enhanced DVD output.")
        self.progress(100)

    def upscale(self, source: Path, output: Path, duration=None):
        self.require("ffmpeg")
        output.unlink(missing_ok=True)
        if duration is None:
            duration = self.ffprobe(source).duration
        self.begin_phase(f"Encoding 1080p version: {output.name}")
        vf = "bwdif=mode=send_frame:parity=auto:deint=interlaced,scale=w='trunc(1080*dar/2)*2':h=1080:flags=lanczos,setsar=1"
        subtitle_codec = "srt" if source.suffix.lower() in {".mp4", ".m4v"} else "copy"
        args = [self.s.ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", str(source),
                "-map", "0:v:0", "-map", "0:a?", "-map", "0:s?", "-map_metadata", "0", "-map_chapters", "0",
                "-vf", vf, "-c:v", "libx264", "-preset", self.s.preset, "-crf", str(self.s.crf),
                "-pix_fmt", "yuv420p", "-c:a", "copy", "-c:s", subtitle_codec,
                "-max_muxing_queue_size", "4096", "-progress", "pipe:1", "-nostats", str(output)]
        parser = lambda line: parse_ffmpeg_progress(line, duration)
        rc = self.run(args, check=False, progress_parser=parser, progress_line_filter=is_ffmpeg_progress_line)
        if rc != 0:
            # Retry with yadif on builds lacking bwdif.
            vf = "yadif=mode=send_frame:parity=auto:deint=interlaced,scale=w='trunc(1080*dar/2)*2':h=1080:flags=lanczos,setsar=1"
            args[args.index("-vf") + 1] = vf
            self.log("Retrying with yadif deinterlacing...")
            self.begin_phase(f"Retrying 1080p encoding: {output.name}")
            self.run(args, progress_parser=parser, progress_line_filter=is_ffmpeg_progress_line)
        if not output.exists(): raise RuntimeError("FFmpeg did not create the 1080p output.")
        self.progress(100)

    def validate_processed(self, source_info, output, target="1080"):
        self.begin_phase(f"Validating output: {Path(output).name}")
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
        self.progress(100)
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
        base = movie_library_base(meta)
        job = staging_root / "rip-jobs" / f"{stamp}-title-{disc_title.title_id}"
        job.mkdir(parents=True, exist_ok=True)
        self.register_cancel_cleanup(job)
        self.log(f"Ripping DVD title {disc_title.title_id}: {base}")
        self.run(self.makemkv_title_command(disc_title.title_id, job))
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
            self.enhanced(raw_named, processed, info.duration)
            self.validate_processed(info, processed, target="native")
            processed_name = processed.name
        elif mode == MODE_UPSCALE:
            processed = job / f"{base} - 1080p.mkv"
            self.upscale(raw_named, processed, info.duration)
            self.validate_processed(info, processed, target="1080")
            processed_name = processed.name
        dest_dir = movies_root / base
        if (dest_dir / raw_named.name).exists(): raise RuntimeError(f"Destination already exists: {dest_dir / raw_named.name}")
        if processed and (dest_dir / processed.name).exists(): raise RuntimeError(f"Destination already exists: {dest_dir / processed.name}")
        copy_verified(raw_named, dest_dir / raw_named.name, self.log, self.progress, self.phase, self.check_cancelled)
        if processed:
            copy_verified(processed, dest_dir / processed.name, self.log, self.progress, self.phase, self.check_cancelled)
        raw_named.unlink(missing_ok=True)
        if processed: processed.unlink(missing_ok=True)
        try: job.rmdir()
        except OSError: pass
        self.finish_cancel_cleanup(job)
        self.log(f"COMPLETE: {dest_dir}")
        self.log(f"  {raw_named.name}")
        if processed_name: self.log(f"  {processed_name}")

    def stage_extra_titles(self, disc_titles: list[DiscTitle], meta: MovieMetadata) -> StagedExtrasBatch:
        self.require("makemkv", "ffprobe")
        if not disc_titles:
            raise RuntimeError("Select at least one DVD extra.")
        staging_root = Path(self.s.staging)
        staging_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        review_root = staging_root / "extra-review" / f"{stamp}-{sanitize_title(meta.title)}"
        review_root.mkdir(parents=True, exist_ok=False)
        self.register_cancel_cleanup(review_root)
        staged = []
        total = len(disc_titles)
        for index, disc_title in enumerate(disc_titles, start=1):
            self.begin_phase(
                f"Ripping DVD extra {index} of {total}: title {disc_title.title_id} ({disc_title.duration})",
                (index - 1) / total * 100,
            )
            title_root = review_root / f"title-{disc_title.title_id}"
            title_root.mkdir()
            self.run(self.makemkv_title_command(disc_title.title_id, title_root))
            mkvs = list(title_root.glob("*.mkv"))
            if len(mkvs) != 1:
                raise RuntimeError(
                    f"Expected one MKV for DVD title {disc_title.title_id}; found {len(mkvs)}."
                )
            source = mkvs[0]
            info = self.ffprobe(source)
            if info.duration <= 0:
                raise RuntimeError(f"DVD title {disc_title.title_id} failed duration validation.")
            staged_path = review_root / f"DVD Title {disc_title.title_id} - {disc_title.duration.replace(':', '-')}.mkv"
            source.rename(staged_path)
            title_root.rmdir()
            staged.append(StagedExtra(disc_title, staged_path))
            self.progress(index / total * 100)
        self.finish_cancel_cleanup(review_root)
        self.log(f"DVD extras are ready for review: {review_root}")
        return StagedExtrasBatch(review_root, staged)

    def publish_extras(
        self,
        batch: StagedExtrasBatch,
        meta: MovieMetadata,
        plans: list[ExtraMetadata],
    ) -> list[Path]:
        if len(batch.items) != len(plans):
            raise RuntimeError("The number of extra names does not match the staged DVD titles.")
        if not self.s.movies.strip():
            raise RuntimeError("Media Library Destination is not configured. Open Settings first.")
        movies_root = Path(self.s.movies)
        if not movies_root.exists():
            raise RuntimeError(f"Media Library Destination is unavailable: {movies_root}")
        movie_root = movies_root / movie_library_base(meta)
        prepared = []
        unique_destinations = set()
        for staged, plan in zip(batch.items, plans):
            folder = plan.folder.strip().lower()
            if folder not in JELLYFIN_EXTRA_FOLDERS:
                raise RuntimeError(f"Unsupported Jellyfin extras folder: {plan.folder}")
            name = sanitize_title(plan.name)
            destination = movie_root / folder / f"{name}.mkv"
            destination_key = str(destination).casefold()
            if destination_key in unique_destinations:
                raise RuntimeError(f"Two selected extras would use the same destination: {destination}")
            if destination.exists():
                raise RuntimeError(f"Destination already exists: {destination}")
            if not staged.path.is_file():
                raise RuntimeError(f"Staged DVD extra is missing: {staged.path}")
            unique_destinations.add(destination_key)
            prepared.append((staged, destination))

        completed = []
        total = len(prepared)
        try:
            for index, (staged, destination) in enumerate(prepared, start=1):
                self.begin_phase(f"Adding extra {index} of {total}: {destination.name}")
                copy_verified(
                    staged.path,
                    destination,
                    self.log,
                    lambda value, index=index: self.progress(((index - 1) + value / 100) / total * 100),
                    self.phase,
                    self.check_cancelled,
                )
                completed.append(destination)
        except Exception:
            # The destinations were preflighted as new files, so removing any
            # files finalized by this batch restores the library to its original
            # state while leaving every staged source available for another try.
            for _staged, destination in prepared:
                destination.with_name(destination.name + ".partial").unlink(missing_ok=True)
            for destination in completed:
                destination.unlink(missing_ok=True)
                try:
                    destination.parent.rmdir()
                except OSError:
                    pass
            raise

        for staged, _destination in prepared:
            staged.path.unlink(missing_ok=True)
        try:
            batch.root.rmdir()
            batch.root.parent.rmdir()
        except OSError:
            pass
        self.log("DVD EXTRAS COMPLETE")
        self.log(f"Movie: {movie_root}")
        for destination in completed:
            self.log(f"Extra: {destination.relative_to(movie_root)}")
        return completed

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
        self.register_cancel_cleanup(stage)
        encoded = stage / f"{sanitize_title(base)} - 1080p.mkv"
        self.upscale(source, encoded, source_info.duration)
        encoded_info = self.validate_processed(source_info, encoded, target="1080")
        # Rename old single-version file only after successful encode.
        if original_dest != source:
            if original_dest.exists(): raise RuntimeError(f"Cannot rename original; destination exists: {original_dest}")
            source.rename(original_dest)
            source = original_dest
        copy_verified(encoded, target, self.log, self.progress, self.phase, self.check_cancelled)
        final = self.ffprobe(target)
        if final.height != encoded_info.height or abs(final.duration - encoded_info.duration) > 2:
            target.unlink(missing_ok=True)
            raise RuntimeError("1080p file failed verification after finalization; NAS copy removed.")
        encoded.unlink(missing_ok=True)
        try: stage.rmdir()
        except OSError: pass
        self.finish_cancel_cleanup(stage)
        self.log("1080p VERSION ADDED")
        self.log(f"Original: {source.name}")
        self.log(f"1080p:    {target.name}")

    def analyze_existing(self, source: Path):
        self.require("ffprobe")
        if not source.exists():
            raise RuntimeError("Source video no longer exists.")
        if not is_supported_video(source):
            raise RuntimeError("Source must be an MKV, MP4, or M4V file.")
        info = self.ffprobe(source)
        if info.width <= 0 or info.height <= 0 or info.duration <= 0:
            raise RuntimeError("FFprobe could not identify a valid video stream and duration.")
        recommendation, reason = recommend_existing_processing(info)
        return info, recommendation, reason

    def process_existing(self, source: Path, mode: str):
        if mode not in {PROCESS_ENHANCED, PROCESS_UPSCALE, PROCESS_BOTH}:
            raise RuntimeError(f"Unsupported processing mode: {mode}")
        source_info, recommendation, reason = self.analyze_existing(source)
        self.log(analysis_summary(source, source_info, recommendation, reason))
        if mode in {PROCESS_ENHANCED, PROCESS_BOTH}:
            self.require("handbrake")
        if mode in {PROCESS_UPSCALE, PROCESS_BOTH}:
            self.require("ffmpeg")

        folder = source.parent
        base = existing_video_base(source)
        native = resolution_label(source_info)
        enhanced_target = folder / f"{base} - {native} Enhanced.mkv"
        upscale_target = folder / f"{base} - 1080p.mkv"

        if mode in {PROCESS_ENHANCED, PROCESS_BOTH}:
            if enhanced_target == source:
                raise RuntimeError("The selected source is already the Enhanced version.")
            if enhanced_target.exists():
                raise RuntimeError(f"Enhanced version already exists: {enhanced_target}")
        if mode in {PROCESS_UPSCALE, PROCESS_BOTH}:
            if upscale_target == source:
                raise RuntimeError("The selected source is already the 1080p version.")
            if upscale_target.exists():
                raise RuntimeError(f"A 1080p version already exists: {upscale_target}")

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        stage = Path(self.s.staging) / "process-video-jobs" / stamp
        stage.mkdir(parents=True, exist_ok=True)
        self.register_cancel_cleanup(stage)
        enhanced_encoded = None
        upscale_encoded = None

        if mode in {PROCESS_ENHANCED, PROCESS_BOTH}:
            enhanced_encoded = stage / f"{sanitize_title(base)} - {native} Enhanced.mkv"
            self.log(f"Encoding Enhanced version locally: {enhanced_encoded}")
            self.enhanced(source, enhanced_encoded, source_info.duration)
            self.validate_processed(source_info, enhanced_encoded, target="native")
        if mode in {PROCESS_UPSCALE, PROCESS_BOTH}:
            upscale_encoded = stage / f"{sanitize_title(base)} - 1080p.mkv"
            self.log(f"Encoding 1080p version locally: {upscale_encoded}")
            self.upscale(source, upscale_encoded, source_info.duration)
            self.validate_processed(source_info, upscale_encoded, target="1080")

        completed = []
        if enhanced_encoded:
            copy_verified(
                enhanced_encoded, enhanced_target, self.log, self.progress, self.phase, self.check_cancelled
            )
            try:
                self.validate_processed(source_info, enhanced_target, target="native")
            except Exception:
                enhanced_target.unlink(missing_ok=True)
                raise
            completed.append(enhanced_target)
        if upscale_encoded:
            copy_verified(
                upscale_encoded, upscale_target, self.log, self.progress, self.phase, self.check_cancelled
            )
            try:
                self.validate_processed(source_info, upscale_target, target="1080")
            except Exception:
                upscale_target.unlink(missing_ok=True)
                raise
            completed.append(upscale_target)

        if enhanced_encoded:
            enhanced_encoded.unlink(missing_ok=True)
        if upscale_encoded:
            upscale_encoded.unlink(missing_ok=True)
        try:
            stage.rmdir()
        except OSError:
            pass
        self.finish_cancel_cleanup(stage)
        self.log("EXISTING VIDEO PROCESSING COMPLETE")
        self.log(f"Original: {source.name} (unchanged)")
        for path in completed:
            self.log(f"Added:    {path.name}")
        return completed


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


class ExtrasReviewDialog(tk.Toplevel):
    def __init__(self, parent, batch: StagedExtrasBatch, meta: MovieMetadata):
        super().__init__(parent)
        self.title("Review and Name DVD Extras")
        self.geometry("980x560")
        self.minsize(820, 430)
        self.result = None
        self.rows = []
        self.transient(parent)
        self.grab_set()

        header = ttk.Frame(self, padding=(12, 12, 12, 6))
        header.pack(fill="x")
        ttk.Label(
            header,
            text=f"Extras for {movie_library_base(meta)}",
            font=("Segoe UI", 11, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            header,
            text=(
                "Play each staged title, enter the name shown on the DVD menu or packaging, and choose "
                "the Jellyfin extras folder. Extras do not receive their own TMDb match."
            ),
            wraplength=920,
            justify="left",
        ).pack(anchor="w", pady=(4, 0))

        container = ttk.Frame(self, padding=(12, 4, 12, 4))
        container.pack(fill="both", expand=True)
        canvas = tk.Canvas(container, highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        rows_frame = ttk.Frame(canvas)
        rows_frame.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        window = canvas.create_window((0, 0), window=rows_frame, anchor="nw")
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        for column, (text, width) in enumerate([
            ("DVD title", 19), ("Preview", 10), ("Extra name", 48), ("Jellyfin folder", 22)
        ]):
            ttk.Label(rows_frame, text=text, font=("Segoe UI", 9, "bold"), width=width).grid(
                row=0, column=column, sticky="w", padx=(0, 8), pady=(0, 6)
            )
        rows_frame.columnconfigure(2, weight=1)

        for row_number, staged in enumerate(batch.items, start=1):
            disc_title = staged.disc_title
            ttk.Label(
                rows_frame,
                text=f"{disc_title.title_id}  |  {disc_title.duration}  |  {disc_title.size}",
            ).grid(row=row_number, column=0, sticky="w", padx=(0, 8), pady=4)
            ttk.Button(
                rows_frame,
                text="Play",
                command=lambda path=staged.path: self.play(path),
                width=8,
            ).grid(row=row_number, column=1, sticky="w", padx=(0, 8), pady=4)
            name_var = tk.StringVar(value=f"DVD Title {disc_title.title_id}")
            folder_var = tk.StringVar(value="featurettes")
            ttk.Entry(rows_frame, textvariable=name_var).grid(
                row=row_number, column=2, sticky="ew", padx=(0, 8), pady=4
            )
            ttk.Combobox(
                rows_frame,
                textvariable=folder_var,
                values=JELLYFIN_EXTRA_FOLDERS,
                state="readonly",
                width=20,
            ).grid(row=row_number, column=3, sticky="w", pady=4)
            self.rows.append((name_var, folder_var))

        bottom = ttk.Frame(self, padding=12)
        bottom.pack(fill="x")
        ttk.Label(
            bottom,
            text=f"Cancel keeps the lossless staged MKVs at: {batch.root}",
            foreground="#5f6368",
            wraplength=650,
            justify="left",
        ).pack(side="left", fill="x", expand=True)
        ttk.Button(bottom, text="Cancel and Keep Staged Files", command=self.destroy).pack(side="right")
        ttk.Button(bottom, text="Add Extras to Library", command=self.accept).pack(side="right", padx=8)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def play(self, path: Path):
        try:
            os.startfile(str(path))
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not open the staged extra:\n\n{path}\n\n{exc}", parent=self)

    def accept(self):
        plans = []
        seen = set()
        for name_var, folder_var in self.rows:
            name = name_var.get().strip()
            folder = folder_var.get().strip().lower()
            if not name:
                messagebox.showwarning(APP_NAME, "Every DVD extra needs a descriptive name.", parent=self)
                return
            key = (folder, sanitize_title(name).casefold())
            if key in seen:
                messagebox.showwarning(
                    APP_NAME,
                    f"Two extras have the same name in the {folder} folder. Give each one a unique name.",
                    parent=self,
                )
                return
            seen.add(key)
            plans.append(ExtraMetadata(name, folder))
        self.result = plans
        self.destroy()


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
        self.analyzed_source = None
        self.analyzed_info = None
        self.analyzed_recommendation = None
        self.analyzed_reason = None
        self.job_running = False
        self.active_job_kind = None
        self.cancel_event = threading.Event()
        self.active_runner = None
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
        self.up_tab = ttk.Frame(nb, padding=10); nb.add(self.up_tab, text="Process Existing Video")
        self.set_tab = ttk.Frame(nb, padding=10); nb.add(self.set_tab, text="Settings")
        self.about_tab = ttk.Frame(nb, padding=16); nb.add(self.about_tab, text="About")
        self._build_rip(); self._build_up(); self._build_settings(); self._build_about()
        lf = ttk.LabelFrame(self, text="Activity", padding=6); lf.pack(fill="both", expand=False, padx=10, pady=(0,10))
        progress_header = ttk.Frame(lf); progress_header.pack(fill="x", pady=(0, 2))
        self.activity_status = tk.StringVar(value="Idle")
        self.activity_percent = tk.StringVar(value="0%")
        ttk.Label(progress_header, textvariable=self.activity_status).pack(side="left")
        ttk.Label(progress_header, textvariable=self.activity_percent).pack(side="right")
        self.cancel_job_button = ttk.Button(
            progress_header,
            text="Cancel Active Job",
            command=self.cancel_active_job,
            state="disabled",
        )
        self.cancel_job_button.pack(side="right", padx=(0, 10))
        self.progress_var = tk.DoubleVar(value=0)
        ttk.Progressbar(lf, variable=self.progress_var, maximum=100).pack(fill="x", pady=(0, 6))
        self.activity_log_frame = ttk.Frame(lf)
        self.activity_log_frame.pack(fill="both", expand=True)
        self.activity_log_scrollbar = ttk.Scrollbar(self.activity_log_frame, orient="vertical")
        self.logbox = tk.Text(
            self.activity_log_frame,
            height=6,
            wrap="word",
            state="disabled",
            yscrollcommand=self.activity_log_scrollbar.set,
        )
        self.activity_log_scrollbar.configure(command=self.logbox.yview)
        self.activity_log_scrollbar.pack(side="right", fill="y")
        self.logbox.pack(side="left", fill="both", expand=True)

    def _build_rip(self):
        top = ttk.Frame(self.rip_tab); top.pack(fill="x")
        ttk.Label(top, text="Insert a DVD, then scan it. Select one or more titles to rip.").pack(side="left")
        self.scan_button = ttk.Button(top, text="Scan DVD", command=self.scan)
        self.scan_button.pack(side="right")
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
        self.rip_selected_button = ttk.Button(opts,text="Rip Selected as Movie(s)",command=self.rip_selected)
        self.rip_selected_button.pack(side="right")
        self.rip_extras_button = ttk.Button(opts,text="Rip Selected as Extras",command=self.rip_selected_extras)
        self.rip_extras_button.pack(side="right", padx=(0, 8))
        mode_help = ttk.LabelFrame(self.rip_tab, text="What this option creates", padding=(10, 7))
        mode_help.pack(fill="x", pady=(0, 7))
        self.mode_help = ttk.Label(mode_help, wraplength=880, justify="left")
        self.mode_help.pack(anchor="w", fill="x")
        self._update_mode_help()
        ttk.Label(
            self.rip_tab,
            text=(
                "Movies get a TMDb match. Extras inherit one parent movie, then you preview, name, and "
                "place them in Jellyfin extras folders."
            ),
        ).pack(anchor="w")

    def _update_mode_help(self, _event=None):
        self.mode_help.configure(text=MODE_DESCRIPTIONS.get(self.mode.get(), ""))

    def _build_up(self):
        ttk.Label(
            self.up_tab,
            text="Analyze an existing MKV, MP4, or M4V and create an Enhanced version, a 1080p version, or both.",
        ).pack(anchor="w", pady=(0, 4))
        ttk.Label(
            self.up_tab,
            text="RipFoundry preserves the original and creates each selected output from that source.",
            foreground="#5f6368",
        ).pack(anchor="w", pady=(0, 12))
        row=ttk.Frame(self.up_tab); row.pack(fill="x")
        self.movie_query=tk.StringVar()
        ttk.Entry(row,textvariable=self.movie_query).pack(side="left",fill="x",expand=True)
        ttk.Button(row,text="Search Library",command=self.search_movies).pack(side="left",padx=(8,0))
        ttk.Button(row,text="Choose Video...",command=self.browse_existing_video).pack(side="left",padx=(8,0))
        self.movie_tree=ttk.Treeview(self.up_tab,columns=("path",),show="headings",selectmode="browse",height=8)
        self.movie_tree.heading("path",text="Matching MKV, MP4, and M4V files"); self.movie_tree.column("path",width=780)
        self.movie_tree.pack(fill="both",expand=True,pady=10)
        self.movie_tree.bind("<<TreeviewSelect>>", self._existing_selection_changed)
        analyze_row = ttk.Frame(self.up_tab); analyze_row.pack(fill="x", pady=(0, 8))
        ttk.Label(analyze_row, text="Step 1: Inspect the selected file before deciding what to create.").pack(side="left")
        self.analyze_existing_button = ttk.Button(
            analyze_row, text="Analyze Selected Video", command=self.analyze_selected_video
        )
        self.analyze_existing_button.pack(side="right")
        analysis_frame = ttk.LabelFrame(self.up_tab, text="Analysis and recommendation", padding=(10, 7))
        analysis_frame.pack(fill="x", pady=(0, 10))
        self.analysis_text_scrollbar = ttk.Scrollbar(analysis_frame, orient="vertical")
        self.analysis_text = tk.Text(
            analysis_frame,
            height=9,
            wrap="word",
            state="disabled",
            relief="flat",
            yscrollcommand=self.analysis_text_scrollbar.set,
        )
        self.analysis_text_scrollbar.configure(command=self.analysis_text.yview)
        self.analysis_text_scrollbar.pack(side="right", fill="y")
        self.analysis_text.pack(side="left", fill="x", expand=True)
        actions = ttk.Frame(self.up_tab); actions.pack(fill="x")
        ttk.Label(actions, text="Step 2: Choose what to create:").pack(side="left")
        self.existing_mode = tk.StringVar(value="")
        self.existing_mode_combo = ttk.Combobox(
            actions,
            textvariable=self.existing_mode,
            values=[PROCESS_ENHANCED, PROCESS_UPSCALE, PROCESS_BOTH],
            state="readonly",
            width=32,
        )
        self.existing_mode_combo.pack(side="left", padx=8)
        self.existing_mode_combo.bind("<<ComboboxSelected>>", self._existing_mode_changed)
        self.process_existing_button = ttk.Button(
            actions, text="Process Video", command=self.start_existing_processing, state="disabled"
        )
        self.process_existing_button.pack(side="right")
        self._clear_existing_analysis()

    def _helper(self, parent, text, row, column=0, columnspan=4):
        label = ttk.Label(parent, text=text, foreground="#5f6368", wraplength=760, justify="left")
        label.grid(row=row, column=column, columnspan=columnspan, sticky="w", pady=(0, 7))
        return label

    def _browse_for(self, key, folder=False):
        var = getattr(self, "var_" + key)
        if folder:
            titles = {
                "movies": "Choose the media library destination",
                "staging": "Choose the local staging folder",
            }
            value = choose_folder(
                self,
                initialdir=var.get() or None,
                title=titles.get(key, "Choose a folder"),
            )
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
        suffix = " Required whenever you choose an Enhanced output." if optional else ""
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
            "ffmpeg.exe creates optional 1080p versions from DVDs or existing videos. Download a Windows build from a provider linked by FFmpeg.org.",
            "ffmpeg",
        )
        self._tool_row(
            deps, 4, "ffprobe", "FFprobe",
            "ffprobe.exe validates duration, resolution, streams, and codecs. It normally comes in the same folder as FFmpeg.",
            "ffmpeg",
        )
        self._tool_row(
            deps, 6, "handbrake", "HandBrakeCLI",
            "HandBrakeCLI.exe creates optional Enhanced versions from DVDs or existing videos. Use the command-line download, not only the desktop app.",
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

    def runner(self):
        runner = Runner(
            self.settings,
            self.log,
            self.set_progress,
            self.set_activity,
            self.cancel_event if self.job_running else None,
        )
        if self.job_running:
            self.active_runner = runner
        return runner
    def log(self,msg): self.q.put(("log",str(msg)))
    def set_progress(self,v): self.q.put(("progress",float(v)))
    def set_activity(self,message): self.q.put(("activity",str(message)))
    def done(self,msg=None): self.q.put(("done",msg))
    def error(self,e): self.q.put(("error",str(e)))

    def _drain(self):
        try:
            while True:
                kind,val=self.q.get_nowait()
                if kind=="log":
                    self.logbox.configure(state="normal"); self.logbox.insert("end",val+"\n"); self.logbox.see("end"); self.logbox.configure(state="disabled")
                elif kind=="progress":
                    value=max(0.0,min(100.0,float(val)))
                    self.progress_var.set(value)
                    self.activity_percent.set(f"{value:.1f}%" if 0 < value < 100 else f"{int(value)}%")
                elif kind=="activity":
                    self.activity_status.set(val)
                elif kind=="scan_result":
                    titles,hint=val
                    self.show_scan(titles,hint)
                    self.progress_var.set(100); self.activity_percent.set("100%"); self.activity_status.set("Complete")
                    self._set_job_running(False)
                elif kind=="extras_staged":
                    batch,meta=val
                    self.progress_var.set(100); self.activity_percent.set("100%"); self.activity_status.set("Ready for review")
                    self._set_job_running(False)
                    self.after(0,lambda batch=batch,meta=meta:self.review_staged_extras(batch,meta))
                elif kind=="done":
                    self.progress_var.set(100); self.activity_percent.set("100%"); self.activity_status.set("Complete")
                    self._set_job_running(False)
                    if val: messagebox.showinfo(APP_NAME,val)
                elif kind=="cancelled":
                    self.activity_status.set("Cancelled")
                    if self.active_job_kind == "scan":
                        self.disc_label.configure(text="DVD scan cancelled.")
                    self.logbox.configure(state="normal")
                    self.logbox.insert("end", "CANCELLED: The active operation stopped. Any partial staging output was removed.\n")
                    self.logbox.see("end")
                    self.logbox.configure(state="disabled")
                    self._set_job_running(False)
                    messagebox.showinfo(APP_NAME, val or "The active job was cancelled.")
                elif kind=="error":
                    if self.active_job_kind == "scan":
                        self.disc_label.configure(text="DVD scan failed. Check the error and try again.")
                    self.activity_status.set("Failed"); self._set_job_running(False); messagebox.showerror(APP_NAME,val)
        except queue.Empty: pass
        self.after(100,self._drain)

    def threaded(self,fn,job_kind="processing"):
        if self.job_running:
            messagebox.showwarning(APP_NAME, "RipFoundry is already processing a job. Wait for it to finish before starting another.")
            return
        self.cancel_event.clear()
        self.active_runner = None
        self.active_job_kind = job_kind
        self._set_job_running(True)
        self.set_activity("Starting...")
        self.set_progress(0)
        def work():
            try: fn()
            except JobCancelled as e:
                if self.active_runner:
                    self.active_runner.cleanup_cancelled_job()
                self.q.put(("cancelled", str(e)))
            except Exception as e: self.error(e)
        threading.Thread(target=work,daemon=True).start()

    def cancel_active_job(self):
        if not self.job_running or self.cancel_event.is_set():
            return
        detail = (
            "MakeMKV will stop scanning the DVD. No media files will be changed."
            if self.active_job_kind == "scan"
            else "The active operation will stop. Any partial staging output will be removed, and the original source will remain unchanged."
        )
        if not messagebox.askyesno(
            APP_NAME,
            f"Cancel the active job?\n\n{detail}",
        ):
            return
        self.cancel_event.set()
        self.activity_status.set("Cancelling...")
        self.cancel_job_button.configure(state="disabled")
        self.log("Cancellation requested. Stopping the active process...")
        if self.active_runner:
            self.active_runner.cancel()

    def scan(self):
        self.save_settings_silent()
        if self.job_running:
            messagebox.showwarning(APP_NAME, "RipFoundry is already processing a job. Wait for it to finish before starting another.")
            return
        self.disc_label.configure(text="Scanning DVD... This can take about a minute.")
        def work():
            runner=self.runner()
            runner.begin_phase("Scanning DVD...")
            titles,hint=runner.scan_disc()
            self.q.put(("scan_result",(titles,hint)))
        self.threaded(work,job_kind="scan")

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

    def rip_selected_extras(self):
        self.save_settings_silent()
        ids=[int(x) for x in self.title_tree.selection()]
        if not ids:
            messagebox.showwarning(APP_NAME,"Select at least one DVD extra."); return
        chosen=[next(t for t in self.disc_titles if t.title_id==i) for i in ids]
        parent_meta=self.metadata_dialog(self.disc_hint)
        if not parent_meta:
            return
        details="\n".join(f"DVD title {title.title_id}: {title.duration}, {title.size}" for title in chosen)
        if not messagebox.askyesno(
            APP_NAME,
            f"Rip these titles as lossless extras for {movie_library_base(parent_meta)}?\n\n{details}\n\n"
            "They will be staged locally so you can play, name, and categorize them before anything is added to the library.",
        ):
            return
        def work():
            runner=self.runner()
            batch=runner.stage_extra_titles(chosen,parent_meta)
            self.q.put(("extras_staged",(batch,parent_meta)))
        self.threaded(work,job_kind="extras-stage")

    def review_staged_extras(self,batch,meta):
        dialog=ExtrasReviewDialog(self,batch,meta)
        self.wait_window(dialog)
        if not dialog.result:
            self.log(f"Staged DVD extras kept for later review: {batch.root}")
            messagebox.showinfo(
                APP_NAME,
                f"No library files were changed. The lossless staged extras were kept here:\n\n{batch.root}",
            )
            return
        plans=dialog.result
        preview="\n".join(
            f"DVD title {staged.disc_title.title_id} -> {plan.folder}\\{sanitize_title(plan.name)}.mkv"
            for staged,plan in zip(batch.items,plans)
        )
        if not messagebox.askyesno(
            APP_NAME,
            f"Add these extras to {movie_library_base(meta)}?\n\n{preview}\n\n"
            "Each file will be copied and SHA-256 verified before the staged source is removed.",
        ):
            self.log(f"Staged DVD extras kept for later review: {batch.root}")
            return
        def work():
            completed=self.runner().publish_extras(batch,meta,plans)
            self.done(f"Added and verified {len(completed)} DVD extra(s) for {meta.title}.")
        self.threaded(work,job_kind="extras-publish")

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
                except JobCancelled:
                    raise
                except Exception as e:
                    self.log(f"FAILED title {t.title_id}: {e}"); failures.append(f"Title {t.title_id}: {e}")
            if failures: raise RuntimeError("Some titles failed:\n"+"\n".join(failures))
            r.eject_disc()
            self.done("Rip completed and verified on the NAS. DVD ejected.")
        self.threaded(work)

    def search_movies(self):
        self.save_settings_silent()
        root=Path(self.settings.movies); q=self.movie_query.get().lower().strip()
        self._clear_existing_analysis()
        for x in self.movie_tree.get_children(): self.movie_tree.delete(x)
        if not self.settings.movies.strip(): messagebox.showerror(APP_NAME,"Media Library Destination is not configured. Open Settings first."); return
        if not root.exists(): messagebox.showerror(APP_NAME,f"Media Library Destination unavailable:\n{root}"); return
        try:
            matches=[]
            candidates = set()
            for pattern in ("*.mkv", "*.mp4", "*.m4v"):
                candidates.update(root.rglob(pattern))
            for p in sorted(candidates, key=lambda item: str(item).lower()):
                text=(p.parent.name+" "+p.name).lower()
                if not q or all(term in text for term in q.split()): matches.append(p)
                if len(matches)>=100: break
            for i,p in enumerate(matches): self.movie_tree.insert("","end",iid=str(i),values=(str(p),))
            self.log(f"Found {len(matches)} matching video file(s).")
        except Exception as e: messagebox.showerror(APP_NAME,str(e))

    def browse_existing_video(self):
        initial = self.settings.movies if self.settings.movies and Path(self.settings.movies).exists() else None
        selected = filedialog.askopenfilename(
            initialdir=initial,
            title="Choose an existing video",
            filetypes=[("Supported video", "*.mkv *.mp4 *.m4v"), ("All files", "*.*")],
        )
        if not selected:
            return
        self._clear_existing_analysis()
        for item in self.movie_tree.get_children():
            self.movie_tree.delete(item)
        self.movie_tree.insert("", "end", iid="selected", values=(selected,))
        self.movie_tree.selection_set("selected")
        self.movie_tree.focus("selected")

    def _set_analysis_text(self, value):
        self.analysis_text.configure(state="normal")
        self.analysis_text.delete("1.0", "end")
        self.analysis_text.insert("1.0", value)
        self.analysis_text.configure(state="disabled")

    def _clear_existing_analysis(self):
        self.analyzed_source = None
        self.analyzed_info = None
        self.analyzed_recommendation = None
        self.analyzed_reason = None
        if hasattr(self, "existing_mode"):
            self.existing_mode.set("")
        if hasattr(self, "process_existing_button"):
            self.process_existing_button.configure(state="disabled")
        if hasattr(self, "analysis_text"):
            self._set_analysis_text(
                "Select a video and click Analyze Selected Video. RipFoundry will inspect the source and "
                "recommend an output without creating or changing any files."
            )

    def _existing_selection_changed(self, _event=None):
        selection = self.movie_tree.selection()
        selected_source = Path(self.movie_tree.item(selection[0], "values")[0]) if selection else None
        if selected_source != self.analyzed_source:
            self._clear_existing_analysis()

    def _existing_mode_changed(self, _event=None):
        ready = not self.job_running and self.analyzed_source is not None and self.existing_mode.get() in {
            PROCESS_ENHANCED, PROCESS_UPSCALE, PROCESS_BOTH
        }
        self.process_existing_button.configure(state="normal" if ready else "disabled")

    def _set_job_running(self, running):
        self.job_running = bool(running)
        if hasattr(self, "scan_button"):
            self.scan_button.configure(state="disabled" if running else "normal")
        if hasattr(self, "rip_selected_button"):
            self.rip_selected_button.configure(state="disabled" if running else "normal")
        if hasattr(self, "rip_extras_button"):
            self.rip_extras_button.configure(state="disabled" if running else "normal")
        if hasattr(self, "mode_combo"):
            self.mode_combo.configure(state="disabled" if running else "readonly")
        if hasattr(self, "analyze_existing_button"):
            self.analyze_existing_button.configure(state="disabled" if running else "normal")
        if hasattr(self, "existing_mode_combo"):
            self.existing_mode_combo.configure(state="disabled" if running else "readonly")
        if hasattr(self, "process_existing_button"):
            self._existing_mode_changed()
        if hasattr(self, "cancel_job_button"):
            self.cancel_job_button.configure(state="normal" if running else "disabled")
        if not running:
            self.active_runner = None
            self.active_job_kind = None
            self.cancel_event.clear()

    def analyze_selected_video(self):
        selection = self.movie_tree.selection()
        if not selection:
            messagebox.showwarning(APP_NAME, "Select an MKV, MP4, or M4V first.")
            return
        source = Path(self.movie_tree.item(selection[0], "values")[0])
        self.save_settings_silent()
        try:
            info, recommendation, reason = self.runner().analyze_existing(source)
        except Exception as exc:
            self._clear_existing_analysis()
            messagebox.showerror(APP_NAME, str(exc))
            return
        self.analyzed_source = source
        self.analyzed_info = info
        self.analyzed_recommendation = recommendation
        self.analyzed_reason = reason
        guidance = (
            "\n\nReview the recommendation, choose an output below, and click Process Video only if you want to continue."
        )
        if recommendation == PROCESS_NONE:
            self.existing_mode.set("")
            guidance = (
                "\n\nNo processing is selected because RipFoundry found no clear benefit. You can stop here, or "
                "choose an explicit output below if you still want one."
            )
        else:
            self.existing_mode.set(recommendation)
        self._set_analysis_text(analysis_summary(source, info, recommendation, reason) + guidance)
        self._existing_mode_changed()
        self.log(f"Analyzed existing video: {source.name}; recommendation: {recommendation}")

    def start_existing_processing(self):
        if self.job_running:
            messagebox.showwarning(APP_NAME, "RipFoundry is already processing a job. Wait for it to finish before starting another.")
            return
        sel=self.movie_tree.selection()
        if not sel: messagebox.showwarning(APP_NAME,"Select an MKV, MP4, or M4V first."); return
        source=Path(self.movie_tree.item(sel[0],"values")[0])
        if source != self.analyzed_source or self.analyzed_info is None:
            messagebox.showwarning(APP_NAME, "Analyze the selected video before choosing a processing option.")
            return
        info = self.analyzed_info
        selected_mode = self.existing_mode.get()
        if selected_mode not in {PROCESS_ENHANCED, PROCESS_UPSCALE, PROCESS_BOTH}:
            messagebox.showwarning(APP_NAME, "Choose Enhanced, 1080p, or Both before processing.")
            return
        base = existing_video_base(source)
        native = resolution_label(info)
        outputs = []
        if selected_mode in {PROCESS_ENHANCED, PROCESS_BOTH}:
            outputs.append(f"{base} - {native} Enhanced.mkv")
        if selected_mode in {PROCESS_UPSCALE, PROCESS_BOTH}:
            outputs.append(f"{base} - 1080p.mkv")
        output_plan = "\n".join(f"  - {name}" for name in outputs)
        prompt = (
            f"Selected processing: {selected_mode}\n\nCreate:\n{output_plan}"
            + "\n\nThe original will remain unchanged. Continue?"
        )
        if not messagebox.askyesno(APP_NAME, prompt):
            return
        def work():
            completed = self.runner().process_existing(source, selected_mode)
            names = "\n".join(path.name for path in completed)
            self.done(f"Existing video processing completed and verified.\n\n{names}")
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
