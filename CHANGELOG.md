# Changelog

All notable changes to RipFoundry for Windows are documented here.

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
