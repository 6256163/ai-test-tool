"""ingest/parser.py —— 需求/代码/已有用例 → 结构化任务（P0 极简版）。

P0 不做复杂 NLP，先把「一句话需求」原样接住，后续阶段再增强
（识别页面对象、抽取字段、结合已有用例做 few-shot）。
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class Task(BaseModel):
    feature: str
    description: str
    target_url: str | None = None
    framework: str = "playwright"
    hints: list[str] = Field(default_factory=list)


def parse(user_input: str) -> Task:
    cleaned = (user_input or "").strip()
    first_line = cleaned.splitlines()[0] if cleaned else "未命名场景"
    feature = first_line[:40]
    return Task(feature=feature, description=cleaned)
