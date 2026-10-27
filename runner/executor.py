"""runner/executor.py —— 调 pytest 运行生成的用例并收集结果。

P0 直接子进程跑 pytest；P1 已接 healer（runner/healer.py，定位器自愈）和 CI。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile

# 项目根 conftest.py（提供 page fixture）。生成用例会被写到临时目录，
# 而 pytest 只收集「用例文件所在目录及其祖先」下的 conftest，所以必须复制过去。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONFTEST = os.path.join(_ROOT, "conftest.py")


class RunResult:
    def __init__(self, exit_code: int, passed: int, failed: int, output: str):
        self.exit_code = exit_code
        self.passed = passed
        self.failed = failed
        self.output = output

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and self.failed == 0


def execute(test_code: str, *, pytest_args=None) -> RunResult:
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "test_gen.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(test_code)
        if os.path.exists(_CONFTEST):
            shutil.copy2(_CONFTEST, os.path.join(d, "conftest.py"))
        # 用当前解释器跑 pytest：裸 `pytest` 依赖 PATH，虚拟环境未激活时会跑到别的解释器上
        cmd = [sys.executable, "-m", "pytest", path, "-q", "--no-header", "-p", "no:cacheprovider"]
        cmd += (pytest_args or [])
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=d)
        out = proc.stdout + proc.stderr
        passed = _count(out, r"(\d+) passed")
        failed = _count(out, r"(\d+) failed")
        return RunResult(proc.returncode, passed, failed, out)


def _count(text: str, pat: str) -> int:
    m = re.search(pat, text)
    return int(m.group(1)) if m else 0
