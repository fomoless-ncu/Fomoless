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
from shared.adapters.portal import from_portal_crawler
from shared.privacy import contains_pii, redact_pii
from week import week_range


SECRET = "local-test-channel-secret"

# 固定「現在」為月中週三（台北 12:00），本週與下週都在同一個月，
# 避免月底執行時跨月查兩次，讓 query 呼叫次數不受實際執行日期影響。
FIXED_NOW = datetime(2026, 11, 11, 4, 0, tzinfo=timezone.utc)


class FrozenDatetime(datetime):
    """只覆寫 now()，其餘行為與 datetime 相同。"""

    @classmethod
    def now(cls, tz=None):
        return FIXED_NOW if tz is None else FIXED_NOW.astimezone(tz)


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

    # 一般案例使用固定日期，跨月／跨年案例可另外指定。
    now = getattr(request, "param", FIXED_NOW)
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


def test_this_week_renders_portal_activity_as_flex(local_services):
    """真實 Portal 樣本從匯入格式一路轉成卡片，欄位與按鈕正確。"""
    path = Path(__file__).parent / "fixtures" / "portal" / "learning_passport_2621.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    tomorrow, _ = week_range(FIXED_NOW + timedelta(days=1))
    raw["start_at"] = (tomorrow + timedelta(hours=13)).isoformat()
    raw["end_at"] = (tomorrow + timedelta(hours=17)).isoformat()
    event = from_portal_crawler(raw).to_dynamo_item()
    local_services["table"].query.return_value = {"Items": [event]}
    local_services["gemini_client"].models.generate_content.return_value.text = (
        ActivityQuery(period="this_week").model_dump_json()
    )

    assert app.handler(signed_event("本週活動"), None)["statusCode"] == 200
    request = local_services["messaging"].return_value.reply_message.call_args.args[0]
    message = request.messages[0].to_dict()
    assert message["type"] == "flex"
    card = message["contents"]
    assert card["header"]["backgroundColor"] == "#800020"
    assert card["body"]["backgroundColor"] == "#FFFFFF"
    lines = [part["text"] for part in card["body"]["contents"] if part["type"] == "text"]
    assert lines == [
        "當人文學遇見AI：研究方法、知識生產與教學實踐工作坊",
        f"時間　{tomorrow:%m/%d} 13:00–17:00",
        "地點　文學一館A-107文學院學士班巢映電影空間",
        "報名　已截止（0/80 人）",
        "學習護照　人文藝術 3 小時",
    ]
    action = card["footer"]["contents"][0]["action"]
    assert action["label"] == "查看活動"
    assert action["uri"] == "https://cis.ncu.edu.tw/iNCU/messageNotice/activityManagement/activity/2621"


def test_this_week_groups_matching_sessions_in_one_card(local_services):
    """同活動的多場次合併成一張卡，逐場列出時間與地點。"""
    tomorrow, _ = week_range(FIXED_NOW + timedelta(days=1))
    session = {
        "event_id": "portal-1", "activity_id": "100", "source": "crawler_portal",
        "activity_title": "CPR 急救訓練", "title": "CPR 第一場",
        "start_at": (tomorrow + timedelta(hours=9)).isoformat(),
        "location": "第一教室", "status": "published",
    }
    second = {
        **session, "event_id": "portal-2", "title": "CPR 第二場",
        "start_at": (tomorrow + timedelta(hours=14)).isoformat(), "location": "第二教室",
    }
    other = {
        **session, "event_id": "manual-1", "activity_id": None, "activity_title": None,
        "title": "職涯分享", "start_at": (tomorrow + timedelta(days=1, hours=9)).isoformat(),
        "location": None,
    }
    local_services["table"].query.return_value = {"Items": [second, other, session]}
    local_services["gemini_client"].models.generate_content.return_value.text = (
        ActivityQuery(period="this_week").model_dump_json()
    )

    assert app.handler(signed_event("本週活動"), None)["statusCode"] == 200
    request = local_services["messaging"].return_value.reply_message.call_args.args[0]
    cards = request.messages[0].to_dict()["contents"]["contents"]
    assert [card["body"]["contents"][0]["text"] for card in cards] == ["CPR 急救訓練", "職涯分享"]
    assert "本次查到 2 場" in cards[0]["header"]["contents"][0]["text"]
    texts = [part.get("text") for part in cards[0]["body"]["contents"]]
    assert f"{tomorrow:%m/%d} 09:00｜第一教室" in texts
    assert f"{tomorrow:%m/%d} 14:00｜第二教室" in texts
    assert "以下資訊以最近一場為準" in texts
    assert not cards[1].get("footer")


def test_this_week_counts_hidden_activities_after_grouping(local_services):
    """合併後超過 8 個活動時，附文字說明未顯示的數量。"""
    tomorrow, _ = week_range(FIXED_NOW + timedelta(days=1))
    events = [
        {
            "event_id": f"event-{number}",
            "activity_id": f"activity-{number}",
            "title": f"活動 {number}",
            "start_at": (tomorrow + timedelta(hours=number)).isoformat(),
            "status": "published",
        }
        for number in range(9)
    ]
    extra_session = {**events[0], "event_id": "extra-session"}
    local_services["table"].query.return_value = {"Items": [extra_session, *events]}
    local_services["gemini_client"].models.generate_content.return_value.text = (
        ActivityQuery(period="this_week").model_dump_json()
    )

    assert app.handler(signed_event("本週活動"), None)["statusCode"] == 200
    request = local_services["messaging"].return_value.reply_message.call_args.args[0]
    cards = request.messages[0].to_dict()["contents"]["contents"]
    assert len(cards) == 8
    assert cards[-1]["body"]["contents"][0]["text"] == "活動 7"
    assert request.messages[1].text == "還有 1 個活動未顯示。"


def test_this_week_paraphrase_also_gets_flex(local_services):
    """不同說法只要解析成本週，就回卡片，不限定一字不差的「本週活動」。"""
    tomorrow, _ = week_range(FIXED_NOW + timedelta(days=1))
    event = {"title": "職涯分享", "start_at": (tomorrow + timedelta(hours=9)).isoformat(), "status": "published"}
    local_services["table"].query.return_value = {"Items": [event]}
    local_services["gemini_client"].models.generate_content.return_value.text = (
        ActivityQuery(period="this_week").model_dump_json()
    )

    assert app.handler(signed_event("這週有什麼活動"), None)["statusCode"] == 200
    request = local_services["messaging"].return_value.reply_message.call_args.args[0]
    assert request.messages[0].to_dict()["type"] == "flex"


def test_flex_rejected_falls_back_to_text(local_services):
    """LINE 拒絕卡片時改送文字清單，學生不會收不到回覆。"""
    tomorrow, _ = week_range(FIXED_NOW + timedelta(days=1))
    event = {"title": "職涯分享", "start_at": (tomorrow + timedelta(hours=9)).isoformat(), "status": "published"}
    local_services["table"].query.return_value = {"Items": [event]}
    local_services["gemini_client"].models.generate_content.return_value.text = (
        ActivityQuery(period="this_week").model_dump_json()
    )
    reply_message = local_services["messaging"].return_value.reply_message
    reply_message.side_effect = [RuntimeError("400 Bad Request"), None]

    assert app.handler(signed_event("本週活動"), None)["statusCode"] == 200
    assert reply_message.call_count == 2
    fallback = reply_message.call_args.args[0].messages[0]
    assert "職涯分享" in fallback.text


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
