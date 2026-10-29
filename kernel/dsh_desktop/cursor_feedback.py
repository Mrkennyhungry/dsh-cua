"""Only change the docked commander pose; never move it to input coordinates."""
_ACTIONS = {'right_click':'right_click','click':'click','check':'click','select':'click',
            'invoke':'click','batch':'click','type':'type','paste':'type','press':'key',
            'key':'key','scroll':'scroll','swipe':'drag','drag':'drag','hover':'move',
            'move':'move','analyze':'observe','snapshot':'observe','capture':'observe',
            'read':'observe','tree':'observe','wait':'thinking','table':'observe'}

def classify(name):
    if name in ('desktop_cursor','desktop_highlight'): return None
    return next((value for key,value in _ACTIONS.items() if key in name),None)

def feedback(name, args):
    action = classify(name)
    if action is None: return None
    from .cursor_overlay import peek_cursor_overlay
    overlay = peek_cursor_overlay()
    if overlay is not None: overlay.action(action, duration=float('inf'))
    return overlay
