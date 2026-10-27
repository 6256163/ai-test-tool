"""runner/healer.py —— P1: 定位器自愈（Healer）。

执行失败 ≠ 产品 bug。相当一部分失败其实是「选择器 drift」：
  - 同一页面出现多个同名元素（商品列表 / 表格行）→ Playwright strict mode 直接抛错
  - 前端重构把 data-testid 改名 → 原 locator 在 DOM 里彻底找不到了
  - LLM 幻觉出的 testid（不在 catalog 中）被运行时打脸

Healer 在「不改断言」的护栏下，用「运行时失败信息 + 前端目录（catalog）」自动修补
Case 的 locator，再重新生成代码跑一遍：
  - STRICT_MODE（多元素命中）→ 给 locator 补 index=0（取第一个）
  - NOT_FOUND 且 catalog 没有该 testid → 用 difflib 找最接近的重命名并替换 value
  - NOT_FOUND 且 catalog 有该 testid（但在当前页没出现）→ 留给人工，不猜（可能是路由/上下文错）
  - TIMEOUT_VISIBLE（元素在但不满足可见/可点）→ 不自愈，留给人工（可能是真 bug，自愈会掩盖）

护栏（禁改断言）：
  只改 locator 的 by/value/index；绝不修改 Step.expect，也绝不把 assert_* 降级成更弱的检查。
  这一点和「三智能体替换」里讨论的 Healer 护栏一致——自愈可以修定位器，但不能把失败的断言改成通过。
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Callable

from ingest.schema import Case, Locator, LocatorStrategy
from runner.executor import RunResult

if TYPE_CHECKING:  # pragma: no cover - 仅类型提示，避免运行期循环依赖
    from prober.dom_catalog import Catalog


class FailType(str, Enum):
    STRICT_MODE = "strict_mode"          # 多元素命中，需要 index
    NOT_FOUND = "not_found"              # 0 元素 / 超时，定位器在 DOM 找不到
    TIMEOUT_VISIBLE = "timeout_visible"  # 元素在，但不满足「可见 / 可点」
    OTHER = "other"


@dataclass
class Failure:
    """从一次运行输出里解析出的一条失败信息。"""
    fail_type: FailType
    by: str | None          # 定位方式：testid / text / ...
    value: str | None       # 定位值（testid 或文案）
    raw: str = ""           # 命中的原始错误行（用于回放/审计）


@dataclass
class HealAction:
    """一次成功的自愈修补：改了第几步、哪个字段、从什么改成什么。"""
    step_index: int         # 1-based 步骤序号
    field: str              # 改了 locator 的哪个字段（by / value / index）
    old: object
    new: object
    reason: str


@dataclass
class HealOutcome:
    healed_case: Case
    applied: list[HealAction] = field(default_factory=list)
    needs_review: list[Failure] = field(default_factory=list)
    unhealable: list[Failure] = field(default_factory=list)
    runs: list[RunResult] = field(default_factory=list)
    final_ok: bool = False


# ====================== 错误解析 ======================
# Playwright Python 的报错行形如：
#   playwright._impl._errors.Error: strict mode violation: locator.getByTestId('product-name') resolved to 2 elements.
#   playwright._impl._errors.Error: page.get_by_test_id("x").click: Timeout 30000ms exceeded.
# 源码行也会包含 get_by_test_id(...)，但它不带错误关键字，因此只扫描「错误行」。
_STRICT_RE = re.compile(r"strict mode violation", re.IGNORECASE)
_RESOLVED_RE = re.compile(r"resolved to (\d+) elements", re.IGNORECASE)
_LOC_TESTID = re.compile(r"getByTestId\(['\"]([^'\"]+)['\"]\)", re.IGNORECASE)
_LOC_TESTID2 = re.compile(r"get_by_test_id\(['\"]([^'\"]+)['\"]\)")
_LOC_TEXT = re.compile(r"getByText\(['\"]([^'\"]+)['\"]\)", re.IGNORECASE)
_LOC_TEXT2 = re.compile(r"get_by_text\(['\"]([^'\"]+)['\"]\)")
_TIMEOUT_RE = re.compile(r"(timeout|exceeded|waiting for)", re.IGNORECASE)
_VISIBLE_RE = re.compile(r"(not visible|to be visible|is not visible|is hidden)", re.IGNORECASE)
_ERROR_LINE_RE = re.compile(r"(error|strict mode|violation|timeout|exceeded|waiting for)", re.IGNORECASE)


def _extract_locator(text: str) -> tuple[str | None, str | None]:
    """从一行错误里抽出 (by, value)。优先 testid，其次 text。"""
    m = _LOC_TESTID.search(text) or _LOC_TESTID2.search(text)
    if m:
        return "testid", m.group(1)
    m = _LOC_TEXT.search(text) or _LOC_TEXT2.search(text)
    if m:
        return "text", m.group(1)
    return None, None


def parse_failures(output: str) -> list[Failure]:
    """解析 pytest/Playwright 的运行输出，提取可被自愈识别的失败信息。

    只扫「错误行」（包含 error/strict/timeout 等关键字），跳过源码行，避免把
    正常出现的 get_by_test_id(...) 误判成失败定位器。
    """
    failures: list[Failure] = []
    seen: set[tuple] = set()
    for raw_line in output.splitlines():
        # 只处理 pytest 的报错行（以 "E" 为前缀）；源码行（">"）、摘要行等都排除，天然去噪。
        if not raw_line.startswith("E"):
            continue
        line = raw_line[1:].lstrip()  # 去掉 "E" 标记
        # 跳过 Playwright 错误块的「续行」（如 "- Expect ... with timeout 5000ms"），
        # 它们是主错误行的补充说明，单独解析会被误判成 not_found。
        if line.startswith("- "):
            continue
        if not _ERROR_LINE_RE.search(line):
            continue
        by, value = _extract_locator(line)
        if by is None:
            # 没有可定位信息的一般错误（断言 expect 摘要行等）——不是定位器漂移信号，丢弃
            continue
        if _STRICT_RE.search(line) and _RESOLVED_RE.search(line):
            ft = FailType.STRICT_MODE
        elif _VISIBLE_RE.search(line) and not _TIMEOUT_RE.search(line):
            # 明确「元素在但不可见」（如 is not visible），不超时 → 可能是真 bug，交人工
            ft = FailType.TIMEOUT_VISIBLE
        elif _TIMEOUT_RE.search(line):
            # 超时 / exceeded / waiting for → 定位器在 DOM 找不到，给重命名自愈机会
            ft = FailType.NOT_FOUND
        else:
            ft = FailType.NOT_FOUND
        key = (ft, by, value)
        if key in seen:
            continue
        seen.add(key)
        failures.append(Failure(ft, by, value, raw=line))
    return failures


# ====================== 单次修补（不重跑） ======================
def heal_case(case: Case, failures: list[Failure], catalog: "Catalog | None") -> HealOutcome:
    """对一份 Case 应用自愈修补，返回修补后的 Case 与分类清单（不执行）。

    护栏：只改 locator 的 by/value/index，绝不碰 Step.expect，也绝不把 assert_* 降级。
    """
    healed = case.model_copy(deep=True)
    out = HealOutcome(healed_case=healed)

    known: set[str] = set(getattr(catalog, "all_testids", None) or [])

    for f in failures:
        if f.value is None:
            out.unhealable.append(f)
            continue

        # 找到所有命中该定位器的步骤（by 已知时再校验 by，避免 text 与 testid 串台）
        targets = [
            (i, s) for i, s in enumerate(healed.steps, 1)
            if s.locator is not None
            and s.locator.value == f.value
            and (f.by is None or s.locator.by.value == f.by)
        ]
        if not targets:
            out.unhealable.append(f)  # 失败定位器在用例里找不到对应步骤
            continue

        if f.fail_type == FailType.STRICT_MODE:
            # 多元素命中 → 补 index=0（取第一个）。已是带 index 的还报 strict，说明冲突，交人工。
            changed = False
            for i, s in targets:
                loc = s.locator
                if getattr(loc, "index", None) is None:
                    old = loc.index
                    loc.index = 0
                    out.applied.append(HealAction(i, "index", old, 0,
                        "strict mode 多元素命中，补 index=0 取第一个"))
                    changed = True
            if not changed:
                out.needs_review.append(f)

        elif f.fail_type == FailType.NOT_FOUND:
            if f.by == "testid" and f.value in known:
                # catalog 里有这个 testid，但当前页没出现 → 可能是路由/上下文错，不猜
                out.needs_review.append(f)
            elif f.by == "testid":
                # catalog 里没有 → 多半是前端改名。用 difflib 找最接近的重命名。
                cand = difflib.get_close_matches(f.value, list(known), n=1, cutoff=0.7)
                if cand:
                    new_val = cand[0]
                    for i, s in targets:
                        old = s.locator.value
                        s.locator.value = new_val
                        out.applied.append(HealAction(i, "value", old, new_val,
                            f"testid `{f.value}` 不在前端目录，按最接近重命名为 `{new_val}`"))
                else:
                    out.unhealable.append(f)  # 连近似都找不到，是真漂移
            else:
                # text 定位器找不到：catalog 只有 testid，无法安全重命名，交人工
                out.needs_review.append(f)

        elif f.fail_type == FailType.TIMEOUT_VISIBLE:
            # 元素在但不满足可见/可点：可能是真 bug（被遮挡/未渲染），自愈会掩盖，交人工
            out.needs_review.append(f)

        else:  # OTHER
            out.needs_review.append(f)

    return out


# ====================== 修补 + 重跑循环 ======================
def heal_and_rerun(
    case: Case,
    *,
    catalog: "Catalog | None",
    execute_fn: Callable[[str], RunResult],
    base_url: str = "http://localhost:3000",
    max_attempts: int = 2,
) -> HealOutcome:
    """执行 → 失败则解析 → 修补 → 重新生成代码再跑，最多 max_attempts 轮。

    一轮内若没有任何可应用的修补（applied 为空），说明已经修不动，立即停，
    避免对「逻辑错误 / 断言失败」空转。最终态写回 HealOutcome.healed_case。
    """
    from generator.codegen import case_to_code  # 延迟导入，避免循环依赖

    current = case
    out = HealOutcome(healed_case=current)

    for _ in range(max_attempts):
        code = case_to_code(current, base_url=base_url)
        run = execute_fn(code)
        out.runs.append(run)
        if run.ok:
            out.final_ok = True
            out.healed_case = current
            return out

        failures = parse_failures(run.output)
        part = heal_case(current, failures, catalog)
        out.needs_review.extend(part.needs_review)
        out.unhealable.extend(part.unhealable)
        if not part.applied:
            out.healed_case = current
            return out  # 修不动了

        out.applied.extend(part.applied)
        current = part.healed_case

    out.healed_case = current
    return out


def render_report(out: HealOutcome) -> str:
    """把 HealOutcome 渲染成可读的自愈报告（供 CLI 打印）。"""
    lines = []
    if out.applied:
        lines.append("  ✅ 已自愈:")
        for a in out.applied:
            lines.append(f"     步骤{a.step_index} locator.{a.field}: {a.old!r} → {a.new!r}  ({a.reason})")
    if out.needs_review:
        lines.append("  ⚠️ 需人工评审（未自动修改，可能是真 bug / 路由上下文问题）:")
        for f in out.needs_review:
            lines.append(f"     - [{f.fail_type.value}] {f.by}={f.value}")
    if out.unhealable:
        lines.append("  ❌ 无法自愈（定位器在用例中无对应步骤，或漂移过大）:")
        for f in out.unhealable:
            lines.append(f"     - [{f.fail_type.value}] {f.by}={f.value}")
    verdict = "PASS ✅（自愈后通过）" if out.final_ok else "FAIL ❌（仍有未解决失败）"
    lines.append(f"  结论: {verdict}  | 共执行 {len(out.runs)} 轮")
    return "\n".join(lines)
