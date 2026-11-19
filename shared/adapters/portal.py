import json
import logging
from pathlib import Path

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

logger = logging.getLogger(__name__)


def _truncate(val: str | None, max_len: int) -> str | None:
    if val is None:
        return None
    val = val.strip()
    return val[: max_len - 6] + "......" if len(val) > max_len else val


def _hour_entries(
    raw_items: list[dict],
    event_start: datetime.datetime,
    event_end: datetime.datetime,
    allowed_categories: list[str] | None = None,
) -> list[HourEntry]:
    hour_entries: list[HourEntry] = []
    max_hours = (event_end - event_start).total_seconds() / 3600

    for item in raw_items:
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
        if entry.hours > max_hours:
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
    if session_title is None or session_title == "":
        session_title = activity_title
    elif isinstance(activity_title, str) and activity_title not in session_title:
        session_title = f"{activity_title}｜{session_title}"
    session_title = _truncate(session_title, 120)

    audience = item.get("audience") or {}
    audience_str = _truncate(
        "；".join(f"{(AUDIENCE_MAPPING.get(k, k))}：{v}" for k, v in audience.items()),
        100,
    )

    description = _truncate(item.get("raw_fields", {}).get("活動內容", None), 3000)

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

    # event start and end time as datetime objects
    event_start = datetime.datetime.fromisoformat(item.get("start_at", ""))
    event_end = datetime.datetime.fromisoformat(item.get("end_at", ""))

    learning_passport_hours: list[HourEntry] = _hour_entries(
        item.get("learning_passport_hours", []) or [],
        event_start,
        event_end,
        allowed_categories=LEARNING_PASSPORT_CATEGORIES
    )
    soft_skill_hours: list[HourEntry] = _hour_entries(
        item.get("soft_skill_hours", []) or [],
        event_start,
        event_end,
    )
    hour_tags: list[HourEntry] = _hour_entries(
        item.get("hour_tags", []) or [],
        event_start,
        event_end,
    )

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
    elif need_other:
        passport_cond = PassportCondition.OTHER
    else:
        logger.warning('`learning_passport_condition` is not a valid value; using `None` instead')
        passport_cond = None

    # simply guess tags by keywords; should be replaced w/ AI in the future?
    tags: list[str] = []
    combined_title = item.get("session_name", "") + "；" + item.get("title", "")
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
        event_id=make_event_id(Source.CRAWLER_PORTAL, item.get("pk", "")),
        activity_id=item.get("activity_id"),
        session_id=item.get("session_id"),
        title=title,
        activity_title=item.get("title"),
        host=item.get("organizer", ""),
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
        sdgs_tags=item.get("sdgs_tag", []),
        scraped_at=item.get("scraped_at"),
        views=item.get("views"),
        source=Source.CRAWLER_PORTAL,
        status=Status.PUBLISHED,
        mode=Mode.ONSITE,
        # there's no possible ways to detect if an event is held online; using ONSTIE as default for now
        tags=tags,
    )

    return event


def load_from_crawled_data(path: str | None = None) -> list[Event]:
    events = []

    if path is None:
        crawl_data_folder = (
            Path(__file__).parent.parent.parent / "tests" / "fixtures" / "portal"
        )
    else:
        crawl_data_folder = Path(path)

    for filename in crawl_data_folder.iterdir():
        logger.info(f"Processing file: {filename}")
        with open(filename, "r", encoding="utf-8") as file:
            events.append(from_portal_crawler(json.load(file)))

    return events


if __name__ == "__main__":
    from pprint import pprint

    pprint(load_from_crawled_data())
