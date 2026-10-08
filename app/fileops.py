"""File operations on the media library: replace, restore, backups and recovery.

Every operation that touches library or backup files runs under FILE_LOCK, and
every status change goes through transition_job. A double click, the scheduler
and the web UI therefore cannot run the same operation twice. The backup path is
stored before any file is moved, so an interrupted operation can be finished or
rolled back from the SHA-1 of the files found on disk.
"""
import ctypes
import json
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from . import statuses
from .db import (
    add_event, backup_job_paths, connect, find_videos_by_size, get_job,
    jobs_with_backups_older_than, jobs_with_status, transition_job, update_job,
    update_video_after_replacement, upsert_video,
)
from .encoder import safe_source, sha1_file, utcnow, work_root
from .scanner import media_roots, record_for

REPLACEMENT_ENABLED = os.environ.get("REPLACEMENT_ENABLED", "false").lower() == "true"
ENCODER_TAG = "HandBrake 1.11.2"

FILE_LOCK = threading.RLock()
EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="file-ops")


@contextmanager
def file_lock(timeout=None):
    acquired = FILE_LOCK.acquire(timeout=-1 if timeout is None else timeout)
    if not acquired:
        raise RuntimeError("Выполняется другая файловая операция; повторите позже")
    try:
        yield
    finally:
        FILE_LOCK.release()


def _background(function, job_id):
    def run():
        try:
            function(job_id)
        except Exception:
            pass  # The failure is already stored in the job and in events.
    EXECUTOR.submit(run)


def fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_file(path):
    with open(path, "rb") as stream:
        os.fsync(stream.fileno())


