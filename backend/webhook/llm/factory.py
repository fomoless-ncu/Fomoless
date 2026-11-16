"""依環境設定建立活動查詢解析器。"""

import os
from collections.abc import Callable

from .activity_query import ActivityQueryParser


def required_setting(name: str) -> str:
    """讀取必要設定，缺少時回報設定名稱。"""
    value = os.getenv(name, "").strip()

    if not value:
        raise ValueError(f"缺少環境設定：{name}")

    return value


def create_query_parser(
    get_secret: Callable[[str], str],
) -> ActivityQueryParser:
    """依必要設定 AI_PROVIDER 建立解析器，缺少設定時回報錯誤。"""
    provider = required_setting("AI_PROVIDER").lower()

    if provider == "azure":
        from .azure_query import AzureActivityQueryParser

        return AzureActivityQueryParser(
            base_url=required_setting("AZURE_OPENAI_BASE_URL"),
            deployment=required_setting("AZURE_OPENAI_DEPLOYMENT"),
            api_key=get_secret("azure-openai-api-key"),
        )

    if provider == "gemini":
        from .gemini_query import GeminiActivityQueryParser

        return GeminiActivityQueryParser(
            model=required_setting("GEMINI_MODEL"),
            api_key=get_secret("gemini-api-key"),
        )

    raise ValueError(f"不支援的 AI_PROVIDER：{provider}")
