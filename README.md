# RipFoundry for Windows

<a href="https://github.com/sponsors/kylereddoch"><img src="https://img.shields.io/badge/GitHub%20Sponsors-30363D?style=for-the-badge&logo=GitHub-Sponsors&logoColor=white" alt="GitHub Sponsors" height="20px"></a>
<a href="https://ko-fi.com/kylereddoch"><img src="https://img.shields.io/badge/Ko--fi-F16061?style=for-the-badge&logo=ko-fi&logoColor=white" alt="Ko-fi" height="20px"></a>
<a href="https://buymeacoffee.com/kylereddoch"><img src="https://img.shields.io/badge/Buy%20me%20a%20coffee-FFDD00?style=for-the-badge&logo=buymeacoffee&logoColor=000000" alt="Buy me a coffee" height="20px"></a>

![RipFoundry for Windows existing-video analysis and live encoding progress](docs/images/ripfoundry-windows.png)

RipFoundry for Windows is the Windows-native GUI companion to [RipFoundry for Linux](https://github.com/kylereddoch/RipFoundry-for-Linux), the original project this version grew from. It rips DVDs and creates Jellyfin-ready movie versions while keeping the ripping, encoding, validation, and staging work on the Windows PC. Only completed and verified files are finalized in the configured media library.

Version 1.3.0 adds guided DVD-extras handling, consistent MakeMKV title selection, cancellable DVD scanning, a modern Windows folder picker, clearer tool failures, and optional project-support links.

## Download and run

RipFoundry is distributed as a portable Windows application.

1. Download the latest `RipFoundry-Windows-*-portable.zip` from [Releases](https://github.com/kylereddoch/RipFoundry-for-Windows/releases).
2. Extract the entire ZIP to a permanent folder.
3. Keep `RipFoundry.exe` and the `_internal` folder together.
4. Double-click **RipFoundry.exe**.
5. Open **Settings**, confirm the required tools, and choose the media library and local staging folders.

The portable EXE includes the Python runtime and opens without a console window. Users do not need to install Python or run the source-code batch launcher.

To create a desktop shortcut, keep `Install-Shortcut.ps1` beside `RipFoundry.exe`, right-click the script, and choose **Run with PowerShell**. The shortcut launches the EXE directly.

Windows may show a first-run reputation warning for an unsigned community application. Confirm that the ZIP came from the official RipFoundry repository before running it.

## Windows requirements

- 64-bit Windows 10 or Windows 11
- [MakeMKV](https://www.makemkv.com/download/) for scanning and ripping DVDs
- [FFmpeg and FFprobe](https://ffmpeg.org/download.html#build-windows) for 1080p creation, media inspection, and validation
- [HandBrakeCLI](https://handbrake.fr/downloads2.php) when creating an Enhanced output
- Enough local free space for staging the source and selected outputs

RipFoundry checks `PATH` and common Windows installation locations. The **Settings** tab reports **Ready** or **Not found** for each tool and provides controls to locate the executable manually.

For MakeMKV, select `makemkvcon64.exe` or `makemkvcon.exe`, not the regular `MakeMKV.exe` desktop interface. FFmpeg and FFprobe normally live together in the same extracted `bin` folder.

## What RipFoundry does

- Scans MakeMKV DVD titles and supports single-title or multi-title selection.
- Uses TMDb-assisted metadata with a manual fallback.
- Creates Jellyfin folders using `Movie Name (Year) [tmdbid-ID]` naming.
- Rips bonus features into a lossless review area so they can be played, named, and categorized before being added to Jellyfin extras folders.
- Retains the untouched MakeMKV remux when ripping a DVD.
- Optionally creates a playback-friendly H.264 Enhanced version at the source resolution.
- Optionally creates a separate H.264 1080p version with aspect-ratio-preserving scaling.
- Preserves audio, subtitles, metadata, and chapters where the selected encoder supports them.
- Validates codec, resolution, duration, audio tracks, and subtitle tracks with FFprobe.
- Copies completed files through a temporary `.partial` destination.
- Verifies SHA-256 checksums before finalizing a media-library file.
- Processes existing MKV, MP4, and M4V sources without changing the original.

## First-time setup

Open **Settings** after launching `RipFoundry.exe`.

### Media Library Destination

Choose the folder where completed movies should be finalized. Supported examples include:

```text
\\server\share\Movies
M:\Movies
D:\Media\Movies
```

Click **Test Destination** to confirm that Windows can reach the folder and create a temporary file. A UNC path is generally more reliable than a mapped drive because it does not depend on a drive-letter mapping.

### Local staging

Rips and encodes are created locally before they are validated and copied. The default is:

```text
%USERPROFILE%\Videos\RipFoundry Staging
```

Choose a drive with enough free space for the source plus every output selected for the job.

### DVD drive

RipFoundry lists detected Windows DVD/CD drives by drive letter and maps them to MakeMKV sources. For example:

```text
D: - DVD/CD drive - MakeMKV disc:0
```

Click **Refresh Drives** after connecting a USB optical drive. Advanced users can enter a `disc:N` source directly.

### TMDb

Paste a TMDb **API Read Access Token** into Settings. RipFoundry stores its configuration in:

```text
%APPDATA%\RipFoundry\config.json
```

## Rip a DVD

1. Insert the DVD and click **Scan DVD**.
2. Select the main title. Ctrl-click supports discs containing multiple titles.
3. Choose one processing mode:
   - **Original DVD + Enhanced DVD** keeps the untouched remux and creates a native-resolution H.264 copy with HandBrakeCLI.
   - **Original DVD + 1080p** keeps the untouched remux and creates a separate 1080p H.264 copy with FFmpeg.
   - **Original DVD only** keeps only the untouched MakeMKV remux.
4. Click **Rip Selected as Movie(s)**.
5. Match each selected title to TMDb and confirm the plan.

Completed files use Jellyfin multi-version naming such as:

```text
Movie Name (Year) [tmdbid-123]\
    Movie Name (Year) [tmdbid-123] - 480p.mkv
    Movie Name (Year) [tmdbid-123] - 480p Enhanced.mkv
    Movie Name (Year) [tmdbid-123] - 1080p.mkv
```

### Rip DVD extras and featurettes

DVD title records normally contain runtimes, sizes, and chapter counts but not the human names shown on the disc menu. Bonus features inherit the parent movie's TMDb identity; they do not get separate TMDb matches.

1. Scan the DVD and select one or more bonus-feature titles.
2. Click **Rip Selected as Extras**.
3. Match the parent movie once in TMDb.
4. Confirm the selected title numbers. RipFoundry losslessly remuxes them into a local review folder. The DVD stays inserted so you can rip the main movie afterward without rescanning.
5. Click **Play** beside each staged title, enter its descriptive name, and choose a Jellyfin folder such as `featurettes`, `behind the scenes`, `deleted scenes`, `interviews`, or `trailers`.
6. Review the exact destination paths and click **Add Extras to Library**.

Each extra is copied with a temporary `.partial` filename and SHA-256 verified before its staged source is removed. Cancelling the review keeps the staged MKVs and makes no library changes.

```text
Movie Name (Year) [tmdbid-123]\
    Movie Name (Year) [tmdbid-123] - 480p.mkv
    featurettes\
        Making the Movie.mkv
    interviews\
        Interview with the Director.mkv
    trailers\
        Theatrical Trailer.mkv
```

## Process an existing video

Analysis and processing are deliberately separate decisions.

1. Open **Process Existing Video**.
2. Search the configured library or click **Choose Video...**.
3. Select an MKV, MP4, or M4V file.
4. Click **Analyze Selected Video**. Analysis reads media details but does not encode, copy, rename, or otherwise change the source.
5. Review the source details and RipFoundry's recommendation. The analysis panel has its own scrollbar when the complete reason is longer than the visible area.
6. Choose Enhanced, 1080p, both outputs, or stop without processing.
7. Click **Process Video**, review the exact filenames, and confirm the job.

The analyzer considers the container, video codec, resolution, display aspect ratio, field order, audio tracks, and subtitle tracks. Its conservative recommendations are:

- Interlaced or non-H.264 SD: Enhanced + 1080p
- Progressive H.264 SD or 720p: 1080p
- Progressive H.264 near 1080p: no additional encode
- Interlaced or non-H.264 near 1080p: Enhanced native-resolution
- Above 1080p: a separate 1080p compatibility version

When both outputs are selected, each output is encoded directly from the unchanged original. RipFoundry never creates the 1080p version from the Enhanced version.

MP4 and M4V text subtitles are converted to SubRip for MKV compatibility. Unsupported subtitle conversion fails safely instead of silently dropping the track.

## Activity, progress, and cancellation

The **Activity** area remains visible at the normal window size and reports:

- The current processing phase
- Live FFmpeg or HandBrake percentage
- Copy progress
- SHA-256 verification progress
- Encoder and validation messages in a vertically scrollable log

RipFoundry permits one background job at a time. Analysis, mode selection, and processing controls are disabled while a job is active, preventing the same source from being submitted twice.

Click **Cancel Active Job** to stop the current encoder or copy operation. RipFoundry asks for confirmation, stops the active process, prevents FFmpeg from starting a fallback retry, removes that job's partial staging and destination files, and leaves the original source unchanged.

## Encoding settings

- **CRF** controls H.264 quality. The default `18` produces high quality and larger files; higher values reduce size and quality.
- **1080p x264 preset** controls speed versus compression efficiency. `slow` is the recommended balance.
- **Local staging** controls where temporary working files are created.

A DVD-to-1080p encode is conventional scaling. It can improve compatibility but cannot recover HD detail that was not present on the DVD. The untouched MakeMKV remux remains the archival version.

## Safety behavior

RipFoundry does not place unfinished encoder output directly in the media library. It validates local output first, copies to a `.partial` filename, compares SHA-256 checksums, and only then assigns the final filename.

- A normal processing failure retains local staging files for troubleshooting.
- An intentional cancellation removes the cancelled job's partial staging and destination files.
- Existing completed outputs are never overwritten.
- The original MKV, MP4, or M4V remains unchanged.

## Build the EXE from source

This section is for contributors and users who want to build RipFoundry themselves. Building requires Python 3.10 or newer.

From the repository folder, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\Build-EXE.ps1
```

The script installs or updates PyInstaller and creates:

```text
dist\RipFoundry\RipFoundry.exe
```

Run the built executable from inside `dist\RipFoundry`; keep its `_internal` folder beside it. `Launch-RipFoundry.bat` remains available only as a source-development launcher.

## Changelog and roadmap

See [CHANGELOG.md](CHANGELOG.md) for release details.

A traditional signed Windows installer remains a possible future improvement. The current 1.3.0 release is portable: extract the complete folder and run `RipFoundry.exe`.

## Support RipFoundry

If RipFoundry has been useful and you want to support future updates, you can contribute through any of these optional links:

<a href="https://github.com/sponsors/kylereddoch"><img src="https://img.shields.io/badge/GitHub%20Sponsors-30363D?style=for-the-badge&logo=GitHub-Sponsors&logoColor=white" alt="GitHub Sponsors" height="24px"></a>
<a href="https://ko-fi.com/kylereddoch"><img src="https://img.shields.io/badge/Ko--fi-F16061?style=for-the-badge&logo=ko-fi&logoColor=white" alt="Ko-fi" height="24px"></a>
<a href="https://buymeacoffee.com/kylereddoch"><img src="https://img.shields.io/badge/Buy%20me%20a%20coffee-FFDD00?style=for-the-badge&logo=buymeacoffee&logoColor=000000" alt="Buy me a coffee" height="24px"></a>

## Project origins

RipFoundry for Windows grew from [RipFoundry for Linux](https://github.com/kylereddoch/RipFoundry-for-Linux). The Windows edition keeps the original project's Jellyfin-focused ripping, naming, validation, and verified-transfer ideas while providing a native Windows interface and Windows-local encoding workflow.

## Project attribution

RipFoundry is built by **Kyle Reddoch (CybersecKyle)**.

Website: **https://www.kylereddoch.me**

Repository: **https://github.com/kylereddoch/RipFoundry-for-Windows**

The application's **About** tab includes this attribution, the RipFoundry mark, the current version, and a link to the repository.
