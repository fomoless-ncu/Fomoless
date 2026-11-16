"""使用 Gemini 解析活動查詢條件。"""

from google import genai
from google.genai import types

from .activity_query import (       # 繼承 ActivityQueryParser
    ActivityQuery,
    ActivityQueryParser,
    QUERY_PROMPT,
)


class GeminiActivityQueryParser(ActivityQueryParser):
    """ActivityQueryParser 的 Gemini 實作。"""

    def __init__(self, *, api_key: str, model: str):
        self.api_key = api_key
        self.model = model

    def parse(self, text: str) -> ActivityQuery:
        """呼叫 Gemini，取得經驗證的活動查詢條件。"""

        options = types.HttpOptions(
            # Gemini 伺服器要求至少 10 秒期限，客戶端仍在 8 秒後逾時。
            headers={"X-Server-Timeout": "10"},
            timeout=8000,
            retry_options=types.HttpRetryOptions(attempts=1),
        )

        with genai.Client(
            api_key=self.api_key,
            http_options=options,
        ) as client:
            response = client.models.generate_content(
                model=self.model,
                contents=text,
                config=types.GenerateContentConfig(
                    system_instruction=QUERY_PROMPT,
                    response_mime_type="application/json",
                    response_json_schema=ActivityQuery.model_json_schema(),
                ),
            )

        return ActivityQuery.model_validate_json(response.text or "")
