"""在本機從 Lambda handler 入口驗證 LINE 簽章、解析、查詢與回覆流程。

執行 python -X utf8 -m pytest tests/test_lambda_local.py -v -s 可查看模擬回覆。
使用測試金鑰及 mock 外部服務，無須 AWS 登入或實際 LINE reply token。
"""

import base64
import hashlib
import hmac
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # shared.privacy；直接執行 pytest 時也能找到共用套件。
sys.path.insert(0, str(ROOT / "backend" / "webhook"))

import app
from llm.activity_query import ActivityQuery
from shared.privacy import contains_pii, redact_pii
from week import week_range


SECRET = "local-test-channel-secret"


def signed_event(text: str, encoded: bool = False) -> dict:
    """產生和 API Gateway 相同形狀的事件，使用測試 channel secret 計算簽章。"""
    body = json.dumps({
        "destination": "Udeadbeefdeadbeefdeadbeefdeadbeef",
        "events": [{
            "type": "message",
            "mode": "active",
            "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
            "webhookEventId": "01LOCALTEST0000000000000000",
            "deliveryContext": {"isRedelivery": False},
            "replyToken": "00000000000000000000000000000000",
            "source": {"type": "user", "userId": "U00000000000000000000000000000000"},
            "message": {"id": "1", "type": "text", "quoteToken": "q", "text": text},
        }],
    }, ensure_ascii=False)
    signature = base64.b64encode(
        hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).digest()
    ).decode()
    return {
        "version": "2.0",
        "headers": {"X-Line-Signature": signature},
        "body": base64.b64encode(body.encode()).decode() if encoded else body,
        "isBase64Encoded": encoded,
    }


@pytest.fixture
def local_services(monkeypatch, request):
    """提供本機 Lambda 設定及三個外部服務的替身，每次測試重建快取。"""
    settings = {
        "AI_PROVIDER": "gemini",
        "GEMINI_MODEL": "test-model",
        "GEMINI_API_KEY": "test-key",
        "AZURE_OPENAI_BASE_URL": "https://example.test/openai/v1/",
        "AZURE_OPENAI_DEPLOYMENT": "test-deployment",
        "AZURE_OPENAI_API_KEY": "test-key",
        "LINE_CHANNEL_SECRET": SECRET,
        "LINE_CHANNEL_ACCESS_TOKEN": "test-token",
    }
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(app, "EVENTS_TABLE", "local-test-events")
    caches = (app.get_secret, app.get_parser, app.get_line_config, app.get_query_parser)
    for cached in caches:
        cached.cache_clear()

    # 固定一般案例的日期，避免「只查一個月份」的斷言在月底偶發失敗。
    # 跨月案例可用 indirect parametrize 指定另一個日期。
    now = getattr(request, "param", datetime(2026, 11, 4, 6, tzinfo=timezone.utc))
    clock = Mock(wraps=datetime)
    clock.now.return_value = now
    monkeypatch.setattr(app, "datetime", clock)
    start, _ = week_range(now)
    next_monday = start + timedelta(days=7 - start.weekday())
    matching = {
        "title": "文學院講座", "host": "文學院", "perks": "提供便當",
        "start_at": (next_monday + timedelta(days=1, hours=12)).isoformat(),
        "status": "published",
    }
    table = Mock()
    table.query.return_value = {"Items": [matching, {**matching, "title": "管理學院講座", "host": "管理學院"}]}
    query = ActivityQuery(period="next_week", host="文學院", perks=["便當"])

    with patch.object(app.boto3, "resource") as dynamodb, \
         patch.object(app, "MessagingApi") as messaging, \
         patch("llm.gemini_query.genai.Client") as gemini, \
         patch("llm.azure_query.OpenAI") as azure, \
         patch.object(app, "query_bounds", wraps=app.query_bounds) as bounds:
        dynamodb.return_value.Table.return_value = table
        gemini_client = gemini.return_value.__enter__.return_value
        gemini_client.models.generate_content.return_value.text = query.model_dump_json()
        azure.return_value.__enter__.return_value.responses.parse.return_value.output_parsed = query
        yield {
            "table": table, "dynamodb": dynamodb, "messaging": messaging,
            "gemini": gemini, "gemini_client": gemini_client, "azure": azure,
            "bounds": bounds, "next_monday": next_monday,
        }

    for cached in caches:
        cached.cache_clear()


