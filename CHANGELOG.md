# Changelog

## [0.5.2] - 2026-11-25

### Changed

- Load Host configuration independently and keep the default bridge focused on unified operations and optional companion state.
- Share browser launch settings and DOM collection, with persistent context ownership in BrowserRuntime and the existing single browser worker.
- Publish an explicit runtime file whitelist and provision OCR/YOLO dependencies separately with bootstrap.ps1 -Vision.
- Accept the installed Cordis 4.1 SDK within the optional 4.x peer range.

### Removed

- Move legacy Host registrations to standalone/legacy-host.js and historical HTTP routes to source-only standalone_bridge.py.
- Exclude the autonomous Agent, workbench, IM/WeCom, cron, provider-driven decisions, caption pipeline, static workbench assets and model weights from the default release. Original data directories and recovery steps are documented in docs/MIGRATION-0.5.md.
- Remove standalone-only Python requirements and the duplicate psutil requirement from the base installation.

## [0.5.1] - 2026-11-25

### Fixed

- Prefer the bootstrap Python runtime during automatic startup so a checkout test interpreter cannot silently reduce desktop input and state-directory access. Explicit pythonPath retains precedence.
- Return queryable operations for slow observations and retain truthful worker state through cancellation.
- Recover lost operation responses by querying the durable request identity before returning their result.

## [0.5.0] - 2026-11-24

### Added

- Add six unified desktop tools with operation tracking, initial and post-action observations, and DSH image attachments.
- Capture action-triggered transient text through a pre-armed DOM observer or bounded UIA sampling.

### Changed

- Require the unified runtime capability when adopting a kernel and report its loaded build and version in runtime status.
- Move legacy Host tool registrations behind a separate migration module while the default entry registers six tools.

### Fixed

- Track open operations on their backend thread so cancellation keeps in-flight navigation and input locks observable.
- Admit two UIA calls consistently during apartment startup while preserving bounded backpressure.
- Return bounded argument validation details and document the required input mode and top-level wait timeout.
- Map unified observations, waits and action steps to the companion's existing feedback poses.

## [0.4.19] - 2026-11-24

### Added

- Expose unified observations through an owner-scoped kernel bridge endpoint.

## [0.4.18] - 2026-11-23

### Added

- Unified action validation, semantic and frame-point dispatch, short batches and post-action observations through DesktopRuntime.act.
- Durable SQLite operation identities, write-ahead dispatch records, completed and uncertain steps, cancellation and bounded status following.
- Shared text, element, window and target-state condition checks with physical input released during expectations.

### Changed

- Physical runtime movement uses a direct deterministic path. Input, selection, checkbox state, double/right click, hover, wheel, drag, file upload, key and close requests share reference and identity checks.
- Target locks precede the global input lock. Backend threads retain locks during timed-out provider calls and cancel their input lease before further dispatch.
- Browser dialogs stay available for explicit Enter/Escape handling. New dialogs, navigation, tabs and native modal windows interrupt remaining batch steps.

### Fixed

- Mouse clicks, wheel detents, clipboard paste and drag segments recheck cancellation at their actual input boundary.
- Foreground-only changes preserve geometrically valid image frames. Browser closure during live validation produces a closed-target result.
- Queued cancellations release operation capacity immediately. Runtime restart preserves request identities independently of full observation results.

## [0.4.17] - 2026-11-23

### Added

- Unified semantic observations with document-scoped DOM identities, full internal locators, UIA wrapper identities, query and source-resolution image regions.
- Stable snapshot pagination, bounded owner-scoped image resources, frame/reference expiry and backend call/timing traces.
- A single lazy COM apartment worker with one queued request and bounded shutdown for unresponsive providers.

### Changed

- DOM collection preserves field types, values, checked/selected state, options, overlay ordering and nested-frame provenance.
- Local OCR and YOLO initialize independently on demand. Semantic observations and screenshots keep caption network requests at zero.
- Stable semantic layouts return lightweight observations. Image bounds are emitted with the corresponding image frame.

### Fixed

- OCR fallback from the legacy element table reuses its completed UIA scan.
- Browser capture preserves input caret styles, keeping screenshot-side DOM mutations out of target revisions.

## [0.4.16] - 2026-11-23

### Added

- Owner-scoped desktop, window, tab and frame identities with live geometry revisions. A window-lifetime property detects same-process HWND reuse.
- Dedicated persistent browser targets on the existing browser worker, explicit CDP connection, stable tab identity across connections, and call-identity replay protection.
- Bounded frame records with source, client, window, crop, scale, DPI and virtual-origin metadata and stale-coordinate rejection.

### Changed

- New browser profiles live under stateDir/browser. Existing home/web_profile_edge and home/web_profile directories are reused, with process occupancy checks and a cross-process profile lease.
- Whole-window capture and client fallback use the shared CaptureService implementation and report their actual coordinate basis.
- Session cleanup releases its target and frame records. Shared browser tabs transfer to a surviving owner, while browser shutdown retains persistent site data.

## [0.4.15] - 2026-11-24

### Added

- Shared DesktopRuntime with lazy visual parser, runtime-only configuration and health build, epoch, capabilities and initialized backend metadata.
- Protocol 2 handshake with capability validation and cancellable startup probes.

### Changed

- DSH starts desktop services directly. UIA, capture and browser DOM operations run with empty provider configuration and optional Agent and vision packages absent.
- Standalone AppController inherits shared desktop services. Delegated task routes are installed through the explicit legacy bridge adapter.
- Companion consumes runtime services and a cancellation callback. Shutdown releases picker, watch, overlay and browser resources.

### Fixed

- Selection polling and plugin unload preserve idle kernel state through bounded background requests.
- Desktop tool imports establish physical-pixel DPI awareness before PyAutoGUI initialization.
- Companion response reader threads retain their own process handles through shutdown.

### Removed

- Delegated Agent execution from the default DSH kernel startup. Historical `/tasks` entry points return an explicit optional-adapter diagnostic. The implementation remains in `kernel/dsh_desktop/legacy_bridge.py`, enabled with `create_router(runtime, legacy=eternity)`. Existing config, session and knowledge data remain available under the configured kernel home for the standalone adapter.
