# Pixel whale-maid cursor

> Historical design, now retired. Current behavior is physical input plus a docked
> task commander. See [commander-mode.md](commander-mode.md).

The default cursor uses the reference-image whale maid at 48×48 physical pixels.
Three palettes are bundled: `ocean`, `lavender`, and `mint`; scale 2 gives a 96px
accessibility option. The tiny
white corner at the sprite's top-left is the exact action hotspot. The character
sits to its bottom-right to keep the actual target visible.

| State | Visual |
|---|---|
| idle | open eyes / blink |
| thinking | four-frame chin-rest / thought-bubble animation |
| move / hover | swimming streaks |
| click / right click / UIA invoke | raised hand then slap, hotspot impact |
| type / paste | small keyboard with alternating hands |
| key / shortcut | keyboard pose |
| scroll | up/down indicator |
| drag | reaching hand, follows the drag path |
| observe / wait | magnifying glass |
| error / refusal | worried face and exclamation |

Every decorated desktop, UIA and DOM tool uses the same feedback layer. Known
coordinates and UIA rectangles take priority; typing uses the native caret when
available and otherwise retains the last target. DOM targets use locator bounding
boxes and browser geometry (native toolbar size, screen position, device scale).
DOM geometry is best effort in unusual browser/DPI configurations. Observation
hides the overlay while collecting screenshots so it cannot become an OCR target.
The glow breathes every 2 seconds and disappears after 2 idle seconds.

## Configure a style

`doraemon_cursor` accepts `theme="whale-maid"`, `palette="ocean"`, `scale=1`, and
`action="idle"` along with its existing coordinates/visibility. Configuration
persists in the running kernel. `visible=false` hides it until the next operation.

Style plugins use `cursor_theme.register_theme(name, renderer)`. A renderer accepts
`(action, frame, palette)` and returns a NumPy uint8 BGRA array with straight alpha.
Use a stable canvas size and hotspot `(0,0)` across frames. Registration is an
in-process Python extension point; no downloaded code is executed automatically.
The default uses imagegen sprites based on the user's reference. The older
code-drawn style remains available as `theme="classic"`. Character direction
was inspired by the blue hair, fin ears, maid headdress/apron and tail described in
[whale-maid-lora](https://github.com/Sumireaks/whale-maid-lora).

Render the contact sheet with:

```powershell
.runtime/venv/Scripts/python.exe scripts/preview-cursor.py
```

## Thinking and loading this checkout

The host plugin listens for DSH `step/start` / `turn/start` and `turn/end` events.
It enables thinking before model/network waiting and clears it when the turn ends.
The Python task runner also brackets its own task lifetime, so internal model
waiting is covered. Concrete tool animations take priority; after the brief action
animation they return to thinking while a task is active. The first thinking pose
appears near the work area's bottom-right; later poses retain the last target.
Owners are tracked separately so one completed task does not hide another task.
Screenshots hide the sprite only during pixel acquisition, not throughout OCR.

The local DSH `node_modules/dsh-doraemon` junction already points to this checkout.
Reload both the host plugin and Python kernel after updating:

1. Finish any active Doraemon task.
2. From this repository run `powershell -ExecutionPolicy Bypass -File scripts/reload-cursor-kernel.ps1`.
3. Restart DSH. The next Doraemon tool automatically starts the updated kernel.

If Windows refuses to stop an elevated kernel, run step 2 from an elevated terminal.
The helper authenticates to the recorded kernel and checks its live PID before
stopping it; it does not terminate unrelated Python processes.

## Virtual input and compatibility

`DSH_DESKTOP_INPUT_MODE=virtual` is the default. DOM and UIA keep their native
semantic routes. Coordinate clicking, right clicking, scrolling, movement and
same-window dragging use Win32 messages and never set the system cursor position.
A message being posted is reported as `effect="unconfirmed"`: inspect the UI
before assuming the action worked. Apps may ignore synthesized messages.
Chromium coordinate clicks and cross-window/OLE or Chromium drags return explicit
unsupported errors; use DOM, UIA or the application's file API for those tasks.
There is no silent physical fallback.

Set `DSH_DESKTOP_INPUT_MODE=physical` in the kernel environment and restart it to
opt into native pointer input for incompatible apps. This mode moves the real
pointer during the operation, then restores its previous position for clicks,
right clicks, scrolls and drags. A cosmetic overlay alone cannot replace native
OS input for apps that require real mouse events. Keyboard input retains the
existing focus checks.

### Direct diagnostics

Run `.runtime/venv/Scripts/python.exe scripts/verify-native-cursor.py --bridge`
to exercise owned native windows against the actual kernel without a model or
DSH client. `docs/cursor-http-results.json` contains the observed results. The
older `verify-virtual-input.py` is a Tk compatibility diagnostic; Tk mouse-message
input is unsupported, so that script is not an acceptance pass.

Native Edit/RichEdit virtual typing and basic keys are sent to the bound control,
without global keyboard injection. Other custom input controls should use UIA/DOM.
Observations report `mouse_position.source="virtual"` and the sprite's coordinates
in virtual mode. Health reports the loaded `input_revision`, `input_mode`, source
path and cosmetic cursor target/error so stale kernels can be distinguished.
