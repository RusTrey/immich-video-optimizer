import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import statuses
from .db import create_job, get_job, get_video, job_counts, transition_job, update_job
from .profile import (
    BASE_PRESET, ENCODER_PRESET, defaults as profile_defaults,
    job_profile, process_command, snapshot as profile_snapshot,
)
from .scanner import media_roots, rational, rotation_of

MINIMUM_SAVING_PERCENT = float(os.environ.get("MINIMUM_SAVING_PERCENT", "20"))
# Android capture tags and Apple QuickTime keys (creation date with time zone,
# make, model, Live Photo identifier) live in the QuickTime Keys group.
COPY_TAGS = ("Keys:all", "UserData:Make", "UserData:Model")
VERIFY_TAGS = (
    "CreateDate", "MediaCreateDate", "TrackCreateDate", "GPSLatitude", "GPSLongitude",
    "AndroidVersion", "AndroidMake", "AndroidModel", "AndroidCaptureFPS",
    "CreationDate", "ContentIdentifier", "Make", "Model",
)
# The output is always MP4, so only ISO base media sources keep a truthful name.
SUPPORTED_CONTAINER_PREFIX = "mov,mp4"
HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}
MAXIMUM_FPS = 60.0
PROGRESS_RE = re.compile(
    r"Encoding:.*?([0-9]+(?:\.[0-9]+)?)\s*%.*?([0-9]+(?:\.[0-9]+)?)\s*fps.*?ETA\s+([^\)\r\n]+)",
    re.IGNORECASE,
)


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def encoder_settings():
    return json.dumps(profile_snapshot(), ensure_ascii=False, sort_keys=True)


class EncodeState:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.phase = "idle"
        self.job_id = None
        self.video_id = None
        self.input = ""
        self.output = ""
        self.progress = 0.0
        self.fps = None
        self.eta = ""
        self.started_at = None
        self.finished_at = None
        self.message = ""
        self.error = ""
        self.process = None
        self.stop_requested = False

    def update(self, **changes):
        with self.lock:
            for key, value in changes.items():
                setattr(self, key, value)

    def snapshot(self):
        with self.lock:
            result = {
                "running": self.running, "phase": self.phase, "job_id": self.job_id,
                "video_id": self.video_id, "input": self.input,
                "output": self.output, "progress": self.progress, "fps": self.fps,
                "eta": self.eta, "started_at": self.started_at,
                "finished_at": self.finished_at, "message": self.message, "error": self.error,
            }
        result["queue"] = job_counts()
        return result


STATE = EncodeState()


def work_root():
    return Path(os.environ.get("WORK_ROOT", "/storage/optimizer-work")).resolve()


def safe_source(row):
    key = "source_path" if "source_path" in row.keys() else "path"
    source = Path(row[key]).resolve(strict=True)
    if not any(source.is_relative_to(root) for root in media_roots()):
        raise ValueError("Исходный файл находится вне каталогов медиатеки")
    return source


def unsupported_reason(row):
    """Why a video cannot be optimized safely, or None."""
    if row["probe_error"]:
        return "У исходника есть ошибка ffprobe"
    container = row["container"] or ""
    if not container.startswith(SUPPORTED_CONTAINER_PREFIX):
        return f"Контейнер {container or 'не определён'} не поддерживается: результат MP4 нельзя сохранить под этим именем"
    if int(row["subtitle_streams"] or 0) or int(row["data_streams"] or 0):
        return "Видео с subtitle/data потоками не обрабатываются, чтобы не потерять их"
    if int(row["audio_streams"] or 0) > 1:
        return "Видео с несколькими аудиопотоками не обрабатываются, чтобы не потерять их"
    return None


def needs_ten_bit(row):
    pixel_format = row["pixel_format"] or ""
    return (row["color_transfer"] in HDR_TRANSFERS
            or any(depth in pixel_format for depth in ("10", "12")))


def video_encoder(job, settings):
    """HDR and deep-colour sources are encoded with 10-bit x265 to keep their range."""
    return "x265_10bit" if needs_ten_bit(job) else settings["encoder"]


