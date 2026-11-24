"""使用 Azure OpenAI 解析活動查詢條件。"""

from openai import OpenAI

from .activity_query import (       # 繼承 ActivityQueryParser
    ActivityQuery,
    ActivityQueryParser,
    QUERY_PROMPT,
)

class AzureActivityQueryParser(ActivityQueryParser):
    """ActivityQueryParser 的 Azure 實作。"""

    # 建立物件時會執行，將連線設定存進這個實例
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        deployment: str,
    ):
        self.base_url = base_url
        self.api_key = api_key
        self.deployment = deployment

    def parse(self, text: str) -> ActivityQuery:
        """呼叫 Azure，取得經驗證的活動查詢條件。"""

        with OpenAI(        # 建立 API 用戶端，離開 with 區塊時關閉連線資源
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=8.0,        # 逾時設為 8 秒
            max_retries=0,
        ) as client:
            response = client.responses.parse(      # 傳入使用者文字與共用提示詞
                model=self.deployment,
                instructions=QUERY_PROMPT,
                input=text,
                text_format=ActivityQuery,
            )

        query = response.output_parsed

        if query is None:
            raise ValueError("Azure 未回傳可解析的活動查詢條件")

        return query