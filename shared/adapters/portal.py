import datetime
import logging

from pydantic import ValidationError

from shared.models import (
    Event,
    HourEntry,
    Source,
    RegistrationMethod,
    RegistrationStatus,
    PassportCondition,
    Status,
    Mode,
    LEARNING_PASSPORT_CATEGORIES,
    TAG_CATALOG,
    make_event_id,
)

AUDIENCE_MAPPING: dict[str, str] = {
    "students": "學生",
    "staff": "教職員",
    "public": "校外人士",
}

REGISTRATION_METHOD_MAPPING: dict[str, RegistrationMethod] = {
    "portal": RegistrationMethod.PORTAL,
    "external": RegistrationMethod.EXTERNAL,
    "none": RegistrationMethod.NONE,
}

REGISTRATION_STATUS_MAPPING: dict[str, RegistrationStatus] = {
    "開放報名": RegistrationStatus.OPEN,
    "報名中": RegistrationStatus.OPEN,
    "已額滿": RegistrationStatus.FULL,
    "已截止": RegistrationStatus.CLOSED,
    "尚未開始": RegistrationStatus.NOT_STARTED,
    "未開放報名": RegistrationStatus.NOT_STARTED,
}

TAG_CATALOG_KEYWORDS: dict[str, set[str]] = {
    "講座": {"演講", "論壇"},
    "競賽": {"比賽", "大賽"},
}

DEFAULT_HOST = "未提供"

logger = logging.getLogger(__name__)


def _to_dt(value: str | None) -> datetime.datetime | None:
    """ISO 字串轉 datetime；空值（None、""）回傳 None。"""
    return datetime.datetime.fromisoformat(value) if value else None


def _truncate(val: str | None, max_len: int) -> str | None:
    if val is None:
        return None
    val = val.strip()
    return val[: max_len - 6] + "......" if len(val) > max_len else val


def _hour_entries(
    raw_items: list[dict] | None,
    max_hours: float | None,
    allowed_categories: list[str] | None = None,
) -> list[HourEntry]:
    """逐項轉換時數；無效的那一項丟掉並記 warning，不讓整筆失敗。"""
    hour_entries: list[HourEntry] = []

    for item in raw_items or []:
        try:
            entry = HourEntry(**item)
        except ValidationError:
            logger.warning("Invalid hour entry: %s; skipping", item)
            continue
        if allowed_categories is not None and entry.category not in allowed_categories:
            logger.warning(
                "Hour entry has invalid category: %s; skipping", entry.category
            )
            continue
        if max_hours is not None and entry.hours > max_hours:
            logger.warning(
                "Hour entry hours (%s) exceed event duration (%s); skipping",
                entry.hours,
                max_hours,
            )
            continue
        hour_entries.append(entry)

    return hour_entries


