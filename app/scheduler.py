import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .db import (
    connect, create_scheduler_run, get_setting, replaced_jobs_with_backups, set_settings,
    update_scheduler_run, utcnow,
)
from .scanner import media_roots, run_scan_sync


SETTING_KEY = "scheduler"


def default_timezone():
    name = os.environ.get("TZ", "").strip()
    try:
        ZoneInfo(name)
        return name
    except (ValueError, ZoneInfoNotFoundError):
        return "UTC"


DEFAULTS = {
    "enabled": False,
    "time": "04:00",
    "days": [1, 2, 3, 4, 5, 6, 7],
    "timezone": default_timezone(),
    "scan_before_run": True,
    "minimum_size_enabled": True,
    "minimum_size_gb": 3.0,
    "minimum_size_unit": "gb",
    "minimum_bitrate_enabled": True,
    "minimum_bitrate_mbps": 30.0,
    "minimum_duration_enabled": False,
    "minimum_duration_seconds": 0,
    "codec_enabled": False,
    "codecs": ["h264", "hevc"],
    "resolution_enabled": False,
    "minimum_width": 1920,
    "minimum_height": 1080,
    "minimum_age_enabled": True,
    "minimum_age_hours": 24,
    "path_enabled": False,
    "path_contains": "",
    "exclude_handbrake_tagged": True,
    "maximum_jobs_per_run": 20,
    "missed_run_grace_minutes": 30,
    "auto_replace_ready": False,
    "cleanup_backups_enabled": False,
    "backup_retention_days": 30,
}

BOOLEAN_KEYS = {
    "enabled", "scan_before_run", "minimum_size_enabled",
    "minimum_bitrate_enabled", "minimum_duration_enabled", "codec_enabled",
    "resolution_enabled", "minimum_age_enabled", "path_enabled",
    "exclude_handbrake_tagged", "auto_replace_ready", "cleanup_backups_enabled",
}


def _number(value, name, minimum, maximum, integer=False):
    try:
        result = int(value) if integer else float(value)
    except (TypeError, ValueError):
        raise ValueError(f"Некорректное значение «{name}»") from None
    if result < minimum or result > maximum:
        raise ValueError(f"«{name}» должно быть от {minimum} до {maximum}")
    return result


def validate_settings(values):
    unknown = set(values) - set(DEFAULTS)
    if unknown:
        raise ValueError(f"Неизвестные настройки: {', '.join(sorted(unknown))}")
    result = dict(DEFAULTS)
    result.update(values)

    for key in BOOLEAN_KEYS:
        if not isinstance(result[key], bool):
            raise ValueError(f"«{key}» должно быть логическим значением")

    try:
        datetime.strptime(str(result["time"]), "%H:%M")
    except ValueError:
        raise ValueError("Время запуска должно иметь формат ЧЧ:ММ") from None

    result["timezone"] = str(result["timezone"]).strip()
    try:
        ZoneInfo(result["timezone"])
    except (ValueError, ZoneInfoNotFoundError):
        raise ValueError("Неизвестный часовой пояс") from None

    if not isinstance(result["days"], list) or not result["days"]:
        raise ValueError("Нужно выбрать хотя бы один день недели")
    result["days"] = sorted({_number(day, "день недели", 1, 7, True) for day in result["days"]})

    result["minimum_size_gb"] = _number(result["minimum_size_gb"], "минимальный размер", 0, 10000)
    result["minimum_size_unit"] = str(result["minimum_size_unit"]).lower()
    if result["minimum_size_unit"] not in {"mb", "gb"}:
        raise ValueError("Единица минимального размера должна быть МБ или ГБ")
    result["minimum_bitrate_mbps"] = _number(result["minimum_bitrate_mbps"], "минимальный битрейт", 0, 10000)
    result["minimum_duration_seconds"] = _number(result["minimum_duration_seconds"], "минимальная длительность", 0, 864000, True)
    result["minimum_width"] = _number(result["minimum_width"], "минимальная ширина", 0, 16384, True)
    result["minimum_height"] = _number(result["minimum_height"], "минимальная высота", 0, 16384, True)
    result["minimum_age_hours"] = _number(result["minimum_age_hours"], "минимальный возраст", 0, 87600, True)
    result["maximum_jobs_per_run"] = _number(result["maximum_jobs_per_run"], "лимит заданий", 1, 1000, True)
    result["missed_run_grace_minutes"] = _number(result["missed_run_grace_minutes"], "допуск запуска", 1, 1440, True)
    result["backup_retention_days"] = _number(result["backup_retention_days"], "срок хранения резервов", 1, 36500, True)

    allowed_codecs = {"h264", "hevc", "av1", "vp9", "mpeg4"}
    if not isinstance(result["codecs"], list):
        raise ValueError("Список кодеков должен быть массивом")
    result["codecs"] = sorted({str(value).lower() for value in result["codecs"]})
    if set(result["codecs"]) - allowed_codecs:
        raise ValueError("В списке есть неподдерживаемый кодек")
    if result["codec_enabled"] and not result["codecs"]:
        raise ValueError("Выберите хотя бы один исходный кодек")

    result["path_contains"] = str(result["path_contains"]).strip()[:500]
    if result["path_enabled"] and not result["path_contains"]:
        raise ValueError("Укажите часть пути")
    return result


