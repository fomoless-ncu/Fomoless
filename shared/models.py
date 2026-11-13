"""FomoLess 共用資料模型：所有模組對「活動資料」的唯一標準。

一筆 Event = Portal 上的「一個場次」（一個活動可能有多個場次，各自有時間、地點、名額、時數）。
同一活動的場次共用 activity_id。

誰會用到：
- 爬蟲、電算中心 API 轉接器、AI 海報辨識：產生 Event，經驗證後寫入資料庫
- 資料庫組：to_dynamo_item() / from_dynamo_item() 存取 DynamoDB
- LINE Bot、Discord Bot、前端：讀取 Event 顯示

草稿版本，欄位以電算中心 API 定案為準。修改請發 PR 並通知所有組。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, computed_field, field_validator, model_validator

TAIPEI = timezone(timedelta(hours=8))


def now_taipei() -> datetime:
    """現在時間（台灣時間，精確到秒）。"""
    return datetime.now(TAIPEI).replace(microsecond=0)


# ---------------------------------------------------------------------------
# 列舉值
# ---------------------------------------------------------------------------
class Category(str, Enum):
    """四大活動類型。"""

    FREE_PERKS = "free_perks"        # 免費福利型
    CREDIT_HOURS = "credit_hours"    # 學分門檻型（學習護照時數）
    PERFORMANCE = "performance"      # 表演娛樂型
    CAREER = "career"                # 職涯成長型


CATEGORY_LABELS: dict[Category, str] = {
    Category.FREE_PERKS: "🍱 免費福利",
    Category.CREDIT_HOURS: "⏱ 學習護照時數",
    Category.PERFORMANCE: "🎸 表演娛樂",
    Category.CAREER: "💼 職涯成長",
}


class Mode(str, Enum):
    ONSITE = "onsite"    # 實體
    ONLINE = "online"    # 線上
    HYBRID = "hybrid"    # 實體 + 線上


class Source(str, Enum):
    NCU_API = "ncu_api"                    # 電算中心活動 API（最終資料來源）
    CRAWLER_OSA = "crawler_osa"            # 課外組網站爬蟲（過渡期）
    CRAWLER_PORTAL = "crawler_portal"      # Portal 活動頁爬蟲（過渡期）
    HOST_SUBMISSION = "host_submission"    # 主辦方投稿
    EXTENSION_NCUTALK = "extension_ncutalk"  # NCU TALK 擴充功能
    MANUAL = "manual"                      # 團隊手動登錄


class Status(str, Enum):
    DRAFT = "draft"          # 草稿，待人工確認（AI 預填、擴充功能擷取）
    PUBLISHED = "published"  # 已發布，學生看得到、會被推播
    CANCELLED = "cancelled"  # 已取消
    POSTPONED = "postponed"  # 延期


class RegistrationMethod(str, Enum):
    PORTAL = "portal"        # Portal 活動報名系統
    EXTERNAL = "external"    # 外部連結（例如 Google 表單）
    NONE = "none"            # 免報名，可逕行參加


class PassportCondition(str, Enum):
    """學習護照認證條件。"""

    SIGN_IN = "sign_in"      # 簽到
    SIGN_OUT = "sign_out"    # 簽退
    BOTH = "both"            # 簽到 + 簽退
    OTHER = "other"


# ---------------------------------------------------------------------------
# 學習護照類別（畢業門檻 100 小時）：依服務學習發展中心公告整理，待電算中心提供正式代碼表
# ---------------------------------------------------------------------------
LEARNING_PASSPORT_CATEGORIES: list[str] = [
    "院週會",
    "大一週會",
    "自我探索與生涯規劃",
    "大一CPR",
    "其他生活知能",
    "服務學習課程",
    "校外服務學習",
    "人文藝術",
    "國際視野",
]

# 八大軟實力：目前只確認六項，待電算中心提供完整清單
SOFT_SKILLS_KNOWN: list[str] = ["跨域整合", "科技應用", "人文關懷", "批判思考", "領導統御", "國際移動"]


# ---------------------------------------------------------------------------
# 標籤目錄：學生訂閱用。標籤一律從這裡選，不能自創，否則訂閱配對會失效。
# 學習護照類別會由 learning_passport_hours 自動帶入標籤。新增標籤請發 PR。
# ---------------------------------------------------------------------------
TAG_CATALOG: dict[str, Category] = {
    # 免費福利型
    "免費便當": Category.FREE_PERKS,
    "免費點心": Category.FREE_PERKS,
    "抽獎": Category.FREE_PERKS,
    "贈品": Category.FREE_PERKS,
    # 學分門檻型：學習護照類別
    **{c: Category.CREDIT_HOURS for c in LEARNING_PASSPORT_CATEGORIES},
    "講座": Category.CREDIT_HOURS,
    # 表演娛樂型
    "成果發表": Category.PERFORMANCE,
    "演唱會": Category.PERFORMANCE,
    "迎新": Category.PERFORMANCE,
    "音樂節": Category.PERFORMANCE,
    "展覽": Category.PERFORMANCE,
    # 職涯成長型
    "實習說明會": Category.CAREER,
    "企業參訪": Category.CAREER,
    "黑客松": Category.CAREER,
    "競賽": Category.CAREER,
    "工作坊": Category.CAREER,
    "職涯講座": Category.CAREER,
}


# ---------------------------------------------------------------------------
# 子模型
# ---------------------------------------------------------------------------
class HourEntry(BaseModel):
    """一項時數：類別 + 時數，例如「人文藝術 3 小時」、「批判思考 0.66 小時」。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    category: str = Field(min_length=1, max_length=30)
    hours: float = Field(ge=0, le=100)


