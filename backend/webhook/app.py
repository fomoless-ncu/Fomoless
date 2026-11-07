"""FomoLess LINE webhook (M1).

Flow: LINE -> API Gateway (HTTP API) -> this Lambda
  1. Verify the X-Line-Signature header with the channel secret.
  2. For text messages containing a keyword, look up this week's events in DynamoDB.
  3. Reply with the Reply API (free; does not count toward the LINE message quota).
"""

import base64
import json
import logging
import os
from datetime import datetime, timezone
from functools import lru_cache

import boto3
from boto3.dynamodb.conditions import Attr, Key
from linebot.v3 import WebhookParser
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import FollowEvent, MessageEvent, TextMessageContent

from week import months_between, week_range

logger = logging.getLogger()
logger.setLevel(logging.INFO)

EVENTS_TABLE = os.environ.get("EVENTS_TABLE", "")
SSM_PREFIX = os.environ.get("SSM_PREFIX", "/fomoless/dev")
WEEK_KEYWORDS = ("本週", "這週", "本周", "這周", "活動")

WELCOME_TEXT = (
    "歡迎加入 FomoLess！\n"
    "輸入「本週活動」看看接下來 7 天有哪些校內活動。"
)
HELP_TEXT = "Hello FomoLess！👋\n目前可以輸入「本週活動」查詢接下來 7 天的校內活動，更多功能開發中！"


# ---------- secrets ----------
@lru_cache(maxsize=None)
def get_secret(name: str) -> str:
    """Read a secret: env var first (local testing), then SSM Parameter Store.

    SSM names: {SSM_PREFIX}/line-channel-secret, {SSM_PREFIX}/line-channel-access-token
    Env vars:  LINE_CHANNEL_SECRET, LINE_CHANNEL_ACCESS_TOKEN
    """
    env_name = name.upper().replace("-", "_")
    if os.environ.get(env_name):
        return os.environ[env_name]
    ssm = boto3.client("ssm")
    resp = ssm.get_parameter(Name=f"{SSM_PREFIX}/{name}", WithDecryption=True)
    return resp["Parameter"]["Value"]


@lru_cache(maxsize=None)
def get_parser() -> WebhookParser:
    return WebhookParser(get_secret("line-channel-secret"))


@lru_cache(maxsize=None)
def get_line_config() -> Configuration:
    return Configuration(access_token=get_secret("line-channel-access-token"))


# ---------- data ----------
def find_events_this_week(now: datetime | None = None) -> list[dict]:
    """Query the month GSI for events starting within the next 7 days."""
    if not EVENTS_TABLE:
        return []
    start, end = week_range(now or datetime.now(timezone.utc))
    table = boto3.resource("dynamodb").Table(EVENTS_TABLE)
    items: list[dict] = []
    for month in months_between(start, end):
        resp = table.query(
            IndexName="month-start_at-index",
            KeyConditionExpression=Key("month").eq(month)
            & Key("start_at").between(start.isoformat(), end.isoformat()),
            FilterExpression=Attr("status").eq("published"),  # 草稿不給學生看
        )
        items.extend(resp.get("Items", []))
    return sorted(items, key=lambda e: e.get("start_at", ""))


def _num(v) -> str:
    """2 -> "2"、Decimal("0.66") -> "0.66"。"""
    f = float(v)
    return str(int(f)) if f.is_integer() else f"{f:g}"


def format_event_line(e: dict) -> str:
    """一筆活動的顯示文字，例如：
    • 11-10 10:00 院週會｜國際會議廳
      ⏱ 院週會 2h｜👥 已額滿
    """
    when = e.get("start_at", "")[5:16].replace("T", " ")  # MM-DD HH:MM
    place = f"｜{e['location']}" if e.get("location") else ""
    line = f"• {when} {e.get('title', '（未命名活動）')}{place}"

    extras = []
    hours = e.get("learning_passport_hours") or []
    if hours:
        extras.append("⏱ " + "、".join(f"{h['category']} {_num(h['hours'])}h" for h in hours))
    cap, cnt = e.get("capacity"), e.get("registered_count")
    if cap is not None and cnt is not None:
        extras.append("👥 已額滿" if cnt >= cap else f"👥 {_num(cnt)}/{_num(cap)}")
    if e.get("perks"):
        extras.append(f"🎁 {e['perks']}")
    return line + ("\n  " + "｜".join(extras) if extras else "")


def format_events(events: list[dict]) -> str:
    if not events:
        return "接下來 7 天還沒有收錄到活動，之後再來看看！"
    lines = ["📅 接下來 7 天的活動："]
    lines.extend(format_event_line(e) for e in events[:8])
    if len(events) > 8:
        lines.append(f"…還有 {len(events) - 8} 個活動")
    return "\n".join(lines)


# ---------- LINE ----------
def reply(reply_token: str, text: str) -> None:
    with ApiClient(get_line_config()) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(reply_token=reply_token, messages=[TextMessage(text=text)])
        )


def handle_event(event) -> None:
    if isinstance(event, FollowEvent):
        reply(event.reply_token, WELCOME_TEXT)
    elif isinstance(event, MessageEvent) and isinstance(event.message, TextMessageContent):
        text = event.message.text.strip()
        if any(k in text for k in WEEK_KEYWORDS):
            reply(event.reply_token, format_events(find_events_this_week()))
        else:
            reply(event.reply_token, HELP_TEXT)


# ---------- Lambda entry ----------
def _response(status: int, body: str) -> dict:
    return {"statusCode": status, "body": body}


def handler(event, context):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    signature = headers.get("x-line-signature", "")
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")

    try:
        line_events = get_parser().parse(body, signature)
    except InvalidSignatureError:
        logger.warning("Invalid LINE signature")
        return _response(400, "Invalid signature")

    for line_event in line_events:
        try:
            handle_event(line_event)
        except Exception:  # keep processing other events; LINE only needs a 200
            logger.exception("Failed to handle event type=%s", getattr(line_event, "type", "?"))

    return _response(200, json.dumps({"ok": True}))
