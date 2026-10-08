import json
import math
import shutil
from functools import wraps
from urllib.parse import urlsplit

from flask import Blueprint, jsonify, request

from . import statuses
from .db import (
    dismiss_events, get_job, job_counts, overview_facts, query_events, query_jobs,
    query_scheduler_runs, query_videos, summary, video_ids_for_filters,
)
from .encoder import (
    STATE as ENCODE_STATE,
    cancel_queued_job,
    discard_job,
    enqueue_videos,
    output_paths,
    retry_job,
    revalidate_failed_job,
    stop_running_job,
    unsupported_reason,
    work_root,
)
from .fileops import (
    FILE_LOCK,
    REPLACEMENT_ENABLED,
    backup_summary,
    delete_all_backups,
    delete_job_backup,
    request_reconcile,
    request_replace,
    request_replace_all,
    request_restore,
)
from .runtime import request_scheduler_run, runtime_snapshot, wake_runtime
from .profile import (
    get_encoding_profile, profile_options, save_encoding_profile,
)
from .scanner import STATE as SCAN_STATE, start_scan
from .scheduler import (
    get_scheduler_settings, next_scheduled_run, preview_candidates, run_plan,
    save_scheduler_settings,
)


api = Blueprint("api", __name__, url_prefix="/api")
CSRF_HEADER = "X-Optimizer-Request"


@api.before_request
def reject_cross_site_mutations():
    """The UI has no login, so a foreign page must not be able to trigger actions.

    A custom header cannot be sent cross-origin without a CORS preflight, which
    this API never grants.
    """
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return None
    origin = request.headers.get("Origin")
    if request.headers.get(CSRF_HEADER) != "1" or (
        origin and urlsplit(origin).netloc != request.host
    ):
        return jsonify(ok=False, error="Запрос отклонён: нет заголовка интерфейса оптимизатора"), 403
    return None


def as_dict(row):
    return dict(row) if row is not None else None


def pagination(page, page_size, total):
    return {
        "page": page,
        "page_size": page_size,
        "total_items": total,
        "total_pages": max(1, math.ceil(total / page_size)),
    }


def page_args(default_size=50):
    page = max(1, request.args.get("page", 1, type=int))
    size = request.args.get("page_size", default_size, type=int)
    return page, min(100, max(1, size))


def video_filters(source):
    return {
        "query": str(source.get("query", "")).strip(),
        "codec": str(source.get("codec", "")).strip().lower(),
        "classification": str(source.get("classification", "")).strip(),
        "minimum_mib": source.get("minimum_mib", 0) or 0,
        "minimum_bitrate_mbps": source.get("minimum_bitrate_mbps", 0) or 0,
        "job_status": str(source.get("job_status", "")).strip(),
    }


def mutation(handler):
    @wraps(handler)
    def wrapped(*args, **kwargs):
        try:
            result = handler(*args, **kwargs)
            if result is None:
                result = {}
            return jsonify(ok=True, **result)
        except (ValueError, RuntimeError, OSError) as error:
            return jsonify(ok=False, error=str(error)), 409
    return wrapped


@api.get("/overview")
def overview():
    root = work_root()
    while not root.exists() and root != root.parent:
        root = root.parent
    disk = shutil.disk_usage(root)
    return jsonify(
        summary=as_dict(summary()),
        jobs=job_counts(),
        backups=backup_summary(),
        scan=SCAN_STATE.snapshot(),
        encode=ENCODE_STATE.snapshot(),
        runtime=runtime_snapshot(),
        scheduler=get_scheduler_settings(),
        next_run=next_scheduled_run(),
        facts=overview_facts(),
        events=[as_dict(row) for row in query_events(limit=20)],
        disk={"free": disk.free, "total": disk.total},
    )


@api.get("/plan")
def plan():
    return jsonify(replacement_enabled=REPLACEMENT_ENABLED, **run_plan())


@api.get("/videos")
def videos():
    page, page_size = page_args()
    filters = video_filters(request.args)
    rows, total = query_videos(
        filters, page=page, page_size=page_size,
        sort=request.args.get("sort", "size"),
        order=request.args.get("order", "desc"),
    )
    return jsonify(
        items=[as_dict(row) for row in rows],
        pagination=pagination(page, page_size, total),
    )


@api.get("/jobs")
def jobs():
    page, page_size = page_args()
    view = request.args.get("view", "")
    selected = None
    if view == "queue":
        selected = list(statuses.QUEUE_VIEW)
    elif view == "journal":
        selected = list(statuses.JOURNAL_VIEW)
    if request.args.get("status"):
        requested = [item for item in request.args["status"].split(",") if item]
        selected = [item for item in requested if selected is None or item in selected]
    rows, total = query_jobs(
        page=page, page_size=page_size, statuses=selected,
        created_by=request.args.get("created_by", ""),
        sort=request.args.get("sort", "id"),
        order=request.args.get("order", "desc"),
        query=request.args.get("query", "").strip(),
        has_backup=request.args.get("has_backup") == "1",
    )
    return jsonify(
        items=[as_dict(row) for row in rows],
        pagination=pagination(page, page_size, total),
    )


@api.get("/jobs/<int:job_id>")
def job_detail(job_id):
    job = get_job(job_id)
    if job is None:
        return jsonify(ok=False, error="Задание не найдено"), 404
    log_tail = ""
    try:
        _, _, log_path = output_paths(job, create=False)
        if log_path.is_file():
            with log_path.open("rb") as stream:
                stream.seek(max(0, log_path.stat().st_size - 16384))
                log_tail = stream.read().decode("utf-8", "replace")
    except (OSError, ValueError, KeyError):
        pass
    item = as_dict(job)
    try:
        item["validation"] = json.loads(job["validation_json"] or "null")
    except ValueError:
        item["validation"] = None
    item["unsupported_reason"] = unsupported_reason(job)
    return jsonify(job=item, log_tail=log_tail)


