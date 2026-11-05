# 0.4.10

三处改动，两个来自真机验收的反馈，一个是顺手修的可观测性问题。

## 1. 鲸鱼娘不再一秒眨四次眼

`cursor_overlay` 的动画帧原来是 `int(monotonic() * 4) % 4`，而精灵是成对的
`(睁眼, 闭眼)`：`thinking`/`idle` 用 `(2,3)`，实际效果是每秒换 4 次眼，
看着像在抽搐。改成每 `BLINK_PERIOD`（4s）眨一次、闭眼保持 `BLINK_DURATION`
（0.14s）。单帧动作（`scroll`/`drag`/`observe`/`done`）不受影响。

顺带澄清一个误会：`capture_guard()` 是唯一会在截图时隐藏精灵的机制，它当时
**没有任何调用点**，`_capture_depth` 恒为 0。所以 watch 采样不会让精灵闪烁。

## 2. watch 补上桌面抓取的盲区

GDI 桌面截图拍不到 WinUI / DirectComposition 应用的合成层内容。Win11 记事本
实测：`capture_desktop` 里编辑区全白，状态栏却写着"41 个字符"；同一时刻
PrintWindow 拍到的窗口内容完整。而 `_grab()` 默认优先桌面抓取（为了不漏掉
屏幕右下角的系统通知），于是这类窗口的内容永远进不了 OCR，反而把 OCR 名额
让给了 DSH 自己的重绘。

现在第一次采样时做一次几何判定，三条件同时成立才判"盲"：

- 桌面里窗口那块区域的方差接近 0（纯色）；
- PrintWindow 拍到的同一块有明显内容；
- 两者灰度平均差 > `BLIND_DIFF`。

判盲之后，每个采样帧额外抓一次窗口，把窗口内的变化矩形并进 `rois`——toast
在窗口内也读得到，桌面级通知照旧走原通道。非盲会话只在探测帧上付这一次
PrintWindow（每 100 帧复核一次），不会把每帧成本翻倍。判定结果通过
`since()` / `status` 的 `capture_blind` 暴露，方便排查。

已知限制：判定用 `GetWindowRect` 的屏幕坐标去裁桌面帧。多显示器且副屏在左侧
时虚拟桌面原点为负，裁切会错位；错位会让灰度差变大，但方差比通常过不了，
结果是"不判盲"（退化为旧行为），不会误判成盲。

## 3. watch 不再无限期采样

`start` 之后没人 `stop` 是最常见的浪费：10fps 一直采下去，CPU 和磁盘产物都在
涨。新增 `ttl`（默认 120s，5-600）：

- 到点**只停采样线程**，环形缓冲保留，`since()` 照样读得到已经采到的那段；
- `since()` / `status` 返回 `expired: true`，调用方能区分"没开始"和"采样已到期"。

同时把使用边界写进了工具描述和 agent 提示词：默认不开 watch，常规观察用
`desktop_analyze` / `desktop_capture` / `desktop_uia_tree`，等界面稳定用
`desktop_wait_stable`；只有"某一步预期会冒出一条瞬时提示，且这条提示本身
就是下一步的判断依据"时才开，当步内 `start` → `since` → `stop`。

## 验证

- `pytest test`：261 passed, 1 skipped（新增 `test_watch_blind_capture.py` 6 条）
- `node --test test/*.test.js`：14 pass / 1 fail，失败的是既有的
  `plugin.test.js`（缺 `@deepseek-ai/schemastery` peer 依赖），干净工作区复跑同样失败
- 未做的：盲判定与补通道只在合成帧上验证过，真机（Win11 记事本 / 微信输入框等
  WinUI 目标）仍需公司环境实测；`ttl` 到点后的 `since` 也需要真机确认一次。

## 说明

上一版 0.4.9 的分发物与 tag 已对齐；从本版起版本号在 tag 上打齐，`version.py`
与 `package.json` 同步。
