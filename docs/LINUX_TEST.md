# MediaDL v1.0 Linux User Validation

Run this only after the final standalone build is complete. These checks are for the first real user test before any wider platform release.

## 1. Enter the project

```bash
cd ~/Projects/MediaDL
```

## 2. Verify the standalone binary before installing

For x86_64 Linux:

```bash
./dist/mdl-linux-x86_64 --version
./dist/mdl-linux-x86_64 doctor
./dist/mdl-linux-x86_64 --help
```

Expected version: `MediaDL 1.0.1`.

## 3. Install once for your user

```bash
sh packaging/install.sh ./dist/mdl-linux-x86_64
```

Open a fresh terminal if PATH was newly added, then:

```bash
mdl --version
mdl doctor
```

After this one-time install, normal use is just `mdl ...`; no virtual environment or reinstall is required.

## 4. Verify the guided terminal workflow

Run:

```bash
mdl
```

Paste a channel or video URL you choose yourself. For a channel, confirm MediaDL checks the chosen Videos/Shorts/Streams section before proceeding, immediately returns to the section menu if that tab is empty, and lets Everything skip confirmed-empty media tabs while keeping available ones. Also choose Playlists once: confirm MediaDL discovers the channel playlist catalog, shows a numbered paginated list, lets you search titles and choose one/many/all playlists, and then applies the normal media selection/filter/output choices to the selected playlists. Confirm the selection menu shows examples that distinguish item counts from view/like thresholds, the quick maximum-duration prompt accepts values such as `6m`, `30m`, or `1h`, and the remaining views/likes/date/duration/title/custom filters, output format, quality, and sorting controls are present. Collection preview should default to Yes; keep preview enabled first so no media bytes are downloaded.

## 5. Start with metadata-only previews

Use channels/content you are allowed to save.

```bash
mdl CHANNEL_URL --latest 5 --preview
mdl CHANNEL_URL --views-above 10L --preview
mdl CHANNEL_URL --where "likes>=10K" --where "duration<=20m" --preview
mdl CHANNEL_A CHANNEL_B --videos --latest 3 --preview
```

Preview must not download media.

## 6. Test one video

```bash
mdl VIDEO_URL --mp4 --quality 720
```

Then test the same command again. Safe dedupe should recognize the completed source/profile rather than download it again.

For audio:

```bash
mdl VIDEO_URL --mp3
```

MP4 and MP3 are intentional different output profiles and may both exist.

## 7. Test a small collection job

```bash
mdl CHANNEL_URL --latest 3 --mp4 --quality 720 --yes
```

Check:
- readable collision-safe filenames containing source IDs
- channel folder organization
- concise transfer progress
- final summary

Then inspect:

```bash
mdl history
```

## 8. Test resume/retry only if needed

If a job becomes paused because of a temporary network/disk condition:

```bash
mdl resume
```

For retryable failures:

```bash
mdl retry
```

## 9. Test custom filtering

Examples:

```bash
mdl CHANNEL_URL --where "views>=12.5L" --preview
mdl CHANNEL_URL --where "likes<3L" --preview
mdl CHANNEL_URL --where "duration<=15m" --preview
mdl CHANNEL_URL --where "date>=2025-01-01" --preview
mdl CHANNEL_URL --where "title~linux" --preview
mdl CHANNEL_URL --where "channel~example" --preview
mdl CHANNEL_URL --where "availability=public" --preview
mdl CHANNEL_URL --where "type=short" --preview
```

Multiple `--where` expressions use AND semantics.

## 10. Do not test platform release publishing yet

Windows/macOS/other-architecture native builds and release publishing are intentionally deferred until the Linux user test is accepted.
