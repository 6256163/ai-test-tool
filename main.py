"""main.py —— P0 流水线入口：需求 → 结构化任务 → 用例 → 代码 → 执行 → 质量门。

用法：
  # 真实跑（需要 LLM_API_KEY 和 playwright 浏览器）
  export LLM_API_KEY=xxx
  python main.py "在登录页输入手机号 13800000000 和密码，点击登录，预期进入首页"

  # 离线演示（不调真实模型，用 MockLLM 生成 3 个真实业务场景）
  python scenarios/demo.py
"""
from __future__ import annotations

import argparse
import os
import sys

try:
    import yaml
except ModuleNotFoundError:  # 裸解释器常见：依赖只装在项目虚拟环境里
    print("❌ 当前解释器缺少依赖（PyYAML 等），无法运行。", file=sys.stderr)
    print(f"   正在使用的 python：{sys.executable}", file=sys.stderr)
    print("\n请用装了依赖的环境执行，例如：\n"
          '  python main.py "一句话需求"\n'
          "或先安装依赖：\n"
          "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt", file=sys.stderr)
    sys.exit(2)

# 让 `python main.py` 从项目根目录直接能跑
ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ingest.parser import parse  # noqa: E402
from ingest.schema import framework_to_platform  # noqa: E402
from generator.llm_client import LLMClient  # noqa: E402
from generator.codegen import case_to_code  # noqa: E402
from runner.executor import execute  # noqa: E402
from runner.healer import heal_and_rerun  # noqa: E402
from evaluate.judge import gate, lint_locators  # noqa: E402
from security.secrets import (  # noqa: E402
    build_cred_mapping, cred_slots_text, load_profiles, reconcile_with_registry, rehydrate_case,
)
from prober.dom_catalog import scan_source  # noqa: E402


def load_config(path: str = "config.yaml") -> dict:
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    # 环境变量展开：${LLM_API_KEY}
    api_key = cfg["llm"]["api_key"]
    if isinstance(api_key, str) and api_key.startswith("${") and api_key.endswith("}"):
        env = api_key[2:-1]
        cfg["llm"]["api_key"] = os.environ.get(env, "")
    return cfg


def _framework_ctx(cfg: dict) -> dict:
    """从 framework 配置解析出 (platform, base_url, mobile, app_package)。"""
    fw = cfg.get("framework", {})
    ftype = (fw.get("type") or "web").lower()
    web = fw.get("web", {}) or {}
    app = fw.get("app", {}) or {}
    if ftype == "app":
        return {
            "platform": "app",
            "base_url": app.get("base_url", "http://localhost"),
            "mobile": False,
            "app_package": app.get("app_package", "com.shopdemo.app"),
        }
    return {
        "platform": "web",
        "base_url": web.get("base_url", "http://localhost:3000"),
        "mobile": bool(web.get("mobile", False)),
        "app_package": app.get("app_package", "com.shopdemo.app"),
    }


def _load_catalog(cfg: dict):
    """P1：从前端源码抽取定位器目录。

    返回 Catalog；返回 None 表示不抽取（frontend_src 未配置/不存在/扫描失败）。
    """
    fw = cfg.get("framework", {})
    web = fw.get("web", {}) or {}
    src = (web.get("frontend_src") or "").strip()
    if not src:
        return None
    if not os.path.isabs(src):
        src = os.path.join(ROOT, src)
    if not os.path.isdir(src):
        print(f"⚠️ frontend_src 不存在，跳过目录抽取：{src}")
        return None
    try:
        return scan_source(src)
    except Exception as e:  # 目录抽取失败不应打断主流程
        print(f"⚠️ 目录抽取失败，跳过：{e}")
        return None


def _build_dom_hints(cfg: dict) -> str | None:
    """把目录压成注入 LLM 的 hints 文本；None 表示盲生成。"""
    cat = _load_catalog(cfg)
    return cat.to_hints_text() if cat else None


