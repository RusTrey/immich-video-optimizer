# Immich Video Optimizer safety model

[Русская версия](safety.md) · [Back to README](../README.en.md)

Immich Video Optimizer works directly with media files. This document describes
validation, atomic replacement, rollback, and the deliberate Immich checksum
trade-off.

## Access boundaries

- The optimizer does not connect to Immich PostgreSQL or Redis and does not call
  the internal Immich API.
- The container sees only its private SQLite database and explicitly mounted
  `/storage` directories.
- The Web UI has no authentication and must not be exposed to an untrusted
  network without external protection.
- The container runs as root so replacement can preserve ownership and mode of
  externally managed media files.

## Encoding never overwrites the original

The source remains in place. Output is created separately below `WORK_ROOT`.
Stopping the container, a HandBrake failure, or a validation failure does not
delete or overwrite the source.

Every job stores an encoding profile snapshot. Later setting changes do not
alter the existing queue.

## Output validation

Before replacement is allowed, the optimizer verifies:

- the expected video and audio stream count;
- absence of subtitle and data streams;
- duration relative to the source;
- orientation and the no-upscale resolution ceiling;
- video stream duration and frame rate (at most 60 fps);
- for HDR and 10-bit sources: transfer function, primaries, bit depth and Dolby
  Vision metadata;
- dates, GPS and QuickTime Keys metadata (Android tags, creation date, model,
  Live Photo identifier);
- source and output SHA-1;
- sufficient reduction in file size;
- full output decodability through FFmpeg.

A file with multiple audio tracks, subtitle/data streams, an `ffprobe` error, a
container outside the MP4/MOV family, or insufficient size reduction is rejected
rather than replaced.

By default, output must be at least 20% smaller than the source. For example, a
1 GB source requires output of approximately 800 MB or less. The threshold is
configured through `MINIMUM_SAVING_PERCENT`.

## Atomic replacement

Immediately before replacement, source and output size, mtime, and SHA-1 are
checked again. This prevents replacement when a file changed after job creation.

The source and `WORK_ROOT` must reside on one filesystem. Linux
`renameat2(RENAME_EXCHANGE)` swaps the files atomically: observers see either the
old or new version, never a partially written file. The old original is then
moved to:

```text
optimizer-work/backups/<job-id>/
```

The optimizer stores independent source, output, and backup SHA-1 values in its
SQLite database.

File operations (replace, restore, backup deletion, cleanup) run one at a time,
and a job status changes only from the expected one. A repeated click, "Replace
all" and scheduler auto-replacement cannot run the same replacement twice.

## Crash recovery

The backup path is stored in SQLite before files are moved. If the container
stops during a replacement or restore, the next start finds the job files (in the
library, the work directory and backups), identifies each version by size and
SHA-1, and completes or rolls back the operation automatically. If the state is
ambiguous, the job stays "Replacement interrupted" or "Restore interrupted" with a
description of the files found, and nothing is deleted.

## Rollback and backup deletion

Before restoration, both current files and their recorded SHA-1 values are
verified. The original is atomically restored to its previous path, the
optimized version is removed, and the job stays in history as "Original
restored", so the scheduler does not pick the video again. If Immich moved the
file after replacement, restore finds it by size and SHA-1 in the refreshed index.

Manual and automatic backup deletion verify recorded size and SHA-1. An
unexpectedly changed backup is not deleted. Automatic cleanup keeps a backup when
the optimized file is missing from the library. "Delete all backups" deletes only
verified backups of replaced videos; files without a job are not touched.

## Immich checksum

The checksum in the Immich database identifies the original file previously
uploaded by a phone. The mobile backup client uses it to determine that the
original already exists on the server.

Optimization changes the server-side bytes. If Immich were updated with the new
checksum, the original still on the phone would no longer match and could be
uploaded again during a later synchronization, creating a duplicate.

The optimizer therefore deliberately leaves the Immich database unchanged and
preserves the old checksum. The mobile client continues to recognize the source
upload.

This is a deliberate trade-off: after replacement, the Immich checksum is no
longer a hash of the physical bytes on disk. The optimizer's own SHA-1 records
are used for destructive operations.

Immich continues to see the same asset and path, but stored technical metadata or
generated derivatives may become stale. Run the appropriate Immich metadata jobs
when required.

## Automation boundaries

- Administrator-created jobs are replaced manually only.
- Automatic replacement applies only to scheduler jobs and is disabled by
  default.
- Automatic backup deletion is a separate opt-in setting and is disabled by
  default.
- Backup retention defaults to 30 days.
- One job failure does not stop the queue or broaden automation scope.

## Before enabling replacement

1. Confirm that `/storage/data` and `/storage/optimizer-work` reside on one
   filesystem.
2. Confirm that the mount is readable and writable.
3. Keep `REPLACEMENT_ENABLED=false` and complete several encodes.
4. Preview the output and inspect its metadata.
5. Test replacement and restoration on a disposable file.
6. Only then enable automatic replacement or backup cleanup.