def output_paths(job, create=True):
    settings = job_profile(job)
    directory = work_root() / "jobs" / str(job["id"])
    if create:
        directory.mkdir(parents=True, exist_ok=True)
    stem = Path(job["relative_path"]).stem
    final = directory / (
        f"{stem}_HB_LINUX_{settings['encoder']}_{ENCODER_PRESET}_"
        f"RF{settings['quality']}_max{settings['resolution']}.mp4"
    )
    return final, directory / f".{final.stem}.partial.mp4", directory / f"{final.name}.log"


def display_dimensions(width, height, rotation=None):
    width, height = int(width or 0), int(height or 0)
    try:
        quarter_turn = int(round(float(rotation or 0))) % 180 == 90
    except (TypeError, ValueError):
        quarter_turn = False
    return (height, width) if quarter_turn else (width, height)


def profile_limits(width, height, rotation=None, settings=None):
    settings = settings or profile_snapshot(profile_defaults())
    display_width, display_height = display_dimensions(width, height, rotation)
    long_edge = int(settings["max_long_edge"])
    short_edge = int(settings["max_short_edge"])
    return ((short_edge, long_edge) if display_height > display_width
            else (long_edge, short_edge))


def expected_output_dimensions(width, height, rotation=None, settings=None):
    settings = settings or profile_snapshot(profile_defaults())
    display_width, display_height = display_dimensions(width, height, rotation)
    limit_width, limit_height = profile_limits(width, height, rotation, settings)
    if not display_width or not display_height:
        return 0, 0
    scale = min(1.0, limit_width / display_width, limit_height / display_height)

    def even(value):
        return max(2, int(value / 2 + 0.5) * 2)

    return even(display_width * scale), even(display_height * scale)


def handbrake_command(source, partial, settings=None, width=None, height=None, rotation=None,
                      encoder=None):
    settings = settings or profile_snapshot(profile_defaults())
    encoder = encoder or settings["encoder"]
    max_width, max_height = profile_limits(width, height, rotation, settings)
    # --encopts replaces the preset's editing-oriented x264 options
    # (keyint=30, ref=1) and limits encoder threads to the selected CPUs.
    threads = "pools" if encoder.startswith("x265") else "threads"
    command = [
        "HandBrakeCLI", "--input", str(source),
        "--output", str(partial), "--preset", BASE_PRESET,
        "--encoder", encoder,
        "--encoder-preset", ENCODER_PRESET, "--quality", str(settings["quality"]),
        "--encopts", f"{threads}={settings['cpu_count']}",
        "--maxWidth", str(max_width), "--maxHeight", str(max_height),
        "--crop-mode", "none",
        "--aencoder", "copy", "--audio-copy-mask", "aac",
        "--audio-fallback", "av_aac", "--ab", str(settings["audio_bitrate"]),
    ]
    return process_command(command, settings)


def sha1_file(path):
    digest = hashlib.sha1(usedforsecurity=False)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_metadata(path, tags=VERIFY_TAGS, *, grouped=False):
    command = ["exiftool", "-j", "-n", *(["-G1"] if grouped else []),
               *[f"-{tag}" for tag in tags], str(path)]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or "ExifTool не смог прочитать метаданные")
    rows = json.loads(completed.stdout)
    row = rows[0] if rows else {}
    if grouped:
        return {key: value for key, value in row.items() if key != "SourceFile"}
    return {tag: row[tag] for tag in tags if tag in row}


def copy_capture_metadata(source, target):
    present = read_metadata(source, COPY_TAGS, grouped=True)
    if not present:
        return
    command = ["exiftool", "-overwrite_original", "-TagsFromFile", str(source)]
    command.extend(f"-{tag}" for tag in present)
    command.append(str(target))
    completed = subprocess.run(command, capture_output=True, text=True, timeout=300, check=False)
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or "ExifTool не смог перенести метаданные")


