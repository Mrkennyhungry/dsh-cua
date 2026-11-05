# 0.4.11

真机验收后的修正。0.4.10 的抓取盲区补通道有个基准错误，本版修掉；同时记录
验收结果，避免把没复现的问题当成已解决。

## 修了什么

`_grab_window()` 原来调 `CaptureService.capture(hwnd)`，而这个方法**默认走 mss
屏幕抓取**，只有 mss 抛错才回落 PrintWindow。拿一张同样是盲的图去和桌面盲图
比对，盲判定永远不会成立。现在强制走整窗 PrintWindow
（`desktop_tools._print_window_frame`）。

第二个错是基准不一致：原来用 `GetWindowRect` 裁桌面，却拿**整窗**帧当对照，
标题栏那几十像素被算进"内容差异"。现在两边都用 `ClientToScreen` 裁到客户区，
逐像素可比。

## 真机验收结果（Windows 11 / 2560x1440）

- **Steam（CEF）**：`capture_blind=false` 正确；`ttl=20` 到点后 `active=false`
  且 `expired=true`，`buffered` 帧仍可读；`ocr_limit=0` 返回 `runs=0` 并给出
  "本次不跑 OCR" 警告，耗时 113ms（同窗口带 OCR 是 7185ms）；
  帧数超限时 `frames_warning` 正常提示"窗口内 24 帧，时间线只展示 8 帧"。
- **Win11 记事本**：桌面裁块与 PrintWindow 的灰度平均差只有 **0.2**，方差也
  接近——也就是说这台机器上 mss 桌面抓取**并不盲**，记事本内容一直在桌面帧里。
  0.4.9 验收时"记事本编辑区全白"的结论无法复现，按现在的实测为准。
- **全屏 shell 窗口**：反过来是 PrintWindow 返回近黑（均值 5.7）、桌面有内容。
  此时 `var(printwindow) < var(desktop)`，方差护栏拒绝切换，不会把黑图当内容。

## 结论

盲通道在当前机器上不会触发（没有可复现的盲目标），它作为安全网保留：每 100
个采样帧付一次 PrintWindow，不触发时零成本。**不要**把它当作已解决的 WinUI
问题——真遇到盲目标时，行为仍需在目标机上确认。

## 附带发现（未修）

watch 采的是整块桌面，所以 DSH 自身窗口的重绘会占掉大量 OCR 候选。验收里
`text_candidates` 出现大量 "工具调用·doraemon_windows·bind" 这类自身 UI 文字，
而目标窗口的 toast 会被稀释。按窗口绑定把 ROI 优先级提到绑定窗口内，是下一步
值得做的改动。
