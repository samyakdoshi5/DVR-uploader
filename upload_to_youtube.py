from __future__ import annotations

import argparse
import json
import re
import shutil
import ssl
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

try:
    import google_auth_httplib2
    import httplib2
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError, ResumableUploadError
    from googleapiclient.http import MediaFileUpload
except ModuleNotFoundError as exc:
    google_auth_httplib2 = None
    httplib2 = None
    RefreshError = RuntimeError
    Request = None
    Credentials = None
    InstalledAppFlow = None
    build = None
    MediaFileUpload = None

    class HttpError(Exception):
        pass

    class ResumableUploadError(Exception):
        pass

    GOOGLE_IMPORT_ERROR = exc
else:
    GOOGLE_IMPORT_ERROR = None


SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/youtube.readonly",
]
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".mkv"}
UPLOADER_FOLDER_NAME = "FPV YT Uploader"
STATE_FILE = "state.json"
IGNORED_FOLDERS_FILE = "ignored_folders.txt"
DEFAULT_UPLOAD_CHUNK_MB = 8
DEFAULT_HTTP_TIMEOUT_SECONDS = 120
DEFAULT_UPLOAD_RETRIES = 5
DEFAULT_MIN_FILE_SIZE_MB = 50
RATE_LIMIT_RETRY_SECONDS = 60 * 10
LAST_STATUS_LEN = 0

RETRYABLE_UPLOAD_EXCEPTIONS = tuple(
    item
    for item in (
        httplib2.HttpLib2Error if httplib2 is not None else None,
        ConnectionError,
        TimeoutError,
        ssl.SSLError,
    )
    if item is not None
)

ISO_DATE_RE = re.compile(r"(?P<date>\d{4}-\d{2}-\d{2})")
DAY_MONTH_RE = re.compile(
    r"\b(?P<day>[0-3]?\d)\s+"
    r"(?P<month>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b",
    re.IGNORECASE,
)
MONTHS = {
    "jan": "Jan",
    "january": "Jan",
    "feb": "Feb",
    "february": "Feb",
    "mar": "Mar",
    "march": "Mar",
    "apr": "Apr",
    "april": "Apr",
    "may": "May",
    "jun": "Jun",
    "june": "Jun",
    "jul": "Jul",
    "july": "Jul",
    "aug": "Aug",
    "august": "Aug",
    "sep": "Sep",
    "sept": "Sep",
    "september": "Sep",
    "oct": "Oct",
    "october": "Oct",
    "nov": "Nov",
    "november": "Nov",
    "dec": "Dec",
    "december": "Dec",
}


@dataclass(frozen=True)
class DateInfo:
    display: str
    key: str
    sort_key: tuple
    source_folder: str
    label: Optional[str] = None
    has_year: bool = True


@dataclass(frozen=True)
class VideoItem:
    path: Path
    rel_key: str
    craft: str
    location: Optional[str]
    date: Optional[DateInfo]
    serial: int
    title: str
    playlists: list[str]


def load_json(path: Path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    tmp_path.replace(path)


def ensure_state_shape(state: dict) -> dict:
    state.setdefault("files", {})
    state.setdefault("playlists", {})
    return state


def find_client_secret(uploader_dir: Path) -> Path:
    candidates = sorted(uploader_dir.glob("client_secret*.json"))
    if not candidates:
        raise FileNotFoundError(
            f"No client_secret*.json found in {uploader_dir}. "
            "Put the Google OAuth desktop-app client JSON for this channel in this folder."
        )
    return candidates[0]


def validate_client_secret(client_secret: Path) -> None:
    data = load_json(client_secret, {})
    if "installed" in data:
        return
    if "web" in data:
        raise ValueError(
            "The OAuth JSON is a web client, but this uploader expects a desktop/installed app client.\n"
            "Create an OAuth client of type 'Desktop app', download that JSON, and place it here."
        )
    raise ValueError(f"Unrecognized OAuth JSON format in {client_secret}. Expected an 'installed' block.")


def get_credentials(uploader_dir: Path) -> Any:
    ensure_google_dependencies()
    token_path = uploader_dir / "token.json"
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        try:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                raise RefreshError("No usable refresh token")
        except RefreshError:
            if token_path.exists():
                token_path.unlink()
            client_secret = find_client_secret(uploader_dir)
            validate_client_secret(client_secret)
            flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES)
            creds = flow.run_local_server(port=0)
        save_json(token_path, json.loads(creds.to_json()))

    return creds


