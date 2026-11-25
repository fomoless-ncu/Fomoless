"""Gemini 推播文案實作；沿用查詢模組的 SDK 與逾時設定。"""

from google import genai
from google.genai import types

from .activity_push import ActivityPushWriter, PUSH_PROMPT, PushDraft, PushInput


class GeminiActivityPushWriter(ActivityPushWriter):
    """只處理一次模型請求；重試、備援及最終文字組裝交給呼叫端。"""

    def __init__(self, *, api_key: str, model: str):
        self.api_key = api_key
        self.model = model

    def generate(self, request: PushInput) -> PushDraft:
        options = types.HttpOptions(
            # 與現有 Gemini 查詢相同：伺服器期限至少 10 秒，客戶端 8 秒逾時。
            headers={"X-Server-Timeout": "10"},
            timeout=8000,
            retry_options=types.HttpRetryOptions(attempts=1),
        )
        with genai.Client(api_key=self.api_key, http_options=options) as client:
            response = client.models.generate_content(
                model=self.model,
                contents=request.to_prompt(),
                config=types.GenerateContentConfig(
                    system_instruction=PUSH_PROMPT,
                    response_mime_type="application/json",
                    response_json_schema=PushDraft.model_json_schema(),
                    max_output_tokens=2048,
                ),
            )

        # 空回應、非法 JSON、額外欄位、重複 ID 等都會在這裡被拒絕。
        return PushDraft.model_validate_json(response.text or "")
