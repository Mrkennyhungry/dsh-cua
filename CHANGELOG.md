# Changelog

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