def get_scheduler_settings():
    stored = get_setting(SETTING_KEY, {})
    if not isinstance(stored, dict):
        stored = {}
    return validate_settings({key: value for key, value in stored.items() if key in DEFAULTS})


def save_scheduler_settings(values):
    validated = validate_settings(values)
    set_settings({SETTING_KEY: validated})
    return validated


def _candidate_where(settings):
    where = [
        "videos.present=1",
        "videos.probe_error IS NULL",
        "videos.subtitle_streams=0",
        "videos.data_streams=0",
        "videos.audio_streams<=1",
        "videos.container LIKE 'mov,mp4%'",
        "NOT EXISTS (SELECT 1 FROM optimization_jobs j WHERE j.video_id=videos.id)",
    ]
    values = []
    if settings["exclude_handbrake_tagged"]:
        where.append("videos.classification!='historical_handbrake'")
    if settings["minimum_size_enabled"]:
        where.append("videos.size_bytes>=?")
        values.append(int(settings["minimum_size_gb"] * 1024 ** 3))
    if settings["minimum_bitrate_enabled"]:
        where.append("videos.bit_rate>=?")
        values.append(int(settings["minimum_bitrate_mbps"] * 1_000_000))
    if settings["minimum_duration_enabled"]:
        where.append("videos.duration_seconds>=?")
        values.append(settings["minimum_duration_seconds"])
    if settings["codec_enabled"]:
        placeholders = ",".join("?" for _ in settings["codecs"])
        where.append(f"videos.video_codec IN ({placeholders})")
        values.extend(settings["codecs"])
    if settings["resolution_enabled"]:
        where.extend(["videos.width>=?", "videos.height>=?"])
        values.extend([settings["minimum_width"], settings["minimum_height"]])
    if settings["minimum_age_enabled"]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=settings["minimum_age_hours"])
        where.extend(["videos.first_seen_at IS NOT NULL", "videos.first_seen_at<=?"])
        values.append(cutoff.isoformat())
    if settings["path_enabled"]:
        escaped = settings["path_contains"].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("videos.relative_path LIKE ? ESCAPE '\\'")
        values.append(f"%{escaped}%")
    return where, values


def _path_is_stable(row):
    try:
        path = Path(row["path"]).resolve(strict=True)
        if not any(path.is_relative_to(root) for root in media_roots()):
            return False
        stat = path.stat()
        return stat.st_size == int(row["size_bytes"]) and stat.st_mtime_ns == int(row["mtime_ns"])
    except (OSError, ValueError):
        return False


def find_candidates(settings=None, *, preview_limit=20, enqueue_limit=None):
    settings = validate_settings(settings or get_scheduler_settings())
    where, values = _candidate_where(settings)
    requested = int(enqueue_limit or preview_limit)
    with connect() as connection:
        sql_where = " AND ".join(where)
        rows = connection.execute(
            f"SELECT * FROM videos WHERE {sql_where} "
            "ORDER BY size_bytes DESC, id ASC LIMIT ?",
            (*values, 10000),
        ).fetchall()
    stable = [row for row in rows if _path_is_stable(row)]
    return stable[:requested], len(stable)


def preview_candidates(settings=None):
    settings = validate_settings(settings or get_scheduler_settings())
    rows, total = find_candidates(settings, preview_limit=20)
    return {
        "total": total,
        "shown": len(rows),
        "total_bytes_shown": sum(row["size_bytes"] for row in rows),
        "items": [dict(row) for row in rows],
    }