def auth_bootstrap(uploader_dir: Path) -> None:
    creds = get_credentials(uploader_dir)
    print("Authentication saved locally.")
    print(f"Token file: {uploader_dir / 'token.json'}")
    print(f"Scopes: {', '.join(SCOPES)}")
    if creds.expired:
        print("Note: token existed but was expired and has been refreshed.")


def build_youtube_client(creds: Any, timeout_seconds: int):
    ensure_google_dependencies()
    http = httplib2.Http(timeout=timeout_seconds)
    http.redirect_codes = frozenset(code for code in http.redirect_codes if code != 308)
    authorized_http = google_auth_httplib2.AuthorizedHttp(creds, http=http)
    return build("youtube", "v3", http=authorized_http)


def execute_with_rate_limit_retry(request):
    while True:
        try:
            return request.execute()
        except HttpError as exc:
            if not is_rate_limit_error(exc):
                raise
            print("rate limit exceeded")
            time.sleep(RATE_LIMIT_RETRY_SECONDS)


def ensure_google_dependencies() -> None:
    if GOOGLE_IMPORT_ERROR is None:
        return
    raise RuntimeError(
        "Missing Google API dependencies. Install them with:\n"
        f'pip install -r "{Path(__file__).resolve().parent / "requirements.txt"}"'
    ) from GOOGLE_IMPORT_ERROR


def is_rate_limit_error(exc: HttpError) -> bool:
    message = str(exc).lower()
    return exc.resp.status == 429 or "rate_limit_exceeded" in message


def is_youtube_signup_required_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return "youtubesignuprequired" in message or "youtube signup required" in message


def is_upload_limit_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return (
        "uploadlimitexceeded" in message
        or "video uploads per day" in message
        or "quota exceeded" in message
        or "exceeded the number of videos" in message
    )


def load_ignored_folders(uploader_dir: Path) -> set[str]:
    ignore_path = uploader_dir / IGNORED_FOLDERS_FILE
    if not ignore_path.exists():
        return set()

    ignored: set[str] = set()
    for raw_line in ignore_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        ignored.add(line)
    return ignored


def path_is_ignored(path: Path, ignored_folders: set[str]) -> bool:
    return any(part in ignored_folders for part in path.parts)


def natural_key(text: str):
    key = []
    for part in re.split(r"(\d+)", text.lower()):
        if part.isdigit():
            key.append((0, int(part), len(part)))
        else:
            key.append((1, part))
    return key


def file_sort_key(path: Path):
    return [natural_key(part) for part in path.parts]


def parse_date_from_folder(folder_name: str) -> Optional[DateInfo]:
    iso_match = ISO_DATE_RE.search(folder_name)
    if iso_match:
        raw = iso_match.group("date")
        parsed = datetime.strptime(raw, "%Y-%m-%d")
        label = folder_name[: iso_match.start()] + folder_name[iso_match.end() :]
        label = clean_folder_label(label)
        return DateInfo(
            display=parsed.strftime("%d/%m/%y"),
            key=raw,
            sort_key=(0, parsed.year, parsed.month, parsed.day),
            source_folder=folder_name,
            label=label or None,
            has_year=True,
        )

    day_month_match = DAY_MONTH_RE.search(folder_name)
    if day_month_match:
        day = int(day_month_match.group("day"))
        month_raw = day_month_match.group("month").lower()
        month = MONTHS[month_raw]
        month_number = MONTHS_TO_NUMBER[month]
        display = f"{day:02d}/{month_number:02d}"
        label = folder_name[: day_month_match.start()] + folder_name[day_month_match.end() :]
        label = clean_folder_label(label)
        return DateInfo(
            display=display,
            key=display,
            sort_key=(1, month_number, day),
            source_folder=folder_name,
            label=label or None,
            has_year=False,
        )

    return None


def clean_folder_label(label: str) -> str:
    label = re.sub(r"^[\s\-_()]+|[\s\-_()]+$", "", label)
    label = re.sub(r"\s+", " ", label)
    return label.strip()


MONTHS_TO_NUMBER = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}


