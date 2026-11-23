# 0002. 对轻量 runtime 使用协议 2 握手

- 状态为已采纳。
- 背景为旧协议 1 的 health 无法证明启动路径已经脱离内置 Agent，也缺少 runtime 能力和 epoch 标识。
- 决定为新 Host 要求协议 2 和 runtime、windows、capture、uia、web、lazy_vision 能力，拒绝缺失能力的驻留进程并提供处理活跃操作后的重启指引。
- 后果为旧 kernel 需要完成或停止进行中的操作后关闭，再由插件启动匹配的 runtime。协议变化随行为版本 0.4.15 记录，六工具仍在后续 0.5.0 阶段切换。