def run_pipeline(requirement: str, cfg: dict, *, heal: bool = False) -> dict:
    task = parse(requirement)
    fctx = _framework_ctx(cfg)
    catalog = _load_catalog(cfg)
    dom_hints = catalog.to_hints_text() if catalog else None

    # P1-B：凭据注册表（真值留本机，出网的只有 ${CRED.xxx} 槽位名）
    profiles, cred_warnings = load_profiles(cfg)
    for w in cred_warnings:
        print(f"⚠️ {w}")

    llm = LLMClient(
        base_url=cfg["llm"]["base_url"],
        api_key=cfg["llm"]["api_key"],
        model=cfg["llm"]["model"],
        max_retries=cfg["llm"].get("max_retries", 3),
        timeout=cfg["llm"].get("timeout", 60),
        desensitize=cfg["llm"].get("desensitize", False),
    )
    case = llm.generate_case(
        task.description, platform=fctx["platform"],
        dom_hints=dom_hints, cred_slots=cred_slots_text(profiles),
    )

    # P1-C：生成后回填。注册表优先，其次用出域前抽到的真值
    secrets = build_cred_mapping(profiles)
    if llm.last_secrets:
        reconciled, overrides = reconcile_with_registry(llm.last_secrets, profiles)
        for w in overrides:
            print(f"⚠️ {w}")
        secrets = {**secrets, **reconciled}
    case = rehydrate_case(case, secrets)  # 必须在 codegen 之前

    code = case_to_code(
        case,
        base_url=fctx["base_url"],
        mobile=fctx["mobile"],
        app_package=fctx["app_package"],
    )

    run = execute(code)
    # P1：执行失败时可选启动定位器自愈（Healer），只修 locator，不碰断言
    heal_out = None
    if heal and not run.ok:
        heal_out = heal_and_rerun(
            case, catalog=catalog, execute_fn=execute,
            base_url=fctx["base_url"], max_attempts=3,
        )
        run = heal_out.runs[-1]
        case = heal_out.healed_case
        code = case_to_code(case, base_url=fctx["base_url"])
    passed, reasons = gate(
        case, run, code,
        min_coverage_tags=cfg["gate"].get("min_coverage_tags", 1),
        require_assert=cfg["gate"].get("require_assert", True),
        catalog=catalog,
    )
    _, loc_warnings = lint_locators(case, catalog)

    heal_summary = None
    if heal_out is not None:
        heal_summary = {
            "final_ok": heal_out.final_ok,
            "applied": [vars(a) for a in heal_out.applied],
            "needs_review": [f"{f.fail_type.value}:{f.by}={f.value}" for f in heal_out.needs_review],
            "unhealable": [f"{f.fail_type.value}:{f.by}={f.value}" for f in heal_out.unhealable],
            "runs": len(heal_out.runs),
        }
    return {
        "case": case,
        "code": code,
        "run": run,
        "gate_passed": passed,
        "reasons": reasons,
        "locator_warnings": loc_warnings,
        "secret_slots": sorted(secrets.keys()),
        "heal": heal_summary,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="AI 测试提效工具 P0 流水线（支持 web/app 双端）")
    ap.add_argument("requirement", nargs="?", help="一句话需求")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--platform", choices=["web", "app"], default=None,
                    help="覆盖 config.framework.type，强制指定生成平台")
    ap.add_argument("--heal", action="store_true",
                    help="执行失败后启动定位器自愈（Healer）并重跑")
    args = ap.parse_args()

    if not args.requirement:
        ap.print_help()
        return

    cfg = load_config(args.config)
    if args.platform:
        cfg.setdefault("framework", {})["type"] = args.platform
    if not cfg["llm"]["api_key"]:
        print("⚠️ 未设置 LLM_API_KEY 环境变量，无法调用真实模型。试试 `python scenarios/demo.py` 离线演示。")
        return

    res = run_pipeline(args.requirement, cfg, heal=args.heal)
    if res["secret_slots"]:
        print("=== 敏感数据（真值未出网，仅占位符上云）===")
        print("  占位符:", "、".join("${" + k + "}" for k in res["secret_slots"]))
    print("=== 生成的用例代码 ===")
    print(res["code"])
    print("=== 执行结果 ===")
    print(res["run"].output[-1500:])
    print("=== 质量门 ===")
    print("PASS ✅" if res["gate_passed"] else f"FAIL ❌ -> {res['reasons']}")
    if res["locator_warnings"]:
        print("=== 定位器建议（不影响通过）===")
        for w in res["locator_warnings"]:
            print(f"  ⚠️ {w}")
    if res["heal"]:
        h = res["heal"]
        print("=== Healer 自愈报告 ===")
        print(f"  结论: {'自愈后通过 ✅' if h['final_ok'] else '仍有未解决失败 ❌'} | 共执行 {h['runs']} 轮")
        if h["applied"]:
            print("  已自愈:", h["applied"])
        if h["needs_review"]:
            print("  需人工评审:", h["needs_review"])
        if h["unhealable"]:
            print("  无法自愈:", h["unhealable"])


if __name__ == "__main__":
    main()
