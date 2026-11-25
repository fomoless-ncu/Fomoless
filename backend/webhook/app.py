"""FomoLess LINE Webhook（M1）。

【這支程式在做什麼】
學生在 LINE 傳訊息給 FomoLess 官方帳號時，LINE 平台會把訊息送到我們的 Webhook 網址，
API Gateway 收到後交給這支 Lambda 處理：

    學生傳訊息 → LINE 平台 → API Gateway（POST /webhook）→ 這支 Lambda 的 handler()
      1. 驗證簽章：確認請求真的是 LINE 送來的，不是別人偽造的
      2. 使用 Azure／Gemini 解析文字條件，再查詢及篩選 DynamoDB 活動資料
      3. 用 Reply API 回覆學生（免費，不計入 LINE 訊息額度）

【建議閱讀順序】
從檔案最下面的 handler() 開始往上看：handler → handle_event → respond_to_text → filter_events
"""

# ---------- 匯入套件 ----------
import base64       # 解碼 API Gateway 可能用 base64 包裝的請求內容
import json
import logging      # 寫日誌，可用 `sam logs` 或 CloudWatch 查看
import os           # 讀取環境變數（template.yaml 裡設定的 EVENTS_TABLE 等）
import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache  # 快取：同一個 Lambda 執行環境內，金鑰只需要讀一次