def find_date_info(relative_parent_parts: tuple[str, ...]) -> Optional[DateInfo]:
    # Prefer the deepest dated folder, because craft/location folders can be generic while
    # a date-specific folder usually sits below them.
    for folder_name in reversed(relative_parent_parts):
        info = parse_date_from_folder(folder_name)
        if info is not None:
            return info
    return None


def playlist_title(name: str) -> str:
    return name


def date_title_part(date_info: DateInfo) -> str:
    if date_info.label:
        return f"{date_info.display} {date_info.label}"
    return date_info.display


def label_title_part(date_info: Optional[DateInfo]) -> str:
    if date_info is None:
        return "No Label"
    return date_info.label or "No Label"


def format_title(item: VideoItem) -> str:
    parts = [f"{item.serial:03d}"]
    if item.date is not None:
        parts.append(item.date.display)
        if item.date.label:
            parts.append(item.date.label)
    parts.append(item.craft)
    if item.location:
        parts.append(item.location)
    return " || ".join(parts)


def discover_video_candidates(root: Path, uploader_dir: Path, ignored_folders: set[str]) -> list[dict]:
    candidates = []
    uploader_dir_resolved = uploader_dir.resolve()

    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        try:
            if path.resolve().is_relative_to(uploader_dir_resolved):
                continue
        except ValueError:
            pass

        rel = path.relative_to(root)
        if path_is_ignored(rel, ignored_folders):
            continue
        if len(rel.parts) < 2:
            continue

        craft = rel.parts[0]
        if craft == UPLOADER_FOLDER_NAME:
            continue

        parent_parts = rel.parent.parts
        date_info = find_date_info(parent_parts)
        location = None
        if len(rel.parts) >= 3 and parse_date_from_folder(rel.parts[1]) is None:
            location = rel.parts[1]
        candidates.append(
            {
                "path": path,
                "rel_key": rel.as_posix(),
                "craft": craft,
                "location": location,
                "date": date_info,
            }
        )

    return sorted(candidates, key=lambda item: file_sort_key(item["path"].relative_to(root)))


def build_upload_plan(
    root: Path,
    uploader_dir: Path,
    state: dict,
    ignored_folders: set[str],
    min_file_size_bytes: int,
) -> list[VideoItem]:
    candidates = discover_video_candidates(root, uploader_dir, ignored_folders)
    serials: dict[tuple[str, str], int] = {}
    items: list[VideoItem] = []

    for candidate in candidates:
        if candidate["path"].stat().st_size < min_file_size_bytes:
            continue

        date_info = candidate["date"]
        if date_info is not None:
            serial_key = ("date", date_info.key)
        else:
            serial_key = ("craft", candidate["craft"])

        serials[serial_key] = serials.get(serial_key, 0) + 1
        playlists = [playlist_title(candidate["craft"])]
        if candidate["location"]:
            playlists.append(playlist_title(candidate["location"]))
        if date_info is not None:
            playlists.append(playlist_title(date_title_part(date_info)))

        item = VideoItem(
            path=candidate["path"],
            rel_key=candidate["rel_key"],
            craft=candidate["craft"],
            location=candidate["location"],
            date=date_info,
            serial=serials[serial_key],
            title="",
            playlists=playlists,
        )
        item = VideoItem(
            path=item.path,
            rel_key=item.rel_key,
            craft=item.craft,
            location=item.location,
            date=item.date,
            serial=item.serial,
            title=format_title(item),
            playlists=item.playlists,
        )

        file_state = state.get("files", {}).get(item.rel_key, {})
        if file_state.get("status") == "playlist_added":
            continue
        items.append(item)

    return items


def ensure_playlist(youtube, playlists_cache: dict[str, str], title: str) -> str:
    if title in playlists_cache:
        return playlists_cache[title]

    request = youtube.playlists().list(
        part="id,snippet,status",
        mine=True,
        maxResults=50,
    )
    while request is not None:
        response = execute_with_rate_limit_retry(request)
        for playlist in response.get("items", []):
            snippet = playlist.get("snippet", {})
            if snippet.get("title") == title:
                status = playlist.get("status", {})
                if status.get("privacyStatus") != "unlisted":
                    youtube.playlists().update(
                        part="status",
                        body={
                            "id": playlist["id"],
                            "status": {"privacyStatus": "unlisted"},
                        },
                    ).execute()
                playlists_cache[title] = playlist["id"]
                return playlist["id"]
        request = youtube.playlists().list_next(request, response)

    created = youtube.playlists().insert(
        part="snippet,status",
        body={
            "snippet": {
                "title": title,
                "description": f"Auto-created playlist for {title}",
            },
            "status": {"privacyStatus": "unlisted"},
        },
    )
    created = execute_with_rate_limit_retry(created)
    playlists_cache[title] = created["id"]
    return created["id"]


