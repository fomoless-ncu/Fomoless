"""FomoLess LINE Webhook（M1）。

【這支程式在做什麼】
學生在 LINE 傳訊息給 FomoLess 官方帳號時，LINE 平台會把訊息送到我們的 Webhook 網址，
API Gateway 收到後交給這支 Lambda 處理：

    學生傳訊息 → LINE 平台 → API Gateway（POST /webhook）→ 這支 Lambda 的 handler()
      1. 驗證簽章：確認請求真的是 LINE 送來的，不是別人偽造的
      2. 判斷訊息內容：有「本週」「活動」等關鍵字 → 去 DynamoDB 查接下來 7 天的活動
      3. 用 Reply API 回覆學生（免費，不計入 LINE 訊息額度）

【建議閱讀順序】
從檔案最下面的 handler() 開始往上看：handler → handle_event → find_events_this_week → format_events
"""

# ---------- 匯入套件 ----------
import base64       # 解碼 API Gateway 可能用 base64 包裝的請求內容
import json
import logging      # 寫日誌，可用 `sam logs` 或 CloudWatch 查看
import os           # 讀取環境變數（template.yaml 裡設定的 EVENTS_TABLE 等）
from datetime import datetime, timezone
from functools import lru_cache  # 快取：同一個 Lambda 執行環境內，金鑰只需要讀一次

import boto3        # AWS 官方 Python SDK，用來存取 DynamoDB、Parameter Store
from boto3.dynamodb.conditions import Attr, Key
from linebot.v3 import WebhookParser                      # LINE SDK：驗證簽章並解析事件
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (                        # LINE SDK：送出回覆訊息
    ApiClient,
    Configuration,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import FollowEvent, MessageEvent, TextMessageContent

from week import has_ended, months_between, query_bounds, week_range  # 同資料夾的 week.py：計算「本週」範圍（台灣時間）

# ---------- 全域設定 ----------
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# 這兩個值由 template.yaml 的 Environment.Variables 傳進來，dev 和 prod 會不同
EVENTS_TABLE = os.environ.get("EVENTS_TABLE", "")              # 活動資料表名稱，例如 fomoless-dev-events
SSM_PREFIX = os.environ.get("SSM_PREFIX", "/fomoless/dev")     # 金鑰在 Parameter Store 的路徑前綴

# 訊息中包含任一個關鍵字，就回覆本週活動
WEEK_KEYWORDS = ("本週", "這週", "本周", "這周", "活動")

# 固定的回覆文字
WELCOME_TEXT = (
    "歡迎加入 FomoLess！\n"
    "輸入「本週活動」看看接下來 7 天有哪些校內活動。"
)
HELP_TEXT = "Hello FomoLess！👋\n目前可以輸入「本週活動」查詢接下來 7 天的校內活動，更多功能開發中！"


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


# =====================================================================
# 二、查詢活動
# =====================================================================
def find_events_this_week(now: datetime | None = None) -> list[dict]:
    """從 DynamoDB 查出接下來 7 天、已發布的活動，依開始時間排序。

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


def format_events(events: list[dict]) -> str:
    """把活動清單組成一則訊息；最多列 8 筆，避免訊息太長。"""
    if not events:
        return "接下來 7 天還沒有收錄到活動，之後再來看看！"
    lines = ["📅 接下來 7 天的活動："]
    lines.extend(format_event_line(e) for e in events[:8])
    if len(events) > 8:
        lines.append(f"…還有 {len(events) - 8} 個活動")
    return "\n".join(lines)


# =====================================================================
# 四、回覆學生
# =====================================================================
def reply(reply_token: str, text: str) -> None:
    """用 Reply API 回覆一則文字訊息。

    reply_token 是 LINE 隨事件附上的「回覆憑證」，只能用一次、而且要在短時間內使用。
    用 Reply API 回覆不計入官方帳號的每月訊息額度；主動推播（Push API）才會計費。
    之後 Gabe 要改成 Flex Message（卡片式訊息），就是改這裡的 messages。
    """
    with ApiClient(get_line_config()) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(reply_token=reply_token, messages=[TextMessage(text=text)])
        )


def handle_event(event) -> None:
    """依事件類型決定怎麼回覆。之後要新增指令（例如「訂閱」），就在這裡加判斷。"""
    if isinstance(event, FollowEvent):
        # 學生加好友（或解除封鎖）
        reply(event.reply_token, WELCOME_TEXT)
    elif isinstance(event, MessageEvent) and isinstance(event.message, TextMessageContent):
        # 學生傳文字訊息
        text = event.message.text.strip()
        if any(k in text for k in WEEK_KEYWORDS):
            reply(event.reply_token, format_events(find_events_this_week()))
        else:
            reply(event.reply_token, HELP_TEXT)
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
