"""prober/dump_prompt.py —— 打印「一句话需求」最终发给 LLM 的完整请求体。

用途：调试提示词 / 排查 LLM 为什么生成了某个 locator。
不调用真实模型（拦截 chat），只把 messages + response_format schema 打出来。

用法：
  python prober/dump_prompt.py "使用admin/admin123456测试登陆成功流程"
  python prober/dump_prompt.py "..." --no-hints     # 对比盲生成（不注入目录）
  python prober/dump_prompt.py "..." --json          # 输出完整请求 payload（含 schema）
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _preflight() -> None:
    """依赖自检：裸解释器跑本项目会缺 yaml/pydantic/httpx，给可执行提示而不是 traceback。"""
    missing = [m for m in ("yaml", "pydantic", "httpx")
               if importlib.util.find_spec(m) is None]
    if not missing:
        return
    print(f"❌ 当前解释器缺少依赖：{', '.join(missing)}", file=sys.stderr)
    print(f"   正在使用的 python：{sys.executable}", file=sys.stderr)
    print("\n请用装了依赖的虚拟环境执行。二选一：\n", file=sys.stderr)
    print("  # 1) 用已有的 aitest 环境（本机已装好）", file=sys.stderr)
    print('  python '
          'prober/dump_prompt.py "一句话需求"\n', file=sys.stderr)
    print("  # 2) 在项目内新建环境并装依赖", file=sys.stderr)
    print("  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt", file=sys.stderr)
    print('  .venv/bin/python prober/dump_prompt.py "一句话需求"', file=sys.stderr)
    sys.exit(2)


_preflight()

from main import _build_dom_hints, _framework_ctx, load_config  # noqa: E402
from ingest.parser import parse  # noqa: E402
from generator.llm_client import LLMClient  # noqa: E402


def build_payload(requirement: str, cfg: dict, *, use_hints: bool = True) -> dict:
    """复刻 main.run_pipeline 里构造 LLM 请求的过程，返回完整 payload。"""
    task = parse(requirement)
    fctx = _framework_ctx(cfg)
    hints = _build_dom_hints(cfg) if use_hints else None

    llm = LLMClient(
        base_url=cfg["llm"]["base_url"],
        api_key="<DUMMY>",
        model=cfg["llm"]["model"],
        desensitize=cfg["llm"].get("desensitize", False),
    )

    captured: dict = {}

    def fake_chat(messages, *, temperature=0.2, response_schema=None):
        # 只记录「首次请求」。若 stub 输出不合 schema，generate_case 会追加一次修复重试，
        # 那 4 条消息不是首发内容，覆盖掉会误导人。
        if "messages" not in captured:
            captured["messages"] = messages
            captured["temperature"] = temperature
            captured["response_schema"] = response_schema
        return '{"name":"stub"}'

    llm.chat = fake_chat  # 拦截网络，只取请求体
    cred_slots = None
    if use_hints:
        # 凭据槽位同样出网（只有槽位名，无真值）
        from security.secrets import cred_slots_text, load_profiles

        profiles, _w = load_profiles(cfg)
        cred_slots = cred_slots_text(profiles)
    try:
        llm.generate_case(task.description, platform=fctx["platform"],
                          dom_hints=hints, cred_slots=cred_slots)
    except Exception:
        pass  # stub 输出不合 schema 时 pydantic 会抛，但 payload 已被捕获

    payload = {
        "model": cfg["llm"]["model"],
        "temperature": captured.get("temperature", 0.2),
        "messages": captured.get("messages", []),
    }
    if captured.get("response_schema"):
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "test_case", "schema": captured["response_schema"]},
        }
    return {
        "payload": payload,
        "task": task.model_dump(),
        "context": fctx,
        "hints": hints,
        "secrets": sorted(llm.last_secrets.keys()),  # 出域前抽走、留在本机的敏感项
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="打印发给 LLM 的完整请求体")
    ap.add_argument("requirement", help="一句话需求")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--no-hints", action="store_true", help="不注入 DOM 目录（对比盲生成）")
    ap.add_argument("--json", action="store_true", help="输出完整 JSON payload（含 schema）")
    args = ap.parse_args()

    cfg = load_config(args.config)
    built = build_payload(args.requirement, cfg, use_hints=not args.no_hints)

    if args.json:
        print(json.dumps(built["payload"], ensure_ascii=False, indent=2))
        return

    print("=" * 70)
    print(f"parse()（本地原始输入，尚未脱敏）→ {json.dumps(built['task'], ensure_ascii=False)}")
    print(f"context → platform={built['context']['platform']}, base_url={built['context']['base_url']}")
    print(f"provider → {cfg['llm']['base_url']}  model={cfg['llm']['model']}  "
          f"stream=False  temperature={built['payload']['temperature']}")
    print(f"response_format → json_schema(test_case)  "
          f"schema_bytes={len(json.dumps(built['payload'].get('response_format', {}), ensure_ascii=False))}")
    if built.get("secrets"):
        print(f"脱敏 → 已从需求句抽出并留在本机：{', '.join('${'+k+'}' for k in built['secrets'])}")
    print("=" * 70)
    print(f"messages → {len(built['payload']['messages'])} 条（仅首次请求；"
          "若模型输出不合 schema，运行时会额外追加一次修复重试）")
    for m in built["payload"]["messages"]:
        print(f"\n───────── role={m['role']}  ({len(m['content'])} chars) ─────────")
        print(m["content"])


if __name__ == "__main__":
    main()
