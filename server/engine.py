"""server/engine.py —— 阶段1 执行核心（同步接口与异步 worker 共用）。

把「配置 / catalog 加载 + codegen + execute + heal + gate + lint」收敛到一处，
避免 app.py 与 jobs.py 各写一份。引擎逻辑本身零重写，仍复用 main / generator /
runner / evaluate 的既有实现。

设计：
  - get_cfg / get_catalog 带模块级缓存（与 app 旧实现等价）。
  - run_case 是唯一的「跑一次用例」入口：返回包含 RunResult 对象的 dict。
  - 同步接口（/api/run-case、/api/generate 内部）用 run_in_threadpool 调它；
    异步 worker（jobs.py）直接在线程里调它，不进事件循环。
"""
from __future__ import annotations

import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import main  # noqa: E402  (load_config / _framework_ctx / _load_catalog)
from ingest.schema import Case  # noqa: E402
from generator.codegen import case_to_code  # noqa: E402
from generator.llm_client import LLMClient  # noqa: E402
from ingest.parser import parse  # noqa: E402
from runner.executor import execute  # noqa: E402
from runner.healer import heal_and_rerun  # noqa: E402
from evaluate.judge import gate, lint_locators  # noqa: E402
from security.secrets import extract_secrets, cred_slots_text, load_profiles  # noqa: E402

_cfg = None
_catalog = None
_catalog_loaded = False


def get_cfg() -> dict:
    global _cfg
    if _cfg is None:
        _cfg = main.load_config()
    return _cfg


def get_catalog():
    global _catalog, _catalog_loaded
    if not _catalog_loaded:
        _catalog = main._load_catalog(get_cfg())
        _catalog_loaded = True
    return _catalog


def framework_ctx(cfg: dict | None = None) -> dict:
    return main._framework_ctx(cfg or get_cfg())


def heal_summary(heal_out) -> dict | None:
    """把 HealOutcome 压成可 JSON 化的摘要（与 main.run_pipeline 一致）。"""
    if heal_out is None:
        return None
    return {
        "final_ok": heal_out.final_ok,
        "applied": [vars(a) for a in heal_out.applied],
        "needs_review": [f"{f.fail_type.value}:{f.by}={f.value}" for f in heal_out.needs_review],
        "unhealable": [f"{f.fail_type.value}:{f.by}={f.value}" for f in heal_out.unhealable],
        "runs": len(heal_out.runs),
    }


def build_case(raw) -> Case:
    """把任意来源（dict / JSON str）的用例反序列化成 Case，失败抛异常。"""
    if isinstance(raw, str):
        import json
        raw = json.loads(raw)
    return Case.model_validate(raw)


def build_prompt_preview(requirement: str, platform: str | None = None,
                         use_hints: bool = True) -> dict:
    """构造「将发给 LLM 的完整提示词」预览（不发起任何网络请求）。

    复刻 main.run_pipeline 里构造 LLM 请求的完整过程：parse 需求 → framework 上下文 →
    DOM 目录 hints → 凭据脱敏 → 凭据槽位 → 组装 system/user 消息 + JSON Schema。
    与 generate_case 实际发出的内容完全一致（含脱敏后的文案与 DOM 目录）。

    用途：服务端缺 LLM_API_KEY 时，让 Web UI 把完整提示词打印出来，便于本地调试/排查，
    而不只是报错。
    """
    import copy

    cfg = copy.deepcopy(get_cfg())
    if platform:
        cfg.setdefault("framework", {})["type"] = platform
    task = parse(requirement)
    fctx = framework_ctx(cfg)
    hints = main._build_dom_hints(cfg) if use_hints else None

    # 脱敏与 generate_case 内部一致：先抽凭据，出网的只有占位符（真值留本机）
    req = task.description
    secrets: dict[str, str] = {}
    if cfg["llm"].get("desensitize", False):
        req, secrets = extract_secrets(req)
    cred_slots = None
    if use_hints:
        profiles, _w = load_profiles(cfg)
        cred_slots = cred_slots_text(profiles)

    llm = LLMClient(
        base_url=cfg["llm"]["base_url"], api_key="<DUMMY>",
        model=cfg["llm"]["model"], desensitize=cfg["llm"].get("desensitize", False),
    )
    messages, schema = llm.build_messages(
        req, platform=fctx["platform"], dom_hints=hints, cred_slots=cred_slots,
    )
    return {
        "messages": messages,
        "schema": schema,
        "provider": {"base_url": cfg["llm"]["base_url"], "model": cfg["llm"]["model"]},
        "secrets": sorted(secrets.keys()),
    }


def run_case(case: Case, *, base_url: str | None = None, heal: bool = False,
             repeat: int = 1) -> dict:
    """执行一份用例，返回统一结果 dict（run 字段是 RunResult 对象）。

    repeat>1 时会在（可能已被自愈过的）代码上重复执行，用于 flaky 检测：
    返回 meta={"repeat":N,"oks":[...],"stable":bool}，canonical run 取最后一次。
    """
    cfg = get_cfg()
    catalog = get_catalog()
    fctx = framework_ctx(cfg)
    bu = (base_url or "").strip() or fctx["base_url"]

    code = case_to_code(case, base_url=bu, mobile=fctx["mobile"],
                        app_package=fctx["app_package"])
    run = execute(code)

    heal_out = None
    if heal and not run.ok:
        heal_out = heal_and_rerun(
            case, catalog=catalog, execute_fn=execute,
            base_url=bu, max_attempts=3,
        )
        run = heal_out.runs[-1]
        case = heal_out.healed_case
        code = case_to_code(case, base_url=bu)

    oks = [bool(run.ok)]
    for _ in range(max(0, (repeat or 1) - 1)):
        r2 = execute(code)
        oks.append(bool(r2.ok))
        run = r2

    meta = None
    if (repeat or 1) > 1:
        meta = {"repeat": repeat, "oks": oks, "stable": len(set(oks)) == 1}

    passed, reasons = gate(
        case, run, code,
        min_coverage_tags=cfg["gate"].get("min_coverage_tags", 1),
        require_assert=cfg["gate"].get("require_assert", True),
        catalog=catalog,
    )
    _, loc_warnings = lint_locators(case, catalog)
    return {
        "case": case,
        "code": code,
        "run": run,
        "gate_passed": passed,
        "reasons": reasons,
        "locator_warnings": loc_warnings,
        "heal": heal_summary(heal_out),
        "secret_slots": [],
        "meta": meta,
    }
