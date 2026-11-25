"""Azure OpenAI 推播文案實作，使用與既有查詢相同的 Responses API。"""

from openai import OpenAI

from .activity_push import ActivityPushWriter, PUSH_PROMPT, PushDraft, PushInput


class AzureActivityPushWriter(ActivityPushWriter):
    """base_url 為 Azure 的 /openai/v1/ 網址；deployment 使用實際部署名稱。"""

    def __init__(self, *, base_url: str, api_key: str, deployment: str):
        self.base_url = base_url
        self.api_key = api_key
        self.deployment = deployment

    def generate(self, request: PushInput) -> PushDraft:
        with OpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=8.0,
            max_retries=0,
        ) as client:
            response = client.responses.parse(
                model=self.deployment,
                instructions=PUSH_PROMPT,
                input=request.to_prompt(),
                text_format=PushDraft,
                max_output_tokens=2048,
                store=False,
            )

        if response.output_parsed is None:
            raise ValueError("Azure 未回傳可解析的推播文案")
        # 再驗證一次，保持兩個供應商的回傳契約一致。
        return PushDraft.model_validate(response.output_parsed.model_dump())
