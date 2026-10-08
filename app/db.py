import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import statuses


DB_PATH = Path(os.environ.get("OPTIMIZER_DB", "/state/optimizer.sqlite3"))
SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    root TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    extension TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    duration_seconds REAL,
    bit_rate INTEGER,
    container TEXT,
    video_codec TEXT,
    audio_codec TEXT,
    width INTEGER,
    height INTEGER,
    fps REAL,
    pixel_format TEXT,
    color_space TEXT,
    color_transfer TEXT,
    color_primaries TEXT,
    encoder_tag TEXT,
    capture_time TEXT,
    has_location INTEGER NOT NULL DEFAULT 0,
    rotation INTEGER,
    audio_streams INTEGER NOT NULL DEFAULT 0,
    subtitle_streams INTEGER NOT NULL DEFAULT 0,
    data_streams INTEGER NOT NULL DEFAULT 0,
    classification TEXT NOT NULL DEFAULT 'untracked',
    probe_error TEXT,
    present INTEGER NOT NULL DEFAULT 1,
    last_scan_id TEXT NOT NULL,
    scanned_at TEXT NOT NULL,
    first_seen_at TEXT
);

CREATE TABLE IF NOT EXISTS scan_runs (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    discovered INTEGER NOT NULL DEFAULT 0,
    probed INTEGER NOT NULL DEFAULT 0,
    unchanged INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    error TEXT
);