import boto3        # AWS 官方 Python SDK，用來存取 DynamoDB、Parameter Store
from boto3.dynamodb.conditions import Attr, Key
from linebot.v3 import WebhookParser                      # LINE SDK：驗證簽章並解析事件
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (                        # LINE SDK：送出回覆訊息
    ApiClient,
    Configuration,
    FlexMessage,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import FollowEvent, MessageEvent, TextMessageContent

from activity_flex import ActivityReply, build_activity_reply
from week import has_ended, months_between, query_bounds, week_range  # 同資料夾的 week.py：計算「本週」範圍（台灣時間）

from llm.activity_query import ActivityQuery, ActivityQueryParser
from llm.factory import create_query_parser

# ---------- 全域設定 ----------
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# 這兩個值由 template.yaml 的 Environment.Variables 傳進來，dev 和 prod 會不同
EVENTS_TABLE = os.environ.get("EVENTS_TABLE", "")              # 活動資料表名稱，例如 fomoless-dev-events
SSM_PREFIX = os.environ.get("SSM_PREFIX", "/fomoless/dev")     # 金鑰在 Parameter Store 的路徑前綴

# LLM 無法解析時，用既有關鍵字提供接下來 7 天的活動清單。
LINE_TEXT_LIMIT = 5000  # LINE 文字訊息字數上限
WEEK_KEYWORDS = ("本週", "這週", "本周", "這周", "活動")

# 固定的回覆文字
WELCOME_TEXT = (
    "歡迎加入 FomoLess！\n"
    "輸入「本週活動」看看接下來 7 天有哪些校內活動。"
)
HELP_TEXT = "Hello FomoLess！👋\n可以詢問「本週活動」或「下週文學院有便當的活動嗎？」查詢校內活動。"


# =====================================================================
# 一、讀取金鑰
# LINE 的金鑰不寫在程式碼裡（會被 commit 到 GitHub），而是存在 AWS Parameter Store。
# =====================================================================
@lru_cache(maxsize=None)
def get_secret(name: str) -> str:
    """讀取金鑰：本機測試時優先讀環境變數，部署到 AWS 後讀 Parameter Store。

    Parameter Store 名稱：{SSM_PREFIX}/line-channel-secret、{SSM_PREFIX}/line-channel-access-token
    本機環境變數名稱：  LINE_CHANNEL_SECRET、LINE_CHANNEL_ACCESS_TOKEN（寫在 env.json）
    """
    env_name = name.upper().replace("-", "_")  # line-channel-secret → LINE_CHANNEL_SECRET
    if os.environ.get(env_name):
        return os.environ[env_name]
    ssm = boto3.client("ssm")
    # WithDecryption=True：金鑰以 SecureString 加密儲存，讀取時要解密
    resp = ssm.get_parameter(Name=f"{SSM_PREFIX}/{name}", WithDecryption=True)
    return resp["Parameter"]["Value"]


@lru_cache(maxsize=None)
def get_parser() -> WebhookParser:
    """簽章驗證器：用 Channel secret 檢查請求是否真的來自 LINE。"""
    return WebhookParser(get_secret("line-channel-secret"))


@lru_cache(maxsize=None)
def get_line_config() -> Configuration:
    """回覆訊息用的設定：用 Channel access token 呼叫 LINE API。"""
    return Configuration(access_token=get_secret("line-channel-access-token"))

@lru_cache(maxsize=1)
def get_query_parser() -> ActivityQueryParser:
    """依供應商設定建立並快取活動查詢解析器。"""
    return create_query_parser(get_secret)

# =====================================================================
# 二、查詢活動
# =====================================================================
def find_events_this_week(
    now: datetime | None = None,
    *,
    period: str = "this_week",
) -> list[dict]:
    """從 DynamoDB 查出本週或下週已發布的活動，依開始時間排序。

    查詢方式：用 template.yaml 定義的索引 month-start_at-index
      - 先依「月份」（例如 2026-11）找到那個月的活動
      - 再篩選開始時間在本週範圍內的
      - 本週如果跨月（例如 11/28～12/4），就兩個月各查一次
    這樣不用掃描整張表（Scan），資料再多也又快又便宜。
    今天稍早已經結束的活動（end_at 已過）不列出。
    """
    if not EVENTS_TABLE:  # 本機測試沒設定資料表時，直接回傳空清單
        return []
    now = now or datetime.now(timezone.utc)
    start, end = week_range(now)

    if period == "next_week":
        # 從今天零時移到下週一，再取完整七天。
        start += timedelta(days=7 - start.weekday())
        end = start + timedelta(days=7)

    low, high = query_bounds(start, end)  # BETWEEN 兩端都包含，上界取第 7 天 23:59:59
    table = boto3.resource("dynamodb").Table(EVENTS_TABLE)
    items: list[dict] = []
    for month in months_between(start, end):
        resp = table.query(
            IndexName="month-start_at-index",
            # 月份相同，且開始時間介於本週起訖之間（時間是 +08:00 格式的字串，可直接比大小）
            KeyConditionExpression=Key("month").eq(month) & Key("start_at").between(low, high),
            FilterExpression=Attr("status").eq("published"),  # 草稿不給學生看
        )
        items.extend(resp.get("Items", []))
    upcoming = [e for e in items if not has_ended(e, now)]
    return sorted(upcoming, key=lambda e: e.get("start_at", ""))


# =====================================================================
# 三、把活動整理成 LINE 訊息文字
# =====================================================================
def _num(v) -> str:
    """把數字轉成好看的字串：2 → "2"、Decimal("0.66") → "0.66"。
    DynamoDB 讀出來的數字是 Decimal 型別，直接印會很醜。"""
    f = float(v)
    return str(int(f)) if f.is_integer() else f"{f:g}"


def format_event_line(e: dict) -> str:
    """一筆活動的顯示文字，例如：
    • 11-10 10:00 院週會｜國際會議廳
      ⏱ 院週會 2h｜👥 已額滿
    第二行只在有時數、名額或福利時才會出現。
    """
    when = e.get("start_at", "")[5:16].replace("T", " ")  # 2026-11-10T10:00:00+08:00 → 11-10 10:00
    place = f"｜{e['location']}" if e.get("location") else ""
    line = f"• {when} {e.get('title', '（未命名活動）')}{place}"

    extras = []
    hours = e.get("learning_passport_hours") or []
    if hours:  # 學習護照時數，例如「⏱ 院週會 2h」
        extras.append("⏱ " + "、".join(f"{h['category']} {_num(h['hours'])}h" for h in hours))
    cap, cnt = e.get("capacity"), e.get("registered_count")
    if cap is not None and cnt is not None:  # 名額，例如「👥 64/200」或「👥 已額滿」
        extras.append("👥 已額滿" if cnt >= cap else f"👥 {_num(cnt)}/{_num(cap)}")
    if e.get("perks"):  # 福利，例如「🎁 出席即提供晚餐」
        extras.append(f"🎁 {e['perks']}")
    return line + ("\n  " + "｜".join(extras) if extras else "")


def format_events(
    events: list[dict],
    label: str = "接下來 7 天",
) -> str:
    """把活動清單組成一則訊息；最多列 8 筆，避免訊息太長。"""
    if not events:
        return f"{label}還沒有收錄到活動，之後再來看看！"
    lines = [f"📅 {label}的活動："]
    lines.extend(format_event_line(e) for e in events[:8])
    if len(events) > 8:
        lines.append(f"…還有 {len(events) - 8} 個活動")
    text = "\n".join(lines)
    # LINE 文字訊息上限 5000 字，超過會整則被拒。
    return text if len(text) <= LINE_TEXT_LIMIT else text[: LINE_TEXT_LIMIT - 1] + "…"


# =====================================================================
# 四、回覆學生
# =====================================================================
def activity_has_perk(event: dict, perk: str) -> bool:
    """從福利說明與既有標籤比對福利，排除明確否定該福利的文字。"""
    perk = perk.strip()
    if not perk:
        return False

    description = event.get("perks") or ""
    denied_perk = perk.removeprefix("免費") or perk
    negative = (
        rf"(?:不(?:會)?(?:提供|供應|含|附贈)|未(?:提供|供應)|沒有|無)"
        rf"\s*(?:免費)?{re.escape(denied_perk)}"
    )
    if re.search(negative, description, flags=re.IGNORECASE):
        return False

    expected = perk.casefold()
    return expected in description.casefold() or any(
        expected in tag.casefold() for tag in (event.get("tags") or [])
    )


def filter_events(
    events: list[dict],
    query: ActivityQuery,
) -> list[dict]:
    """依文字、代碼、福利及時數類別篩選，同一場次須符合所有條件。"""
    matched = events

    # 名稱、單位、講者與地點的文字包含條件
    for field in ("title", "host", "speaker", "location"):
        expected = getattr(query, field)    # 依欄位名稱取得查詢值
        # 使用者有指定這個條件時才篩選
        if expected:
            expected = expected.strip().casefold()

            matched = [
                event
                for event in matched
                if expected in (event.get(field) or "").casefold()
            ]
    # 活動形式、報名方式與護照認證方式
    for field in (
        "mode",
        "registration_method",
        "learning_passport_condition",
    ):
        expected = getattr(query, field)

        if expected is not None:
            matched = [
                event
                for event in matched
                if event.get(field) == expected
            ]

    # 福利條件，同一場次必須提供清單中所有福利
    for perk in query.perks:
        matched = [event for event in matched if activity_has_perk(event, perk)]

    # 分別檢查學習護照與軟實力時數，類別須相同且時數大於零。
    for query_field, hours_field in (
        ("learning_passport_category", "learning_passport_hours"),
        ("soft_skill_category", "soft_skill_hours"),
    ):
        category = getattr(query, query_field)
        if category:
            matched = [
                event
                for event in matched
                if any(
                    hour.get("category") == category and (hour.get("hours") or 0) > 0
                    for hour in (event.get(hours_field) or [])
                )
            ]

    return matched

def respond_to_text(text: str) -> str | ActivityReply:
    """優先用 LLM 解析及篩選活動，解析失敗時嘗試關鍵字備援。"""
    text = text.strip()

    if not text:
        return HELP_TEXT

    try:
        try:
            parser = get_query_parser()     # 依設定取得 Azure 或 Gemini 解析器
            query = parser.parse(text)      # 將使用者文字解析成 ActivityQuery
        except Exception as error:
            logger.warning("Activity parsing failed: %s", type(error).__name__)
            # 只有 LLM 設定或解析失敗才備援；資料庫失敗由外層處理。
            if any(keyword in text for keyword in WEEK_KEYWORDS):
                events = find_events_this_week()
                return (
                    "暫時無法解析查詢條件，先提供接下來 7 天的活動清單"
                    "（未套用其他篩選條件）：\n" + format_events(events)
                )
            return "暫時無法查詢活動，請稍後再試。"

        if query.period == "unsupported":
            return "目前支援本週、下週及已定義的活動條件查詢，請換個方式詢問。"

        # 依解析出的期間查詢資料，再套用其他條件
        events = find_events_this_week(period=query.period)
        matched = filter_events(events, query)

        label = "下週" if query.period == "next_week" else "接下來 7 天"

        if not matched:
            return f"{label}沒有符合查詢條件的活動。"

        text_reply = format_events(matched, label=label)
        # 依解析結果判斷，「這週活動」「本週有什麼活動」等說法都會拿到卡片。
        if query.period == "this_week":
            return ActivityReply(build_activity_reply(matched, label), text_reply)
        return text_reply

    except Exception as error:
        logger.warning("Activity query failed: %s", type(error).__name__)
        return "暫時無法查詢活動，請稍後再試。"

def reply(reply_token: str, content: str | list[FlexMessage | TextMessage]) -> None:
    """用 Reply API 回覆文字或活動卡片。

    reply_token 是 LINE 隨事件附上的「回覆憑證」，只能用一次、而且要在短時間內使用。
    用 Reply API 回覆不計入官方帳號的每月訊息額度；主動推播（Push API）才會計費。
    """
    messages = [TextMessage(text=content)] if isinstance(content, str) else content
    with ApiClient(get_line_config()) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(reply_token=reply_token, messages=messages)
        )


