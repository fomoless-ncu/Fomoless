"""Build LINE activity cards from the sessions returned by an activity query."""

from datetime import datetime
from urllib.parse import urlsplit

from linebot.v3.messaging import FlexContainer, FlexMessage, TextMessage


BURGUNDY = "#800020"
WHITE = "#FFFFFF"
INK = "#271E23"
MAX_ACTIVITY_CARDS = 8

REGISTRATION_LABELS = {
    "open": "開放報名",
    "closed": "已截止",
    "full": "已額滿",
    "not_started": "尚未開放報名",
}


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


def _activity_url(sessions: list[dict]) -> str | None:
    for session in sessions:
        for field in ("info_url", "source_url"):
            url = session.get(field)
            if not url:
                continue
            try:
                parsed = urlsplit(url)
                if parsed.scheme in ("https", "http") and parsed.hostname:
                    return url
            except ValueError:
                continue
    return None


def _activity_card(sessions: list[dict], label: str) -> dict:
    first = sessions[0]
    title = first.get("activity_title") or first.get("title") or "未命名活動"
    header = f"FomoLess｜{label}"
    time_label = "時間"
    if len(sessions) > 1:
        header += f"｜本次查到 {len(sessions)} 場"
        time_label = "最近一場"

    details = [
        _text(title, size="lg", weight="bold"),
        {"type": "separator"},
        _text(f"{time_label}　{_session_time(first)}"),
    ]
    if location := first.get("location"):
        details.append(_text(f"地點　{location}"))
    if status := REGISTRATION_LABELS.get(first.get("registration_status")):
        details.append(_text(f"報名　{status}", color=BURGUNDY, weight="bold"))
    if hours := first.get("learning_passport_hours"):
        credits = "、".join(f"{entry['category']} {float(entry['hours']):g} 小時" for entry in hours)
        details.append(_text(f"學習護照　{credits}"))
    if perks := first.get("perks"):
        details.append(_text(f"福利　{perks}"))

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


def build_activity_reply(events: list[dict], label: str) -> list[FlexMessage | TextMessage]:
    """Group a nonempty, time-ordered query result into up to eight activity cards."""
    groups: dict[tuple, list[dict]] = {}
    for index, event in enumerate(events):
        activity_id = event.get("activity_id")
        # IDs are source-specific. A legacy row without an activity ID is its own group.
        key = (event.get("source"), activity_id, None if activity_id else index)
        groups.setdefault(key, []).append(event)

    cards = [_activity_card(group, label) for group in list(groups.values())[:MAX_ACTIVITY_CARDS]]
    container = cards[0] if len(cards) == 1 else {"type": "carousel", "contents": cards}
    messages: list[FlexMessage | TextMessage] = [
        FlexMessage(alt_text=f"{label}的活動，共 {len(groups)} 個", contents=FlexContainer.from_dict(container)),
    ]
    remaining = len(groups) - len(cards)
    if remaining:
        messages.append(TextMessage(text=f"還有 {remaining} 個活動未顯示。"))
    return messages