def probe(path, settings=None):
    settings = settings or profile_snapshot(profile_defaults())
    completed = subprocess.run(
        process_command(
            ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
            settings,
        ),
        capture_output=True, text=True, timeout=120, check=False,
    )
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or "ffprobe завершился с ошибкой")
    return json.loads(completed.stdout)


def metadata_values_equal(tag, expected, actual):
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        tolerance = 0.00005 if tag in {"GPSLatitude", "GPSLongitude"} else 0.000001
        return abs(float(expected) - float(actual)) <= tolerance
    return str(actual) == str(expected)


def _stream_duration(stream, fallback):
    try:
        return float(stream.get("duration") or fallback or 0)
    except (TypeError, ValueError):
        return float(fallback or 0)


def _has_dolby_vision(stream):
    return any("DOVI" in str(item.get("side_data_type", "")).upper()
               for item in stream.get("side_data_list", []))


def validate_output(path, job, source_metadata, settings=None, source_data=None):
    settings = settings or job_profile(job)
    data = probe(path, settings)
    streams = data.get("streams", [])
    videos = [stream for stream in streams if stream.get("codec_type") == "video"]
    audios = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if len(videos) != 1:
        raise RuntimeError(f"Ожидался один видеопоток, получено: {len(videos)}")
    if len(audios) != int(job["audio_streams"] or 0):
        raise RuntimeError(f"Число аудиопотоков изменилось: {job['audio_streams']} → {len(audios)}")
    expected_codec = "hevc" if video_encoder(job, settings).startswith("x265") else "h264"
    if videos[0].get("codec_name") != expected_codec:
        raise RuntimeError(
            f"Неожиданный видеокодек результата: "
            f"{videos[0].get('codec_name') or 'не определён'} вместо {expected_codec}"
        )
    if any(stream.get("codec_name") != "aac" for stream in audios):
        raise RuntimeError("Аудиодорожка результата имеет кодек, отличный от AAC")
    duration = float(data.get("format", {}).get("duration") or 0)
    source_duration = float(job["duration_seconds"] or 0)
    tolerance = max(1.0, source_duration * 0.002)
    if not duration or (source_duration and abs(duration - source_duration) > tolerance):
        raise RuntimeError(f"Некорректная длительность результата: {duration:.3f} с")
    source_streams = (source_data or {}).get("streams", [])
    source_video = next((stream for stream in source_streams if stream.get("codec_type") == "video"), {})
    if source_video:
        expected_video = _stream_duration(source_video, source_duration)
        actual_video = _stream_duration(videos[0], duration)
        if expected_video and abs(actual_video - expected_video) > max(1.0, expected_video * 0.002):
            raise RuntimeError(
                f"Длительность видеопотока изменилась: {expected_video:.3f} → {actual_video:.3f} с"
            )
    source_fps = rational(source_video.get("avg_frame_rate")) if source_video else None
    source_fps = source_fps or (float(job["fps"]) if job["fps"] else None)
    output_fps = rational(videos[0].get("avg_frame_rate"))
    if output_fps and output_fps > MAXIMUM_FPS + 0.5:
        raise RuntimeError(f"Частота кадров результата выше {MAXIMUM_FPS:g}: {output_fps:.2f}")
    if source_fps and output_fps:
        expected_fps = min(source_fps, MAXIMUM_FPS)
        if abs(output_fps - expected_fps) > max(1.0, expected_fps * 0.1):
            raise RuntimeError(
                f"Неожиданная частота кадров: {source_fps:.2f} → {output_fps:.2f} кадр/с"
            )
    if job["color_transfer"] in HDR_TRANSFERS:
        output_video = videos[0]
        for key in ("color_transfer", "color_primaries"):
            if job[key] and output_video.get(key) != job[key]:
                raise RuntimeError(
                    f"HDR-характеристика {key} изменилась: {job[key]} → {output_video.get(key)}"
                )
    if needs_ten_bit(job) and "10" not in str(videos[0].get("pix_fmt", "")):
        raise RuntimeError(f"Ожидался 10-битный результат, получено: {videos[0].get('pix_fmt')}")
    if source_video and _has_dolby_vision(source_video) and not _has_dolby_vision(videos[0]):
        raise RuntimeError("Метаданные Dolby Vision исходника не сохранились")
    width, height = int(videos[0].get("width") or 0), int(videos[0].get("height") or 0)
    output_rotation = rotation_of(videos[0])
    display_width, display_height = display_dimensions(width, height, output_rotation)
    limit_width, limit_height = profile_limits(
        job["width"], job["height"], job["rotation"], settings
    )
    if (not display_width or not display_height or display_width > limit_width
            or display_height > limit_height):
        raise RuntimeError(
            f"Некорректное отображаемое разрешение результата: "
            f"{display_width}x{display_height}"
        )
    expected_width, expected_height = expected_output_dimensions(
        job["width"], job["height"], job["rotation"], settings
    )
    if (abs(display_width - expected_width) > 2
            or abs(display_height - expected_height) > 2):
        source_width, source_height = display_dimensions(
            job["width"], job["height"], job["rotation"]
        )
        raise RuntimeError(
            f"Неожиданное изменение отображаемого разрешения: "
            f"{source_width}x{source_height} → {display_width}x{display_height}; "
            f"ожидалось {expected_width}x{expected_height}"
        )
    actual_metadata = read_metadata(path)
    mismatches = {tag: {"source": value, "output": actual_metadata.get(tag)}
                  for tag, value in source_metadata.items()
                  if not metadata_values_equal(tag, value, actual_metadata.get(tag))}
    if mismatches:
        raise RuntimeError("Не совпали метаданные: " + ", ".join(mismatches))

    decoded = subprocess.run(
        process_command(
            ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v", "-map", "0:a?", "-f", "null", "-"],
            settings,
        ),
        capture_output=True, text=True, timeout=max(3600, int(duration * 10)), check=False,
    )
    if decoded.returncode:
        raise RuntimeError(decoded.stderr.strip() or "Полное декодирование результата завершилось с ошибкой")
    return {
        "duration_seconds": duration, "width": width, "height": height,
        "rotation": output_rotation, "fps": output_fps,
        "pixel_format": videos[0].get("pix_fmt"),
        "color_transfer": videos[0].get("color_transfer"),
        "video_codec": videos[0].get("codec_name"),
        "video_bit_rate": int(videos[0].get("bit_rate") or data.get("format", {}).get("bit_rate") or 0),
        "audio_streams": len(audios), "full_decode": "passed", "metadata": actual_metadata,
        "profile": settings,
    }


