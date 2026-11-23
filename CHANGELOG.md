# Changelog

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
