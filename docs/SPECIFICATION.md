# MediaDL v1.0 Specification

## Product goal
MediaDL is a clean, cross-platform terminal CLI for downloading one video, playlists, or complete channel collections without requiring users to learn yt-dlp syntax. After one-time installation the user runs `mdl` from any terminal.

## Core source types
- Single video URL
- Playlist URL
- Channel root URL
- Channel Videos tab
- Channel Shorts tab
- Channel Streams/Live tab
- Channel Playlists tab for guided playlist discovery; users can choose one, several, search by title, or use all playlists
- Multiple mixed sources in one invocation; each collection keeps independent scan/plan/job/resume state

## Core selection modes
- All
- First N
- Last N
- Range A:B
- Latest N
- Oldest N
- Most viewed / least viewed
- Most liked / least liked
- Upload date range
- Duration range
- Title search
- Advanced compound filters

## Numeric engagement filters
Views, likes, and similar supported numeric metadata must accept:
- Above threshold
- Below threshold
- Between two thresholds
- Plain integers: `1000000`
- International shorthand: `1K`, `1M`, `1B`
- Indian shorthand: `10L`, `50L`, `1Cr`, `2.5Cr`

Examples:
- `--views-above 10L`
- `--views-below 1Cr`
- `--views-between 10L:1Cr`
- `--likes-above 50K`
- `--likes-between 1L:5L`

Threshold parsing is case-insensitive and deterministic. Invalid ambiguous values must fail with a clear error before downloads begin.

## Generic user-entered filters
MediaDL must not require a new hard-coded flag for every possible threshold. Repeatable `--where` expressions provide a general comparison layer while convenience flags remain available for common views/likes cases.

Supported v1 fields include views, likes, duration, upload date, title, channel/uploader, availability/status, and media type. Numeric/date comparisons support `>`, `>=`, `<`, `<=`, `=`, and `!=`; text comparisons support `=`, `!=`, contains `~`, and not-contains `!~`. Values use the same human-friendly numeric, duration, and date parsers as dedicated options.

Examples: `--where "views>=12.5L"`, `--where "likes<3L"`, `--where "duration<=15m"`, `--where "date>=2025-01-01"`, `--where "channel~example"`. Multiple expressions are combined with AND semantics. If a source still does not expose a required field after detailed enrichment, MediaDL treats that value as unknown: the item does not match that filter, and metric/date/duration sorting places unknown values after known values rather than inventing `0` or aborting the whole collection.

## Formats
Video: MP4, WebM, MKV, original/best.
Audio: MP3, M4A, Opus, FLAC, WAV.

## Quality
Video presets: best, 2160p, 1440p, 1080p, 720p, 480p, 360p, lowest.
Default requested-resolution behavior is requested quality or closest sensible lower quality. Exact mode is available explicitly.
Audio quality supports sensible format-specific presets.

## Filenames and folders
Default filenames are readable and collision-safe. Video/channel downloads use a sanitized title plus immutable source ID, optionally prefixed by upload date when useful.
Example: `2025-08-04 - Building a Linux PC [x7AbC92].mp4`.

Channel downloads use a clean channel root with only folders that are actually used, e.g. Videos, Shorts, Streams, Audio. Internal state is stored in the app data database rather than scattered sidecar files by default.

## Duplicate prevention
Duplicate detection is conservative by default and never treats title equality alone as proof.
Layers:
1. Source identity: platform + source video ID.
2. Exact file SHA-256.
3. Normalized decoded-media fingerprints/hashes where appropriate.
4. Audio fingerprinting for candidate media.
5. Video perceptual sampling/fingerprinting for suspicious candidates.

Classification: EXACT, SAME_MEDIA, AUDIO_VARIANT, LIKELY_VARIANT, DIFFERENT, UNKNOWN.

Default Safe policy skips same source IDs, exact duplicates, and extremely high-confidence same-media duplicates. It keeps lyric vs official videos, live performances, remixes, edits, and uncertain matches. MP3/audio dedupe may keep the best-quality copy when the audio is effectively identical. MP4/video dedupe must not discard visually distinct videos solely because audio matches.

## Download planning
Large jobs are planned before execution. Preview shows channel/source, selected count, order/filter, output format, quality, duplicate mode, destination, estimated size when available, and available disk space.

`--preview` performs planning without downloading.

