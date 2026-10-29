# 物理输入与像素指挥员

当前方案使用真实鼠标执行坐标点击、右键、滚动和拖动，鼠标停留在动作结束点。
之前的虚拟鼠标已停用，`doraemon_cursor` 不再注册，旧 `/cursor` 操作返回停用提示。
`DSH_DESKTOP_INPUT_MODE=virtual` 不会重新开启旧输入方式。
DOM/UIA 仍按其语义接口执行；小人的姿势只用于反馈，不决定输入是否成功。

## 登场与姿势

- 仅调用 Doraemon 工具时登场；普通聊天和健康检查不触发。
- DSH 窗口可见时，从其下方输入区域附近出现，再移向该显示器右上角。
  起点按原生窗口边界估算，不读取具体聊天气泡的 DOM；窗口不可见时直接在角落登场。
- 角色为 112px，完整透明画布为 300×204px，包含中文气泡。
- 登场台词：终于轮到我玩电脑了！思考时托腮，点击和右键时挥杖，
  输入、滚动、拖动、观察有独立姿势与台词。
- 角色固定停靠，不跟随鼠标或目标坐标。呼吸灯周期和闲置保留均为 2 秒。

## 不干扰与清理

置顶窗口使用 layered / transparent / toolwindow / noactivate 样式，
命中测试返回透明，鼠标激活消息返回不激活。角色和气泡没有可选文本或交互控件。
窗口类列入装饰白名单，从窗口枚举和自动清理目标中排除。

支持的 Windows 上使用 `WDA_EXCLUDEFROMCAPTURE` 排除截图；截图入口另有短暂隐藏保护。
正常完成、取消及插件卸载清理任务所有权。远端状态每 8 秒续租、35 秒到期；
绑定的客户端进程退出会被检测清理。内部独立任务在其实际执行结束时清理。
多个并行任务共享同一角色，一个任务结束不会误隐藏仍在执行的另一个任务。
没有活动任务时，晚到的动作反馈不能重新显示角色。

## 加载

当前 DSH 安装目录的 junction 指向此源码仓库。Python 内核已重载为物理输入模式。
重启 DSH 一次，使新的工具注册和任务登场钩子生效。以后单独更新 Python 代码时，可执行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/reload-cursor-kernel.ps1
```

角色预览：`docs/images/commander-preview.png`。
内置 imagegen 生成立绘，`scripts/build-commander-sprites.py` 切帧，
`scripts/preview-commander.py` 生成含中文气泡的预览。

## 验证

```powershell
.runtime/venv/Scripts/python.exe scripts/verify-commander.py --bridge
```

该脚本创建自己的原生窗口，直连高权限 HTTP 内核，不调用模型或 DSH 客户端，
验证实际物理右键菜单、输入、拖动、点击穿透、焦点、截图排除及生命周期清理。
测试窗口会清理，剪贴板通过 OLE 保存并恢复。
结果保存到 `docs/commander-acceptance.json`。
普通执行进程在当前环境无法注入系统鼠标事件，因此真实输入验收通过实际高权限内核进行。

此前 `cursor-*-results.json`、虚拟主题文档和旧验证脚本是历史方案记录，不代表当前行为。