@pytest.mark.parametrize("provider", ["gemini", "azure"])
@pytest.mark.parametrize("encoded", [False, True])
def test_signed_lambda_query(local_services, monkeypatch, provider, encoded):
    """兩個供應商均從簽章事件走到 LINE 回覆，並涵蓋 API Gateway base64 本文。"""
    monkeypatch.setenv("AI_PROVIDER", provider)

    response = app.handler(signed_event("下週文學院主辦且提供便當的活動", encoded), None)

    assert response["statusCode"] == 200
    local_services["messaging"].return_value.reply_message.assert_called_once()
    request = local_services["messaging"].return_value.reply_message.call_args.args[0]
    text = request.messages[0].text
    assert "下週" in text and "文學院講座" in text
    assert "管理學院講座" not in text
    local_services["dynamodb"].assert_called_once_with("dynamodb")
    assert local_services["bounds"].call_args.args == (
        local_services["next_monday"], local_services["next_monday"] + timedelta(days=7),
    )
    expression = local_services["table"].query.call_args.kwargs["FilterExpression"]
    assert expression.get_expression()["values"][1] == "published"
    print(f"\n供應商 {provider}，base64={encoded}，HTTP {response['statusCode']}\n{text}")


@pytest.mark.parametrize("provider", ["gemini", "azure"])
@pytest.mark.parametrize("text", [
    "下週文學院主辦且提供便當的活動，聯絡王小姐 test@example.org 0912-345-678 分機12345",
    "下週文學院主辦且提供便當的活動",
    "下週文學院主辦的活動，網址 https://example.org/?id=0912345678",
])
def test_query_uses_shared_redaction_before_provider(local_services, monkeypatch, provider, text):
    """從 LINE 入口檢查兩家 SDK 的實際輸入；全部 API 與資料庫仍使用 mock。"""
    monkeypatch.setenv("AI_PROVIDER", provider)

    assert app.handler(signed_event(text), None)["statusCode"] == 200
    if provider == "gemini":
        arguments = local_services["gemini_client"].models.generate_content.call_args.kwargs
        model_input = arguments["contents"]
    else:
        client = local_services["azure"].return_value.__enter__.return_value
        model_input = client.responses.parse.call_args.kwargs["input"]

    assert model_input == redact_pii(text)
    assert not contains_pii(model_input)
    assert "王小姐" not in model_input
    assert "test@example.org" not in model_input
    assert "0912-345-678" not in model_input
    assert "分機12345" not in model_input
    # 不含個資的查詢及網址參數應保留原值，不改變既有篩選語意。
    if not contains_pii(text):
        assert model_input == text
    local_services["messaging"].return_value.reply_message.assert_called_once()


def test_invalid_signature_is_rejected(local_services):
    """錯誤簽章回傳 400，服務替身均不應被呼叫。"""
    event = signed_event("下週活動")
    event["headers"]["X-Line-Signature"] = "invalid-signature"

    assert app.handler(event, None)["statusCode"] == 400
    local_services["dynamodb"].assert_not_called()
    local_services["gemini"].assert_not_called()
    local_services["azure"].assert_not_called()
    local_services["messaging"].assert_not_called()