## Reliability
- Persistent SQLite jobs
- Safe retry/backoff
- Resume interrupted downloads
- yt-dlp archive as a second protection layer
- Atomic finalization: temporary/partial files never count as complete
- Skip unavailable/private/deleted items without aborting the whole job
- `mdl recover-unavailable JOB_ID` explicitly requeues only items previously recorded as unavailable; completed downloads and duplicate skips remain terminal and untouched
- Retry only retryable failures
- Disk-space guard before transfer and continuously during yt-dlp progress; low disk pauses collection work as retryable
- Network interruption handling
- Multi-source failure isolation: one failed source does not prevent remaining sources from being processed, while the final command still exits non-zero for partial completion
- Direct-video source/profile and exact-file dedupe uses the same persistent SQLite state as collection downloads
- Clear final summary

## Indexing
Channel metadata is cached locally. Quick metadata is used for operations that do not require deep statistics. Expensive metadata enrichment is requested only when necessary, for example most-liked sorting when the listing does not expose sufficient data. Subsequent scans reuse cached data and check for changes.

## Authentication
Support browser-cookie or equivalent user-authorized access where yt-dlp supports it. Never log secrets. No DRM bypass functionality.

## User experience
Running bare `mdl` in an interactive terminal opens the guided workflow. It asks for a YouTube URL or `@channel`, detects the source type, and performs lightweight availability checks before allowing a selected media section or pasted collection to proceed. Channel roots offer Videos, Shorts, Streams/Live, Playlists, and Everything. Videos/Shorts/Streams are probed before continuing; unavailable individual sections return the user to the section menu, while Everything automatically skips confirmed-empty media tabs and continues with the available ones. Playlists are handled separately because they are organizational collections that can overlap the same videos already present in channel tabs: MediaDL discovers the channel playlist catalog, shows a numbered paginated list, supports title search, and lets the user choose one, several, all search matches, or all channel playlists. The next media-selection/filter/output choices then apply independently to each chosen playlist and persistent dedupe handles videos repeated across playlists. Direct playlist URLs remain supported. The selection menu uses explicit examples so item counts cannot be confused with view/like thresholds, and very large N values receive a confirmation warning. Guided collections expose all/first/last/latest/oldest/range/top-or-bottom viewed/liked selection, a quick maximum-duration skip for long uploads (for example `6m`, `30m`, or `1h`), repeatable views/likes/date/duration/title/custom filters, optional sorting, output format, quality, and preview-only mode. Preview is the safe default for collections. The guided workflow compiles these answers into the same normal CLI arguments used by power users; there is no second download engine or reduced-capability path.

Normal transfers show concise terminal progress with current item position, bytes, percentage, transfer speed, and ETA when yt-dlp exposes those values. Progress is transient so completed jobs do not leave a large terminal UI behind. MediaDL uses bounded concurrent DASH/HLS fragment fetching (4 fragments by default) to improve single-media throughput while keeping separate media items/jobs sequential and preserving retry/dedupe safety. Actual speed still depends on the source CDN, selected format, network path, and local system.

Common commands remain short:
- `mdl URL`
- `mdl URL --mp4`
- `mdl URL --mp3`
- `mdl CHANNEL --latest 20`
- `mdl CHANNEL --all --mp4 --quality 1080`
- `mdl resume`
- `mdl retry`
- `mdl recover-unavailable JOB_ID`
- `mdl history`
- `mdl doctor`
- `mdl update`
- `mdl config`

Interactive prompts appear only when required information is absent. Explicit CLI flags bypass corresponding prompts.

## Installation
One-time install. The end user should not need to activate a virtual environment, `cd` into the repository, start a service, or reinstall for every use. Release artifacts target Linux x86_64/ARM64, Windows x64, macOS Intel, and macOS Apple Silicon where build infrastructure permits.

## External components
- yt-dlp: extraction/download engine, integrated behind an adapter
- JavaScript runtime/EJS: standalone builds bundle Deno 2.9.5 and yt-dlp EJS for modern YouTube challenge solving. Source/pip installs may use supported Deno, Node 22+, QuickJS, or Bun; YouTube operations fail fast when no supported runtime/EJS is available rather than misclassifying extractor breakage as media unavailability
- FFmpeg/ffprobe: FFmpeg is required for muxing/conversion; ffprobe supports probing and normalized media/fingerprint operations
- Chromaprint/fpcalc: audio fingerprinting where available
- SQLite: built-in persistent metadata/job database

MediaDL does not fork yt-dlp. External engines are isolated behind internal interfaces so engine changes do not force application-wide rewrites.

## Security
- Never construct unsafe shell strings from untrusted media metadata
- Sanitize and constrain output paths
- Block traversal and invalid filenames
- Redact credentials/cookies from logs
- Handle hostile/oversized metadata safely

## v1.0 completion definition
v1.0 is complete only after all 20 locked stages are implemented and verified, including large-channel, duplicate, interruption, security, packaging, and cross-platform regression work.