@api.get("/events")
def events():
    include = request.args.get("all") == "1"
    return jsonify(items=[as_dict(row) for row in query_events(include_dismissed=include, limit=200)])


@api.post("/events/<int:event_id>/dismiss")
@mutation
def dismiss_event(event_id):
    dismiss_events(event_id)
    return {}


@api.post("/events/dismiss-all")
@mutation
def dismiss_all_events():
    dismiss_events()
    return {}


@api.get("/scheduler-runs")
def scheduler_runs():
    page, page_size = page_args(25)
    rows, total = query_scheduler_runs(page=page, page_size=page_size)
    return jsonify(
        items=[as_dict(row) for row in rows],
        pagination=pagination(page, page_size, total),
    )


@api.get("/settings/scheduler")
def scheduler_settings():
    return jsonify(settings=get_scheduler_settings())


@api.put("/settings/scheduler")
@mutation
def update_scheduler_settings_api():
    values = request.get_json(silent=True)
    if not isinstance(values, dict):
        raise ValueError("Ожидался JSON-объект настроек")
    return {"settings": save_scheduler_settings(values)}


@api.get("/settings/encoding")
def encoding_settings():
    return jsonify(settings=get_encoding_profile(), options=profile_options())


@api.put("/settings/encoding")
@mutation
def update_encoding_settings_api():
    values = request.get_json(silent=True)
    if not isinstance(values, dict):
        raise ValueError("Ожидался JSON-объект настроек")
    return {"settings": save_encoding_profile(values), "options": profile_options()}


@api.post("/scheduler/preview")
@mutation
def scheduler_preview():
    values = request.get_json(silent=True)
    return {"preview": preview_candidates(values if isinstance(values, dict) else None)}


@api.post("/scheduler/run-now")
@mutation
def scheduler_run_now():
    if not request_scheduler_run():
        raise RuntimeError("Ручной запуск планировщика уже ожидает выполнения")
    return {"accepted": True}


@api.post("/scan")
@mutation
def scan():
    if not start_scan():
        raise RuntimeError("Сканирование уже выполняется")
    return {"accepted": True}


@api.post("/jobs")
@mutation
def create_jobs():
    payload = request.get_json(silent=True) or {}
    if "video_ids" in payload:
        ids = payload["video_ids"]
        if not isinstance(ids, list):
            raise ValueError("video_ids должен быть массивом")
        ids = [int(value) for value in ids[:10000]]
    elif "filters" in payload:
        if not isinstance(payload["filters"], dict):
            raise ValueError("filters должен быть объектом")
        ids = video_ids_for_filters(video_filters(payload["filters"]), limit=10000)
    else:
        raise ValueError("Не переданы видео или фильтр")
    created, skipped = enqueue_videos(ids, created_by="admin")
    wake_runtime()
    return {"created": created, "skipped": skipped}


@api.post("/jobs/<int:job_id>/retry")
@mutation
def retry(job_id):
    retry_job(job_id)
    return {"job_id": job_id}


@api.post("/jobs/<int:job_id>/cancel")
@mutation
def cancel(job_id):
    cancel_queued_job(job_id)
    return {"job_id": job_id}


@api.post("/jobs/<int:job_id>/discard")
@mutation
def discard(job_id):
    discard_job(job_id)
    return {"job_id": job_id}


@api.post("/jobs/<int:job_id>/stop")
@mutation
def stop(job_id):
    stop_running_job(job_id)
    return {"job_id": job_id}


@api.post("/jobs/<int:job_id>/reconcile")
@mutation
def reconcile(job_id):
    request_reconcile(job_id)
    return {"job_id": job_id, "accepted": True}


@api.post("/jobs/<int:job_id>/revalidate")
@mutation
def revalidate(job_id):
    status, saving = revalidate_failed_job(job_id)
    return {"job_id": job_id, "status": status, "saving_percent": saving}


@api.post("/jobs/<int:job_id>/replace")
@mutation
def replace(job_id):
    request_replace(job_id)
    return {"job_id": job_id, "accepted": True}


@api.post("/jobs/replace-ready")
@mutation
def replace_ready():
    return request_replace_all()


@api.post("/jobs/<int:job_id>/backup/delete")
@mutation
def delete_backup(job_id):
    if not FILE_LOCK.acquire(timeout=1):
        raise RuntimeError("Выполняется другая файловая операция; повторите позже")
    try:
        delete_job_backup(job_id)
    finally:
        FILE_LOCK.release()
    return {"job_id": job_id}


@api.post("/backups/delete-all")
@mutation
def delete_backups():
    if not FILE_LOCK.acquire(timeout=1):
        raise RuntimeError("Выполняется другая файловая операция; повторите позже")
    try:
        return delete_all_backups()
    finally:
        FILE_LOCK.release()


@api.post("/jobs/<int:job_id>/restore")
@mutation
def restore(job_id):
    request_restore(job_id)
    return {"job_id": job_id, "accepted": True}


@api.get("/runtime-status")
def runtime_status():
    return jsonify(
        scan=SCAN_STATE.snapshot(), encode=ENCODE_STATE.snapshot(),
        runtime=runtime_snapshot(), jobs=job_counts(),
    )


@api.get("/statuses")
def status_groups():
    return jsonify(statuses.for_client())
