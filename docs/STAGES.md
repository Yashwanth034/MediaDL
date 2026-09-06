# MediaDL v1.0 — 20-Stage Completion Record

The v1.0 architecture remained within the original 20-stage plan. A stage was marked complete only after implementation and relevant verification passed.

| Stage | Scope | Status |
|---|---|---|
| 0 | Final specification and architecture lock | Complete |
| 1 | Project skeleton, config, logging, error model | Complete |
| 2 | SQLite schema and migrations | Complete |
| 3 | yt-dlp engine adapter | Complete |
| 4 | Single-video MP4/MP3 downloads | Complete |
| 5 | Format and quality engine | Complete |
| 6 | Filename and folder system | Complete |
| 7 | Channel/playlist resolver and scanner | Complete |
| 8 | Metadata index/cache | Complete |
| 9 | First/last/range/latest/oldest selections | Complete |
| 10 | Metric/date/title/duration sorting/filtering and K/L/M/Cr/B shorthand | Complete |
| 11 | Preview and immutable download-plan system | Complete |
| 12 | Queue, retry, interruption, and resume engine | Complete |
| 13 | Basic duplicate system: source identity, archive, SHA-256 | Complete |
| 14 | Smart audio/video fingerprint duplicate engine | Complete |
| 15 | Authentication, network, availability, and edge cases | Complete |
| 16 | Clean interactive/non-interactive CLI UX | Complete |
| 17 | Large-channel optimization and torture testing | Complete |
| 18 | Standalone installers, updater, and doctor | Complete |
| 19 | Final regression, documentation, Linux-ready v1.0.0 product state | Complete |

## Stage 19 scope for the first release test

The user explicitly chose to perform native platform release/build execution later, after first testing the finished product on Linux. Therefore Stage 19 product completion does not require running Windows/macOS/other-architecture release jobs in this development session.

Stage 19 local completion requires:
- version locked to 1.0.0
- full lint/test regression green
- release/package metadata consistent
- Linux standalone rebuilt from final source
- standalone `--version`, `--help`, and `doctor` smoke green
- isolated one-time installer/idempotency smoke green
- documentation synchronized with implemented behavior
- no unresolved product-code failure

Native Windows/macOS/Linux-ARM release execution remains a release-validation step after Linux user acceptance. The native build workflow and packaging tools are already present for that later step.

## Final local evidence

- Ruff: clean
- Tests: 329/329 passing
- Coverage audit: 86% across production code after adding guided channel-playlist discovery/selection; core engines remain strongly covered
- Dependency check: no broken requirements
- Python bytecode compilation: clean
- Package wheel: `mediadl-1.0.0-py3-none-any.whl` rebuilt and verified from the final v1.0.0 source
- Linux x86_64 standalone: rebuilt from the final v1.0.0 source with yt-dlp EJS and bundled Deno 2.9.5
- Standalone `--version`, `--help`, empty-PATH bundled-runtime smoke, and normal `doctor`: green
- Standalone database: schema 3, `PRAGMA integrity_check = ok`
- One-time POSIX installer run twice in isolated home: green, one PATH marker only, installed bytes match standalone SHA exactly; installed `doctor` reports yt-dlp 2026.08.19, EJS, bundled Deno 2.9.5, FFmpeg, and FFprobe healthy
- Guided standalone flow: bare `mdl` detects the source, verifies channel-section availability, exposes clear item-count examples, accepts any valid user-entered maximum duration directly (for example `7m`, `12m`, `1.5h`, `01:30:00`, or blank for no limit), and now ends with one normal `Start download?` confirmation instead of a separate guided preview-only question. YouTube-style ordering is labeled clearly as Latest, Popular (most viewed), and Oldest while retaining the broader advanced sort set. The advanced `--preview` CLI flag remains available for explicit power-user/testing use; unit coverage also verifies empty-section re-prompting and available-only Everything behavior
- Channel-playlist discovery verified against the real FOLK SONGS channel: MediaDL found 167 playlists, displayed a 20-item paginated numbered list, title search returned matching playlists, one/many/all-match selection compiled to normal playlist URLs, and the rebuilt standalone previewed a selected playlist without downloading media
- Transfer progress renderer hardened after real-terminal acceptance exposed redraw flooding: downloads now use one short title-free carriage-return line with a compact visual bar (`item/total`, percent, bytes, speed, ETA). Metadata startup shows an indeterminate `Preparing…` bar and audio post-processing shows `Converting MP3…`/equivalent instead of falsely appearing frozen at 100%. Focused tests plus an actual rebuilt-binary run in isolated GNOME Terminal confirmed clean in-place rendering with no persistent redraw history.
- Multi-playlist batches are fully planned before transfer starts and use one batch confirmation. This removes the old planning pause between playlist #1 and playlist #2 and prevents repeated Y/N prompts for later selected playlists. The rebuilt standalone visibly showed two frozen playlist plans followed by one `Start all selected downloads?` prompt.
- MP3 gap profiling on a long real playlist item measured SHA/dedupe at ~0.04s and next-item metadata startup at ~2.4s; the large apparent pause was FFmpeg MP3 conversion after source transfer completion. Guided audio choices now state that MP3 requires conversion and label MP3 `Best` as the slowest conversion choice rather than silently reducing quality.
- Live rebuilt standalone: user-chosen 720p MP4 transfer wrote 446,396,763 bytes in 43.08 seconds, FFprobe reported a valid 1197.57-second file, and no `.part` file remained
- Network/pipeline verification: no artificial `ratelimit`; `concurrent_fragment_downloads=4` is passed to yt-dlp for DASH/HLS media. Collection workers are bounded by output type: M4A/Opus up to 4 direct workers, video up to 3, and MP3/FLAC/WAV up to 3 source-download workers feeding up to 2 FFmpeg conversion workers
- Real heavy-audio collection smoke: a live six-item YouTube MP3 job completed 6/6 with 0 duplicate, 0 unavailable, and 0 failed through the split download/conversion pipeline
- Real SIGINT/resume smoke: a 20-item MP3 collection was interrupted during active work, persisted as paused with 0 active rows, then resumed the same job to 20/20 completed with 0 leftover `.part`, `.ytdl`, or conversion temp files
- Stale dedupe recovery verified live: after deleting a previously downloaded file, the next identical command re-downloaded it instead of falsely skipping; a third run then skipped normally with the valid file present
- Unavailable-recovery safety is covered by repository/CLI regression tests: `mdl recover-unavailable JOB_ID` requeues only `skipped_unavailable` rows and leaves completed/duplicate rows untouched
- JavaScript-runtime integration verified live: standalone `mdl doctor` reports bundled Deno 2.9.5 and yt-dlp EJS; real standalone M4A and source M4A/MP3 transfers completed without the old JavaScript-runtime/challenge/requested-format warnings
- Live 2-item Shorts collection job: completed with 1 downloaded, 1 safely skipped as duplicate, 0 failed; history persisted correctly
- Unavailable-video classification verified against a real unavailable YouTube URL: clear permanent `This media is unavailable.` error

## Scope lock

The behavior in `SPECIFICATION.md` and boundaries in `ARCHITECTURE.md` remain the v1 contract. Final-audit changes were correctness/completeness fixes inside those boundaries: guided bare-`mdl` UX, generalized user-entered filters, multi-source batches, bounded first-N deep-metadata planning, bounded fragment concurrency, continuous disk-space protection, concise progress, source failure isolation, and direct-video persistent exact/source dedupe.