def add_video_to_playlist(youtube, playlist_id: str, video_id: str) -> None:
    request = youtube.playlistItems().insert(
        part="snippet",
        body={
            "snippet": {
                "playlistId": playlist_id,
                "resourceId": {
                    "kind": "youtube#video",
                    "videoId": video_id,
                },
            }
        },
    )
    try:
        execute_with_rate_limit_retry(request)
    except HttpError as exc:
        if exc.resp.status == 409:
            return
        raise


def upload_video(
    youtube,
    file_path: Path,
    title: str,
    chunk_size_mb: int,
    retries: int,
) -> str:
    chunk_size = chunk_size_mb * 1024 * 1024
    media = MediaFileUpload(str(file_path), chunksize=chunk_size, resumable=True)
    request = youtube.videos().insert(
        part="snippet,status",
        body={
            "snippet": {
                "title": title,
                "description": f"Uploaded from {file_path.parent}",
                "categoryId": "22",
            },
            "status": {
                "privacyStatus": "unlisted",
            },
        },
        media_body=media,
    )

    response = None
    start_time = time.monotonic()
    last_percent = -1
    try:
        while response is None:
            for attempt in range(retries + 1):
                try:
                    status, response = request.next_chunk(num_retries=retries)
                    break
                except RETRYABLE_UPLOAD_EXCEPTIONS as exc:
                    if attempt >= retries:
                        raise
                    delay = retry_delay_seconds(attempt)
                    write_status_line(
                        f"Upload connection error: {format_upload_retry_error(exc)}. "
                        f"Retrying in {delay}s ({attempt + 1}/{retries})..."
                    )
                    time.sleep(delay)
            if status:
                total = status.total_size or 0
                current = status.resumable_progress or 0
                if total:
                    percent = int(current * 100 / total)
                    if percent != last_percent:
                        elapsed = max(time.monotonic() - start_time, 0.001)
                        speed = current / elapsed
                        remaining = max(total - current, 0)
                        eta_seconds = int(remaining / speed) if speed > 0 else 0
                        write_status_line(
                            format_progress_line(
                                file_path.name,
                                current,
                                total,
                                percent,
                                speed,
                                eta_seconds,
                            )
                        )
                        last_percent = percent
                else:
                    write_status_line(f"Progress: {human_size(current)} uploaded")
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        sys.stdout.flush()
        raise
    except Exception:
        sys.stdout.write("\n")
        sys.stdout.flush()
        raise

    write_status_line(f"Progress: {file_path.name} complete")
    sys.stdout.write("\n")
    sys.stdout.flush()

    return response["id"]


def retry_delay_seconds(attempt: int) -> int:
    return min(2**attempt, 60)


def format_upload_retry_error(exc: BaseException) -> str:
    winerror = getattr(exc, "winerror", None)
    if winerror:
        return f"{type(exc).__name__} [WinError {winerror}]: {exc}"
    return f"{type(exc).__name__}: {exc}"


def update_file_state(state: dict, state_path: Path, rel_key: str, **fields) -> None:
    file_state = state.setdefault("files", {}).setdefault(rel_key, {})
    file_state.update(fields)
    file_state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    save_json(state_path, state)


def remove_file_state(state: dict, state_path: Path, rel_key: str) -> None:
    if rel_key in state.get("files", {}):
        state["files"].pop(rel_key, None)
        save_json(state_path, state)


def human_size(num_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(num_bytes)
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024.0
    return f"{num_bytes} B"


def format_eta(total_seconds: int) -> str:
    total_seconds = max(total_seconds, 0)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:d}:{seconds:02d}"


def truncate_middle(text: str, max_length: int) -> str:
    if max_length <= 0:
        return ""
    if len(text) <= max_length:
        return text
    if max_length <= 3:
        return text[:max_length]
    left = (max_length - 3) // 2
    right = max_length - 3 - left
    return f"{text[:left]}...{text[-right:]}"


