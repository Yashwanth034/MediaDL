# MediaDL Architecture

## Design principles
1. Simple common path, powerful advanced path.
2. External engines are adapters, never application-wide dependencies.
3. Persist every long-running operation as durable state.
4. Conservative duplicate decisions: uncertain media is kept.
5. No hidden shell construction from user/media metadata.
6. No internal state clutter in user download folders by default.
7. Every expensive operation is demand-driven and cacheable.
8. Cross-platform path/process behavior is isolated behind helpers.

## Layers

### cli
Owns command parsing, interactive prompts, terminal rendering, validation, and exit codes. Bare `mdl` uses a guided argument builder that compiles user choices into the same normal download command arguments used by explicit CLI invocations; it does not create a second execution path. The CLI does not directly call yt-dlp/FFmpeg.

### core
Stable models, enums, errors, config, paths, numeric parsing, and application services shared by all layers.

### sources
Resolves user input into source descriptors and normalized source identities. Initial source adapter is YouTube via yt-dlp. Channel playlist discovery is isolated here as a metadata-only catalog: the guided CLI can enumerate/search/select playlists first, then feed the chosen normal playlist URLs back into the same collection planner used everywhere else.

### engines
Adapters around yt-dlp, ffmpeg/ffprobe, and Chromaprint/fpcalc. Adapter boundaries make upgrades replaceable.

### index
Scans channels/playlists, caches metadata, performs demand-driven enrichment, and detects changed/new items.

### selection
Pure deterministic filtering/sorting/range logic. Numeric thresholds are normalized before filtering.

### downloads
Creates immutable download plans and durable jobs, executes a queue, handles retry/backoff/resume, and emits progress events. Collection execution uses bounded format-aware parallelism: M4A/Opus can download up to four media items concurrently; MP3/FLAC/WAV use up to three source-download workers feeding a separate bounded pool of up to two FFmpeg conversion workers; video collections use up to three media workers. Each yt-dlp transfer may also fetch up to four DASH/HLS fragments concurrently. Dedupe checks happen before bandwidth use, SQLite state transitions remain transactional, and Ctrl+C drains/stops workers into a resumable paused job.

### dedupe
Candidate generation, exact identity/hash checks, audio fingerprints, video perceptual sampling, classification, and user policy decisions.

### storage
SQLite repositories, schema migrations, atomic file finalization, output naming, archive integration, and application-data paths.

### auth
Cookie/browser-auth configuration without exposing credentials to logs.

### diagnostics/update
`doctor` checks runtime health. Update system handles app/engine update policy independently.

## Stable interfaces

### SourceAdapter
- resolve(url) -> SourceDescriptor
- enumerate(source, scan_options) -> MediaItem stream
- enrich(items, fields) -> MediaItem stream

### DownloadEngine
- inspect(media) -> DownloadCapabilities
- build_request(media, format_policy) -> EngineRequest
- download(request, progress_hook) -> EngineResult

### MediaEngine
- probe(path) -> MediaProbe
- remux/convert(...)
- normalized hashes / samples

### FingerprintEngine
- available() -> bool
- audio_fingerprint(path) -> fingerprint
- video_fingerprint(path, strategy) -> fingerprint

### SelectionEngine
- select(items, SelectionSpec) -> ordered selection

### JobRepository
- create_plan/job
- update item state transactionally
- recover interrupted jobs
- query history/failures

### StorageRepository
- source/media/download/cache records
- fingerprint records
- duplicate groups

## Persistent data model
SQLite schema is migration-versioned from the first release.

Primary entities:
- schema_migrations
- sources
- media_items
- media_stats
- jobs
- job_items
- downloads
- files
- fingerprints
- duplicate_groups
- duplicate_members
- failures
- settings

The database stores normalized source identity separately from file paths so downloads can move without losing source history.

## Job state machine
Job: PLANNED -> RUNNING -> COMPLETED | COMPLETED_WITH_FAILURES | PAUSED | CANCELLED

Job item: PENDING -> INSPECTING -> DOWNLOADING -> POSTPROCESSING -> VERIFYING -> COMPLETED
Alternative terminal states: SKIPPED_DUPLICATE, SKIPPED_UNAVAILABLE, FAILED_RETRYABLE, FAILED_FINAL, CANCELLED.

No item enters COMPLETED until the final output is verified and atomically finalized.

## Duplicate pipeline
1. Normalize source identity.
2. Check existing source ID/archive.
3. Cheap metadata candidate grouping: duration bucket, normalized title tokens, channel/source family, approximate size when useful.
4. Exact SHA-256 when file exists.
5. Audio fingerprint only for plausible candidates.
6. Video sampled perceptual fingerprint only when video-specific confirmation is necessary.
7. Classify with explicit evidence and confidence.
8. Apply policy.

Safe policy never removes/omits an uncertain visual variant because its audio matches.

## Selection/filter semantics
All user numeric input is parsed into integer base units before query/filter execution.

Supported suffixes:
- K = 1,000
- L = 100,000
- M = 1,000,000
- Cr = 10,000,000
- B = 1,000,000,000

Decimal shorthand is accepted only where exact conversion to an integer base unit is deterministic, e.g. 2.5Cr -> 25,000,000.

`above` and `below` are strict comparisons. `between A:B` is inclusive at both ends unless a future flag explicitly requests exclusive boundaries.

## Output naming
Naming is centralized and deterministic. It sanitizes Windows/macOS/Linux-invalid components, blocks traversal and reserved names, constrains length, and retains immutable source IDs for collision safety.

## Process safety
External commands are invoked with argument arrays/API calls, never interpolated shell strings. Timeouts, cancellation, exit status, and stderr redaction are centralized.

## Observability
Normal mode shows concise user-facing progress. Structured internal logs live in app data. `--verbose` exposes useful engine diagnostics with secret redaction.

## Packaging
Development uses Python packaging. Release artifacts are PyInstaller one-file executables so end users do not activate a venv. Python dependencies, yt-dlp, yt-dlp EJS, and a checksum-verified supported Deno runtime are embedded; migration SQL resources are explicitly bundled and verified through the normal schema checksum path. FFmpeg remains a required external media capability and ffprobe remains an external probing capability discovered through adapters; `mdl doctor` reports required/optional status accurately.

Per-platform builds emit a platform-tagged executable plus a SHA-256 manifest fragment. The release manifest maps OS/architecture keys to HTTPS assets and checksums. Standalone installs check the official latest-release manifest on a bounded six-hour cadence; failures are best-effort and never block the requested command. `mdl update` remains the explicit path, downloads to a staging path, verifies SHA-256 before replacement, uses atomic replacement on POSIX, and schedules post-exit replacement on Windows where a running executable cannot safely overwrite itself.

Per-user installers validate `mdl --version` before replacing the installed copy and update PATH idempotently. Linux/macOS use a user bin directory; Windows uses `%LOCALAPPDATA%\\MediaDL\\bin`.
