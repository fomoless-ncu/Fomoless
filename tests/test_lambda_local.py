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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "webhook"))

import app
from llm.activity_query import ActivityQuery
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
def local_services(monkeypatch):
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

    start, _ = week_range(datetime.now(timezone.utc))
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


def test_invalid_signature_is_rejected(local_services):
    """錯誤簽章回傳 400，服務替身均不應被呼叫。"""
    event = signed_event("下週活動")
    event["headers"]["X-Line-Signature"] = "invalid-signature"

    assert app.handler(event, None)["statusCode"] == 400
    local_services["dynamodb"].assert_not_called()
    local_services["gemini"].assert_not_called()
    local_services["azure"].assert_not_called()
    local_services["messaging"].assert_not_called()


def test_fixed_command_skips_ai(local_services):
    """本週固定指令直接查詢並回覆活動。"""
    assert app.handler(signed_event("本週活動"), None)["statusCode"] == 200
    local_services["gemini"].assert_not_called()
    local_services["azure"].assert_not_called()
    local_services["table"].query.assert_called_once()
    local_services["messaging"].return_value.reply_message.assert_called_once()


def test_ai_failure_sends_retry_message(local_services):
    """模擬 AI 逾時，LINE 應收到稍後再試的訊息。"""
    local_services["gemini_client"].models.generate_content.side_effect = TimeoutError()

    assert app.handler(signed_event("下週有便當的活動"), None)["statusCode"] == 200
    request = local_services["messaging"].return_value.reply_message.call_args.args[0]
    assert "暫時無法查詢" in request.messages[0].text
    local_services["dynamodb"].assert_not_called()


def test_empty_database_result(local_services):
    """資料庫回傳空清單時，回覆對應期間的查無符合條件訊息。"""
    local_services["table"].query.return_value = {"Items": []}

    assert app.handler(signed_event("下週文學院的活動"), None)["statusCode"] == 200
    request = local_services["messaging"].return_value.reply_message.call_args.args[0]
    assert request.messages[0].text == "下週沒有符合查詢條件的活動。"
