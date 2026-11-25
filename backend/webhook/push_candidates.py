"""接收活動清單和使用者訂閱標籤，用 Python 篩選、去重、排序，回傳候選活動。

訂閱指令與 TAG_CATALOG 驗證由外層負責。本模組接收與現有 webhook 相同的
活動 dict（時間可為 ISO 字串或帶時區 datetime）。
"""

from datetime import datetime
from decimal import Decimal, InvalidOperation

from week import week_range


def normalize_user_tags(user_tags: list[str]) -> list[str]:
    """整理使用者標籤；移除空白與重複標籤，回傳整理過的標籤。"""
    if not isinstance(user_tags, list) or any(not isinstance(tag, str) for tag in user_tags):
        raise TypeError("user_tags 必須是 list[str]")   #確認輸入是清單，而且裡面每個項目都是字串
    return list(dict.fromkeys(tag.strip() for tag in user_tags if tag.strip()))


def event_datetime(value: str | datetime) -> datetime:
    """統一解析時間；拒絕無時區資料，避免受主機時區影響。"""
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("活動時間及 now 必須帶時區")
    return result


def _number(value) -> Decimal | None:
    """統一處理數字；沒有提供數字時，保留 None，並統一成 Decimal。"""
    if value is None:
        return None
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError("名額與報名人數必須是非負有限數字")
    return result


def select_push_candidates(
    events: list[dict],     # 待篩選的活動資料清單
    user_tags: list[str],       # 已從外部取得的使用者訂閱標籤
    *,      # 後面的參數 (now, limit) 必須寫出名稱
    now: datetime,       # 本次判斷所使用的現在時間
    limit: int = 5,        # 最多回傳幾筆候選活動，預設 5 筆
) -> list[dict]:
    """取台灣時間今日起 7 天內、任一訂閱標籤相符的有效場次。

    排除未發布、已結束、明確額滿，以及需報名但尚未開放／已截止的場次；
    未知名額不視為額滿。目前不推薦候補，也不憑標籤推測院系／年級資格。
    同一活動先保留最早有效場次，再按命中標籤數多到少、時間早到晚排序。
    activity_id 缺值時按 event_id 去重，避免把所有缺值活動合併成一筆。
    """
    if type(limit) is not int or limit < 1:
        raise ValueError("limit 必須是正整數")
    now = event_datetime(now)
    start, end = week_range(now)
    wanted = set(normalize_user_tags(user_tags))
    if not wanted:
        return []

    # 暫存通過篩選的活動（開始時間, event_id, 命中標籤數, 活動 dict）
    eligible: list[tuple[datetime, str, int, dict]] = [] 

    for event in events:
        # 檢查 type 格式，確保 event 是 dict，並且 status 為 published
        if not isinstance(event, dict):
            continue
        if event.get("status") != "published":
            continue
        event_id, title = event.get("event_id"), event.get("title")
        if not isinstance(event_id, str) or not 1 <= len(event_id.strip()) <= 80:
            continue
        if event_id != event_id.strip():
            continue 
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 120:
            continue
        activity_id = event.get("activity_id")
        if activity_id is not None and not isinstance(activity_id, str):
            continue
        tags = event.get("tags") or []
        if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
            continue
        # 檢查是否符合訂閱標籤
        matches = wanted.intersection(tags)
        if not matches:
            continue

        try:
            # 檢查活動時間
            when = event_datetime(event["start_at"])
            if not start <= when < end:
                continue
            until = event_datetime(event["end_at"]) if event.get("end_at") else None
            if until is not None and (until < when or until <= now):
                continue

            # 檢查名額與額滿狀態
            capacity = _number(event.get("capacity"))
            registered = _number(event.get("registered_count"))
            if capacity == 0 or (
                capacity is not None and registered is not None and registered >= capacity
            ):
                continue
            if event.get("registration_status") == "full":
                continue

            # 檢查報名方式與報名期間
            method = event.get("registration_method", "none")
            if method not in ("none", "portal", "external"): 
                continue
            if method != "none":
                if event.get("registration_status") in ("closed", "not_started"):
                    continue
                opens = event.get("registration_start")
                closes = event.get("registration_end")
                if opens and event_datetime(opens) > now:
                    continue
                if closes and event_datetime(closes) <= now:
                    continue
                if opens and closes and event_datetime(closes) < event_datetime(opens):
                    continue
        # 略過壞資料，保存有效活動
        except (KeyError, ValueError, TypeError, InvalidOperation):
            continue
        eligible.append((when, event_id, len(matches), event))

    # 去重；同一活動只保留最早有效場次
    seen_events: set[str] = set()
    seen_activities: set[tuple[str, str]] = set()
    unique = []
    for item in sorted(eligible, key=lambda item: (item[0], item[1])):
        _, event_id, _, event = item
        activity_id = event.get("activity_id")
        key = ("activity", activity_id) if activity_id else ("event", event_id)
        if event_id in seen_events or key in seen_activities:
            continue
        seen_events.add(event_id)
        seen_activities.add(key)
        unique.append(item)

    # 最終排序，依序比較三個排序條件；限制回傳筆數
    # 命中標籤數由多到少；相同時，開始時間由早到晚；前兩者都相同時，依 ID 字串順序
    unique.sort(key=lambda item: (-item[2], item[0], item[1]))
    return [item[3] for item in unique[:limit]]
