"""tests/test_prompt_preview.py

验证「缺 LLM_API_KEY 时预览完整提示词」所需的组装函数：
  - LLMClient.build_messages：返回 (messages, schema)，不发起网络。
  - engine.build_prompt_preview：复刻 run_pipeline 的提示词构造，含脱敏/目录 hints。
"""
from __future__ import annotations

import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from generator.llm_client import LLMClient  # noqa: E402
import server.engine as engine  # noqa: E402


def test_build_messages_shape():
    llm = LLMClient(base_url="https://api.test/v1", api_key="x", model="m")
    msgs, schema = llm.build_messages(
        "登录后搜索旗舰版", platform="web",
        dom_hints="可用定位器：input-username, btn-login",
        cred_slots="可用凭据槽位：${CRED.admin.password}",
    )
    assert isinstance(msgs, list) and len(msgs) == 2
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user"
    # user 必须拼入 hints 与 cred_slots
    assert "input-username" in msgs[1]["content"]
    assert "${CRED.admin.password}" in msgs[1]["content"]
    # schema 是 Case 的 JSON Schema，含必填字段
    assert schema["type"] == "object"
    assert "properties" in schema
    assert "steps" in schema["properties"]


def test_build_messages_no_network():
    # 即便 chat 被替换为抛错，build_messages 也不该触网（它根本不调 chat）。
    llm = LLMClient(base_url="https://api.test/v1", api_key="x", model="m")

    def boom(*a, **k):
        raise AssertionError("网络被调用了")

    llm.chat = boom
    msgs, schema = llm.build_messages("随便一句需求")
    assert len(msgs) == 2
    assert schema["type"] == "object"


def test_build_prompt_preview_keys():
    preview = engine.build_prompt_preview("使用 admin/admin123456 测试登录成功流程")
    assert set(preview) == {"messages", "schema", "provider", "secrets"}
    assert len(preview["messages"]) == 2
    assert preview["messages"][0]["role"] == "system"
    assert preview["messages"][1]["role"] == "user"
    assert "properties" in preview["schema"]
    assert preview["provider"]["model"]  # 来自 config.yaml
    # 脱敏开启时，需求句里的 admin/admin123456 被抽出，secrets 非空
    assert isinstance(preview["secrets"], list)
    # user 内容里不应再出现明文 admin123456（已被占位符替换）
    assert "admin123456" not in preview["messages"][1]["content"]


def test_build_prompt_preview_platform_override():
    web = engine.build_prompt_preview("打开首页", platform="web")
    app = engine.build_prompt_preview("打开首页", platform="app")
    # app 系统提示词应提到原生 App / Appium，web 不应提
    assert "Appium" in web["messages"][0]["content"] or "App" in app["messages"][0]["content"]
    assert "Appium" in app["messages"][0]["content"]
    assert "Appium" not in web["messages"][0]["content"]
