import json
import math
import os
from pathlib import Path

from .db import get_setting, set_settings


SETTING_KEY = "encoding_profile"
BASE_PRESET = "Creator 1080p60"
ENCODER_PRESET = "fast"
RESOLUTIONS = {
    "720p": (1280, 720),
    "1080p": (1920, 1080),
    "1440p": (2560, 1440),
    "2160p": (3840, 2160),
}
ENCODERS = {"x265", "x264"}
AUDIO_BITRATES = {192, 320}
PRIORITIES = {"normal": 0, "low": 10, "background": 15}


def available_cpu_ids():
    try:
        ids = sorted(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        ids = list(range(os.cpu_count() or 1))
    return ids or [0]


def cgroup_cpu_quota():
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()[:2]
        if quota == "max":
            return None
        return max(1, math.floor(int(quota) / int(period)))
    except (OSError, ValueError, ZeroDivisionError):
        return None


def available_cpu_count():
    affinity = len(available_cpu_ids())
    quota = cgroup_cpu_quota()
    return min(affinity, quota) if quota else affinity


def default_cpu_count():
    return max(1, int(available_cpu_count() * 0.75 + 0.5))


def defaults():
    return {
        "encoder": "x265",
        "quality": 20,
        "resolution": "1080p",
        "audio_bitrate": 192,
        "cpu_count": default_cpu_count(),
        "priority": "background",
    }


def validate_profile(values):
    expected = set(defaults())
    unknown = set(values) - expected
    if unknown:
        raise ValueError(f"Неизвестные параметры профиля: {', '.join(sorted(unknown))}")
    result = defaults()
    result.update(values)
    result["encoder"] = str(result["encoder"])
    result["resolution"] = str(result["resolution"])
    result["priority"] = str(result["priority"])
    try:
        result["quality"] = int(result["quality"])
        result["audio_bitrate"] = int(result["audio_bitrate"])
        result["cpu_count"] = int(result["cpu_count"])
    except (TypeError, ValueError):
        raise ValueError("Числовые параметры профиля заполнены некорректно") from None
    if result["encoder"] not in ENCODERS:
        raise ValueError("Поддерживаются только кодировщики x264 и x265")
    if not 16 <= result["quality"] <= 30:
        raise ValueError("RF должен быть от 16 до 30")
    if result["resolution"] not in RESOLUTIONS:
        raise ValueError("Неизвестный предел разрешения")
    if result["audio_bitrate"] not in AUDIO_BITRATES:
        raise ValueError("Fallback AAC должен быть 192 или 320 кбит/с")
    maximum = available_cpu_count()
    if not 1 <= result["cpu_count"] <= maximum:
        raise ValueError(f"Число CPU должно быть от 1 до {maximum}")
    if result["priority"] not in PRIORITIES:
        raise ValueError("Неизвестный приоритет процесса")
    return result


def get_encoding_profile():
    stored = get_setting(SETTING_KEY, {})
    if not isinstance(stored, dict):
        stored = {}
    allowed = {key: value for key, value in stored.items() if key in defaults()}
    # A VM/container CPU reduction must not make saved settings unusable.
    allowed["cpu_count"] = min(
        int(allowed.get("cpu_count", default_cpu_count())), available_cpu_count()
    )
    return validate_profile(allowed)


def save_encoding_profile(values):
    validated = validate_profile(values)
    set_settings({SETTING_KEY: validated})
    return validated


def profile_options():
    return {
        "available_cpus": available_cpu_count(),
        "encoders": sorted(ENCODERS),
        "resolutions": list(RESOLUTIONS),
        "audio_bitrates": sorted(AUDIO_BITRATES),
        "priorities": [{"value": key, "nice": value} for key, value in PRIORITIES.items()],
    }


def snapshot(profile=None):
    result = validate_profile(profile or get_encoding_profile())
    long_edge, short_edge = RESOLUTIONS[result["resolution"]]
    return {
        **result,
        "base_preset": BASE_PRESET,
        "encoder_preset": ENCODER_PRESET,
        "max_long_edge": long_edge,
        "max_short_edge": short_edge,
        "nice": PRIORITIES[result["priority"]],
    }


def job_profile(job):
    try:
        stored = json.loads(job["encoder_settings"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        stored = {}
    if "resolution" in stored:
        base = {key: stored[key] for key in defaults() if key in stored}
        base["cpu_count"] = min(
            int(base.get("cpu_count", default_cpu_count())), available_cpu_count()
        )
        return snapshot(base)

    # Jobs stored with the older profile format (resolution as max width/edge).
    long_edge = int(stored.get("max_long_edge") or stored.get("max_width") or 1920)
    resolution = min(RESOLUTIONS, key=lambda key: abs(RESOLUTIONS[key][0] - long_edge))
    legacy = defaults()
    legacy.update({
        "encoder": stored.get("encoder", job["encoder"] or "x265"),
        "quality": stored.get("quality", 20),
        "resolution": resolution,
        "audio_bitrate": 192,
        "cpu_count": min(
            int(stored.get("x265_pools") or default_cpu_count()), available_cpu_count()
        ),
        "priority": "background",
    })
    return snapshot(legacy)


def process_command(command, profile=None):
    settings = snapshot(profile) if not (profile and "nice" in profile) else profile
    count = min(int(settings["cpu_count"]), available_cpu_count())
    cpu_ids = available_cpu_ids()[:count]
    cpu_list = ",".join(str(value) for value in cpu_ids)
    return [
        "ionice", "-c", "3",
        "nice", "-n", str(settings["nice"]),
        "taskset", "-c", cpu_list,
        *[str(value) for value in command],
    ]
