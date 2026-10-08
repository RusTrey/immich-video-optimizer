import threading

from .db import add_event, get_job, next_queued_job, update_job
from .encoder import STATE as ENCODE_STATE, encode_job
from .fileops import reconcile_all, request_replace
from .scheduler import due_scheduled_slot, get_scheduler_settings, run_scheduler


def auto_replace_scheduler_result(job_id, encode_status):
    if encode_status != "ready":
        return False
    job = get_job(job_id)
    if job is None or job["created_by"] != "scheduler":
        return False
    if not get_scheduler_settings()["auto_replace_ready"]:
        return False
    try:
        request_replace(job_id, wait=True)
    except Exception as error:
        # A failed claim leaves no trace in the job; failures during the replacement
        # itself are already stored by fileops.
        current = get_job(job_id)
        if current is not None and current["status"] == "ready" and not current["error"]:
            update_job(job_id, error=f"Автозамена не выполнена: {error}")
            add_event("error", "auto-replace", f"Автозамена задания #{job_id}: {error}", job_id)
        raise
    return True


class RuntimeController:
    """Two daemon threads: the sequential encoder and the scheduler clock.

    The scheduler runs separately so that a long encode does not make the
    daily slot miss its grace window.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.event = threading.Event()
        self.scheduler_event = threading.Event()
        self.thread = None
        self.scheduler_thread = None
        self.last_scheduler_error = ""
        self.manual_scheduler_requested = False

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return False
            self.thread = threading.Thread(target=self._run, name="optimizer-runtime", daemon=True)
            self.scheduler_thread = threading.Thread(
                target=self._run_scheduler_loop, name="optimizer-scheduler", daemon=True
            )
            self.thread.start()
            self.scheduler_thread.start()
        return True

    def wake(self):
        self.event.set()

    def snapshot(self):
        with self.lock:
            return {
                "running": bool(self.thread and self.thread.is_alive()),
                "scheduler_running": bool(self.scheduler_thread and self.scheduler_thread.is_alive()),
                "last_scheduler_error": self.last_scheduler_error,
                "manual_scheduler_requested": self.manual_scheduler_requested,
            }

    def request_scheduler_run(self):
        with self.lock:
            if self.manual_scheduler_requested:
                return False
            self.manual_scheduler_requested = True
        self.scheduler_event.set()
        return True

    def _scheduler_failed(self, error):
        self.last_scheduler_error = str(error)[-1000:]
        add_event("error", "scheduler", f"Запуск планировщика: {error}")

    def _run_scheduler_if_due(self):
        with self.lock:
            manual = self.manual_scheduler_requested
            self.manual_scheduler_requested = False
        if manual:
            trigger, slot = "manual", None
        else:
            slot = due_scheduled_slot()
            if not slot:
                return
            trigger = "schedule"
        try:
            result = run_scheduler(trigger=trigger, scheduled_for=slot)
            self.last_scheduler_error = ""
            if result and result.get("added"):
                self.wake()
        except Exception as error:
            self._scheduler_failed(error)

    def _run_scheduler_loop(self):
        while True:
            try:
                self._run_scheduler_if_due()
            except Exception as error:
                self._scheduler_failed(error)
            self.scheduler_event.wait(30)
            self.scheduler_event.clear()

    def _run(self):
        try:
            reconcile_all()
        except Exception as error:
            add_event("error", "recovery", f"Проверка прерванных операций: {error}")
        while True:
            try:
                queued = next_queued_job()
                if queued:
                    status = encode_job(queued["id"])
                    try:
                        auto_replace_scheduler_result(queued["id"], status)
                    except Exception:
                        pass  # Stored in the job and in events.
                    continue
                if not ENCODE_STATE.snapshot()["running"]:
                    ENCODE_STATE.update(
                        phase="idle", progress=0.0, eta="",
                        message="Очередь пуста", error="",
                    )
            except Exception as error:
                add_event("error", "runtime", f"Обработчик очереди: {error}")
            self.event.wait(30)
            self.event.clear()


CONTROLLER = RuntimeController()


def start_runtime():
    return CONTROLLER.start()


def wake_runtime():
    CONTROLLER.wake()


def runtime_snapshot():
    return CONTROLLER.snapshot()


def request_scheduler_run():
    return CONTROLLER.request_scheduler_run()
