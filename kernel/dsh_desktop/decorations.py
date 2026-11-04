"""Window classes owned by visual feedback, excluded from automation targets."""

# Base class names, without the trailing separator. A window is registered
# either under the bare name (0.4.5's "DesktopCursorOverlay") or with an
# instance suffix ("DshDoraemonCommander_<pid>_<id>"). Keeping base names
# here means both forms match; the old tuple had "DesktopCursorOverlay_"
# which never matched the bare name and was a dead entry.
PREFIXES = ('DshDoraemonCommander', 'DesktopCursorOverlay', 'DesktopWindowGlow')


def is_decoration_window(hwnd):
    import win32gui
    try:
        cls = win32gui.GetClassName(hwnd)
    except Exception:
        return False
    return any(cls == base or cls.startswith(base + '_') for base in PREFIXES)