# ---------------------------------------------------------------------------
# 主模型：一筆 = 一個場次
# ---------------------------------------------------------------------------
class Event(BaseModel):
    """一個活動場次。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # 識別
    event_id: str = Field(min_length=1, max_length=80, description="主鍵（場次層級）；用 make_event_id() 產生")
    activity_id: str | None = Field(default=None, max_length=80, description="所屬活動；同一活動的場次共用")

    # 基本資訊
    title: str = Field(min_length=1, max_length=120, description="場次名稱")
    host: str = Field(min_length=1, max_length=60, description="承辦單位")
    speaker: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=3000)
    audience: str | None = Field(default=None, max_length=100, description="參與對象，例如：全部學生")
    contact_email: str | None = Field(default=None, max_length=100, description="單位信箱；不存承辦人姓名")

    # 時間地點
    start_at: datetime = Field(description="場次開始，必須帶時區；儲存時統一轉成 +08:00")
    end_at: datetime | None = None
    location: str | None = Field(default=None, max_length=120)
    location_code: str | None = Field(default=None, max_length=30, description="館舍代碼，未來連到校園地圖")
    mode: Mode = Mode.ONSITE

    # 報名
    registration_method: RegistrationMethod = RegistrationMethod.NONE
    registration_url: HttpUrl | None = None
    registration_start: datetime | None = None
    registration_end: datetime | None = None
    capacity: int | None = Field(default=None, ge=0, description="報名人數上限")
    registered_count: int | None = Field(default=None, ge=0, description="目前報名人數")

    # 分類與時數
    tags: list[str] = Field(default_factory=list, max_length=10, description="必須來自 TAG_CATALOG")
    learning_passport_hours: list[HourEntry] = Field(default_factory=list)
    learning_passport_condition: PassportCondition | None = None
    soft_skill_hours: list[HourEntry] = Field(default_factory=list)
    perks: str | None = Field(default=None, max_length=200, description="福利說明，例如：前 50 名送便當")

    # 連結與素材
    info_url: HttpUrl | None = Field(default=None, description="活動頁面")
    poster_key: str | None = Field(default=None, description="S3 物件路徑，例如 posters/2026/11/xxx.webp")

    # 來源與狀態
    source: Source
    source_url: HttpUrl | None = Field(default=None, description="原始頁面網址，爬蟲去重用")
    status: Status = Status.DRAFT
    created_at: datetime = Field(default_factory=now_taipei)
    updated_at: datetime = Field(default_factory=now_taipei)

    # ---- 驗證與轉換 ----
    @model_validator(mode="before")
    @classmethod
    def _drop_computed(cls, data: Any) -> Any:
        """讀回 JSON 或 DynamoDB 時，忽略自動計算的欄位。"""
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if k not in ("month", "categories", "is_full")}
        return data

    @field_validator("start_at", "end_at", "registration_start", "registration_end", "created_at", "updated_at")
    @classmethod
    def _to_taipei(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return v
        if v.tzinfo is None:
            raise ValueError("時間必須帶時區，例如 2026-11-06T19:00:00+08:00")
        return v.astimezone(TAIPEI).replace(microsecond=0)

    @field_validator("tags")
    @classmethod
    def _check_tags(cls, v: list[str]) -> list[str]:
        unknown = [t for t in v if t not in TAG_CATALOG]
        if unknown:
            raise ValueError(f"未知標籤 {unknown}，請從 TAG_CATALOG 選擇")
        return list(dict.fromkeys(v))

    @model_validator(mode="after")
    def _check_and_derive(self) -> "Event":
        if self.end_at and self.end_at < self.start_at:
            raise ValueError("end_at 不能早於 start_at")
        if self.registration_start and self.registration_end and self.registration_end < self.registration_start:
            raise ValueError("registration_end 不能早於 registration_start")
        if self.capacity is not None and self.registered_count is not None and self.registered_count > self.capacity:
            raise ValueError("registered_count 不能大於 capacity")
        if self.registration_method == RegistrationMethod.EXTERNAL and not self.registration_url:
            raise ValueError("外部報名必須提供 registration_url")
        # 學習護照類別自動帶入標籤，學生訂閱「人文藝術」就會收到
        derived = [h.category for h in self.learning_passport_hours if h.category in TAG_CATALOG]
        self.tags = list(dict.fromkeys(self.tags + derived))
        if not self.tags:
            raise ValueError("至少需要一個標籤（或有學習護照時數）")
        return self

    # ---- 自動計算的欄位 ----
    @computed_field  # type: ignore[prop-decorator]
    @property
    def month(self) -> str:
        """GSI 分區鍵，YYYY-MM（台灣時間）。"""
        return self.start_at.strftime("%Y-%m")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def categories(self) -> list[Category]:
        """由標籤推導出的活動類型。"""
        return list(dict.fromkeys(TAG_CATALOG[t] for t in self.tags))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_full(self) -> bool:
        """是否已額滿。"""
        return self.capacity is not None and self.registered_count is not None and self.registered_count >= self.capacity

    # ---- DynamoDB 轉換 ----
    def to_dynamo_item(self) -> dict[str, Any]:
        """轉成可寫入 DynamoDB 的 dict：時間為 ISO 字串、數字為 Decimal、省略空值。"""
        return _floats_to_decimal(self.model_dump(mode="json", exclude_none=True))

    @classmethod
    def from_dynamo_item(cls, item: dict[str, Any]) -> "Event":
        return cls.model_validate(_decimals_to_float(item))


class PosterExtraction(BaseModel):
    """Gemini 海報辨識的輸出格式：只含 AI 能從海報看出來的欄位。

    用法：PosterExtraction.model_json_schema() 當作 Gemini 的 response schema，
    回傳的 JSON 用 PosterExtraction.model_validate_json() 驗證，再交給主辦方確認。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str | None = None
    host: str | None = None
    speaker: str | None = None
    description: str | None = None
    start_at: str | None = Field(default=None, description="ISO 8601，帶 +08:00；看不出來就留空")
    end_at: str | None = Field(default=None, description="ISO 8601，帶 +08:00")
    location: str | None = None
    mode: Mode | None = None
    tags: list[str] = Field(default_factory=list, description="只能從 TAG_CATALOG 中選")
    learning_passport_hours: list[HourEntry] = Field(default_factory=list)
    perks: str | None = None
    registration_url: str | None = None


# ---------------------------------------------------------------------------
# 工具函式
# ---------------------------------------------------------------------------
def make_event_id(source: Source, unique_key: str) -> str:
    """產生穩定的 event_id。同一來源、同一 key（例如場次編號或原始網址）永遠得到同一個 id，可用來去重。"""
    digest = hashlib.sha1(f"{source.value}|{unique_key}".encode("utf-8")).hexdigest()[:12]
    return f"{source.value}-{digest}"


def load_sample_events(path: str | None = None) -> list[Event]:
    """讀取 sample_events.json，開發時當作假資料庫使用。"""
    file = Path(path) if path else Path(__file__).with_name("sample_events.json")
    return [Event.model_validate(e) for e in json.loads(file.read_text(encoding="utf-8"))]


def _floats_to_decimal(obj: Any) -> Any:
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _floats_to_decimal(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_floats_to_decimal(v) for v in obj]
    return obj


def _decimals_to_float(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    if isinstance(obj, dict):
        return {k: _decimals_to_float(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decimals_to_float(v) for v in obj]
    return obj