def due_scheduled_slot(settings=None, now=None):
    settings = validate_settings(settings or get_scheduler_settings())
    if not settings["enabled"]:
        return None
    zone = ZoneInfo(settings["timezone"])
    local_now = (now or datetime.now(timezone.utc)).astimezone(zone)
    if local_now.isoweekday() not in settings["days"]:
        return None
    hour, minute = map(int, settings["time"].split(":"))
    scheduled = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    delay = local_now - scheduled
    if delay.total_seconds() < 0:
        return None
    if delay > timedelta(minutes=settings["missed_run_grace_minutes"]):
        return None
    return scheduled.isoformat()


def next_scheduled_run(settings=None, now=None):
    settings = validate_settings(settings or get_scheduler_settings())
    if not settings["enabled"]:
        return None
    zone = ZoneInfo(settings["timezone"])
    local_now = (now or datetime.now(timezone.utc)).astimezone(zone)
    hour, minute = map(int, settings["time"].split(":"))
    for offset in range(8):
        day = local_now + timedelta(days=offset)
        slot = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if slot > local_now and slot.isoweekday() in settings["days"]:
            return slot.isoformat()
    return None


def run_plan(now=None):
    """What the next scheduled run would do with the current index and settings. Read-only."""
    settings = get_scheduler_settings()
    next_run = next_scheduled_run(settings, now=now)
    next_at = datetime.fromisoformat(next_run) if next_run else None
    retention = timedelta(days=settings["backup_retention_days"])
    cleanup_active = settings["enabled"] and settings["cleanup_backups_enabled"]
    backups = []
    for row in replaced_jobs_with_backups():
        expires_at = None
        if cleanup_active and row["replaced_at"]:
            expires_at = datetime.fromisoformat(row["replaced_at"]) + retention
        backups.append({
            "job_id": row["id"], "relative_path": row["relative_path"],
            "bytes": row["source_size"], "replaced_at": row["replaced_at"],
            "expires_at": expires_at.isoformat() if expires_at else None,
            # Cleanup deletes backups replaced at least retention days before the run.
            "due": bool(expires_at and next_at and expires_at <= next_at),
        })
    candidates = {"total": 0, "will_add": 0, "items": []}
    if settings["enabled"]:
        rows, total = find_candidates(settings, preview_limit=5)
        candidates = {
            "total": total, "will_add": min(total, settings["maximum_jobs_per_run"]),
            "items": [
                {key: row[key] for key in ("id", "relative_path", "size_bytes", "bit_rate")}
                for row in rows
            ],
        }
    return {
        "settings": settings, "next_run": next_run,
        "backups": backups, "candidates": candidates,
    }


def run_scheduler(*, trigger="manual", scheduled_for=None):
    settings = get_scheduler_settings()
    if trigger == "schedule" and not settings["enabled"]:
        return None
    run_id = create_scheduler_run(
        trigger=trigger, scheduled_for=scheduled_for, rules=settings, started_at=utcnow()
    )
    if run_id is None:
        return None
    try:
        # Scan first: cleanup looks up files that Immich may have moved.
        scan_id = None
        if settings["scan_before_run"]:
            scan_id = run_scan_sync()["scan_id"]
        cleanup = {"deleted": [], "failed": []}
        if settings["cleanup_backups_enabled"]:
            from .fileops import delete_backups_older_than
            cleanup = delete_backups_older_than(settings["backup_retention_days"])
        rows, candidates = find_candidates(
            settings, enqueue_limit=settings["maximum_jobs_per_run"]
        )
        from .encoder import enqueue_videos
        created, skipped = enqueue_videos(
            [row["id"] for row in rows], created_by="scheduler",
            scheduler_run_id=run_id, wake=False,
        )
        update_scheduler_run(
            run_id, status="completed", scan_run_id=scan_id,
            candidates=candidates, added=len(created), skipped=len(skipped),
            finished_at=utcnow(),
            error=("Не удалось очистить часть резервов: " + "; ".join(
                f"#{item['id']}: {item['error']}" for item in cleanup["failed"]
            ))[-2000:] if cleanup["failed"] else None,
        )
        return {
            "run_id": run_id, "candidates": candidates,
            "added": len(created), "skipped": len(skipped),
            "backups_deleted": len(cleanup["deleted"]),
            "backup_cleanup_failed": len(cleanup["failed"]),
        }
    except Exception as error:
        update_scheduler_run(
            run_id, status="failed", finished_at=utcnow(), error=str(error)[-2000:]
        )
        raise
