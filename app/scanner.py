import json
import os
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .db import connect, get_video_by_path, mark_video_unchanged, upsert_video
from .profile import get_encoding_profile, process_command, snapshot as profile_snapshot


VIDEO_EXTENSIONS = {
    ".3gp", ".avi", ".m2ts", ".m4v", ".mkv", ".mov",
    ".mp4", ".mpeg", ".mpg", ".mts", ".webm",
}


class ScanState:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.phase = "idle"
        self.scan_id = None
        self.total = 0
        self.current = 0
        self.probed = 0
        self.unchanged = 0
        self.errors = 0
        self.message = ""

    def update(self, **changes):
        with self.lock:
            for key, value in changes.items():
                setattr(self, key, value)

    def snapshot(self):
        with self.lock:
            return {
                "running": self.running,
                "phase": self.phase,
                "scan_id": self.scan_id,
                "total": self.total,
                "current": self.current,
                "probed": self.probed,
                "unchanged": self.unchanged,
                "errors": self.errors,
                "message": self.message,
            }


STATE = ScanState()
SCAN_LOCK = threading.Lock()


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def media_roots():
    configured = os.environ.get("MEDIA_ROOTS", "/storage/data/library:/storage/data/upload")
    return [Path(item).resolve() for item in configured.split(":") if item]


def discover_files(roots):
    files = []
    for root in roots:
        if not root.is_dir():
            continue
        for directory, _, names in os.walk(root, followlinks=False):
            for name in names:
                path = Path(directory, name)
                if path.suffix.lower() in VIDEO_EXTENSIONS:
                    files.append((root, path))
    return sorted(files, key=lambda pair: str(pair[1]))


def rational(value):
    try:
        numerator, denominator = value.split("/", 1)
        return float(numerator) / float(denominator) if float(denominator) else None
    except (AttributeError, ValueError, ZeroDivisionError):
        return None


def integer(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def floating(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def probe(path):
    settings = profile_snapshot(get_encoding_profile())
    command = process_command([
        "ffprobe", "-v", "error", "-show_format", "-show_streams",
        "-show_entries", "stream_side_data", "-of", "json", str(path),
    ], settings)
    completed = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
    if completed.returncode:
        message = completed.stderr.strip() or f"ffprobe exited with {completed.returncode}"
        raise RuntimeError(message[-1000:])
    return json.loads(completed.stdout)


def rotation_of(stream):
    for side_data in stream.get("side_data_list", []):
        if "rotation" in side_data:
            return integer(side_data.get("rotation"))
    return None


def record_for(root, path, scan_id, scanned_at):
    stat = path.stat()
    base = {
        "path": str(path),
        "root": str(root),
        "relative_path": str(path.relative_to(root)),
        "extension": path.suffix.lower(),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "duration_seconds": None,
        "bit_rate": None,
        "container": None,
        "video_codec": None,
        "audio_codec": None,
        "width": None,
        "height": None,
        "fps": None,
        "pixel_format": None,
        "color_space": None,
        "color_transfer": None,
        "color_primaries": None,
        "encoder_tag": None,
        "capture_time": None,
        "has_location": 0,
        "rotation": None,
        "audio_streams": 0,
        "subtitle_streams": 0,
        "data_streams": 0,
        "classification": "untracked",
        "probe_error": None,
        "present": 1,
        "last_scan_id": scan_id,
        "scanned_at": scanned_at,
        "first_seen_at": scanned_at,
    }
    try:
        data = probe(path)
        streams = data.get("streams", [])
        video = next((stream for stream in streams if stream.get("codec_type") == "video"), {})
        audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), {})
        fmt = data.get("format", {})
        tags = fmt.get("tags", {})
        encoder = tags.get("encoder")
        base.update(
            duration_seconds=floating(fmt.get("duration")),
            bit_rate=integer(fmt.get("bit_rate")),
            container=fmt.get("format_name"),
            video_codec=video.get("codec_name"),
            audio_codec=audio.get("codec_name"),
            width=integer(video.get("width")),
            height=integer(video.get("height")),
            fps=rational(video.get("avg_frame_rate") or video.get("r_frame_rate")),
            pixel_format=video.get("pix_fmt"),
            color_space=video.get("color_space"),
            color_transfer=video.get("color_transfer"),
            color_primaries=video.get("color_primaries"),
            encoder_tag=encoder,
            capture_time=tags.get("date") or tags.get("creation_time"),
            has_location=int(any(key == "location" or key.startswith("location-") for key in tags)),
            rotation=rotation_of(video),
            audio_streams=sum(stream.get("codec_type") == "audio" for stream in streams),
            subtitle_streams=sum(stream.get("codec_type") == "subtitle" for stream in streams),
            data_streams=sum(stream.get("codec_type") == "data" for stream in streams),
            classification=(
                "historical_handbrake" if encoder and "handbrake" in encoder.lower()
                else "untracked"
            ),
        )
    except Exception as error:
        base["probe_error"] = str(error)[-1000:]
    return base