def test_simple_query_uses_ai(local_services):
    """即使只問本週活動，也先由 AI 解析查詢條件。"""
    query = ActivityQuery(period="this_week")
    local_services["gemini_client"].models.generate_content.return_value.text = query.model_dump_json()
    local_services["table"].query.return_value = {"Items": []}

    assert app.handler(signed_event("本週活動"), None)["statusCode"] == 200
    local_services["gemini_client"].models.generate_content.assert_called_once()
    local_services["azure"].assert_not_called()
    local_services["table"].query.assert_called_once()
    local_services["messaging"].return_value.reply_message.assert_called_once()
    request = local_services["messaging"].return_value.reply_message.call_args.args[0]
    assert request.messages[0].text == "接下來 7 天沒有符合查詢條件的活動。"


@pytest.mark.parametrize("local_services", [
    datetime(2026, 11, 25, 6, tzinfo=timezone.utc),
    datetime(2026, 12, 29, 6, tzinfo=timezone.utc),
], indirect=True)
def test_week_query_reads_both_months(local_services):
    """月底及年底的七天範圍都應查詢兩個月份，這不是重複查詢或備援重試。"""
    query = ActivityQuery(period="this_week")
    local_services["gemini_client"].models.generate_content.return_value.text = query.model_dump_json()
    local_services["table"].query.return_value = {"Items": []}

    assert app.handler(signed_event("本週活動"), None)["statusCode"] == 200
    assert local_services["table"].query.call_count == 2
    local_services["messaging"].return_value.reply_message.assert_called_once()


def test_missing_provider_uses_keyword_fallback(local_services, monkeypatch):
    """缺少 AI 設定時仍提供本週清單，並明確說明未套用篩選條件。"""
    monkeypatch.delenv("AI_PROVIDER")

    assert app.handler(signed_event("本週活動"), None)["statusCode"] == 200
    request = local_services["messaging"].return_value.reply_message.call_args.args[0]
    assert "接下來 7 天" in request.messages[0].text
    assert "未套用其他篩選條件" in request.messages[0].text
    local_services["table"].query.assert_called_once()
    local_services["gemini"].assert_not_called()
    local_services["azure"].assert_not_called()


def test_ai_failure_uses_keyword_fallback(local_services):
    """AI 逾時時提供基本清單，提醒原本的下週與福利條件未套用。"""
    local_services["gemini_client"].models.generate_content.side_effect = TimeoutError()

    assert app.handler(signed_event("下週有便當的活動"), None)["statusCode"] == 200
    request = local_services["messaging"].return_value.reply_message.call_args.args[0]
    assert "暫時無法解析查詢條件" in request.messages[0].text
    assert "接下來 7 天" in request.messages[0].text
    assert "未套用其他篩選條件" in request.messages[0].text
    local_services["table"].query.assert_called_once()


def test_ai_failure_without_keyword_sends_retry_message(local_services):
    """AI 失效且沒有活動關鍵字時，不查詢資料庫，回覆稍後再試。"""
    local_services["gemini_client"].models.generate_content.side_effect = TimeoutError()

    assert app.handler(signed_event("你好"), None)["statusCode"] == 200
    request = local_services["messaging"].return_value.reply_message.call_args.args[0]
    assert "暫時無法查詢" in request.messages[0].text
    local_services["dynamodb"].assert_not_called()


def test_database_failure_does_not_retry_as_keyword_query(local_services):
    """AI 成功但資料庫失敗時，回覆錯誤，避免備援重複查詢相同資料庫。"""
    local_services["table"].query.side_effect = RuntimeError("database unavailable")

    assert app.handler(signed_event("本週活動"), None)["statusCode"] == 200
    request = local_services["messaging"].return_value.reply_message.call_args.args[0]
    assert "暫時無法查詢" in request.messages[0].text
    local_services["table"].query.assert_called_once()


def test_empty_database_result(local_services):
    """資料庫回傳空清單時，回覆對應期間的查無符合條件訊息。"""
    local_services["table"].query.return_value = {"Items": []}

    assert app.handler(signed_event("下週文學院的活動"), None)["statusCode"] == 200
    request = local_services["messaging"].return_value.reply_message.call_args.args[0]
    assert request.messages[0].text == "下週沒有符合查詢條件的活動。"
