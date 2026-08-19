# Changelog

All notable changes to RipFoundry for Windows are documented here.

## 1.3.1 - 2026-08-19

### Changed

- DVD extras are now stream-copied without re-encoding and validated by media properties instead of only by file checksum.

### Fixed

- Reviewed extra names are now written into each MKV's embedded title so Jellyfin does not replace every extra name with the DVD's shared disc title.

## 1.3.0 - 2026-08-19

### Added

- A guided DVD-extras workflow that losslessly stages selected titles for preview, naming, and placement in supported Jellyfin extras folders.
- Parent-movie TMDb matching for bonus features without requiring a separate match for every extra.
- All-or-nothing, SHA-256-verified publishing for a batch of DVD extras.
- A modern Windows Explorer folder picker for media-library and staging locations, with a portable fallback.
- GitHub Sponsors, Ko-fi, and Buy Me a Coffee support links and repository funding metadata.

### Changed

- DVD movie and extras actions now have separate, clearly labeled controls.
- DVD scans now run through the standard job lifecycle with visible status, single-job protection, and cancellation support.
- Command failures include recent tool output to make MakeMKV and encoder errors easier to diagnose.

### Fixed

- MakeMKV now receives the configured minimum-title-length filter during both scanning and ripping so title numbers remain consistent.
- Cancelling a captured MakeMKV scan now stops the active process cleanly.
- A cancelled or failed multi-extra publish no longer leaves a partially completed batch in the media library.
## 1.2.0 - 2026-08-13

### Added

- A two-step **Analyze Selected Video** and **Process Video** workflow for existing MKV, MP4, and M4V files.
- Source inspection and conservative recommendations for Enhanced, 1080p, both outputs, or no additional encode.
- Independent Enhanced and 1080p creation from the unchanged original source when both are selected.
- Live phase and percentage reporting for FFmpeg, HandBrake, file copying, and checksum verification.
- A fixed Activity progress area with a vertically scrollable encoder log.
- A confirmation-backed **Cancel Active Job** control that stops the active encoder or copy operation.
- Single-job protection that disables analysis and processing controls while work is active.
- Portable Windows executable instructions for running RipFoundry without Python or a console window.

### Changed

- Existing-video processing now supports MP4 and M4V sources in addition to MKV.
- MP4 and M4V text subtitles are converted to SubRip when producing MKV output.
- FFmpeg receives `-nostdin`, machine-readable progress output, and clearer fallback handling.
- A user cancellation no longer triggers FFmpeg's deinterlacing fallback retry.
- Cancelled jobs remove their partial staging output and partial destination copy while preserving the original video.
- The desktop-shortcut helper now targets `RipFoundry.exe` instead of the Python batch launcher.

### Fixed

- The Activity progress bar could fall below the visible window unless RipFoundry was maximized.
- Long analysis details and recommendation reasons could extend beyond the visible analysis panel.
- The same source could be submitted more than once while a previous job was still active.
- Failed FFmpeg work could be mistaken for an intentional cancellation and retried unexpectedly.

## 1.1.0 - 2026-08-11

### Added

- Initial public Windows GUI release.
- Local MakeMKV DVD ripping with TMDb-assisted Jellyfin naming.
- Optional Enhanced DVD and 1080p outputs.
- FFprobe validation and SHA-256-verified media-library transfers.
- Settings, optical-drive selection, application artwork, and portable EXE build support.