def handle_event(event) -> None:
    """依事件類型決定怎麼回覆。之後要新增指令（例如「訂閱」），就在這裡加判斷。"""
    if isinstance(event, FollowEvent):
        # 學生加好友（或解除封鎖）
        reply(event.reply_token, WELCOME_TEXT)
    elif isinstance(event, MessageEvent) and isinstance(event.message, TextMessageContent):
        # 先產生回覆內容，再交給 LINE Reply API。
        content = respond_to_text(event.message.text)
        if isinstance(content, ActivityReply):
            try:
                reply(event.reply_token, content.messages)
            except Exception as error:
                # 卡片被 LINE 拒絕（格式不符）時 reply token 尚未使用，改送文字清單。
                logger.warning("Flex reply failed, falling back to text: %s", type(error).__name__)
                reply(event.reply_token, content.fallback_text)
        else:
            reply(event.reply_token, content)
    # 其他事件（貼圖、圖片、封鎖等）目前不處理


# =====================================================================
# 五、Lambda 進入點：API Gateway 收到請求後，會呼叫這個函式
# （template.yaml 裡的 Handler: app.handler 指的就是這裡）
# =====================================================================
def _response(status: int, body: str) -> dict:
    """回給 API Gateway 的格式，API Gateway 會再轉成 HTTP 回應給 LINE。"""
    return {"statusCode": status, "body": body}


def handler(event, context):
    """event：API Gateway 傳來的 HTTP 請求內容（標頭、body）；context：Lambda 執行資訊（這裡沒用到）。"""
    # 1. 取出 LINE 附上的簽章與請求內容
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}  # 標頭名稱統一轉小寫
    signature = headers.get("x-line-signature", "")
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")

    # 2. 驗證簽章：簽章不符代表不是 LINE 送來的，直接拒絕（回 400）
    try:
        line_events = get_parser().parse(body, signature)
    except InvalidSignatureError:
        logger.warning("Invalid LINE signature")
        return _response(400, "Invalid signature")

    # 3. 一次請求可能包含多個事件，逐一處理
    for line_event in line_events:
        try:
            handle_event(line_event)
        except Exception:
            # 某個事件出錯時記錄下來，但繼續處理其他事件。
            # LINE 只需要收到 200；若回錯誤，LINE 可能重送，學生會收到重複回覆。
            logger.exception("Failed to handle event type=%s", getattr(line_event, "type", "?"))

    # 4. 告訴 LINE「收到了」
    return _response(200, json.dumps({"ok": True}))
