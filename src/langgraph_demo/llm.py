"""真实 OpenAI 兼容聊天模型工厂。"""

from __future__ import annotations

from langchain_openai import ChatOpenAI

from langgraph_demo.config import ModelSettings, load_model_settings


def create_chat_model(settings: ModelSettings | None = None) -> ChatOpenAI:
    """创建支持流式输出和工具调用的真实聊天模型。

    图模块在被 Studio 导入时不应立即索取 API Key，因此各图只在节点第一次
    真正执行时调用本函数。测试则可以把模拟模型注入图构建函数，不经过这里。
    """

    active = settings or load_model_settings()
    return ChatOpenAI(
        api_key=active.api_key,
        model=active.model,
        base_url=active.base_url,
        temperature=active.temperature,
        streaming=True,
        max_retries=2,
    )

