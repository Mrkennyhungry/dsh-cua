"""watch 磁盘产物的生命周期：必须 bounded，不能无限累积。

每次 since() 会写 since_*.jpeg 与 band_*.jpeg 两张带时间戳的图，
不清理就是每天几十上百个文件。这里锁定"只清自己的产物、只保留最近 N 个"。

跑法：
    PYTHONPATH=kernel ./.runtime/venv/Scripts/python.exe -m pytest test/test_watch_artifacts.py -q
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from dsh_desktop import watch


def _make_files(directory: Path, prefix: str, count: int) -> list[Path]:
    made = []
    for i in range(count):
        f = directory / f"{prefix}20261105_0000{i:02d}_000000.jpeg"
        f.write_bytes(b"x" * 100)
        # 让 mtime 与序号一致，便于断言"留下的是最新的"
        stamp = time.time() - (count - i) * 10
        os.utime(f, (stamp, stamp))
        made.append(f)
    return made


def test_prunes_to_keep_newest(tmp_path: Path):
    made = _make_files(tmp_path, "since_", 30)
    watch._prune_artifacts(tmp_path, keep=12)
    left = sorted(tmp_path.glob("since_*.jpeg"))
    assert len(left) == 12
    # 最新的那张必须在
    assert made[-1] in left
    # 最旧的那张必须已删
    assert not made[0].exists()


def test_other_files_are_never_touched(tmp_path: Path):
    _make_files(tmp_path, "since_", 20)
    unrelated = tmp_path / "important-user-file.jpeg"
    unrelated.write_bytes(b"keep me")
    other_prefix = tmp_path / "capture_20261105.jpeg"
    other_prefix.write_bytes(b"keep me too")
    watch._prune_artifacts(tmp_path, keep=5)
    assert unrelated.exists() and other_prefix.exists()


def test_band_and_since_pruned_independently(tmp_path: Path):
    _make_files(tmp_path, "since_", 20)
    _make_files(tmp_path, "band_", 20)
    watch._prune_artifacts(tmp_path, keep=6)
    assert len(list(tmp_path.glob("since_*.jpeg"))) == 6
    assert len(list(tmp_path.glob("band_*.jpeg"))) == 6


def test_prune_is_safe_when_directory_missing(tmp_path: Path):
    # 目录不存在/权限异常都不该抛错，否则会影响出图主流程
    watch._prune_artifacts(tmp_path / "does-not-exist")


def test_default_keep_is_bounded():
    assert 1 <= watch.WATCH_KEEP_PER_KIND <= 48


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
