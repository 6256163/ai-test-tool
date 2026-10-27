"""evaluate/judge.py —— 质量门（防「伪通过」）。

AI 生成用例最大的坑：看起来绿、实际没断言、或断言没真命中。
P0 先用「规则门」：执行通过 + 代码里真的有断言 + 覆盖标签达标。
P1 叠加「定位器校验」：LLM 生成的 testid 必须真实存在于前端目录（catalog），
否则就是幻觉定位器——这类问题在运行期只表现为「元素找不到」，容易与产品 bug 混淆。
第⑤层后续再叠加 LLM-as-Judge 打分（开放环节）。
"""
from __future__ import annotations

import difflib
import re
from typing import TYPE_CHECKING, Any

from ingest.schema import Case, framework_to_platform, ACTION_PLATFORMS, PLATFORM_APP
from runner.executor import RunResult

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查，避免运行期循环依赖
    from prober.dom_catalog import Catalog


def _has_assertions(code: str) -> bool:
    return ("expect(" in code) or ("assert " in code)


def _platform_violations(case: Case) -> list[str]:
    """app 用例里若出现 web-only 动作（hover/select/navigate/assert_url），给出违规原因。"""
    if framework_to_platform(case.target_framework) != PLATFORM_APP:
        return []
    bad = []
    for step in case.steps:
        a = step.action.value
        allowed = ACTION_PLATFORMS.get(a, {PLATFORM_APP})
        if PLATFORM_APP not in allowed:
            bad.append(a)
    return bad


def _testid_like_tokens(text: str) -> list[str]:
    """从 note 里抽出「长得像 testid」的 token（含至少一个连字符/下划线的字母数字串）。

    LLM 常用自然语言在 note 里承诺校验，例如
    「校验购物车单价与详情页 detail-price 一致（运行时比对）」——
    这句话点名的 `detail-price` 在整条用例里从未被任何步骤定位过，属于典型「承诺未兑现」。
    """
    return re.findall(r"[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)+", text or "")


def _unrealized_claims(case: Case, known_set: set[str]) -> list[str]:
    """note 声称要校验某个 testid，但没有任何步骤真的定位它 → 该校验并未实现。

    这类问题不会报错：代码照常跑绿，但被测功能实际上从未被验证过——
    比「断言写错」更隐蔽，因为它会让人误以为覆盖到了。
    """
    if not known_set:
        return []
    used = {
        (s.locator.value or "").strip()
        for s in case.steps if s.locator is not None and s.locator.by.value == "testid"
    }
    errors: list[str] = []
    seen: set[str] = set()
    for i, step in enumerate(case.steps, 1):
        for tok in _testid_like_tokens(step.note):
            if tok in known_set and tok not in used and tok not in seen:
                seen.add(tok)
                errors.append(
                    f"步骤{i} note 声称校验 `{tok}`，但整条用例没有步骤定位过它 → "
                    f"该校验并未真正实现（要么补 assert 步骤，要么删掉这句承诺）"
                )
    return errors


def lint_locators(case: Case, catalog: "Catalog | None") -> tuple[list[str], list[str]]:
    """用前端目录校验生成的定位器。

    返回 (errors, warnings)：
      errors   —— 硬伤，质量门直接 FAIL
                  ① testid 不在目录里（幻觉定位器）
                  ② base_path 不在路由表里（幻觉路由）
                  ③ 列表元素（v-for 内的 testid）没带 index → strict mode 必挂
                  ④ fill/select 用 text 定位（命中的是文案节点，对 label fill 必失败）
                  ⑤ note 点名了某 testid 但用例从未定位它 → 承诺的校验没实现
      warnings —— 仅提示，不影响通过
                  ⑥ 该路由明明有 testid，却退回 text/css/xpath（稳定性差）
    """
    errors: list[str] = []
    warnings: list[str] = []
    if catalog is None:
        return errors, warnings

    known = list(catalog.all_testids or [])
    known_set = set(known)
    repeated_set = set(getattr(catalog, "repeated_testids", None) or [])

    for i, step in enumerate(case.steps, 1):
        loc = step.locator
        if loc is None or loc.by.value != "testid":
            continue
        v = (loc.value or "").strip()
        if v and v not in known_set:
            close = difflib.get_close_matches(v, known, n=1, cutoff=0.6)
            hint = f"，最接近的是 `{close[0]}`" if close else ""
            errors.append(f"步骤{i} 定位器幻觉：testid `{v}` 不在前端目录中{hint}")
        elif repeated_set and v in repeated_set and getattr(loc, "index", None) is None:
            # 这个元素在列表里会渲染出多个，Playwright strict mode 下必然抛
            # "resolved to N elements"，必须显式指定 index。
            errors.append(
                f"步骤{i} 列表元素未指定 index：testid `{v}` 位于 v-for 内，页面会出现多个同名元素，"
                "strict mode 必然报错；请给 locator 补 index（0=第一个，-1=最后一个）"
            )

    # fill/select 的目标是输入控件，而 text 定位命中的是「文案节点」（label/span）。
    # 实测：target_demo 登录页 get_by_text("用户名") 与 get_by_label("用户名") 均命中 2 个
    # （登录/注册两个 tab 都在 DOM 里），对 label 调 fill 必然失败 —— 这不是风格问题，是硬伤。
    for i, step in enumerate(case.steps, 1):
        loc = step.locator
        if loc is None:
            continue
        if step.action.value in ("fill", "select") and loc.by.value == "text":
            errors.append(
                f"步骤{i} {step.action.value} 用 text 定位 `{loc.value}`："
                "text 命中的是文案节点而非输入框，必然失败；请改用 testid 或 css"
            )

    errors.extend(_unrealized_claims(case, known_set))

    base_path = (case.base_path or "").strip()
    routes = list(catalog.routes or [])
    if base_path and routes and base_path not in set(routes):
        close = difflib.get_close_matches(base_path, routes, n=1, cutoff=0.5)
        hint = f"，最接近的是 `{close[0]}`" if close else ""
        errors.append(f"base_path `{base_path}` 不在前端路由表中{hint}")

    tids = catalog.route_testids.get(base_path) or []
    if tids:
        for i, step in enumerate(case.steps, 1):
            loc = step.locator
            if loc is None or loc.by.value not in ("text", "css", "xpath"):
                continue
            warnings.append(
                f"步骤{i} 用 {loc.by.value} 定位 `{loc.value}`；"
                f"该路由有 {len(tids)} 个可用 testid，建议优先用 testid 以提升稳定性"
            )
    return errors, warnings


def gate(case: Case, run: RunResult, code: str, *,
         min_coverage_tags: int = 1, require_assert: bool = True,
         catalog: Any = None) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if not run.ok:
        reasons.append(f"执行未通过（exit={run.exit_code}, failed={run.failed}）")
    if require_assert and not _has_assertions(code):
        reasons.append("生成的用例没有断言 → 疑似伪绿，拦截")
    if len(case.coverage_tags) < min_coverage_tags:
        reasons.append(f"覆盖标签不足（{len(case.coverage_tags)} < {min_coverage_tags}）")
    bad_actions = _platform_violations(case)
    if bad_actions:
        reasons.append(f"app 用例含 web-only 动作（{'/'.join(bad_actions)}），应改用 launch/swipe/long_press 等")
    # P1：定位器/路由必须真实存在于前端目录（catalog 为 None 时跳过，退化为 P0 行为）
    errors, _warnings = lint_locators(case, catalog)
    reasons.extend(errors)
    return (len(reasons) == 0), reasons
