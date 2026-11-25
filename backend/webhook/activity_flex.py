"""把活動查詢結果（場次清單）組成 LINE Flex Message 活動卡片。"""

from datetime import datetime
from typing import NamedTuple
from urllib.parse import urlsplit

from linebot.v3.messaging import FlexContainer, FlexMessage, TextMessage


BURGUNDY = "#800020"
WHITE = "#FFFFFF"
INK = "#271E23"
MUTED = "#8C7F86"
MAX_ACTIVITY_CARDS = 8   # carousel 最多 12 張，留餘裕
MAX_SESSIONS_SHOWN = 3   # 一張卡最多列幾個場次，太多會讓整排卡片一起變高
MAX_TITLE_CHARS = 30     # 標題超過就截斷加「…」
MAX_FIELD_CHARS = 60     # 地點、福利等欄位上限，避免單張卡片超過 LINE 的 10 KB
MAX_URI_CHARS = 1000     # LINE uri action 上限

REGISTRATION_LABELS = {
    "open": "開放報名",
    "closed": "已截止",
    "full": "已額滿",
    "not_started": "尚未開放報名",
}


class ActivityReply(NamedTuple):
    """卡片訊息加上文字備援；卡片被 LINE 拒絕時改送 fallback_text。"""

    messages: list[FlexMessage | TextMessage]
    fallback_text: str


def clip(value: str, limit: int) -> str:
    """超過 limit 字就截斷並加「…」。"""
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _text(value: str, **style: str) -> dict:
    return {"type": "text", "text": value, "size": "sm", "color": INK, "wrap": True, **style}


def _session_time(session: dict) -> str:
    start = datetime.fromisoformat(session["start_at"])
    end_at = session.get("end_at")
    if not end_at:
        return f"{start:%m/%d %H:%M}"
    end = datetime.fromisoformat(end_at)
    end_format = "%H:%M" if end.date() == start.date() else "%m/%d %H:%M"
    return f"{start:%m/%d %H:%M}–{end:{end_format}}"


def _is_valid_uri(url: str) -> bool:
    """LINE 會拒絕整則訊息的網址都擋掉：非 http(s)、超長、非 ASCII、含空白。"""
    if len(url) > MAX_URI_CHARS or not url.isascii() or any(c.isspace() for c in url):
        return False
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return parsed.scheme in ("https", "http") and bool(parsed.hostname)


def _activity_url(sessions: list[dict]) -> str | None:
    for session in sessions:
        for field in ("info_url", "source_url"):
            url = session.get(field)
            if isinstance(url, str) and _is_valid_uri(url):
                return url
    return None


def _registration(session: dict) -> str | None:
    """報名狀態；名額已滿時一律顯示「已額滿」，不採信可能過時的 registration_status。"""
    cap, cnt = session.get("capacity"), session.get("registered_count")
    has_count = cap is not None and cnt is not None
    if has_count and cnt >= cap:
        return "已額滿"
    status = REGISTRATION_LABELS.get(session.get("registration_status"))
    if has_count:
        seats = f"{float(cnt):g}/{float(cap):g} 人"
        return f"{status}（{seats}）" if status else seats
    return status


def _passport_credits(session: dict) -> str | None:
    """學習護照時數，略過 0 小時的類別。"""
    entries = [
        entry for entry in session.get("learning_passport_hours") or []
        if float(entry.get("hours") or 0) > 0
    ]
    if not entries:
        return None
    return "、".join(f"{entry['category']} {float(entry['hours']):g} 小時" for entry in entries)


def _session_line(session: dict) -> str:
    location = session.get("location")
    when = _session_time(session)
    return f"{when}｜{clip(location, MAX_FIELD_CHARS)}" if location else when


def _activity_card(sessions: list[dict], label: str) -> dict:
    first = sessions[0]
    title = clip(first.get("activity_title") or first.get("title") or "未命名活動", MAX_TITLE_CHARS)
    header = f"FomoLess｜{label}"
    details = [_text(title, size="lg", weight="bold"), {"type": "separator"}]

    if len(sessions) == 1:
        details.append(_text(f"時間　{_session_time(first)}"))
        if location := first.get("location"):
            details.append(_text(f"地點　{clip(location, MAX_FIELD_CHARS)}"))
    else:
        # 多場次：逐場列出時間與地點，報名等資訊標明以最近一場為準。
        header += f"｜本次查到 {len(sessions)} 場"
        details.append(_text("場次", weight="bold"))
        details.extend(_text(_session_line(s)) for s in sessions[:MAX_SESSIONS_SHOWN])
        if len(sessions) > MAX_SESSIONS_SHOWN:
            details.append(_text(f"…另有 {len(sessions) - MAX_SESSIONS_SHOWN} 場", color=MUTED))
        details.append(_text("以下資訊以最近一場為準", size="xs", color=MUTED))

    if status := _registration(first):
        details.append(_text(f"報名　{status}", color=BURGUNDY, weight="bold"))
    if credits := _passport_credits(first):
        details.append(_text(f"學習護照　{credits}"))
    if perks := first.get("perks"):
        details.append(_text(f"福利　{clip(perks, MAX_FIELD_CHARS)}"))

    card = {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": BURGUNDY,
            "paddingAll": "16px",
            "contents": [_text(header, color=WHITE, weight="bold")],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "backgroundColor": WHITE,
            "contents": details,
        },
    }
    if url := _activity_url(sessions):
        card["footer"] = {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": WHITE,
            "contents": [{
                "type": "button",
                "style": "primary",
                "height": "sm",
                "color": BURGUNDY,
                "action": {"type": "uri", "label": "查看活動", "uri": url},
            }],
        }
    return card


def group_sessions(events: list[dict]) -> list[list[dict]]:
    """同來源、同 activity_id 的場次合併成一組，保留原本的時間順序。"""
    groups: dict[tuple, list[dict]] = {}
    for index, event in enumerate(events):
        activity_id = event.get("activity_id")
        # activity_id 只在同一來源內唯一；沒有 activity_id 的舊資料自成一組。
        key = (event.get("source"), activity_id, None if activity_id else index)
        groups.setdefault(key, []).append(event)
    return list(groups.values())


def build_activity_reply(events: list[dict], label: str) -> list[FlexMessage | TextMessage]:
    """把非空、已依時間排序的查詢結果組成最多 8 張活動卡片。"""
    groups = group_sessions(events)
    cards = [_activity_card(group, label) for group in groups[:MAX_ACTIVITY_CARDS]]
    container = cards[0] if len(cards) == 1 else {"type": "carousel", "contents": cards}
    messages: list[FlexMessage | TextMessage] = [
        FlexMessage(alt_text=f"{label}的活動，共 {len(groups)} 個", contents=FlexContainer.from_dict(container)),
    ]
    remaining = len(groups) - len(cards)
    if remaining:
        messages.append(TextMessage(text=f"還有 {remaining} 個活動未顯示。"))
    return messages