def finalize_result(job, source, partial, final):
    settings = job_profile(job)
    source_metadata = read_metadata(source)
    source_data = probe(source, settings)
    copy_capture_metadata(source, partial)
    validation = validate_output(partial, job, source_metadata, settings, source_data)
    optimized_size = partial.stat().st_size
    saving = (1 - optimized_size / int(job["source_size"])) * 100
    optimized_sha1 = sha1_file(partial)
    partial.replace(final)
    status = "ready" if saving >= float(job["minimum_saving_percent"]) else "rejected_saving"
    finished = utcnow()
    update_job(job["id"], output_path=str(final), optimized_size=optimized_size,
               optimized_sha1=optimized_sha1, optimized_video_codec=validation["video_codec"],
               optimized_bit_rate=validation["video_bit_rate"], saving_percent=saving,
               status=status, phase=status, validation_json=json.dumps(validation, ensure_ascii=False),
               finished_at=finished, error=None)
    return status, saving, finished


def preflight(job, source):
    reason = unsupported_reason(job)
    if reason:
        raise RuntimeError(reason)
    stat = source.stat()
    if stat.st_size != int(job["source_size"]) or stat.st_mtime_ns != int(job["source_mtime_ns"]):
        raise RuntimeError("Исходник изменился после сканирования; сначала обновите индекс медиатеки")


class JobStopped(RuntimeError):
    pass


