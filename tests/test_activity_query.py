"""驗證查詢條件與供應商實作，API 回應由 mock 提供。

pytest 會執行 test_ 開頭的函式，assert 用來檢查結果是否符合預期。
parametrize 會讓同一個測試套用多組資料，每一組都算一個測試案例。
patch 會暫時替換 SDK 用戶端，讓測試能指定 API 回應及重現錯誤情況。
"""

import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # shared.models
sys.path.insert(0, str(ROOT / "backend" / "webhook"))  # llm.*，和 app.py 相同的匯入方式

from llm.activity_query import ActivityQuery, QUERY_PROMPT  # noqa: E402
from llm.azure_query import AzureActivityQueryParser  # noqa: E402
from llm.gemini_query import GeminiActivityQueryParser  # noqa: E402
from llm.factory import create_query_parser  # noqa: E402
from shared.models import Mode, PassportCondition, RegistrationMethod  # noqa: E402


def test_factory_requires_provider(monkeypatch):
    """缺少供應商設定時回報錯誤，且不讀取 API 金鑰。"""
    monkeypatch.delenv("AI_PROVIDER", raising=False)
    get_secret = Mock()

    with pytest.raises(ValueError, match="AI_PROVIDER"):
        create_query_parser(get_secret)
    get_secret.assert_not_called()


@pytest.mark.parametrize(
    "provider, parser_type, secret_name",
    [
        ("azure", AzureActivityQueryParser, "azure-openai-api-key"),
        ("gemini", GeminiActivityQueryParser, "gemini-api-key"),
    ],
)
def test_factory_selects_provider(monkeypatch, provider, parser_type, secret_name):
    """確認工廠依設定建立對應的解析器，並要求正確名稱的金鑰。"""
    monkeypatch.setenv("AI_PROVIDER", provider)
    monkeypatch.setenv("AZURE_OPENAI_BASE_URL", "https://example.test/openai/v1/")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "test-deployment")
    monkeypatch.setenv("GEMINI_MODEL", "test-model")
    get_secret = Mock(return_value="test-key")

    assert isinstance(create_query_parser(get_secret), parser_type)
    get_secret.assert_called_once_with(secret_name)


@pytest.mark.parametrize(
    "provider, missing_setting",
    [
        ("azure", "AZURE_OPENAI_BASE_URL"),
        ("azure", "AZURE_OPENAI_DEPLOYMENT"),
        ("gemini", "GEMINI_MODEL"),
    ],
)
def test_factory_reports_missing_setting(monkeypatch, provider, missing_setting):
    """必要設定缺少時，錯誤應包含設定名稱，便於排查本機與部署問題。"""
    monkeypatch.setenv("AI_PROVIDER", provider)
    monkeypatch.setenv("AZURE_OPENAI_BASE_URL", "https://example.test/openai/v1/")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "test-deployment")
    monkeypatch.setenv("GEMINI_MODEL", "test-model")
    monkeypatch.delenv(missing_setting, raising=False)

    with pytest.raises(ValueError, match=missing_setting):
        create_query_parser(Mock(return_value="test-key"))


def test_factory_rejects_unknown_provider(monkeypatch):
    """不支援的供應商設定應直接回報錯誤。"""
    monkeypatch.setenv("AI_PROVIDER", "unknown")

    with pytest.raises(ValueError, match="AI_PROVIDER"):
        create_query_parser(Mock())


def test_unspecified_filters_remain_unrestricted():
    """只指定本週時，福利應為空清單，其他條件應為 None，避免意外限縮查詢。"""
    query = ActivityQuery(period="this_week")

    assert query.perks == []
    assert all(
        value is None
        for name, value in query.model_dump().items()
        if name not in ("period", "perks")
    )


@pytest.mark.parametrize(
    "field, value",
    [("mode", item.value) for item in Mode]
    + [("registration_method", item.value) for item in RegistrationMethod]
    + [("learning_passport_condition", item.value) for item in PassportCondition],
)
def test_query_accepts_shared_model_codes(field, value):
    """逐一驗證共用模型的活動形式、報名方式及認證方式代碼可作為查詢條件。"""
    query = ActivityQuery(period="next_week", **{field: value})

    assert getattr(query, field) == value


