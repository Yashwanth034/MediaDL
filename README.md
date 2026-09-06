# MediaDL

MediaDL is a terminal-first downloader for YouTube videos, playlists, Shorts, streams, and complete channels. It keeps common downloads simple while adding persistent jobs, filtering, resumability, conservative duplicate protection, and large-channel handling around yt-dlp.

**Version:** 1.0.0

The Linux build has been directly user-validated. The release workflow is configured to build and validate standalone artifacts for Linux x86_64/ARM64, Windows x86_64/ARM64, and macOS Intel/Apple Silicon.

## Common usage

Run bare `mdl` for a guided terminal flow that asks for the source, channel section, selection/filter rules, format, and quality:

```bash
mdl
```

Power users can still use explicit one-line commands:

```bash
mdl VIDEO_URL
mdl VIDEO_URL --mp4
mdl VIDEO_URL --mp3
mdl VIDEO_URL --mp4 --quality 1080

mdl CHANNEL_URL --latest 20
mdl CHANNEL_URL --views-above 10L --mp4 --quality 1080
mdl CHANNEL_URL --likes-between 1L:5L --preview
mdl CHANNEL_URL --sort most_liked --first 50

mdl CHANNEL_A CHANNEL_B --videos --latest 20
mdl CHANNEL_A CHANNEL_B --where "views>=12.5L" --where "likes<3L" --mp4
mdl CHANNEL_URL --where "duration<=15m" --where "channel~example" --preview

mdl resume
mdl retry
mdl recover-unavailable JOB_ID
mdl history
mdl doctor
mdl config
mdl update --check
```

`--where` is repeatable and accepts user-entered values instead of requiring a hard-coded flag for every threshold. Supported v1 fields are views, likes, duration, upload date, title, channel/uploader, availability/status, and media type.

Numeric filters accept plain values plus `K`, `L`, `M`, `Cr`, and `B`, including decimals such as `12.5L` and `2.5Cr`.

## What v1 includes

- Single-video MP4, WebM, MKV, original, MP3, M4A, Opus, FLAC, and WAV output
- Best, bounded-resolution, exact-resolution, lowest, and audio-quality policies
- Direct playlist URLs plus channel playlist discovery/selection, channel Videos, Shorts, Streams, and multi-source batches
- First/last/range/latest/oldest and metric/date/title/duration filtering/sorting
- Persistent SQLite metadata cache, jobs, history, retry, crash recovery, and resume
- Source/profile, SHA-256, Chromaprint audio, and perceptual-video duplicate protection
- Conservative variant handling: same audio does not make two visually different videos duplicates
- Collision-safe filenames containing immutable source IDs
- Runtime disk-space guard that pauses resumable jobs before the destination fills
- Guided bare-`mdl` workflow with lightweight availability checks, clear examples, a clear confirmation before transfer, Videos/Shorts/Streams, searchable numbered channel playlists with one/many/all selection, available-only Everything for media tabs, first/latest/ranges/top-N choices, views/likes/date/duration/title filters, a quick maximum-duration skip for long uploads, format, and quality
- Concise terminal transfer progress with bytes, speed, percentage, and ETA when available
- Bounded concurrent DASH/HLS fragment fetching (4 by default), alongside conservative collection-worker limits for reliable throughput
- Browser-cookie access for content the user is authorized to access
- Standalone builds bundle a validated Deno runtime plus yt-dlp EJS for reliable modern YouTube challenge solving, so users do not need to install or upgrade Node/Deno; source/pip installs still accept supported Deno, Node 22+, QuickJS, or Bun runtimes
- Automatic standalone update checks use the official latest-release manifest and SHA-256 verification; `mdl update --check` remains available for an explicit check
- Daily dependency automation watches Python packages, GitHub Actions, and the bundled Deno runtime; dependency changes must pass the full six-platform release build before they are suitable for release
- `mdl doctor`, checksum-verified standalone updates, and one-time per-user installers
- Large-channel bounded scans and subquadratic smart-dedupe candidate screening

## Installation model

Release binaries are standalone. After one-time installation, normal use is simply:

```bash
mdl URL
```

No virtual-environment activation, repository `cd`, background service, or repeated installation is required. Standalone installs perform a best-effort update check at most once every six hours; an offline or failed update check never blocks the requested command. Administrators can set `MEDIADL_DISABLE_AUTO_UPDATE=1` when centrally managing releases.

FFmpeg is a required external media dependency for merge/conversion operations; ffprobe is used for probing and smart media fingerprinting. The standalone bundles its JavaScript runtime and EJS support, but not FFmpeg/ffprobe. MediaDL never silently replaces system FFmpeg; `mdl doctor` reports its status so the operating system's package manager remains authoritative.

## Documentation

- `docs/SPECIFICATION.md` — v1 product behavior
- `docs/ARCHITECTURE.md` — internal boundaries and persistence design
- `docs/STAGES.md` — 20-stage completion record
- `docs/LINUX_TEST.md` — first Linux user-validation checklist

## Safety and scope

MediaDL is intended for media the user owns or has the right or permission to save. It supports authorized cookies where yt-dlp supports them and does not implement DRM bypassing.