def format_progress_line(
    filename: str,
    current: int,
    total: int,
    percent: int,
    speed_bytes_per_sec: float,
    eta_seconds: int,
) -> str:
    terminal_width = shutil.get_terminal_size(fallback=(120, 20)).columns
    prefix = (
        f"Progress: {human_size(current)} / {human_size(total)}"
        f" | {percent:3d}%"
        f" | {human_size(int(speed_bytes_per_sec))}/s"
        f" | ETA {format_eta(eta_seconds)}"
        f" | "
    )
    filename_width = max(10, terminal_width - len(prefix) - 1)
    return f"{prefix}{truncate_middle(filename, filename_width)}"


def write_status_line(text: str) -> None:
    global LAST_STATUS_LEN
    clear_width = max(LAST_STATUS_LEN, len(text))
    sys.stdout.write("\r" + (" " * clear_width) + "\r")
    sys.stdout.write(text)
    sys.stdout.flush()
    LAST_STATUS_LEN = len(text)


def print_state_summary(state: dict) -> None:
    counts = {
        "uploading": 0,
        "uploaded": 0,
        "playlist_added": 0,
    }
    for file_state in state.get("files", {}).values():
        status = file_state.get("status")
        if status in counts:
            counts[status] += 1
    print("State: " + ", ".join(f"{name}={count}" for name, count in counts.items()))


def print_plan(plan: list[VideoItem]) -> None:
    total_bytes = sum(item.path.stat().st_size for item in plan)
    print(f"Planned uploads: {len(plan)} file(s), {human_size(total_bytes)} total")
    print()
    for item in plan:
        playlists = " + ".join(item.playlists)
        print(f"{item.rel_key}")
        print(f"  title: {item.title}")
        print(f"  playlists: {playlists}")
        print(f"  size: {human_size(item.path.stat().st_size)}")
        print()