def exchange_paths(first, second):
    """Atomically exchange two existing paths using Linux renameat2."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = libc.renameat2
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    at_fdcwd = -100
    rename_exchange = 2
    result = renameat2(at_fdcwd, os.fsencode(first), at_fdcwd, os.fsencode(second), rename_exchange)
    if result:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), f"{first} ↔ {second}")


def backups_root():
    return work_root() / "backups"


def planned_backup_path(job, source):
    return backups_root() / str(int(job["id"])) / f"{Path(source).name}.original"


def _inside(path, root):
    return Path(path).resolve().is_relative_to(Path(root).resolve())


def safe_backup(job):
    if not job["backup_path"]:
        raise ValueError("У задания нет резервного оригинала")
    backup = Path(job["backup_path"]).resolve(strict=True)
    if not backup.is_relative_to(backups_root().resolve()) or not backup.is_file():
        raise RuntimeError("Путь резервного оригинала находится вне каталога backups")
    return backup


def backup_summary():
    root = backups_root()
    attached = {
        str(Path(row["backup_path"]).resolve()) for row in backup_job_paths()
    }
    total = count = unattached = unattached_bytes = 0
    if root.is_dir():
        for path in root.rglob("*"):
            try:
                if not path.is_file():
                    continue
                size = path.stat().st_size
            except FileNotFoundError:
                continue
            total += size
            count += 1
            if str(path.resolve()) not in attached:
                unattached += 1
                unattached_bytes += size
    return {"bytes": total, "count": count, "unattached": unattached,
            "unattached_bytes": unattached_bytes}


def remove_empty_backup_directories(start):
    root = backups_root().resolve()
    current = Path(start).resolve()
    while current != root and current.is_relative_to(root):
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def file_state(path, job):
    """Classify a path as missing, original, optimized or other by size and SHA-1."""
    if path is None:
        return "missing"
    path = Path(path)
    if not os.path.lexists(path):
        return "missing"
    if path.is_symlink() or not path.is_file():
        return "other"
    size = path.stat().st_size
    if size == int(job["source_size"] or -1) and sha1_file(path) == job["original_sha1"]:
        return "original"
    if size == int(job["optimized_size"] or -1) and sha1_file(path) == job["optimized_sha1"]:
        return "optimized"
    return "other"


def locate_current_file(job):
    """Find the optimized file in the library, following moves made by Immich."""
    try:
        source = safe_source(job)
        if source.stat().st_size == int(job["optimized_size"]):
            return source
    except (OSError, ValueError):
        pass
    for row in find_videos_by_size(job["optimized_size"]):
        try:
            candidate = safe_source(row)
        except (OSError, ValueError):
            continue
        if sha1_file(candidate) != job["optimized_sha1"]:
            continue
        update_job(job["id"], source_path=str(candidate), video_id=row["id"])
        add_event("info", "locate", f"Файл задания #{job['id']} перемещён Immich: {candidate}", job["id"])
        return candidate
    raise FileNotFoundError(
        "Оптимизированный файл не найден в медиатеке: он удалён или перемещён "
        "в Immich; обновите индекс медиатеки"
    )


# Replacement ---------------------------------------------------------------

def _claim_replace(job_id):
    if not REPLACEMENT_ENABLED:
        raise RuntimeError("Физическая замена отключена конфигурацией")
    job = get_job(job_id)
    if job is None or job["status"] != statuses.READY:
        raise ValueError("Заменить можно только задание в состоянии ready")
    source = safe_source(job)
    claimed = transition_job(
        job_id, statuses.READY, status=statuses.REPLACING, phase=statuses.REPLACING,
        error=None, backup_path=str(planned_backup_path(job, source)),
    )
    if not claimed:
        raise ValueError("Задание уже обрабатывается другой операцией")


def _finish_replace(job, source, backup):
    validation = json.loads(job["validation_json"] or "{}")
    replaced_stat = Path(source).stat()
    update_video_after_replacement(
        job["video_id"], size_bytes=replaced_stat.st_size,
        mtime_ns=replaced_stat.st_mtime_ns,
        duration_seconds=validation.get("duration_seconds"),
        bit_rate=validation.get("video_bit_rate"),
        video_codec=validation.get("video_codec"),
        width=validation.get("width"), height=validation.get("height"),
        rotation=validation.get("rotation"), encoder_tag=ENCODER_TAG,
    )
    transition_job(
        job["id"], (statuses.REPLACING, *statuses.NEEDS_RECONCILE),
        status=statuses.REPLACED, phase=statuses.REPLACED, backup_path=str(backup),
        replaced_at=utcnow(), error=None,
    )


def _perform_replace(job_id):
    with file_lock():
        job = get_job(job_id)
        if job is None or job["status"] != statuses.REPLACING:
            raise ValueError("Задание не ожидает замены")
        exchanged = False
        try:
            source = safe_source(job)
            optimized = Path(job["output_path"]).resolve(strict=True)
            if not optimized.is_relative_to(work_root()):
                raise RuntimeError("Результат находится вне рабочего каталога")
            backup = Path(job["backup_path"])
            if backup != planned_backup_path(job, source):
                raise RuntimeError("Путь резервного оригинала не совпадает с ожидаемым")
            if os.stat(source).st_dev != os.stat(optimized).st_dev:
                raise RuntimeError("Атомарная замена невозможна: media и work находятся на разных filesystem")
            if not os.access(source.parent, os.W_OK):
                raise RuntimeError("Нет права записи в каталог медиатеки")
            source_stat = source.stat()
            if (source_stat.st_size != int(job["source_size"])
                    or source_stat.st_mtime_ns != int(job["source_mtime_ns"])):
                raise RuntimeError("Исходник изменился после кодирования; замена отменена")
            if sha1_file(source) != job["original_sha1"]:
                raise RuntimeError("SHA-1 исходника больше не совпадает с записанным")
            if optimized.stat().st_size != int(job["optimized_size"]):
                raise RuntimeError("Размер готового результата изменился")
            if sha1_file(optimized) != job["optimized_sha1"]:
                raise RuntimeError("SHA-1 готового результата больше не совпадает с записанным")
            backup.parent.mkdir(parents=True, exist_ok=True)
            if os.path.lexists(backup):
                raise FileExistsError(f"Резервная копия уже существует: {backup}")

            # The replacement should look like the original to filesystem consumers.
            os.chown(optimized, source_stat.st_uid, source_stat.st_gid)
            os.chmod(optimized, source_stat.st_mode & 0o7777)
            os.utime(optimized, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
            fsync_file(optimized)
            exchange_paths(source, optimized)
            exchanged = True
            fsync_directory(source.parent)
            fsync_directory(optimized.parent)
            # After the exchange, optimized points to the old original inode.
            os.replace(optimized, backup)
            fsync_directory(backup.parent)
            fsync_directory(optimized.parent)
            _finish_replace(job, source, backup)
            return backup
        except Exception as error:
            if exchanged:
                transition_job(job_id, statuses.REPLACING, status=statuses.REPLACEMENT_INTERRUPTED,
                               phase=statuses.REPLACEMENT_INTERRUPTED, error=str(error))
            else:
                transition_job(job_id, statuses.REPLACING, status=statuses.READY,
                               phase=statuses.READY, error=str(error), backup_path=None)
            add_event("error", "replace", f"Замена задания #{job_id}: {error}", job_id)
            raise


def request_replace(job_id, *, wait=False):
    _claim_replace(job_id)
    if wait:
        return _perform_replace(job_id)
    _background(_perform_replace, job_id)
    return None


def request_replace_all():
    accepted, failed = [], []
    for row in jobs_with_status((statuses.READY,)):
        try:
            request_replace(row["id"])
            accepted.append(row["id"])
        except Exception as error:
            failed.append({"id": row["id"], "error": str(error)})
    return {"accepted": accepted, "failed": failed}


# Restore -------------------------------------------------------------------

def _finish_restore(job, source, backup):
    """Complete a restore after the original is back at the library path."""
    if os.path.lexists(backup):
        if file_state(backup, job) != "optimized":
            raise RuntimeError("По пути резерва лежит неожиданный файл; нужна ручная проверка")
        backup.unlink()
        fsync_directory(backup.parent)
    root = next((root for root in media_roots() if Path(source).resolve().is_relative_to(root)), None)
    if root is None:
        raise RuntimeError("Файл медиатеки находится вне каталогов MEDIA_ROOTS")
    item = record_for(root, Path(source), f"restore-{job['id']}-{uuid.uuid4().hex}", utcnow())
    with connect() as connection:
        upsert_video(connection, item)
    transition_job(
        job["id"], (statuses.RESTORING, *statuses.NEEDS_RECONCILE),
        status=statuses.RESTORED, phase=statuses.RESTORED, backup_path=None,
        restored_at=utcnow(), error=None,
    )
    remove_empty_backup_directories(backup.parent)


def _perform_restore(job_id):
    with file_lock():
        job = get_job(job_id)
        if job is None or job["status"] != statuses.RESTORING:
            raise ValueError("Задание не ожидает возврата оригинала")
        exchanged = False
        try:
            source = locate_current_file(job)
            job = get_job(job_id)
            backup = safe_backup(job)
            if source.stat().st_dev != backup.stat().st_dev:
                raise RuntimeError("Атомарный возврат невозможен: файлы находятся на разных filesystem")
            if file_state(source, job) != "optimized":
                raise RuntimeError("Файл в медиатеке не совпадает с оптимизированной версией")
            if file_state(backup, job) != "original":
                raise RuntimeError("Резервный оригинал не совпадает с записанным SHA-1")
            exchange_paths(source, backup)
            exchanged = True
            fsync_directory(source.parent)
            fsync_directory(backup.parent)
            _finish_restore(job, source, backup)
        except Exception as error:
            if exchanged:
                transition_job(job_id, statuses.RESTORING, status=statuses.RESTORE_INTERRUPTED,
                               phase=statuses.RESTORE_INTERRUPTED, error=str(error))
            else:
                transition_job(job_id, statuses.RESTORING, status=statuses.REPLACED,
                               phase=statuses.REPLACED, error=str(error))
            add_event("error", "restore", f"Возврат оригинала задания #{job_id}: {error}", job_id)
            raise


def request_restore(job_id, *, wait=False):
    job = get_job(job_id)
    if job is None or job["status"] != statuses.REPLACED or not job["backup_path"]:
        raise ValueError("Вернуть оригинал можно только для заменённого видео с резервом")
    if not transition_job(job_id, statuses.REPLACED, status=statuses.RESTORING,
                          phase=statuses.RESTORING, error=None):
        raise ValueError("Задание уже обрабатывается другой операцией")
    if wait:
        return _perform_restore(job_id)
    _background(_perform_restore, job_id)
    return None


# Backups -------------------------------------------------------------------

def delete_job_backup(job_id, *, require_library_file=False):
    with file_lock():
        job = get_job(job_id)
        if job is None or job["status"] != statuses.REPLACED:
            raise ValueError("Удалить резерв можно только у заменённого видео")
        backup = safe_backup(job)
        if file_state(backup, job) != "original":
            raise RuntimeError("Резервный оригинал не совпадает с записанными размером и SHA-1")
        if require_library_file:
            locate_current_file(job)
        parent = backup.parent
        backup.unlink()
        fsync_directory(parent)
        update_job(job_id, backup_path=None)
        remove_empty_backup_directories(parent)


def delete_all_backups():
    """Delete verified backups of replaced jobs. Unknown files are never touched."""
    deleted, failed = [], []
    with file_lock():
        for row in jobs_with_status((statuses.REPLACED,)):
            job = get_job(row["id"])
            if not job["backup_path"]:
                continue
            try:
                delete_job_backup(job["id"])
                deleted.append(job["id"])
            except Exception as error:
                failed.append({"id": job["id"], "error": str(error)})
    return {"deleted": deleted, "failed": failed, "unattached": backup_summary()["unattached"]}


def delete_backups_older_than(days, *, now=None):
    from datetime import datetime, timedelta, timezone
    try:
        retention_days = int(days)
    except (TypeError, ValueError):
        raise ValueError("Срок хранения резервов должен быть целым числом дней") from None
    if retention_days < 1 or retention_days > 36500:
        raise ValueError("Срок хранения резервов должен быть от 1 до 36500 дней")
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=retention_days)
    deleted, failed = [], []
    for row in jobs_with_backups_older_than(cutoff.isoformat()):
        try:
            delete_job_backup(row["id"], require_library_file=True)
            deleted.append(row["id"])
        except Exception as error:
            failed.append({"id": row["id"], "error": str(error)})
            add_event("warning", "cleanup", f"Резерв задания #{row['id']} не удалён: {error}", row["id"])
    return {"deleted": deleted, "failed": failed}


# Recovery ------------------------------------------------------------------

def _reconcile_replace(job):
    source = Path(job["source_path"])
    output = Path(job["output_path"]) if job["output_path"] else None
    if output is not None and not _inside(output, work_root()):
        output = None
    backup = Path(job["backup_path"]) if job["backup_path"] else planned_backup_path(job, source)
    if not _inside(backup, backups_root()):
        raise RuntimeError("Путь резервного оригинала находится вне каталога backups")
    states = (file_state(source, job), file_state(output, job), file_state(backup, job))
    expected = statuses.NEEDS_RECONCILE

    if states == ("original", "optimized", "missing"):
        transition_job(job["id"], expected, status=statuses.READY, phase=statuses.READY,
                       backup_path=None, error=None)
        return statuses.READY
    if states == ("optimized", "original", "missing"):
        backup.parent.mkdir(parents=True, exist_ok=True)
        os.replace(output, backup)
        fsync_directory(backup.parent)
        fsync_directory(output.parent)
        _finish_replace(job, source, backup)
        return statuses.REPLACED
    if states == ("optimized", "missing", "original"):
        _finish_replace(job, source, backup)
        return statuses.REPLACED
    if states == ("original", "missing", "optimized") and output is not None:
        os.replace(backup, output)
        fsync_directory(output.parent)
        fsync_directory(backup.parent)
        remove_empty_backup_directories(backup.parent)
        transition_job(job["id"], expected, status=statuses.READY, phase=statuses.READY,
                       backup_path=None, error=None)
        return statuses.READY
    if states == ("original", "missing", "missing"):
        transition_job(job["id"], expected, status=statuses.FAILED, phase=statuses.FAILED,
                       backup_path=None,
                       error="Результат кодирования не найден; оригинал на месте, задание можно повторить")
        return statuses.FAILED
    labels = {"original": "оригинал", "optimized": "результат", "missing": "нет", "other": "чужой файл"}
    message = (
        "Автоматическое восстановление невозможно: медиатека — {}, рабочий файл — {}, резерв — {}"
        .format(*(labels[state] for state in states))
    )
    update_job(job["id"], error=message)
    raise RuntimeError(message)


def _reconcile_restore(job):
    source = Path(job["source_path"])
    if not job["backup_path"]:
        raise RuntimeError("У задания не записан путь резерва; нужна ручная проверка")
    backup = Path(job["backup_path"])
    if not _inside(backup, backups_root()):
        raise RuntimeError("Путь резервного оригинала находится вне каталога backups")
    states = (file_state(source, job), file_state(backup, job))
    if states == ("optimized", "original"):
        transition_job(job["id"], statuses.NEEDS_RECONCILE, status=statuses.REPLACED,
                       phase=statuses.REPLACED, error=None)
        return statuses.REPLACED
    if states in {("original", "optimized"), ("original", "missing")}:
        _finish_restore(job, source, backup)
        return statuses.RESTORED
    labels = {"original": "оригинал", "optimized": "результат", "missing": "нет", "other": "чужой файл"}
    message = "Автоматическое восстановление невозможно: медиатека — {}, резерв — {}".format(
        *(labels[state] for state in states))
    update_job(job["id"], error=message)
    raise RuntimeError(message)


def reconcile_job(job_id):
    with file_lock():
        job = get_job(job_id)
        if job is None or job["status"] not in statuses.NEEDS_RECONCILE:
            raise ValueError("Проверить можно только прерванную замену или возврат")
        try:
            if job["status"] == statuses.REPLACEMENT_INTERRUPTED:
                result = _reconcile_replace(job)
            else:
                result = _reconcile_restore(job)
        except Exception as error:
            add_event("error", "recovery", f"Задание #{job_id}: {error}", job_id)
            raise
        add_event("info", "recovery", f"Задание #{job_id} восстановлено после сбоя: {result}", job_id)
        return result


def request_reconcile(job_id):
    job = get_job(job_id)
    if job is None or job["status"] not in statuses.NEEDS_RECONCILE:
        raise ValueError("Проверить можно только прерванную замену или возврат")
    _background(reconcile_job, job_id)


def reconcile_all():
    results = {}
    for row in jobs_with_status(statuses.NEEDS_RECONCILE):
        try:
            results[row["id"]] = reconcile_job(row["id"])
        except Exception as error:
            results[row["id"]] = f"error: {error}"
    return results
