# 0.4.12

修桌宠闪烁。这是 0.4.10 那次没查出来的那条——当时我搜错了目录，误判成"没有
调用点"。

## 根因

`CaptureService.capture_desktop()` 外面包着 `_cursor_guard()`，它转调
`overlay.capture_guard()`：

```python
ShowWindow(hwnd, SW_HIDE)      # 藏起来
time.sleep(FRAME_SECONDS * 2)  # 32ms，等合成器稳定
yield                          # 抓屏
# finally: 下一帧动画循环再把窗口显示出来
```

一次性截图（analyze / capture）这样没问题。但 watch 是 10fps，于是每秒把
桌宠藏十次再放出来——用户看到的就是"疯狂闪烁"。

0.4.11 里我判断"`capture_guard` 没有调用点"，是因为只搜了
`kernel/dsh_desktop/*.py`，没递归到 `game/` 子目录。

## 改法

- `capture_desktop(guard_cursor=True)`：watch 传 `False`，连拍时不隐藏；
  一次性截图保持原行为（一次隐藏看不见）。
- 不隐藏的代价是桌宠会进帧，所以 `_diff_rois` 新增 `ignore` 矩形，把桌宠
  当前屏幕矩形从差异 mask 里整块抹掉。它眨眼、换姿势不会变成 OCR 候选。
- `cursor_overlay.guard_hides` 计数，`/health` 暴露为 `cursor.guard_hides`，
  以后可以直接看"这轮到底藏了几次"。

## 真机验证

Steam 窗口，`ttl=60` 采样 8 秒（约 80 次抓屏）后读 `/health`：

| 动作 | guard_hides |
|---|---|
| watch 采样 8 秒 | **0**（修复前约 80） |
| 之后一次 `doraemon_analyze` | 1 |

`since` 同时确认采样正常：`sampled=169`、`frames_in_window=22`、
`ocr_limit=0` 返回 `runs=0`（80ms）。

## 仍未解决

watch 采整块桌面，DSH 自身窗口的重绘会占掉大量 OCR 候选（验收里
`text_candidates` 反复出现"工具调用·doraemon_watch"这类自身 UI 文字）。按
绑定窗口给 ROI 加权是下一步。