@pytest.mark.parametrize(
    "filters",
    [
        {"period": "tomorrow"},  # 目前時間範圍尚未支援 tomorrow。
        {"mode": "virtual"},  # 活動形式代碼必須使用共用模型中的值。
        {"registration_method": "open"},  # 報名狀態不能填進報名方式。
        {"learning_passport_condition": "signed"},  # 未定義的護照認證代碼。
        {"perks": "便當"},  # 福利應為字串清單，而非單一字串。
        {"host": 123},  # 承辦單位應為文字，嚴格模式會拒絕數字。
        {"invented_field": True},  # extra="forbid" 應拒絕模型以外的欄位。
    ],
)
def test_query_rejects_invalid_values(filters):
    """確認每一組非法資料都會拋出 Pydantic ValidationError。"""
    with pytest.raises(ValidationError):
        ActivityQuery.model_validate({"period": "this_week", **filters})


@pytest.fixture
def expected_query():
    """提供兩個供應商測試共用的預期結果，讓新增的查詢欄位都能被檢查。

    fixture 是 pytest 提供測試資料的機制，這個函式本身不計為一個測試。
    下列內容是指定給 mock 的資料，用來確認欄位傳遞與驗證行為。
    """
    return ActivityQuery(
        period="next_week",
        title="研究方法工作坊",
        host="文學院學士班",
        speaker="測試講者",
        location="文學一館",
        mode="onsite",
        registration_method="external",
        perks=["餐券"],
        learning_passport_category="人文藝術",
        learning_passport_condition="both",
        soft_skill_category="跨域整合",
    )


def test_azure_returns_expanded_query(expected_query):
    """確認 Azure 解析器能回傳完整查詢，並將共用模型與提示詞傳給 SDK。"""
    with patch("llm.azure_query.OpenAI") as factory:
        # 對應正式程式的 with OpenAI(...) as client，取得模擬用戶端。
        client = factory.return_value.__enter__.return_value
        # 指定 SDK 已解析的結果，後續 parser.parse() 應將它傳回。
        client.responses.parse.return_value.output_parsed = expected_query
        parser = AzureActivityQueryParser(
            base_url="https://example.test/openai/v1/",
            api_key="test-key",
            deployment="test-deployment",
        )

        assert parser.parse("下週文學院學士班主辦的活動") == expected_query
        arguments = client.responses.parse.call_args.kwargs
        assert arguments["text_format"] is ActivityQuery
        assert arguments["instructions"] == QUERY_PROMPT


def test_azure_rejects_missing_parsed_response():
    """模擬 Azure 未提供解析結果，確認解析器以 ValueError 回報失敗。"""
    with patch("llm.azure_query.OpenAI") as factory:
        client = factory.return_value.__enter__.return_value
        client.responses.parse.return_value.output_parsed = None
        parser = AzureActivityQueryParser(
            base_url="https://example.test/openai/v1/",
            api_key="test-key",
            deployment="test-deployment",
        )

        with pytest.raises(ValueError):
            parser.parse("下週活動")


def test_gemini_validates_expanded_query(expected_query):
    """確認 Gemini JSON 回應能轉成完整查詢，且請求使用共用模型的 JSON Schema。"""
    with patch("llm.gemini_query.genai.Client") as factory:
        client = factory.return_value.__enter__.return_value
        # Gemini 回傳 JSON 文字，解析器需透過 Pydantic 驗證並還原成物件。
        client.models.generate_content.return_value.text = expected_query.model_dump_json()
        parser = GeminiActivityQueryParser(api_key="test-key", model="test-model")

        assert parser.parse("下週文學院學士班主辦的活動") == expected_query
        # 伺服器期限至少 10 秒，避免實際 API 因期限過短回傳 400。
        options = factory.call_args.kwargs["http_options"]
        assert int(options.headers["X-Server-Timeout"]) >= 10
        config = client.models.generate_content.call_args.kwargs["config"]
        assert config.response_json_schema == ActivityQuery.model_json_schema()


@pytest.mark.parametrize(
    "response_text",
    [
        None,  # API 回應沒有文字。
        "invalid JSON",  # 回應文字不是合法 JSON。
        '{"period":"next_week","mode":"virtual"}',  # JSON 內含非法代碼。
        '{"period":"next_week","invented_field":true}',  # JSON 內含額外欄位。
    ],
)
def test_gemini_rejects_invalid_response(response_text):
    """確認空白、格式錯誤或不符合模型的 Gemini 回應都會被驗證拒絕。"""
    with patch("llm.gemini_query.genai.Client") as factory:
        client = factory.return_value.__enter__.return_value
        client.models.generate_content.return_value.text = response_text
        parser = GeminiActivityQueryParser(api_key="test-key", model="test-model")

        with pytest.raises(ValidationError):
            parser.parse("下週活動")
