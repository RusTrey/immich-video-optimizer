# Changelog

## 0.9.5

First public release.

### Encoding

- HandBrakeCLI 1.11.2 with x265 or x264, quality RF 16–30, a 720p–2160p limit
  that respects orientation and never upscales, AAC passthrough with an AAC-LC
  fallback.
- HDR (HLG, PQ) and 10-bit sources are encoded to 10-bit x265 with the colour
  characteristics preserved; the frame rate is capped at 60 fps.
- MP4, MOV, M4V and 3GP sources; the result keeps the original file name.
- Encoding profile is snapshotted into each job; CPU count and priority are
  limited for HandBrakeCLI, FFmpeg and FFprobe.

### Validation and replacement

- Every result is checked before it can replace anything: streams, codec,
  duration, frame rate, resolution and orientation, HDR and bit depth, metadata
  (QuickTime Keys, date, GPS, device), a full FFmpeg decode and a minimum saving.
- SHA-1 of the source and the result are recorded and checked again right
  before an atomic `renameat2(RENAME_EXCHANGE)` swap; owner and mode of the media
  file are kept.
- The original goes to a backup folder and can be restored or deleted from the
  UI. File operations run one at a time; interrupted replacements and restores
  are checked by SHA-1 after a restart and completed or rolled back.
- Immich's database is never touched.

### Automation

- Scheduler with days, time, time zone, grace period and a per-run limit;
  conditions on size, bitrate, duration, index age, resolution, path and codec.
- Optional index refresh before selection, automatic replacement of scheduler
  jobs and automatic deletion of verified backups after N days — all off by
  default.
- The overview shows what the next run will do.

### Web UI

- Overview, queue, library, journal and settings in English and Russian, with
  light, dark and system themes.
- Job card with checks, paths, backup deletion date and the HandBrake log tail.
- Side-by-side and A/B comparison of the original and the result.
- No login: intended for a trusted network; mutating requests require an
  interface header and a same-origin `Origin`.

### Project

- Docker image for `linux/amd64` with HandBrake built from official sources,
  SBOM and provenance; compose example with dropped capabilities and a
  read-only root filesystem.
- Unit and integration tests with HandBrake, FFmpeg and ExifTool.