CREATE TABLE IF NOT EXISTS scheduler_runs (
    id INTEGER PRIMARY KEY,
    trigger TEXT NOT NULL,
    scheduled_for TEXT,
    status TEXT NOT NULL,
    rules_json TEXT NOT NULL,
    scan_run_id TEXT,
    candidates INTEGER NOT NULL DEFAULT 0,
    added INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS optimization_jobs (
    id INTEGER PRIMARY KEY,
    video_id INTEGER NOT NULL REFERENCES videos(id),
    source_path TEXT NOT NULL,
    source_size INTEGER NOT NULL,
    source_mtime_ns INTEGER NOT NULL,
    original_sha1 TEXT,
    output_path TEXT,
    optimized_size INTEGER,
    optimized_sha1 TEXT,
    original_video_codec TEXT,
    optimized_video_codec TEXT,
    original_bit_rate INTEGER,
    optimized_bit_rate INTEGER,
    preset TEXT NOT NULL,
    encoder TEXT NOT NULL,
    encoder_settings TEXT NOT NULL,
    minimum_saving_percent REAL NOT NULL,
    saving_percent REAL,
    status TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT 'queued',
    progress REAL NOT NULL DEFAULT 0,
    error TEXT,
    validation_json TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    replaced_at TEXT,
    restored_at TEXT,
    backup_path TEXT,
    created_by TEXT NOT NULL DEFAULT 'admin',
    scheduler_run_id INTEGER,
    attempt_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    level TEXT NOT NULL,
    source TEXT NOT NULL,
    job_id INTEGER,
    message TEXT NOT NULL,
    dismissed_at TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS videos_size_idx ON videos(size_bytes DESC);
CREATE INDEX IF NOT EXISTS videos_codec_idx ON videos(video_codec);
CREATE INDEX IF NOT EXISTS videos_classification_idx ON videos(classification);
CREATE INDEX IF NOT EXISTS videos_present_size_idx ON videos(present, size_bytes DESC);
CREATE INDEX IF NOT EXISTS videos_present_bitrate_idx ON videos(present, bit_rate DESC);
CREATE INDEX IF NOT EXISTS videos_present_codec_idx ON videos(present, video_codec);
CREATE INDEX IF NOT EXISTS videos_present_classification_idx ON videos(present, classification);
CREATE INDEX IF NOT EXISTS optimization_jobs_status_idx ON optimization_jobs(status, id);
CREATE INDEX IF NOT EXISTS optimization_jobs_video_idx ON optimization_jobs(video_id, id DESC);
CREATE INDEX IF NOT EXISTS optimization_jobs_created_by_idx ON optimization_jobs(created_by, id DESC);
CREATE INDEX IF NOT EXISTS optimization_jobs_scheduler_run_idx ON optimization_jobs(scheduler_run_id);
CREATE INDEX IF NOT EXISTS events_open_idx ON events(dismissed_at, id DESC);
CREATE UNIQUE INDEX IF NOT EXISTS scheduler_runs_slot_idx
    ON scheduler_runs(scheduled_for) WHERE scheduled_for IS NOT NULL;
"""


def utcnow():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect(path=None):
    database = Path(path) if path is not None else DB_PATH
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def table_exists(connection, name):
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def columns_of(connection, table):
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def apply_schema(connection):
    for statement in SCHEMA.split(";"):
        if statement.strip():
            connection.execute(statement)


def backup_before_migration(connection, version):
    backup_dir = DB_PATH.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir / f"optimizer-before-schema-{version}-{stamp}.sqlite3"
    suffix = 1
    while target.exists():
        target = backup_dir / f"optimizer-before-schema-{version}-{stamp}-{suffix}.sqlite3"
        suffix += 1
    backup = sqlite3.connect(target)
    try:
        connection.backup(backup)
    finally:
        backup.close()
    return target


def migrate_from_zero(connection):
    existing = table_exists(connection, "optimization_jobs")
    if existing:
        if "first_seen_at" not in columns_of(connection, "videos"):
            connection.execute("ALTER TABLE videos ADD COLUMN first_seen_at TEXT")
        if "unchanged" not in columns_of(connection, "scan_runs"):
            connection.execute(
                "ALTER TABLE scan_runs ADD COLUMN unchanged INTEGER NOT NULL DEFAULT 0"
            )

        job_columns = columns_of(connection, "optimization_jobs")
        additions = {
            "backup_path": "TEXT",
            "created_by": "TEXT NOT NULL DEFAULT 'admin'",
            "scheduler_run_id": "INTEGER",
            "attempt_count": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, definition in additions.items():
            if name not in job_columns:
                connection.execute(
                    f"ALTER TABLE optimization_jobs ADD COLUMN {name} {definition}"
                )

    apply_schema(connection)

    connection.execute("UPDATE videos SET first_seen_at=scanned_at WHERE first_seen_at IS NULL")
    connection.execute(
        "UPDATE optimization_jobs SET created_by='admin' WHERE created_by IS NULL OR created_by=''"
    )
    connection.execute("PRAGMA user_version=1")


def migrate_to_two(connection):
    if "restored_at" not in columns_of(connection, "optimization_jobs"):
        connection.execute("ALTER TABLE optimization_jobs ADD COLUMN restored_at TEXT")
    apply_schema(connection)
    connection.execute("PRAGMA user_version=2")


def init_db(path=None):
    global DB_PATH
    original_path = DB_PATH
    if path is not None:
        DB_PATH = Path(path)
    try:
        with connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(f"Схема SQLite {version} новее поддерживаемой {SCHEMA_VERSION}")
            if version < SCHEMA_VERSION and table_exists(connection, "optimization_jobs"):
                backup_before_migration(connection, version)
            connection.execute("BEGIN IMMEDIATE")
            if version == 0:
                migrate_from_zero(connection)
            if version < 2:
                migrate_to_two(connection)
            apply_schema(connection)

            connection.execute(
                """
                UPDATE optimization_jobs
                   SET status='interrupted', phase='interrupted',
                       error=COALESCE(error, 'Контейнер был остановлен во время обработки')
                 WHERE status='running'
                """
            )
            connection.execute(
                """
                UPDATE optimization_jobs
                   SET status='replacement_interrupted', phase='replacement_interrupted',
                       error=COALESCE(error, 'Контейнер был остановлен во время атомарной замены')
                 WHERE status='replacing'
                """
            )
            connection.execute(
                """
                UPDATE optimization_jobs
                   SET status='restore_interrupted', phase='restore_interrupted',
                       error=COALESCE(error, 'Контейнер был остановлен во время возврата оригинала')
                 WHERE status='restoring'
                """
            )
            connection.execute(
                """
                UPDATE scheduler_runs
                   SET status='interrupted', finished_at=COALESCE(finished_at, ?),
                       error=COALESCE(error, 'Контейнер был остановлен во время запуска')
                 WHERE status='running'
                """,
                (utcnow(),),
            )
            connection.execute(
                """
                UPDATE scan_runs
                   SET status='interrupted', finished_at=COALESCE(finished_at, ?),
                       error=COALESCE(error, 'Контейнер был остановлен во время сканирования')
                 WHERE status='running'
                """,
                (utcnow(),),
            )
    finally:
        DB_PATH = original_path


def upsert_video(connection, item):
    item = dict(item)
    item.setdefault("first_seen_at", item.get("scanned_at") or utcnow())
    columns = tuple(item.keys())
    placeholders = ", ".join("?" for _ in columns)
    updates = ", ".join(
        f"{column}=excluded.{column}"
        for column in columns if column not in {"path", "first_seen_at"}
    )
    connection.execute(
        f"INSERT INTO videos ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT(path) DO UPDATE SET {updates}",
        tuple(item[column] for column in columns),
    )


def mark_video_unchanged(connection, path, scan_id, scanned_at):
    connection.execute(
        "UPDATE videos SET present=1, last_scan_id=?, scanned_at=? WHERE path=?",
        (scan_id, scanned_at, str(path)),
    )


def get_video_by_path(connection, path):
    return connection.execute("SELECT * FROM videos WHERE path=?", (str(path),)).fetchone()


VIDEO_SORTS = {
    "path": "videos.relative_path",
    "size": "videos.size_bytes",
    "codec": "videos.video_codec",
    "resolution": "COALESCE(videos.width, 0) * COALESCE(videos.height, 0)",
    "fps": "videos.fps",
    "bitrate": "videos.bit_rate",
    "duration": "videos.duration_seconds",
    "status": "latest.status",
}


def _video_where(filters):
    filters = filters or {}
    where = ["videos.present = 1"]
    values = []
    if filters.get("codec"):
        where.append("videos.video_codec = ?")
        values.append(filters["codec"])
    if filters.get("classification"):
        where.append("videos.classification = ?")
        values.append(filters["classification"])
    if filters.get("minimum_mib"):
        where.append("videos.size_bytes >= ?")
        values.append(int(float(filters["minimum_mib"]) * 1024 * 1024))
    if filters.get("minimum_bitrate_mbps"):
        where.append("videos.bit_rate >= ?")
        values.append(int(float(filters["minimum_bitrate_mbps"]) * 1_000_000))
    if filters.get("query"):
        where.append("videos.relative_path LIKE ? ESCAPE '\\'")
        escaped = str(filters["query"]).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        values.append(f"%{escaped}%")
    if filters.get("job_status"):
        if filters["job_status"] == "none":
            where.append("latest.status IS NULL")
        else:
            where.append("latest.status = ?")
            values.append(filters["job_status"])
    return where, values


VIDEO_FROM = """
    FROM videos
    LEFT JOIN optimization_jobs latest
      ON latest.id = (
          SELECT id FROM optimization_jobs
           WHERE video_id=videos.id ORDER BY id DESC LIMIT 1
      )
"""

VIDEO_SELECT = """
    SELECT videos.*,
           latest.status AS latest_job_status,
           latest.saving_percent AS latest_saving_percent,
           latest.id AS latest_job_id,
           latest.source_size AS latest_source_size,
           latest.optimized_size AS latest_optimized_size,
           latest.backup_path AS latest_backup_path
""" + VIDEO_FROM


def query_videos(filters=None, *, page=1, page_size=50, sort="size", order="desc"):
    page = max(1, int(page))
    page_size = min(100, max(1, int(page_size)))
    sort_sql = VIDEO_SORTS.get(sort, VIDEO_SORTS["size"])
    order_sql = "ASC" if str(order).lower() == "asc" else "DESC"
    where, values = _video_where(filters)
    with connect() as connection:
        total = connection.execute(
            f"SELECT COUNT(*) {VIDEO_FROM} WHERE {' AND '.join(where)}", values
        ).fetchone()[0]
        rows = connection.execute(
            f"{VIDEO_SELECT} WHERE {' AND '.join(where)} "
            f"ORDER BY {sort_sql} {order_sql}, videos.id ASC LIMIT ? OFFSET ?",
            (*values, page_size, (page - 1) * page_size),
        ).fetchall()
    return rows, total


def video_ids_for_filters(filters=None, limit=10000):
    where, values = _video_where(filters)
    sql = (
        f"SELECT videos.id {VIDEO_FROM} WHERE {' AND '.join(where)} "
        "ORDER BY videos.size_bytes DESC, videos.id ASC LIMIT ?"
    )
    with connect() as connection:
        return [row["id"] for row in connection.execute(sql, (*values, int(limit))).fetchall()]


def get_video(video_id):
    with connect() as connection:
        return connection.execute(
            "SELECT * FROM videos WHERE id=? AND present=1", (video_id,)
        ).fetchone()


def summary():
    with connect() as connection:
        return connection.execute(
            """
            SELECT COUNT(*) AS count,
                   COALESCE(SUM(size_bytes), 0) AS bytes,
                   COALESCE(SUM(CASE WHEN classification='historical_handbrake' THEN 1 ELSE 0 END), 0) AS historical,
                   COALESCE(SUM(CASE WHEN classification='untracked' THEN 1 ELSE 0 END), 0) AS untracked,
                   COALESCE(SUM(CASE WHEN probe_error IS NOT NULL THEN 1 ELSE 0 END), 0) AS errors
              FROM videos WHERE present=1
            """
        ).fetchone()


def create_job(video, *, preset, encoder, encoder_settings, minimum_saving_percent,
               created_at, created_by="admin", scheduler_run_id=None):
    with connect() as connection:
        placeholders = ",".join("?" for _ in statuses.BLOCKING)
        duplicate = connection.execute(
            f"SELECT id FROM optimization_jobs WHERE video_id=? AND status IN ({placeholders})",
            (video["id"], *statuses.BLOCKING),
        ).fetchone()
        if duplicate:
            return duplicate["id"], False
        cursor = connection.execute(
            """
            INSERT INTO optimization_jobs (
                video_id, source_path, source_size, source_mtime_ns,
                original_video_codec, original_bit_rate, preset, encoder,
                encoder_settings, minimum_saving_percent, status, phase, created_at,
                created_by, scheduler_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 'queued', ?, ?, ?)
            """,
            (
                video["id"], video["path"], video["size_bytes"], video["mtime_ns"],
                video["video_codec"], video["bit_rate"], preset, encoder,
                encoder_settings, minimum_saving_percent, created_at,
                created_by, scheduler_run_id,
            ),
        )
        return cursor.lastrowid, True


JOB_FIELDS = {
    "original_sha1", "output_path", "optimized_size", "optimized_sha1",
    "optimized_video_codec", "optimized_bit_rate", "saving_percent", "status",
    "phase", "progress", "error", "validation_json", "started_at", "finished_at",
    "replaced_at", "restored_at", "backup_path", "attempt_count", "source_path",
    "video_id",
}


def _job_assignments(changes):
    unknown = set(changes) - JOB_FIELDS
    if unknown:
        raise ValueError(f"Недопустимые поля задания: {', '.join(sorted(unknown))}")
    return ", ".join(f"{name}=?" for name in changes)


def update_job(job_id, **changes):
    if not changes:
        return
    assignments = _job_assignments(changes)
    with connect() as connection:
        connection.execute(
            f"UPDATE optimization_jobs SET {assignments} WHERE id=?",
            (*changes.values(), job_id),
        )


def transition_job(job_id, expected, **changes):
    """Apply changes only if the job is still in one of the expected statuses."""
    expected = (expected,) if isinstance(expected, str) else tuple(expected)
    assignments = _job_assignments(changes)
    placeholders = ",".join("?" for _ in expected)
    with connect() as connection:
        cursor = connection.execute(
            f"UPDATE optimization_jobs SET {assignments} WHERE id=? AND status IN ({placeholders})",
            (*changes.values(), job_id, *expected),
        )
        return cursor.rowcount == 1


def get_job(job_id):
    with connect() as connection:
        return connection.execute(
            """
            SELECT optimization_jobs.*, videos.relative_path, videos.duration_seconds,
                   videos.width, videos.height, videos.rotation, videos.audio_streams,
                   videos.subtitle_streams, videos.data_streams, videos.probe_error,
                   videos.container, videos.fps, videos.pixel_format,
                   videos.color_transfer, videos.color_primaries
              FROM optimization_jobs
              JOIN videos ON videos.id=optimization_jobs.video_id
             WHERE optimization_jobs.id=?
            """,
            (job_id,),
        ).fetchone()


def next_queued_job():
    with connect() as connection:
        return connection.execute(
            "SELECT id FROM optimization_jobs WHERE status='queued' ORDER BY id LIMIT 1"
        ).fetchone()


JOB_SORTS = {
    "id": "optimization_jobs.id",
    "status": "optimization_jobs.status",
    "file": "videos.relative_path",
    "size": "optimization_jobs.source_size",
    "saving": "optimization_jobs.saving_percent",
    "created": "optimization_jobs.created_at",
    "finished": "optimization_jobs.finished_at",
    "source": "optimization_jobs.created_by",
}


def query_jobs(*, page=1, page_size=50, statuses=None, created_by="",
               sort="id", order="desc", query="", has_backup=False):
    page = max(1, int(page))
    page_size = min(100, max(1, int(page_size)))
    where = ["1=1"]
    values = []
    if statuses:
        placeholders = ",".join("?" for _ in statuses)
        where.append(f"optimization_jobs.status IN ({placeholders})")
        values.extend(statuses)
    if created_by:
        where.append("optimization_jobs.created_by=?")
        values.append(created_by)
    if query:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("videos.relative_path LIKE ? ESCAPE '\\'")
        values.append(f"%{escaped}%")
    if has_backup:
        where.append("optimization_jobs.backup_path IS NOT NULL")
    sort_sql = JOB_SORTS.get(sort, JOB_SORTS["id"])
    order_sql = "ASC" if str(order).lower() == "asc" else "DESC"
    join_sql = " FROM optimization_jobs JOIN videos ON videos.id=optimization_jobs.video_id "
    with connect() as connection:
        total = connection.execute(
            f"SELECT COUNT(*) {join_sql} WHERE {' AND '.join(where)}", values
        ).fetchone()[0]
        rows = connection.execute(
            f"SELECT optimization_jobs.*, videos.relative_path {join_sql} "
            f"WHERE {' AND '.join(where)} ORDER BY {sort_sql} {order_sql}, optimization_jobs.id DESC "
            "LIMIT ? OFFSET ?",
            (*values, page_size, (page - 1) * page_size),
        ).fetchall()
    return rows, total


def job_counts():
    with connect() as connection:
        rows = connection.execute(
            "SELECT status, COUNT(*) AS count FROM optimization_jobs GROUP BY status"
        ).fetchall()
    return {row["status"]: row["count"] for row in rows}


def update_video_after_replacement(video_id, *, size_bytes, mtime_ns, duration_seconds,
                                   bit_rate, video_codec, width, height, rotation,
                                   encoder_tag):
    with connect() as connection:
        connection.execute(
            """
            UPDATE videos
               SET size_bytes=?, mtime_ns=?, duration_seconds=?, bit_rate=?,
                   video_codec=?, width=?, height=?, rotation=?, encoder_tag=?,
                   classification='historical_handbrake', probe_error=NULL
             WHERE id=?
            """,
            (size_bytes, mtime_ns, duration_seconds, bit_rate, video_codec,
             width, height, rotation, encoder_tag, video_id),
        )


def clear_job_backup(job_id):
    with connect() as connection:
        connection.execute("UPDATE optimization_jobs SET backup_path=NULL WHERE id=?", (job_id,))


def delete_job(job_id):
    with connect() as connection:
        connection.execute("DELETE FROM optimization_jobs WHERE id=?", (job_id,))


def jobs_with_status(job_statuses):
    placeholders = ",".join("?" for _ in job_statuses)
    with connect() as connection:
        return connection.execute(
            f"SELECT id FROM optimization_jobs WHERE status IN ({placeholders}) ORDER BY id",
            tuple(job_statuses),
        ).fetchall()


def backup_job_paths():
    with connect() as connection:
        return connection.execute(
            "SELECT id, status, backup_path FROM optimization_jobs WHERE backup_path IS NOT NULL"
        ).fetchall()


def find_videos_by_size(size_bytes):
    with connect() as connection:
        return connection.execute(
            "SELECT * FROM videos WHERE present=1 AND size_bytes=? ORDER BY id",
            (int(size_bytes),),
        ).fetchall()


def replaced_jobs_with_backups():
    with connect() as connection:
        return connection.execute(
            """
            SELECT optimization_jobs.id, optimization_jobs.source_size,
                   optimization_jobs.replaced_at, videos.relative_path
              FROM optimization_jobs JOIN videos ON videos.id=optimization_jobs.video_id
             WHERE optimization_jobs.status='replaced'
               AND optimization_jobs.backup_path IS NOT NULL
             ORDER BY optimization_jobs.replaced_at, optimization_jobs.id
            """
        ).fetchall()


def jobs_with_backups_older_than(replaced_before):
    with connect() as connection:
        return connection.execute(
            """
            SELECT id, backup_path, source_size, original_sha1, replaced_at
              FROM optimization_jobs
             WHERE status='replaced' AND backup_path IS NOT NULL
               AND replaced_at IS NOT NULL AND replaced_at<=?
             ORDER BY replaced_at, id
            """,
            (str(replaced_before),),
        ).fetchall()


def get_setting(key, default=None):
    with connect() as connection:
        row = connection.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
    return default if row is None else json.loads(row["value_json"])


def set_settings(values):
    now = utcnow()
    with connect() as connection:
        for key, value in values.items():
            connection.execute(
                """
                INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,
                                               updated_at=excluded.updated_at
                """,
                (key, json.dumps(value, ensure_ascii=False), now),
            )


def create_scheduler_run(*, trigger, scheduled_for, rules, started_at):
    try:
        with connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO scheduler_runs(trigger, scheduled_for, status, rules_json, started_at)
                VALUES (?, ?, 'running', ?, ?)
                """,
                (trigger, scheduled_for, json.dumps(rules, ensure_ascii=False, sort_keys=True), started_at),
            )
            return cursor.lastrowid
    except sqlite3.IntegrityError:
        return None


def update_scheduler_run(run_id, **changes):
    allowed = {
        "status", "scan_run_id", "candidates", "added", "skipped",
        "finished_at", "error",
    }
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"Недопустимые поля запуска: {', '.join(sorted(unknown))}")
    if not changes:
        return
    assignments = ", ".join(f"{name}=?" for name in changes)
    with connect() as connection:
        connection.execute(
            f"UPDATE scheduler_runs SET {assignments} WHERE id=?",
            (*changes.values(), run_id),
        )