def from_portal_crawler(item: dict) -> Event:
    # preprocessing
    activity_title: str | None = item.get("title")
    session_title: str | None = item.get("session_name")
    if not session_title:
        session_title = activity_title
    elif isinstance(activity_title, str) and activity_title not in session_title:
        session_title = f"{activity_title}｜{session_title}"
    session_title = _truncate(session_title, 120)

    audience = item.get("audience") or {}
    audience_str = _truncate(
        "；".join(f"{(AUDIENCE_MAPPING.get(k, k))}：{v}" for k, v in audience.items()),
        100,
    )

    host = (item.get("organizer") or "").strip()
    if not host:
        host = DEFAULT_HOST
        logger.warning('"organizer" is empty; using "%s" instead', DEFAULT_HOST)

    raw_fields = item.get("raw_fields") or {}
    description = _truncate(raw_fields.get("活動內容", None), 3000)

    reg_method = REGISTRATION_METHOD_MAPPING.get(item.get("registration_method", ""))
    if reg_method is None:
        logger.warning(
            '"registration_method" is not a valid value; using `RegistrationMethod.NONE` instead'
        )
        reg_method = RegistrationMethod.NONE

    reg_status = REGISTRATION_STATUS_MAPPING.get(item.get("registration_status", ""))
    if reg_status is None:
        logger.warning(
            '"registration_status" is not a valid value; using `None` instead'
        )
        reg_status = None

    reg_count = item.get("registered_count")
    capacity = item.get("capacity")
    if (
        isinstance(reg_count, int)
        and isinstance(capacity, int)
        and reg_count > capacity
    ):
        logger.warning(
            '"registered_count" is greater than "capacity"; using `None` in "capacity" instead'
        )
        capacity = None

    # event start and end time as datetime objects; end_at may be empty
    event_start = _to_dt(item.get("start_at"))
    event_end = _to_dt(item.get("end_at"))
    max_hours = (
        (event_end - event_start).total_seconds() / 3600
        if event_start and event_end
        else None
    )

    learning_passport_hours = _hour_entries(
        item.get("learning_passport_hours"),
        max_hours,
        allowed_categories=LEARNING_PASSPORT_CATEGORIES,
    )
    soft_skill_hours = _hour_entries(item.get("soft_skill_hours"), max_hours)
    hour_tags = _hour_entries(item.get("hour_tags"), max_hours)

    passport_cond_str = item.get("learning_passport_condition", None)
    need_sign_in, need_sign_out, need_other = False, False, False
    if isinstance(passport_cond_str, str):
        passport_cond_str = passport_cond_str.replace("、", "")
        need_sign_in = "簽到" in passport_cond_str
        need_sign_out = "簽退" in passport_cond_str
        passport_cond_str = (
            passport_cond_str.replace("簽到", "").replace("簽退", "").strip()
        )
        need_other = len(passport_cond_str) > 0

    if passport_cond_str is None or not (need_sign_in or need_sign_out or need_other):
        passport_cond = None
    elif need_sign_in and need_sign_out:
        passport_cond = PassportCondition.BOTH
    elif need_sign_in and not need_sign_out:
        passport_cond = PassportCondition.SIGN_IN
    elif not need_sign_in and need_sign_out:
        passport_cond = PassportCondition.SIGN_OUT
    else:
        passport_cond = PassportCondition.OTHER

    # simply guess tags by keywords; should be replaced w/ AI in the future?
    tags: list[str] = []
    combined_title = f"{activity_title or ''}；{session_title or ''}"
    # 1. check by catalog name
    for catalog in TAG_CATALOG.keys():
        if catalog in combined_title:
            tags.append(catalog)
    # 2. check more examples
    for c, kws in TAG_CATALOG_KEYWORDS.items():
        if any(kw in combined_title for kw in kws):
            tags.append(c)

    # construct Event obj
    event = Event(
        event_id=make_event_id(Source.CRAWLER_PORTAL, item["pk"]),
        # use square brackets to raise error if pk is missing
        activity_id=item.get("activity_id"),
        session_id=item.get("session_id"),
        title=session_title,
        activity_title=item.get("title"),
        host=_truncate(host, 60),
        contact_email=None,
        speaker=item.get("speaker"),
        location=item.get("location"),
        location_code=item.get("location_code"),
        audience=audience_str,
        description=description,
        info_url=item.get("url"),
        source_url=item.get("url"),
        image_urls=item.get("image_url") or item.get("image_urls") or [],
        start_at=event_start,
        end_at=event_end,
        registration_method=reg_method,
        registration_url=item.get("registration_url"),
        registration_status=reg_status,
        registered_count=reg_count,
        registration_start=item.get("registration_start"),
        registration_end=item.get("registration_end"),
        capacity=capacity,
        waitlist_capacity=item.get("waitlist_capacity"),
        waitlist_count=item.get("waitlist_count"),
        learning_passport_hours=learning_passport_hours,
        learning_passport_condition=passport_cond,
        hour_tags=hour_tags,
        soft_skill_hours=soft_skill_hours,
        sdgs_tags=item.get("sdgs_tag") or [],
        scraped_at=item.get("scraped_at"),
        views=item.get("views"),
        source=Source.CRAWLER_PORTAL,
        status=Status.PUBLISHED,
        mode=Mode.ONSITE,
        # there's no possible ways to detect if an event is held online; using ONSTIE as default for now
        tags=tags,
    )

    return event