def _prepare_state(scan_id):
    STATE.update(
        running=True, phase="discovering", scan_id=scan_id, total=0, current=0,
        probed=0, unchanged=0, errors=0, message="Поиск видеофайлов",
    )


def _scan_impl(scan_id):
    started_at = utcnow()
    errors = 0
    probed = 0
    unchanged = 0
    roots = media_roots()
    try:
        missing_roots = [str(root) for root in roots if not root.is_dir()]
        if not roots or missing_roots:
            raise RuntimeError(
                "Недоступны каталоги медиатеки: " + ", ".join(missing_roots or ["не настроены"])
            )
        files = discover_files(roots)
        with connect() as connection:
            previous_present = connection.execute(
                "SELECT COUNT(*) FROM videos WHERE present=1"
            ).fetchone()[0]
        if previous_present and not files:
            raise RuntimeError(
                "Медиатека неожиданно пуста; существующий индекс оставлен без изменений"
            )
        STATE.update(phase="probing", total=len(files), message="Анализ медиатеки")
        with connect() as connection:
            connection.execute(
                """
                INSERT INTO scan_runs(id, started_at, discovered, status)
                VALUES (?, ?, ?, 'running')
                """,
                (scan_id, started_at, len(files)),
            )
            connection.commit()
            for index, (root, path) in enumerate(files, start=1):
                scanned_at = utcnow()
                try:
                    stat = path.stat()
                    old = get_video_by_path(connection, path)
                    if (
                        old is not None
                        and int(old["size_bytes"]) == stat.st_size
                        and int(old["mtime_ns"]) == stat.st_mtime_ns
                        and not old["probe_error"]
                    ):
                        mark_video_unchanged(connection, path, scan_id, scanned_at)
                        unchanged += 1
                    else:
                        record = record_for(root, path, scan_id, scanned_at)
                        errors += int(record["probe_error"] is not None)
                        probed += 1
                        upsert_video(connection, record)
                except FileNotFoundError:
                    pass
                except Exception:
                    errors += 1
                if index % 20 == 0:
                    connection.commit()
                STATE.update(
                    current=index, probed=probed, unchanged=unchanged,
                    errors=errors, message=path.name,
                )
            connection.execute("UPDATE videos SET present=0 WHERE last_scan_id != ?", (scan_id,))
            connection.execute(
                """
                UPDATE scan_runs
                   SET finished_at=?, probed=?, unchanged=?, errors=?, status='completed'
                 WHERE id=?
                """,
                (utcnow(), probed, unchanged, errors, scan_id),
            )
            connection.commit()
        STATE.update(
            phase="done",
            message=f"Готово: {len(files)} файлов, новых/изменённых: {probed}, ошибок: {errors}",
        )
        return {
            "scan_id": scan_id, "discovered": len(files), "probed": probed,
            "unchanged": unchanged, "errors": errors,
        }
    except Exception as error:
        with connect() as connection:
            connection.execute(
                """
                INSERT INTO scan_runs(id, started_at, finished_at, status, error)
                VALUES (?, ?, ?, 'failed', ?)
                ON CONFLICT(id) DO UPDATE SET finished_at=excluded.finished_at,
                    status='failed', error=excluded.error
                """,
                (scan_id, started_at, utcnow(), str(error)[-2000:]),
            )
        STATE.update(phase="error", message=str(error))
        raise
    finally:
        STATE.update(running=False)


def run_scan_sync():
    if not SCAN_LOCK.acquire(blocking=False):
        raise RuntimeError("Сканирование медиатеки уже выполняется")
    scan_id = str(uuid.uuid4())
    _prepare_state(scan_id)
    try:
        return _scan_impl(scan_id)
    finally:
        SCAN_LOCK.release()


def _manual_scan_worker(scan_id):
    try:
        _scan_impl(scan_id)
    except Exception:
        pass
    finally:
        SCAN_LOCK.release()


def start_scan():
    if not SCAN_LOCK.acquire(blocking=False):
        return False
    scan_id = str(uuid.uuid4())
    _prepare_state(scan_id)
    try:
        threading.Thread(
            target=_manual_scan_worker, args=(scan_id,),
            name="media-scanner", daemon=True,
        ).start()
    except Exception:
        SCAN_LOCK.release()
        STATE.update(running=False, phase="error", message="Не удалось запустить сканирование")
        raise
    return True
