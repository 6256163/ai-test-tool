"""generator/llm_client.py
LLM 封装：结构化输出（JSON Schema）+ 脱敏 + 重试。

设计要点（对应你画的「数据安全 / 不白做」避坑）：
- 结构化输出：让模型直接吐符合 Case schema 的 JSON，省掉脆弱的纯文本解析。
- 脱敏：非私有化模型时，出域前把凭据/手机号/邮箱/身份证换成占位符（详见 security/secrets.py）。
  真值留在本机的 `self.last_secrets`，由调用方在 codegen 前回填 —— 只做 mask 不回填会让
  LLM 把 `${PHONE}` 当输入值写死在用例里，反而让用例必然失败。
- 重试：限流(429)与超时做指数退避，避免偶发抖动导致整条流水线挂掉。
"""
from __future__ import annotations

import json
import time

import httpx
from pydantic import ValidationError

from ingest.schema import Case
from security.secrets import extract_secrets, mask_sensitive  # noqa: F401  (mask_sensitive 兼容旧引用)


_DEFAULT_SYS = """你是一名资深测试开发工程师。根据用户给出的需求/页面描述，生成「一条」结构化 UI 自动化测试用例（Web 端，Playwright）。
严格遵守输出 JSON schema（字段名、类型、枚举值）。规则：
1. 步骤覆盖：进入页面 → 关键操作 → 断言。至少包含 1 个断言步骤（assert_*）。
2. 定位器优先用 testid（data-testid）；若用户消息附带了「被测前端可用定位器清单」，则 locator 必须且只能从该清单里选 testid，严禁自创清单中不存在的 testid；清单未覆盖的元素才退而用 text（可见中文文案）或 role。
2.1 若清单里标注了「以下 testid 位于列表中，同名元素会出现多个」，则操作这些 testid 时必须在 locator 里带 index（0=第一个，-1=最后一个，正整数=第 n+1 个）；不给 index 会因 Playwright strict mode 直接报错。唯一元素不要填 index。
3. value 填实际输入值；expect 填断言期望文本（按 to_contain_text 语义）。
4. coverage_tags 从 [冒烟, 主流程, 边界, 异常, 回归] 中选 1~3 个。
5. base_path 填首屏路径，如 /login。
6. 只输出 JSON，不要任何解释文字。"""

# 移动端（原生 App，Appium）专用提示词：把平台约束讲清，否则模型会在 app 上写 hover/navigate。
_SYS_APP = """你是一名资深移动端测试开发工程师。目标被测对象是「原生 App」（Appium，Android/iOS）。
严格按照输出 JSON schema 生成「一条」结构化测试用例。规则：
1. 进入应用用 launch（启动/切回 app），不要用 navigate（原生 App 没有 URL 导航）。
2. 不要使用 hover（触屏没有悬停）；滚动用 scroll；手势滑动用 swipe；长按用 long_press。
3. 下拉选择在原生的 picker 完成，不要用 select。
4. 定位器优先用 testid（对应 accessibility id / content-desc）；若用户消息附带了「被测前端可用定位器清单」，则必须且只能从清单选 testid，严禁自创；清单未覆盖的元素才退而用 text（可见文案）或 xpath；不要凭空捏造元素。
4.1 清单中标注会重复出现的 testid（列表/表单元格），必须在 locator 里带 index；唯一元素不要填 index。
5. 断言可用 assert_text / assert_visible / assert_count；原生 App 没有 URL，不要用 assert_url。
6. 步骤覆盖：进入应用 → 关键操作 → 断言。至少包含 1 个断言。
7. coverage_tags 从 [冒烟, 主流程, 边界, 异常, 回归] 中选 1~3 个。
8. 只输出 JSON，不要任何解释文字。"""

# 平台 → 系统提示词
_SYS_BY_PLATFORM = {"web": _DEFAULT_SYS, "app": _SYS_APP}


class LLMClient:
    def __init__(self, base_url, api_key, model, *,
                 max_retries=3, timeout=60.0, desensitize=False):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.max_retries = max_retries
        self.timeout = timeout
        self.desensitize = desensitize
        # 出域前抽出的真值（占位符 → 真值），仅供本机回填使用，不随请求发出
        self.last_secrets: dict[str, str] = {}

    # ----- 底层 chat（带重试）-----
    def chat(self, messages, *, temperature=0.2, response_schema=None):
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if response_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "test_case", "schema": response_schema},
            }

        last_err = None
        for attempt in range(self.max_retries):
            try:
                resp = httpx.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"]
            except (httpx.TimeoutException, httpx.HTTPStatusError, httpx.TransportError) as e:
                last_err = e
                # 限流退避更久，其它错误短退避
                wait = (2 ** attempt) + (1 if getattr(e, "response", None)
                                        and e.response.status_code == 429 else 0)
                if attempt < self.max_retries - 1:
                    time.sleep(wait)
        raise RuntimeError(f"LLM 调用失败（已重试 {self.max_retries} 次）: {last_err}")

    # ----- 提示词组装（不发起网络，便于预览/调试）-----
    def build_messages(self, requirement: str, *, system_prompt: str | None = None,
                       platform: str = "web", dom_hints: str | None = None,
                       cred_slots: str | None = None) -> tuple[list[dict], dict]:
        """组装「将发给 LLM 的完整提示词」，返回 (messages, schema)。

        与 generate_case 实际发出的内容完全一致（含脱敏后的文案、DOM 目录 hints、
        凭据槽位）。不发起任何网络请求，用于：调试提示词、缺少 API Key 时预览。
        """
        # P1：把前端定位器目录、凭据槽位作为 hints 注入，约束 LLM 不许自创
        user_content = requirement
        for block in (dom_hints, cred_slots):
            if block:
                user_content = f"{user_content}\n\n{block}"

        # 平台决定系统提示词：app 端引导避免 hover/navigate 等 web-only 动作
        sys_prompt = system_prompt or _SYS_BY_PLATFORM.get(platform, _DEFAULT_SYS)
        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_content},
        ]
        return messages, Case.model_json_schema()

    # ----- 结构化生成用例 -----
    def generate_case(self, requirement: str, *, system_prompt: str | None = None,
                      platform: str = "web", dom_hints: str | None = None,
                      cred_slots: str | None = None) -> Case:
        if self.desensitize:
            # 抽出凭据/手机号等真值 → 占位符；真值存 self.last_secrets，出网的只有占位符
            requirement, self.last_secrets = extract_secrets(requirement)

        messages, schema = self.build_messages(
            requirement, system_prompt=system_prompt, platform=platform,
            dom_hints=dom_hints, cred_slots=cred_slots,
        )

        raw = self.chat(messages, response_schema=schema)
        try:
            return Case.model_validate(_extract_json(raw))
        except (ValidationError, ValueError) as e:
            # 兜底：把报错反馈给模型，让它修一次（典型因枚举值写错/字段缺失）。
            # 直接在已组装的 messages 上追加 assistant/user，避免二次拼装漂移。
            fix = self.chat(
                messages + [
                    {"role": "assistant", "content": raw},
                    {"role": "user",
                     "content": f"返回不符合 schema，请修正后只输出 JSON：\n{e}"},
                ],
                response_schema=schema,
            )
            return Case.model_validate(_extract_json(fix))


def _extract_json(text: str):
    """从模型输出里抠出 JSON（兼容 ```json 代码块 与 裸 JSON）。"""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1:
        text = text[start:end + 1]
    return json.loads(text)
