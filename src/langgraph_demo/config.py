"""集中读取并校验真实模型配置。

本模块只读取环境变量，不在代码中放置任何默认密钥。为了兼容参考工程，
除标准的 ``OPENAI_BASE_URL`` 外，也接受旧名称 ``OPENAI_API_BASE``。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


class ModelConfigurationError(RuntimeError):
    """模型环境变量缺失或格式错误时抛出的、便于学习者理解的异常。"""


@dataclass(frozen=True)
class ModelSettings:
    """创建 ChatOpenAI 所需的最小配置集合。"""

    api_key: str
    model: str
    base_url: str | None
    temperature: float


def load_model_settings() -> ModelSettings:
    """从 ``.env`` 和当前进程环境读取模型配置。

    ``load_dotenv`` 默认不会覆盖操作系统中已经存在的变量，因此在服务器、CI
    或 Studio 中注入的环境变量具有更高优先级。
    """

    load_dotenv()

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    model = os.getenv("OPENAI_MODEL", "").strip()
    base_url = (
        os.getenv("OPENAI_BASE_URL", "").strip()
        or os.getenv("OPENAI_API_BASE", "").strip()
        or None
    )
    raw_temperature = os.getenv("OPENAI_TEMPERATURE", "0").strip()

    missing: list[str] = []
    if not api_key:
        missing.append("OPENAI_API_KEY")
    if not model:
        missing.append("OPENAI_MODEL")
    if missing:
        joined = "、".join(missing)
        raise ModelConfigurationError(
            f"缺少模型配置：{joined}。请复制 .env.example 为 .env 后填写真实值。"
        )

    try:
        temperature = float(raw_temperature)
    except ValueError as exc:
        raise ModelConfigurationError(
            "OPENAI_TEMPERATURE 必须是数字，例如 0、0.2 或 1。"
        ) from exc

    if not 0 <= temperature <= 2:
        raise ModelConfigurationError("OPENAI_TEMPERATURE 必须位于 0 到 2 之间。")

    return ModelSettings(
        api_key=api_key,
        model=model,
        base_url=base_url,
        temperature=temperature,
    )