def query_scheduler_runs(*, page=1, page_size=25):
    page = max(1, int(page))
    page_size = min(100, max(1, int(page_size)))
    with connect() as connection:
        total = connection.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()[0]
        rows = connection.execute(
            "SELECT * FROM scheduler_runs ORDER BY id DESC LIMIT ? OFFSET ?",
            (page_size, (page - 1) * page_size),
        ).fetchall()
    return rows, total


def add_event(level, source, message, job_id=None):
    with connect() as connection:
        connection.execute(
            "INSERT INTO events(created_at, level, source, job_id, message) VALUES (?, ?, ?, ?, ?)",
            (utcnow(), level, source, job_id, str(message)[-2000:]),
        )


def query_events(*, include_dismissed=False, limit=50):
    where = "" if include_dismissed else "WHERE dismissed_at IS NULL"
    with connect() as connection:
        return connection.execute(
            f"SELECT * FROM events {where} ORDER BY id DESC LIMIT ?", (int(limit),)
        ).fetchall()


def dismiss_events(event_id=None):
    with connect() as connection:
        if event_id is None:
            connection.execute("UPDATE events SET dismissed_at=? WHERE dismissed_at IS NULL", (utcnow(),))
        else:
            connection.execute(
                "UPDATE events SET dismissed_at=? WHERE id=? AND dismissed_at IS NULL",
                (utcnow(), int(event_id)),
            )


