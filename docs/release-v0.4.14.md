# 0.4.14

两件事：合并社区 PR #1（安装脚本的 PowerShell 5.1 兼容修复），以及给这个修复
补上并发与异常路径的清理。

## PR #1：`-c` 里的引号被 PS 5.1 吃掉

`scripts/bootstrap.ps1` 原本用一行 `-c` 做 import 探针：

```powershell
& $python -c 'import dsh_desktop.dsh_bridge_cli; print("kernel source import: ok")'
```

PowerShell 5.1 在把参数交给原生进程时会剥掉内嵌双引号，python 实际收到的是

```
import dsh_desktop.dsh_bridge_cli; print(kernel source import: ok)
```

——直接 `SyntaxError: '(' was never closed`，安装脚本在这一步失败。

在 Windows PowerShell 5.1 上实测复现：

```
=== OLD form: -c with embedded double quotes ===
  File "<string>", line 1
    import sys; print(probe
                     ^
SyntaxError: '(' was never closed
exit=1
=== NEW form: temp file ===
probe ok
exit=0
```

PR 改为把探针写进临时文件再执行，方向正确，已合并。

## 合并后的加固

固定文件名 `dsh_kernel_import_probe.py` 有两个小问题：并发安装会共用同一个探针
文件（谁先结束谁删），以及中途抛错时可能留下残file。改为：

- 文件名带 GUID，每次 run 唯一；
- 探针放进 `try/finally`，退出码检查在 `try` 内，`finally` 保证清理。

在 Windows PowerShell 5.1 + 真实 kernel 上验证：输出
`kernel source import: ok`，`probe file left behind: False`。

## 同时包含

0.4.13 的客户端修复（输入框里的图片/文件预览消失）也在这条线上，安装 0.4.14
即一并得到。

## 升级后

`bootstrap.ps1` 的改动只影响新安装/重装内核的机器；已装好的环境无需重跑。
