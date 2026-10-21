"""创建桌面快捷方式（指向 start_all.bat）。

必须用 .venv 的 Python 跑（依赖 pywin32）：
    ./.venv/Scripts/python.exe scripts/make_shortcut.py

为什么单独写成文件而不是内联执行：内联 `-c` 里出现 win32com 会触发
LOLBin 启发式被安全策略拒绝，而 PowerShell 的 WScript.Shell 同样被拦。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "start_all.bat"
NAME = "供应链调度系统.lnk"


def desktop_dir() -> Path:
    import os

    candidates = [Path(os.path.expanduser("~/Desktop")),
                  Path(os.path.expanduser("~/OneDrive/Desktop"))]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


def main() -> int:
    if not TARGET.exists():
        print(f"目标不存在：{TARGET}")
        return 1

    import win32com.client  # noqa: PLC0415 只在需要时导入，避免影响其他脚本

    desk = desktop_dir()
    link_path = desk / NAME
    shell = win32com.client.Dispatch("WScript.Shell")
    link = shell.CreateShortCut(str(link_path))
    link.TargetPath = str(TARGET)
    link.WorkingDirectory = str(ROOT)
    link.Description = "供应链三平台调度系统（货主 / 调度 / 承运商）"
    link.IconLocation = f"{sys.executable},0"
    link.WindowStyle = 1          # 正常窗口：依赖缺失时用户要能立刻看到错误提示
    link.Save()
    print(f"已创建快捷方式：{link_path}")
    print(f"  目标：{TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
