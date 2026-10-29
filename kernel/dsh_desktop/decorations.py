"""Window classes owned by visual feedback, excluded from automation targets."""
PREFIXES = ('DshDoraemonCommander_', 'DesktopCursorOverlay_', 'DesktopWindowGlow')

def is_decoration_window(hwnd):
    import win32gui
    try: return win32gui.GetClassName(hwnd).startswith(PREFIXES)
    except Exception: return False