def confirm(prompt: str) -> bool:
    while True:
        answer = input(prompt).strip().lower()
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no", ""}:
            return False
        print("Please answer y or n.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Upload FPV DVR footage to YouTube and organize each video into craft and location playlists."
    )
    parser.add_argument(
        "--root",
        default=str(Path(__file__).resolve().parent.parent),
        help="Folder containing craft folders. Default: parent of this uploader folder.",
    )
    parser.add_argument("--craft", help="Only process one craft folder name.")
    parser.add_argument("--location", help="Only process one location folder name.")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without uploading anything.")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation and upload immediately.")
    parser.add_argument("--auth-only", action="store_true", help="Only sign in and save token.json, then exit.")
    parser.add_argument("--reauth", action="store_true", help="Delete token.json first so Google asks again.")
    parser.add_argument(
        "--upload-chunk-mb",
        type=int,
        default=DEFAULT_UPLOAD_CHUNK_MB,
        help=f"Upload chunk size in MB. Default: {DEFAULT_UPLOAD_CHUNK_MB}.",
    )
    parser.add_argument(
        "--request-timeout",
        type=int,
        default=DEFAULT_HTTP_TIMEOUT_SECONDS,
        help=f"Google API socket timeout in seconds. Default: {DEFAULT_HTTP_TIMEOUT_SECONDS}.",
    )
    parser.add_argument(
        "--upload-retries",
        type=int,
        default=DEFAULT_UPLOAD_RETRIES,
        help=f"Retries per upload chunk for transient errors. Default: {DEFAULT_UPLOAD_RETRIES}.",
    )
    parser.add_argument(
        "--min-file-size-mb",
        type=int,
        default=DEFAULT_MIN_FILE_SIZE_MB,
        help=f"Skip files smaller than this many MB. Default: {DEFAULT_MIN_FILE_SIZE_MB}.",
    )
    args = parser.parse_args()

    root = Path(args.root).resolve()
    uploader_dir = Path(__file__).resolve().parent
    state_path = uploader_dir / STATE_FILE
    token_path = uploader_dir / "token.json"

    if args.upload_chunk_mb <= 0:
        print("--upload-chunk-mb must be greater than 0.")
        return 1
    if args.request_timeout <= 0:
        print("--request-timeout must be greater than 0.")
        return 1
    if args.upload_retries < 0:
        print("--upload-retries cannot be negative.")
        return 1
    if args.min_file_size_mb < 0:
        print("--min-file-size-mb cannot be negative.")
        return 1
    if not root.exists():
        print(f"Root folder does not exist: {root}")
        return 1

    if args.reauth and token_path.exists():
        token_path.unlink()
        print("Removed saved token.json so the next sign-in starts fresh.")

    if args.auth_only:
        auth_bootstrap(uploader_dir)
        return 0

    state = ensure_state_shape(load_json(state_path, {"files": {}, "playlists": {}}))
    ignored_folders = load_ignored_folders(uploader_dir)
    min_file_size_bytes = args.min_file_size_mb * 1024 * 1024
    plan = build_upload_plan(root, uploader_dir, state, ignored_folders, min_file_size_bytes)

    if args.craft:
        plan = [item for item in plan if item.craft == args.craft]
    if args.location:
        plan = [item for item in plan if item.location == args.location]

    if not plan:
        print("Nothing new to upload.")
        return 0

    print_state_summary(state)
    print_plan(plan)

    if args.dry_run:
        print("Dry run complete. No files were uploaded.")
        return 0

    if not args.yes:
        if not confirm("Proceed with these uploads? [y/N]: "):
            print("Cancelled. No files were uploaded.")
            return 0

    creds = get_credentials(uploader_dir)
    youtube = build_youtube_client(creds, args.request_timeout)
    playlists_cache = state.setdefault("playlists", {})

    for item in plan:
        file_state = state.setdefault("files", {}).setdefault(item.rel_key, {})
        existing_status = file_state.get("status")
        video_id = file_state.get("video_id")
        playlist_ids_by_title = file_state.setdefault("playlist_ids_by_title", {})

        if existing_status != "uploaded" or not video_id:
            print(f"Uploading {item.rel_key} as {item.title}")
            if existing_status == "uploading":
                print("Previous run stopped mid-upload; restarting this file from the beginning.")
            update_file_state(
                state,
                state_path,
                item.rel_key,
                status="uploading",
                title=item.title,
                path=str(item.path),
                craft=item.craft,
                location=item.location,
                date=item.date.display if item.date else None,
                day_label=item.date.label if item.date else None,
                serial=item.serial,
                playlists=item.playlists,
            )
            try:
                video_id = upload_video(
                    youtube,
                    item.path,
                    item.title,
                    args.upload_chunk_mb,
                    args.upload_retries,
                )
            except (HttpError, ResumableUploadError) as exc:
                message = str(exc)
                if is_youtube_signup_required_error(exc):
                    remove_file_state(state, state_path, item.rel_key)
                    print()
                    print("YouTube rejected this account: youtubeSignupRequired.")
                    print("Sign in to YouTube with the target Google account and create/select a channel first.")
                    print("Then rerun this script with --reauth --auth-only and choose that YouTube account/channel.")
                    print("This file was returned to the queue.")
                    return 1
                if is_upload_limit_error(exc):
                    remove_file_state(state, state_path, item.rel_key)
                    print()
                    print("YouTube upload limit was reached.")
                    print("This file was returned to the queue, so rerunning later will retry it.")
                    return 1
                raise
            except RETRYABLE_UPLOAD_EXCEPTIONS as exc:
                print(f"\nUpload stopped after connection retries: {format_upload_retry_error(exc)}")
                print("The current file is marked as 'uploading'; rerun the script to retry it.")
                return 1
            update_file_state(
                state,
                state_path,
                item.rel_key,
                status="uploaded",
                video_id=video_id,
                title=item.title,
                uploaded_at=datetime.now().isoformat(timespec="seconds"),
            )

        for playlist in item.playlists:
            playlist_id = ensure_playlist(youtube, playlists_cache, playlist)
            if playlist_ids_by_title.get(playlist) == playlist_id:
                continue
            print(f"Adding {video_id} to playlist: {playlist}")
            add_video_to_playlist(youtube, playlist_id, video_id)
            playlist_ids_by_title[playlist] = playlist_id
            update_file_state(
                state,
                state_path,
                item.rel_key,
                status="uploaded",
                video_id=video_id,
                playlist_ids_by_title=playlist_ids_by_title,
            )

        update_file_state(
            state,
            state_path,
            item.rel_key,
            status="playlist_added",
            video_id=video_id,
            title=item.title,
            playlist_ids_by_title=playlist_ids_by_title,
            completed_at=datetime.now().isoformat(timespec="seconds"),
        )
        print(f"Done: {video_id}")

    save_json(state_path, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