def stop_running_job(job_id):
    with STATE.lock:
        process = STATE.process
        if not STATE.running or STATE.job_id != int(job_id):
            raise ValueError("Это задание сейчас не выполняется")
        STATE.stop_requested = True
    if process is not None and process.poll() is None:
        process.terminate()


def encode_job(job_id):
    job = get_job(job_id)
    STATE.update(stop_requested=False)
    if job is None or not transition_job(job_id, statuses.QUEUED, status=statuses.RUNNING,
                                         phase="preflight", error=None):
        return None  # Cancelled or taken by another operation meanwhile.
    try:
        settings = job_profile(job)
        source = safe_source(job)
        preflight(job, source)
        final, partial, log_path = output_paths(job)
        if final.exists() or partial.exists():
            raise FileExistsError(f"Результат или partial уже существует: {final}")
        if shutil.disk_usage(final.parent).free < int(job["source_size"] * 1.2):
            raise RuntimeError("Недостаточно свободного места во временном каталоге")

        started = utcnow()
        update_job(job_id, phase="hashing-source", started_at=started)
        STATE.update(running=True, phase="hashing-source", job_id=job_id, video_id=job["video_id"],
                     input=str(source), output=str(final), progress=0.0, fps=None, eta="",
                     started_at=started, finished_at=None, message="SHA-1 исходника", error="")
        original_sha1 = sha1_file(source)
        update_job(job_id, original_sha1=original_sha1, phase="encoding")

        if STATE.stop_requested:
            raise JobStopped("Остановлено администратором; оригинал не изменялся")
        STATE.update(phase="encoding", message="Запущен HandBrakeCLI")
        command = handbrake_command(
            source, partial, settings, job["width"], job["height"], job["rotation"],
            encoder=video_encoder(job, settings),
        )
        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(command, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, bufsize=1)
            STATE.update(process=process)
            try:
                buffer = ""
                last_saved = 0.0
                while True:
                    chunk = process.stdout.read(1024)
                    if not chunk:
                        break
                    log.write(chunk)
                    log.flush()
                    buffer = (buffer + chunk)[-4096:]
                    match = PROGRESS_RE.search(buffer)
                    if match:
                        progress = float(match.group(1))
                        if progress - last_saved >= 0.5 or progress >= 100:
                            update_job(job_id, progress=progress)
                            last_saved = progress
                        STATE.update(progress=progress, fps=float(match.group(2)), eta=match.group(3).strip())
                returncode = process.wait()
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                STATE.update(process=None)
        if STATE.stop_requested:
            raise JobStopped("Остановлено администратором; оригинал не изменялся")
        if returncode:
            raise RuntimeError(f"HandBrakeCLI завершился с кодом {returncode}; см. {log_path.name}")

        update_job(job_id, phase="metadata", progress=100.0)
        STATE.update(phase="metadata", progress=100.0, eta="", message="Перенос метаданных устройства")
        update_job(job_id, phase="validating")
        STATE.update(phase="validating", message="ffprobe, метаданные и полное декодирование")
        status, saving, finished = finalize_result(job, source, partial, final)
        message = (f"Готово, экономия {saving:.1f}%" if status == "ready"
                   else f"Проверка пройдена, но экономия {saving:.1f}% ниже порога")
        STATE.update(running=False, phase=status, finished_at=finished, output=str(final), message=message)
        return status
    except Exception as error:
        finished = utcnow()
        status = statuses.INTERRUPTED if isinstance(error, JobStopped) else statuses.FAILED
        update_job(job_id, status=status, phase=status, error=str(error), finished_at=finished)
        STATE.update(running=False, phase=status, finished_at=finished, error=str(error), message=str(error))
        return status
    finally:
        STATE.update(stop_requested=False, process=None)


