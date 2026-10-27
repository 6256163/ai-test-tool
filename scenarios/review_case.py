"""scenarios/review_case.py —— 复审「LLM 已返回的 JSON」：不经模型，直接跑静态校验 + codegen + 真实执行。

用法：
  python \
      scenarios/review_case.py scenarios/sample_cases/xxx.json [--no-run]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import yaml  # noqa: E402

from generator.codegen import case_to_code  # noqa: E402
from evaluate.judge import gate, lint_locators  # noqa: E402
from ingest.schema import Case  # noqa: E402
from prober.dom_catalog import scan_source  # noqa: E402
from runner.executor import execute  # noqa: E402
from runner.healer import heal_and_rerun, render_report  # noqa: E402


def load_catalog(cfg: dict):
    src = (cfg.get("framework", {}).get("web", {}) or {}).get("frontend_src", "")
    if not src:
        return None
    if not os.path.isabs(src):
        src = os.path.join(ROOT, src)
    return scan_source(src) if os.path.isdir(src) else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("case_json")
    ap.add_argument("--no-run", action="store_true", help="只做静态校验，不真跑浏览器")
    ap.add_argument("--repeat", type=int, default=1, help="重复执行次数（>1 时同时做 flaky 检测）")
    ap.add_argument("--heal", action="store_true", help="执行失败后启动定位器自愈（Healer）并重跑")
    args = ap.parse_args()

    with open(os.path.join(ROOT, "config.yaml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    base_url = cfg.get("framework", {}).get("web", {}).get("base_url", "http://localhost:8081")

    with open(args.case_json, encoding="utf-8") as f:
        case = Case.model_validate(json.load(f))

    catalog = load_catalog(cfg)
    errors, warnings = lint_locators(case, catalog)

    print("=== ① 静态校验（judge.lint_locators）===")
    if not errors and not warnings:
        print("  无问题 ✅")
    for e in errors:
        print(f"  ❌ 硬伤: {e}")
    for w in warnings:
        print(f"  ⚠️ 提示: {w}")

    code = case_to_code(case, base_url=base_url)
    print("\n=== ② 生成的代码 ===")
    print(code)

    if args.no_run:
        return

    if args.heal:
        print("=== ③ Healer：执行 → 失败解析 → 修补定位器 → 重跑 ===")
        outcome = heal_and_rerun(
            case, catalog=catalog, execute_fn=execute, base_url=base_url, max_attempts=3,
        )
        print(render_report(outcome))
        healed_code = case_to_code(outcome.healed_case, base_url=base_url)
        print("\n=== ④ 自愈后的用例代码 ===")
        print(healed_code)
        print("\n=== ⑤ 质量门（以自愈后最后一次执行判定）===")
        passed, reasons = gate(outcome.healed_case, outcome.runs[-1], healed_code, catalog=catalog)
        print("PASS ✅" if passed else "FAIL ❌")
        for r in reasons:
            print(f"  - {r}")
        return

    print("=== ③ 真实执行 ===")
    runs = []
    for n in range(1, args.repeat + 1):
        r = execute(code)
        runs.append(r)
        print(f"  第{n}次: {'✅' if r.ok else '❌'} exit={r.exit_code} passed={r.passed} failed={r.failed}")
        if not r.ok:
            print(r.output[-2000:])
    run = runs[-1]
    oks = [bool(r.ok) for r in runs]
    if len(set(oks)) > 1:
        print("\n  ⚠️ 重复执行结果不一致 → 用例存在环境依赖/flaky，不可作为稳定回归用例")
    elif all(oks):
        print(f"\n  ✅ 连续 {len(runs)} 次一致通过")

    print("\n=== ④ 质量门（以最后一次执行判定）===")
    passed, reasons = gate(case, run, code, catalog=catalog)
    print("PASS ✅" if passed else "FAIL ❌")
    for r in reasons:
        print(f"  - {r}")


if __name__ == "__main__":
    main()
