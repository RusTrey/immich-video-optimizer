import mimetypes
import shutil
import subprocess
from pathlib import Path

from flask import Flask, g, jsonify, redirect, render_template, request, send_file

from . import __version__, statuses
from .api import api
from .db import get_job, get_video, init_db
from .encoder import safe_source, work_root
from .fileops import REPLACEMENT_ENABLED, safe_backup
from .i18n import catalog, normalize_language, translate
from .runtime import start_runtime


def tool_version(command, *version_args):
    if not shutil.which(command):
        return None
    completed = subprocess.run(
        [command, *(version_args or ("--version",))],
        capture_output=True, text=True, timeout=10, check=False,
    )
    return (completed.stdout or completed.stderr).splitlines()[0]


def create_app():
    app = Flask(__name__)
    app.register_blueprint(api)
    init_db()
    start_runtime()

    @app.before_request
    def select_language():
        g.language = normalize_language(request.cookies.get("optimizer_language"))

    @app.context_processor
    def translation_context():
        language = getattr(g, "language", "ru")
        return {
            "lang": language,
            "messages": catalog(language),
            "t": lambda key, **values: translate(language, key, **values),
        }

    @app.get("/language/<language>")
    def change_language(language):
        language = normalize_language(language)
        target = request.args.get("next", "/")
        if not target.startswith("/") or target.startswith("//"):
            target = "/"
        response = redirect(target, code=303)
        response.set_cookie(
            "optimizer_language", language, max_age=31536000,
            samesite="Lax", httponly=True,
        )
        return response

    def page(template, active):
        return render_template(
            template, active=active, version=__version__,
            replacement_enabled=REPLACEMENT_ENABLED,
            status_groups=statuses.for_client(),
        )

    @app.get("/")
    def overview_page():
        return page("overview.html", "overview")

    @app.get("/queue")
    def queue_page():
        return page("queue.html", "queue")

    @app.get("/library")
    def library_page():
        return page("library.html", "library")

    @app.get("/journal")
    def journal_page():
        return page("journal.html", "journal")

    @app.get("/settings")
    def settings_page():
        return page("settings.html", "settings")

    def video_file(video_id):
        row = get_video(video_id)
        if row is None:
            raise ValueError("Видео отсутствует в актуальном индексе")
        return safe_source(row)

    @app.get("/watch/<int:video_id>")
    def watch(video_id):
        try:
            path = video_file(video_id)
        except (ValueError, OSError):
            return "Видео не найдено", 404
        response = send_file(path, conditional=True, as_attachment=False)
        response.headers["Cache-Control"] = "private, no-store"
        return response

    @app.get("/watch-original/<int:job_id>")
    def watch_original(job_id):
        job = get_job(job_id)
        if job is None:
            return "Задание не найдено", 404
        try:
            if job["backup_path"] and job["status"] in {"replaced", "restoring", "restore_interrupted"}:
                path = safe_backup(job)
            elif job["status"] not in {"replaced", "replacing", "replacement_interrupted"}:
                path = safe_source(job)
            else:
                return "Резервный оригинал недоступен", 404
        except (ValueError, OSError):
            return "Исходный файл не найден", 404
        original_name = Path(job["source_path"]).name
        mime_type = mimetypes.guess_type(original_name)[0] or "application/octet-stream"
        response = send_file(
            path, conditional=True, as_attachment=False,
            download_name=original_name, mimetype=mime_type,
        )
        response.headers["Cache-Control"] = "private, no-store"
        return response

    def result_file(job):
        """The optimized file of a job: in the work folder before replacement, in the library after."""
        if job["status"] in {"ready", "rejected_saving"} and job["output_path"]:
            path = Path(job["output_path"]).resolve(strict=True)
            if not path.is_relative_to(work_root() / "jobs") or not path.is_file():
                raise ValueError("Результат находится вне рабочего каталога")
            return path
        if job["status"] == "replaced":
            path = safe_source(job)
            if path.stat().st_size != int(job["optimized_size"] or -1):
                raise ValueError("Файл в медиатеке не совпадает с результатом задания")
            return path
        raise ValueError("У задания нет доступного результата")

    @app.get("/watch-result/<int:job_id>")
    def watch_result(job_id):
        job = get_job(job_id)
        if job is None:
            return "Задание не найдено", 404
        try:
            path = result_file(job)
        except (ValueError, OSError):
            return "Результат недоступен", 404
        response = send_file(
            path, conditional=True, as_attachment=False,
            download_name=path.name, mimetype="video/mp4",
        )
        response.headers["Cache-Control"] = "private, no-store"
        return response

    @app.get("/download/<int:video_id>")
    def download(video_id):
        try:
            path = video_file(video_id)
        except (ValueError, OSError):
            return "Видео не найдено", 404
        return send_file(path, conditional=True, as_attachment=True, download_name=path.name)

    @app.get("/api/status")
    def status():
        return jsonify(
            status="ok", version=__version__,
            read_only=not REPLACEMENT_ENABLED,
            replacement_enabled=REPLACEMENT_ENABLED,
            tools={
                "HandBrakeCLI": tool_version("HandBrakeCLI"),
                "ffmpeg": tool_version("ffmpeg"),
                "ffprobe": tool_version("ffprobe"),
                "exiftool": tool_version("exiftool", "-ver"),
            },
        )

    return app


app = create_app()