def overview_facts():
    with connect() as connection:
        saved = connection.execute(
            """
            SELECT COUNT(*) AS count,
                   COALESCE(SUM(source_size - optimized_size), 0) AS bytes,
                   COALESCE(SUM(source_size), 0) AS source_bytes
              FROM optimization_jobs WHERE status='replaced'
            """
        ).fetchone()
        scan = connection.execute(
            """
            SELECT * FROM scan_runs WHERE status='completed'
             ORDER BY finished_at DESC LIMIT 1
            """
        ).fetchone()
        run = connection.execute("SELECT * FROM scheduler_runs ORDER BY id DESC LIMIT 1").fetchone()
        placeholders = ",".join("?" for _ in statuses.ATTENTION)
        attention = connection.execute(
            f"""
            SELECT status, COUNT(*) AS count FROM optimization_jobs
             WHERE status IN ({placeholders}) GROUP BY status
            """,
            statuses.ATTENTION,
        ).fetchall()
        ready_errors = connection.execute(
            "SELECT COUNT(*) FROM optimization_jobs WHERE status='ready' AND error IS NOT NULL"
        ).fetchone()[0]
    return {
        "saved": dict(saved),
        "last_scan": dict(scan) if scan else None,
        "last_scheduler_run": dict(run) if run else None,
        "attention_jobs": {row["status"]: row["count"] for row in attention},
        "ready_with_errors": ready_errors,
    }
