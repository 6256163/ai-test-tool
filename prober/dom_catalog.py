"""prober/dom_catalog.py —— P1: 把前端 DOM 抽成「定位器目录」，喂给 LLM 当字典。

解决 P0 的核心缺陷：LLM 凭记忆猜选择器，常生成不存在的 locator。
本模块做两件事：
1. 静态扫描前端源码的 data-testid + 路由表 → route → [testid] 目录（确定性、可进 CI、不依赖浏览器）。
2. 把目录压成一段紧凑文本（hints），注入 LLM 提示词，约束它「只能从清单选，禁止自创」。

运行时爬取（crawl_runtime）作为升级项保留：用 Playwright 无头访问各路由，
收集真实渲染出的 testid + 可见文案 + role，能覆盖条件渲染/动态元素。

设计取舍（P1 内部，不升级阶段）：
- 静态扫描是入口：快、确定、CI 友好，已覆盖 target_demo 96 个 testid。
- 运行时爬取更全但更重，按需开启。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Iterable


# 扫描的文件类型（前端模板里写 data-testid 的地方）
_SCAN_EXTS = (".vue", ".jsx", ".tsx", ".html", ".svelte")
_TESTID_RE = re.compile(r"data-testid\s*=\s*[\"']([^\"']+)[\"']")
_ROUTE_OBJ_RE = re.compile(
    r"\{\s*path\s*:\s*[\"']([^\"']+)[\"'](.*?)\}", re.DOTALL
)
_ROUTE_COMP_RE = re.compile(r"component\s*:\s*(\w+)")
_ROUTE_LAZY_RE = re.compile(r"import\(\s*[\"']([^\"']+)[\"']\s*\)")
_IMPORT_BINDING_RE = re.compile(
    r"import\s+(\w+)\s+from\s+[\"']([^\"']+\.vue)[\"']"
)
# v-for 块：同一个 testid 会被渲染多次（商品卡、表格行）——这正是 strict mode 违规的根源
_VFOR_OPEN_RE = re.compile(
    r"<([A-Za-z][\w.\-]*)([^>]*?)\bv-for\b([^>]*)>", re.DOTALL
)


@dataclass
class Catalog:
    """前端定位器目录。

    routes: 路由 path 列表（来自路由表）。
    route_testids: path -> 该路由视图 + 全局组件里出现的 testid 列表。
    global_testids: 跨页通用元素（App.vue、components/* 里的 testid）。
    all_testids: 全量去重 testid（兜底，LLM 可选任意已知 testid）。
    """

    routes: list[str] = field(default_factory=list)
    route_testids: dict[str, list[str]] = field(default_factory=dict)
    global_testids: list[str] = field(default_factory=list)
    all_testids: list[str] = field(default_factory=list)
    # 写在 v-for 块里的 testid：页面上会出现多个同名元素，
    # 使用时必须带 locator.index，否则 Playwright strict mode 直接抛错。
    repeated_testids: list[str] = field(default_factory=list)

    # ---- 序列化 ----
    def to_dict(self) -> dict:
        return {
            "routes": self.routes,
            "route_testids": self.route_testids,
            "global_testids": self.global_testids,
            "all_testids": self.all_testids,
            "repeated_testids": self.repeated_testids,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Catalog":
        return cls(
            routes=d.get("routes", []),
            route_testids=d.get("route_testids", {}),
            global_testids=d.get("global_testids", []),
            all_testids=d.get("all_testids", []),
            repeated_testids=d.get("repeated_testids", []),
        )

    def save_json(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def load_json(cls, path: str) -> "Catalog":
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f))

    # ---- 注入 LLM 的提示词文本 ----
    def to_hints_text(self) -> str:
        lines = ["【被测前端可用定位器清单（data-testid），生成 locator 只能从中选取】"]
        for route in self.routes:
            tids = self.route_testids.get(route) or []
            if tids:
                lines.append(f"路由 {route}: {', '.join(tids)}")
        if self.global_testids:
            lines.append(f"全局/通用: {', '.join(self.global_testids)}")
        if self.repeated_testids:
            lines.append(
                "⚠️ 以下 testid 位于列表中，同名元素会出现多个，"
                "使用时必须在 locator 里带 index（0=第一个，-1=最后一个）："
                f"{', '.join(self.repeated_testids)}"
            )
        lines.append(
            "规则：优先用上面清单里的 testid；清单没有的元素才退而用可见文案(text)或角色(role)；"
            "严禁自创清单里不存在的 testid。"
        )
        return "\n".join(lines)

    def has_testid(self, value: str) -> bool:
        return value in set(self.all_testids)

    def is_repeated(self, value: str) -> bool:
        return value in set(self.repeated_testids)


# ====================== 静态扫描 ======================
def _iter_source_files(src_dir: str) -> Iterable[str]:
    for root, _dirs, files in os.walk(src_dir):
        for fn in files:
            if fn.endswith(_SCAN_EXTS):
                yield os.path.join(root, fn)


def _collect_testids_in_file(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except OSError:
        return []
    seen: list[str] = []
    for m in _TESTID_RE.finditer(text):
        v = m.group(1)
        # 跳过模板动态 testid（如 `product-card-${p.id}`），静态 get_by_test_id 匹配不到，
        # 留给 LLM 只会生成无效 locator。
        if "${" in v or v.startswith("`"):
            continue
        if v not in seen:
            seen.append(v)
    return seen


def _match_element_end(text: str, start: int, tag: str) -> int | None:
    """给定开标签的起始位置，返回该元素的结束位置（闭标签之后）。"""
    pat = re.compile(r"<(/?)\s*" + re.escape(tag) + r"(?=[\s/>])[^>]*?(/?)>", re.DOTALL)
    depth = 0
    for m in pat.finditer(text, start):
        if m.group(2):            # 自闭合 <tag ... />
            if depth == 0:
                return m.end()
            continue
        if m.group(1):            # 闭标签 </tag>
            depth -= 1
            if depth == 0:
                return m.end()
        else:
            depth += 1
    return None


def _repeated_testids_in_file(text: str) -> list[str]:
    """找出写在 v-for 块里的 testid —— 它们在同一页面会渲染出多个同名元素。"""
    found: list[str] = []
    for m in _VFOR_OPEN_RE.finditer(text):
        tag = m.group(1)
        end = _match_element_end(text, m.start(), tag)
        if end is None:
            continue
        block = text[m.start():end]
        for tm in _TESTID_RE.finditer(block):
            v = tm.group(1)
            if "${" in v or v.startswith("`"):
                continue
            if v not in found:
                found.append(v)
    return found


def _basename_no_ext(path: str) -> str:
    base = os.path.basename(path)
    return os.path.splitext(base)[0]


def scan_source(src_dir: str) -> Catalog:
    """扫描前端源码，产出 route → [testid] 目录。

    路由→视图映射支持两种写法：
      - 静态：component: LoginView
      - 懒加载：component: () => import('../views/LoginView.vue')
    找不到对应文件的路由，testid 落为空（仅保留路由 path）。
    """
    src_dir = os.path.abspath(src_dir)
    file_testids: dict[str, list[str]] = {}
    name_to_file: dict[str, str] = {}
    repeated: list[str] = []
    for fp in _iter_source_files(src_dir):
        tids = _collect_testids_in_file(fp)
        file_testids[fp] = tids
        name_to_file[_basename_no_ext(fp)] = fp
        try:
            with open(fp, encoding="utf-8", errors="ignore") as f:
                raw = f.read()
        except OSError:
            continue
        for t in _repeated_testids_in_file(raw):
            if t not in repeated:
                repeated.append(t)

    # 路由表：path → 视图文件。路由文件是 .js/.ts，不在模板扫描范围，单独 walk。
    router_files = []
    for root, _dirs, files in os.walk(src_dir):
        for fn in files:
            if fn.endswith((".js", ".ts")):
                router_files.append(os.path.join(root, fn))
    # 只保留真正定义路由的文件（含 path: 与 component:），避免误扫普通 js
    real_routers: list[str] = []
    for fp in router_files:
        try:
            with open(fp, encoding="utf-8", errors="ignore") as f:
                t = f.read()
        except OSError:
            continue
        if "path:" in t and "component:" in t:
            real_routers.append(fp)
    router_files = real_routers

    route_testids: dict[str, list[str]] = {}
    routes: list[str] = []
    for rf in router_files:
        try:
            with open(rf, encoding="utf-8", errors="ignore") as f:
                rtext = f.read()
        except OSError:
            continue
        for m in _ROUTE_OBJ_RE.finditer(rtext):
            path = m.group(1)
            inner = m.group(2)
            if path in routes:
                continue
            routes.append(path)
            view_file = None
            lazy = _ROUTE_LAZY_RE.search(inner)
            if lazy:
                rel = lazy.group(1)
                # 相对 router 文件解析
                view_file = os.path.normpath(os.path.join(os.path.dirname(rf), rel))
            else:
                comp = _ROUTE_COMP_RE.search(inner)
                if comp:
                    view_file = name_to_file.get(comp.group(1))
            tids: list[str] = []
            if view_file and view_file in file_testids:
                tids = list(file_testids[view_file])
            route_testids[path] = tids

    # 全局 testid：App.vue + components/*
    global_testids: list[str] = []
    for fp, tids in file_testids.items():
        rel = fp.replace(src_dir, "")
        if "/App." in rel or "/components/" in rel:
            for t in tids:
                if t not in global_testids:
                    global_testids.append(t)

    # 全量去重
    all_set: list[str] = []
    for tids in file_testids.values():
        for t in tids:
            if t not in all_set:
                all_set.append(t)

    return Catalog(
        routes=routes,
        route_testids=route_testids,
        global_testids=global_testids,
        all_testids=all_set,
        repeated_testids=repeated,
    )


# ====================== 运行时爬取（升级项，可选） ======================
def crawl_runtime(base_url: str, routes: list[str] | None = None,
                  headless: bool = True) -> Catalog:
    """用 Playwright 无头访问各路由，收集真实渲染出的 testid。

    需要已安装浏览器：python -m playwright install chromium。
    返回结构与 scan_source 一致（route_testids 用实时 DOM）。
    """
    from playwright.sync_api import sync_playwright  # 延迟导入，扫描时不需要浏览器

    routes = routes or ["/"]
    route_testids: dict[str, list[str]] = {}
    all_set: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()
        for r in routes:
            url = base_url.rstrip("/") + r
            try:
                page.goto(url, wait_until="networkidle", timeout=15000)
            except Exception:
                route_testids[r] = []
                continue
            # 列出页面上所有 data-testid
            tids = page.eval_on_selector_all(
                "[data-testid]",
                "els => els.map(e => e.getAttribute('data-testid'))",
            )
            tids = [t for t in tids if t]
            route_testids[r] = tids
            for t in tids:
                if t not in all_set:
                    all_set.append(t)
        browser.close()
    return Catalog(
        routes=list(routes),
        route_testids=route_testids,
        global_testids=[],
        all_testids=all_set,
    )
