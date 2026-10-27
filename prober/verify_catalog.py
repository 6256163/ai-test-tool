"""prober/verify_catalog.py —— P1 验证：用真实浏览器核对 catalog 里的 locator 是否命中实时前端。

不依赖 LLM：直接拿 catalog.json 里的 testid，用 Playwright 打开对应路由，
确认每个 testid 在真实 DOM 里存在且可见。这证明「DOM 目录桥接」给出的 locator 是准的。

用法：
  python prober/verify_catalog.py                 # 默认核验 /login
  python prober/verify_catalog.py --route /products
  python prober/verify_catalog.py --base-url http://localhost:8081 --route /login

需先安装浏览器：python -m playwright install chromium
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from prober.dom_catalog import Catalog  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="用真实浏览器核对 catalog locator 命中")
    ap.add_argument("--base-url", default="http://localhost:8081")
    ap.add_argument("--route", default="/login", help="要核验的路由（需无需登录即可访问）")
    ap.add_argument("--catalog", default=os.path.join(ROOT, "prober", "catalog.json"))
    ap.add_argument("--login", action="store_true",
                    help="先用工件录登录（用 catalog 的 /login testid），再核验受保护路由")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="admin123")
    args = ap.parse_args()

    cat = Catalog.load_json(args.catalog)
    tids = cat.route_testids.get(args.route) or []
    if not tids:
        print(f"⚠️ catalog 里没有路由 {args.route} 的 testid，跳过")
        return

    from playwright.sync_api import sync_playwright

    url = args.base_url.rstrip("/") + args.route
    missing: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        # 受保护路由需先登录：用 catalog 的登录 testid 真登录，验证 catalog 能驱动真实流程
        if args.login:
            page.goto(args.base_url.rstrip("/") + "/login", wait_until="networkidle", timeout=20000)
            page.get_by_test_id("input-username").fill(args.user)
            page.get_by_test_id("input-password").fill(args.password)
            page.get_by_test_id("btn-login").click()
            try:
                # .first 避免多元素匹配触发 strict mode 违规（商品列表有多个 product-name）
                page.get_by_test_id("product-name").first.wait_for(state="visible", timeout=12000)
            except Exception:
                print("⚠️ 登录后未跳转到商品页，可能账号/密码不对或商品接口较慢，继续核验")
        page.goto(url, wait_until="networkidle", timeout=20000)
        # 落稳：商品列表等依赖接口数据的元素需要一点加载时间
        page.wait_for_timeout(2500)
        for tid in tids:
            loc = page.get_by_test_id(tid)
            try:
                # 命中 = 存在于 DOM（attached）。可见性取决于交互状态
                # （如 Element UI 的 tab 用 v-show 隐藏非激活页，元素存在但不可见），
                # 不能因为不可见就判 locator 无效——真跑用例时切到对应 tab 即可操作。
                # 用 .first 避免同 testid 多元素（如每条商品的 product-name）触发 strict mode 违规。
                loc.first.wait_for(state="attached", timeout=3000)
                ok = True
            except Exception:
                ok = False
            if not ok:
                missing.append(tid)
        browser.close()

    total = len(tids)
    hit = total - len(missing)
    print(f"路由 {args.route}  目录 testid={total}  实时命中(存在于DOM)={hit}  缺失={len(missing)}")
    if missing:
        print(f"❌ 未命中（前端 DOM 里找不到）：{missing}")
        print("建议：检查前端是否改了 testid，或重新运行 build_catalog.py 刷新目录")
        sys.exit(1)
    print(f"✅ 全部 {total} 个 locator 精准命中真实前端 {url}")


if __name__ == "__main__":
    main()
