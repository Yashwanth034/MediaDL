# MediaDL

[![Release](https://img.shields.io/github/v/release/Yashwanth034/MediaDL)](https://github.com/Yashwanth034/MediaDL/releases/latest)
[![License](https://img.shields.io/github/license/Yashwanth034/MediaDL)](LICENSE)

A clean cross-platform terminal downloader for YouTube videos, playlists, Shorts, streams, and complete channels.

MediaDL keeps simple downloads simple, while adding resumable jobs, filtering, duplicate protection, large-channel handling, and guided terminal workflows on top of yt-dlp.

## Install

Download the correct standalone binary from the [latest release](https://github.com/Yashwanth034/MediaDL/releases/latest):

| Platform | Asset |
|---|---|
| Linux x86_64 | `mdl-linux-x86_64` |
| Linux ARM64 | `mdl-linux-arm64` |
| Windows x86_64 | `mdl-windows-x86_64.exe` |
| Windows ARM64 | `mdl-windows-arm64.exe` |
| macOS Intel | `mdl-macos-x86_64` |
| macOS Apple Silicon | `mdl-macos-arm64` |

Place the binary somewhere on your `PATH` and name it `mdl` (`mdl.exe` on Windows).

The repository also includes one-time installers:

```bash
# Linux / macOS
sh packaging/install.sh PATH_TO_DOWNLOADED_BINARY
```

```powershell
# Windows PowerShell
powershell -ExecutionPolicy Bypass -File packaging/install.ps1 PATH_TO_DOWNLOADED_BINARY
```

After installation, MediaDL can be run from any terminal:

```bash
mdl
```

> **FFmpeg is required** for MediaDL's normal merge and conversion workflows. Run `mdl doctor` to check your installation.

Standalone builds already include the JavaScript runtime and yt-dlp EJS support, so users do not need to install Python, Node, or Deno.

## Usage

Run `mdl` with no arguments for the guided flow:

```bash
mdl
```

Or use direct commands:

```bash
mdl VIDEO_URL
mdl VIDEO_URL --mp4 --quality 1080
mdl VIDEO_URL --mp3

mdl PLAYLIST_URL --latest 20
mdl CHANNEL_URL --videos --latest 50
mdl CHANNEL_URL --views-above 10L --mp4
mdl CHANNEL_URL --where "duration<=15m" --preview

mdl resume
mdl retry
mdl history
mdl doctor
mdl update --check
```

Numeric filters understand values such as `50K`, `10L`, `1M`, `2.5Cr`, and `1B`.

## Features

- Videos, playlists, Shorts, streams, complete channels, and multi-source batches
- MP4, WebM, MKV, MP3, M4A, Opus, FLAC, WAV, and original/best output
- Resolution and audio-quality controls
- First, last, range, latest, oldest, views, likes, date, duration, title, and custom filters
- Persistent jobs with retry, interruption recovery, and resume
- Conservative duplicate protection using source IDs, SHA-256, audio fingerprints, and perceptual video checks
- Guided channel section and playlist selection
- Browser-cookie support for content the user is authorized to access
- Disk-space protection and concise live progress
- Checksum-verified automatic standalone updates

## Automatic updates

Standalone installs periodically check the official MediaDL release feed. When a newer release is available, MediaDL downloads the correct platform build, verifies its SHA-256 checksum, and updates itself.

A failed or offline update check never blocks the command you asked MediaDL to run.

Dependencies are monitored separately in the repository and must pass the full six-platform CI build before they are suitable for a MediaDL release.

Set `MEDIADL_DISABLE_AUTO_UPDATE=1` if releases are centrally managed on your system.

## Supported platforms

Release builds are validated on:

- Linux x86_64
- Linux ARM64
- Windows x86_64
- Windows ARM64
- macOS Intel
- macOS Apple Silicon

## Documentation

- [Specification](docs/SPECIFICATION.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Development stages](docs/STAGES.md)

## License

MIT. See [LICENSE](LICENSE).

MediaDL is intended for media you own or have permission to save. It does not implement DRM bypassing.
