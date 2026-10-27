"""prober/build_catalog.py —— 抽取前端定位器目录并落盘 catalog.json。

用法：
  python prober/build_catalog.py
读取 config.yaml 的 framework.web.frontend_src，静态扫描得到 route→[testid] 目录，
保存到 prober/catalog.json（同时打印摘要，便于核对）。

这是 P1 hint 的数据来源：main.py 每次运行会自动重新扫描（快、确定性），
本脚本用于单独预览/存档目录，也可在 CI 里跑。
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import yaml  # noqa: E402

from prober.dom_catalog import scan_source  # noqa: E402


def main() -> None:
    with open(os.path.join(ROOT, "config.yaml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    src = (cfg.get("framework", {}).get("web", {}).get("frontend_src") or "").strip()
    if not src:
        print("⚠️ config.framework.web.frontend_src 未配置，无法抽取目录")
        return
    if not os.path.isabs(src):
        src = os.path.join(ROOT, src)
    if not os.path.isdir(src):
        print(f"⚠️ frontend_src 不存在：{src}")
        return

    cat = scan_source(src)
    out = os.path.join(ROOT, "prober", "catalog.json")
    cat.save_json(out)

    print(f"✅ 目录已生成：{out}")
    print(f"路由数={len(cat.routes)}  全量 testid={len(cat.all_testids)}  全局 testid={len(cat.global_testids)}")
    print("路由清单：", cat.routes)
    print("--- 各路由 testid ---")
    for r in cat.routes:
        print(f"  {r}: {cat.route_testids.get(r) or []}")


if __name__ == "__main__":
    main()