def enqueue_videos(video_ids, *, created_by="admin", scheduler_run_id=None, wake=True):
    created, skipped = [], []
    settings = profile_snapshot()
    serialized_settings = json.dumps(settings, ensure_ascii=False, sort_keys=True)
    for video_id in dict.fromkeys(video_ids):
        try:
            row = get_video(int(video_id))
            if row is None or unsupported_reason(row):
                skipped.append(int(video_id))
                continue
            safe_source(row)
        except (ValueError, OSError):
            skipped.append(int(video_id))
            continue
        job_id, was_created = create_job(
            row, preset=BASE_PRESET, encoder=settings["encoder"],
            encoder_settings=serialized_settings,
            minimum_saving_percent=MINIMUM_SAVING_PERCENT, created_at=utcnow(),
            created_by=created_by, scheduler_run_id=scheduler_run_id)
        (created if was_created else skipped).append(job_id if was_created else int(video_id))
    if created and wake:
        from .runtime import wake_runtime
        wake_runtime()
    return created, skipped


def _safe_job_directory(job_id):
    root = work_root()
    jobs_root = (root / "jobs").resolve()
    directory = (jobs_root / str(int(job_id))).resolve()
    if directory.parent != jobs_root:
        raise RuntimeError("Некорректный рабочий каталог задания")
    return directory


def _remove_job_work(job_id):
    directory = _safe_job_directory(job_id)
    if directory.exists():
        shutil.rmtree(directory)


def retry_job(job_id):
    job = get_job(job_id)
    if job is None or job["status"] not in statuses.RETRYABLE:
        raise ValueError("Повторить можно только ошибочное, прерванное или отклонённое задание")
    source = safe_source(job)
    stat = source.stat()
    if stat.st_size != int(job["source_size"]) or stat.st_mtime_ns != int(job["source_mtime_ns"]):
        raise RuntimeError("Исходник изменился; сначала обновите индекс медиатеки")
    _remove_job_work(job_id)
    changed = transition_job(
        job_id, statuses.RETRYABLE, original_sha1=None, output_path=None, optimized_size=None,
        optimized_sha1=None, optimized_video_codec=None, optimized_bit_rate=None,
        saving_percent=None, status=statuses.QUEUED, phase=statuses.QUEUED, progress=0,
        error=None, validation_json=None, started_at=None, finished_at=None,
        attempt_count=int(job["attempt_count"] or 0) + 1,
    )
    if not changed:
        raise ValueError("Задание уже изменено другой операцией")
    from .runtime import wake_runtime
    wake_runtime()


def cancel_queued_job(job_id):
    """The job stays in history, so the scheduler does not pick the video again."""
    if not transition_job(job_id, statuses.QUEUED, status=statuses.CANCELLED,
                          phase=statuses.CANCELLED, finished_at=utcnow()):
        raise ValueError("Отменить можно только ожидающее задание")
    _remove_job_work(job_id)


def discard_job(job_id):
    """Drop the temporary output; the job stays in history as discarded."""
    job = get_job(job_id)
    if job is None or not transition_job(
        job_id, statuses.DISCARDABLE, status=statuses.DISCARDED, phase=statuses.DISCARDED,
        finished_at=job["finished_at"] or utcnow(),
    ):
        raise ValueError("Удалить можно только завершённое незаменённое задание")
    _remove_job_work(job_id)


def revalidate_failed_job(job_id):
    job = get_job(job_id)
    if job is None or job["status"] != "failed":
        raise ValueError("Повторно проверить можно только завершившееся с ошибкой задание")
    source = safe_source(job)
    preflight(job, source)
    final, partial, _ = output_paths(job)
    if final.exists() or not partial.exists():
        raise RuntimeError("Не найден единственный partial-файл для повторной проверки")
    if job["original_sha1"] and sha1_file(source) != job["original_sha1"]:
        raise RuntimeError("SHA-1 исходника изменился")
    if not transition_job(job_id, statuses.FAILED, status=statuses.RUNNING,
                          phase="validating", error=None):
        raise ValueError("Задание уже изменено другой операцией")
    try:
        status, saving, _ = finalize_result(job, source, partial, final)
        return status, saving
    except Exception as error:
        update_job(job_id, status="failed", phase="failed", error=str(error), finished_at=utcnow())
        raise
