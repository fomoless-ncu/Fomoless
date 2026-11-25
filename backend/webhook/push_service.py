"""推播內容串接：篩選 → 精簡輸入 → AI → 驗證 → 事實回填 → 長度檢查。

本模組不讀取訂閱／活動資料表，也不發送 LINE 訊息。呼叫端提供 events 與 user_tags。
使用方式（get_secret 可沿用 app.py 的函式）：

    writer = create_push_writer(get_secret)
    result = generate_push(events, user_tags, now=now, writer=writer)
    if result.status != "skipped":
        # 將 result.text 與呼叫端保留的收件者 ID 交給 LINE 發送流程。
        ...

writer  None 表示停用 AI，直接使用固定模板。status 代表文案結果，不代表已送達。
"""

import html
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from llm.activity_push import (
    MAX_MESSAGE_CHARS,
    MAX_RECOMMENDATIONS,
    SUMMARY_MAX_CHARS,
    ActivityPushWriter,
    PushCandidate,
    PushDraft,
    PushInput,
)
from llm.factory import required_setting
from push_candidates import event_datetime, normalize_user_tags, select_push_candidates
from shared.privacy import contains_pii, redact_pii
from week import TAIPEI

logger = logging.getLogger(__name__)
HEADER = "📅 本週活動精選"  # 每則訊息的標題
FALLBACK_OPENING = "依你訂閱的主題，推薦你以下活動!"  # 停用 AI 或 AI 失敗時使用的開場白
# 把資料庫的認證條件轉成使用者看得懂的文字
CONDITION_LABELS = {  
    "sign_in": "需簽到",
    "sign_out": "需簽退",
    "both": "需簽到及簽退",
    "other": "其他認證條件請見活動資訊",
}

# 個資規則由 shared.privacy 統一維護；SAM 透過 SharedPythonLayer 提供 shared 套件
# 建立網址比對規則，供後面移除或檢查文字中的網址
URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
# 檢查 AI 文案是否提到應由原始資料提供的事實
FACT_CLAIM_PATTERN = re.compile(
    r"免費|便當|餐點|點心|贈品|抽獎|時數|小時|學分|認列|名額|報名|簽到|簽退|地點|保證"
)


@dataclass(frozen=True)
class PushResult:
    """交給發送端的結果；略過時 text=None、event_ids 為空清單。"""

    status: Literal["generated", "fallback", "skipped"]  # AI 成功、固定模板、略過推播
    text: str | None  # 最終推播文字；略過時是 None
    event_ids: list[str]  # 最終文字包含的場次 ID
    fallback_reason: str | None = None # 固定原因代碼，避免把 API 錯誤、金鑰或模型原文帶給發送端


def create_push_writer(get_secret: Callable[[str], str]) -> ActivityPushWriter:
    """選擇模型實作，沿用查詢的 AI_PROVIDER 與金鑰命名。

    設定缺少時拋出 ValueError，呼叫端可記錄錯誤並傳 writer=None 使用固定模板。
    """
    provider = required_setting("AI_PROVIDER").lower()
    if provider == "gemini":
        from llm.gemini_push import GeminiActivityPushWriter

        return GeminiActivityPushWriter(
            model=required_setting("GEMINI_MODEL"),
            api_key=get_secret("gemini-api-key"),
        )
    if provider == "azure":
        from llm.azure_push import AzureActivityPushWriter

        return AzureActivityPushWriter(
            base_url=required_setting("AZURE_OPENAI_BASE_URL"),
            deployment=required_setting("AZURE_OPENAI_DEPLOYMENT"),
            api_key=get_secret("azure-openai-api-key"),
        )
    raise ValueError(f"不支援的 AI_PROVIDER：{provider}")


def _text(value) -> str:
    """將換行及多餘空白整理成一行。"""
    return " ".join(str(value or "").split())


def _public_text(value) -> str:
    """清理送給 LLM 的文字；去 HTML、聯絡方式及網址。"""
    value = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", "", str(value or ""), flags=re.I | re.S)
    value = html.unescape(re.sub(r"<[^>]+>", " ", value))
    return _text(URL_PATTERN.sub("", redact_pii(value) or ""))


def _hours_text(entries: list[dict]) -> str:
    """整理學習護照或軟實力時數；保留原文的分類與時數，去除多餘空白。"""
    return "、".join(f"{_text(entry['category'])} {entry['hours']}h" for entry in entries)


def build_push_input(events: list[dict], user_tags: list[str]) -> PushInput:
    """建立新物件(LLM 輸入)；只傳白名單欄位，不把整筆活動直接送給模型。"""
    candidates = []
    for event in events:
        hours = event.get("learning_passport_hours") or []
        passport = None
        if hours:
            condition = CONDITION_LABELS.get(
                event.get("learning_passport_condition"), "認證條件待確認",
            )
            passport = f"{_hours_text(hours)}（{condition}）"
        soft = event.get("soft_skill_hours") or []
        # 建立模型候選資料
        candidates.append(PushCandidate(
            event_id=event["event_id"],
            title=_public_text(event["title"]),
            tags=[_public_text(tag) for tag in event.get("tags", [])],
            summary=_public_text(event.get("description"))[:SUMMARY_MAX_CHARS],  # 截短原文，保留前 180 個字元作為 summary (不截短福利／認證摘要 )
            perks=_public_text(event["perks"]) if event.get("perks") else None,  # 若活動沒有福利，設為 None
            learning_passport_summary=_public_text(passport) if passport else None,
            soft_skill_summary=(
                _public_text(f"{_hours_text(soft)}（認證條件請見活動資訊）") if soft else None
            ),
        ))
    # 產生 Gemini、Azure 共用的輸入物件。Pydantic 會檢查資料是否符合 Schema
    return PushInput(user_tags=normalize_user_tags(user_tags), events=candidates)


def _valid_url(value) -> str | None:
    """檢查可放進訊息的網址；僅接受完整 HTTP(S) 網址；原樣保留 query string，不做切片縮短。"""
    if not value:
        return None
    value = str(value)
    # 不接受空白、換行與控制字元，避免被 LLM 或 LINE 斷行或截斷
    if any(char.isspace() or ord(char) < 32 for char in value):
        return None
    
    try:
        # 把網址拆成協定、主機、路徑等部分
        parts = urlsplit(value)
        # 只接受有主機名稱的 HTTP／HTTPS 網址
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return None
        # 拒絕帶有帳號密碼的網址
        if parts.username is not None or parts.password is not None:
            return None
        _ = parts.port  # 也拒絕不是數字或超出範圍的 port
    except ValueError:
        return None
    return value


def _event_block(event: dict, reason: str | None = None) -> str | None:
    """組合單筆活動文字；必要資訊不完整時，不產生可發送區塊。"""
    when = event_datetime(event["start_at"]).astimezone(TAIPEI)
    lines = [f"• {when:%m/%d %H:%M} {_text(event['title'])}"]
    mode = {"onsite": "實體", "online": "線上", "hybrid": "實體及線上"}.get(
        event.get("mode", "onsite"), "形式待確認",
    )
    lines.append(f"  {mode}｜{_text(event.get('location')) or '地點待確認'}")
    if event.get("audience"):
        # 只有訂閱標籤不足以判斷院系或年級資格，顯示原始對象供學生確認。
        lines.append(f"  對象：{_text(event['audience'])}")
    if event.get("perks"):
        lines.append(f"  福利：{_text(event['perks'])}")
    if event.get("learning_passport_hours"):
        condition = CONDITION_LABELS.get(
            event.get("learning_passport_condition"), "認證條件待確認",
        )
        lines.append(f"  學習護照：{_hours_text(event['learning_passport_hours'])}（{condition}）")
    if event.get("soft_skill_hours"):
        lines.append(f"  軟實力：{_hours_text(event['soft_skill_hours'])}（認證條件請見活動資訊）")

    method = event.get("registration_method", "none")
    registration_url = _valid_url(event.get("registration_url"))
    info_url = _valid_url(event.get("info_url"))
    if method == "none":
        lines.append("  免報名")
        if info_url:
            lines.append(f"  活動：{info_url}")
    elif registration_url:
        lines.append(f"  報名：{registration_url}")
    elif info_url:
        lines.append(f"  報名資訊請見活動頁：{info_url}")
    else:
        return None
    
    # 推薦理由由模型產生，或 fallback 時不顯示。若有理由，換行縮排顯示
    if reason:
        lines.append(f"  推薦：{_text(reason)}")
    return "\n".join(lines)


def _validate_draft(draft: PushDraft, candidates: list[dict]) -> None:
    """檢查 AI 的推薦內容；Schema 主要檢查格式，這個函式補上推薦規則的檢查。"""
    expected_ids = {event["event_id"] for event in candidates}
    actual_ids = {item.event_id for item in draft.recommendations}
    if actual_ids != expected_ids:
        raise ValueError("模型必須為所有選定活動提供文案，不得新增或漏掉活動")
    
    # 取得開場白與推薦理由
    texts = [draft.opening, *(item.reason for item in draft.recommendations)] 

    # 拒絕網址與個資、數字與部分中文數值表述，以及福利、認證與參加條件的事實陳述
    for text in texts:
        if URL_PATTERN.search(text) or contains_pii(text):
            raise ValueError("AI 文案不得包含網址或聯絡資訊")
        if re.search(r"\d|[零一二三四五六七八九十百]+\s*(?:小時|分鐘|名額|學分)", text):
            raise ValueError("AI 文案不得自行產生數值事實")
        if FACT_CLAIM_PATTERN.search(text):
            raise ValueError("福利、認證與參加條件應由程式填入")


def _assemble(
    events: list[dict], opening: str, reasons: dict[str, str],
) -> tuple[str, list[str]]:
    """組裝完整訊息並控制長度；先縮短開場、移除理由，再減少整筆活動；不切斷網址或必要條件。"""
    # 選中的活動按時間排序
    kept = sorted(events, key=lambda event: (event_datetime(event["start_at"]), event["event_id"]))
    
    # 把標題、開場、活動區塊接起來
    def compose(intro: str, include_reasons: bool) -> str:
        blocks = [
            _event_block(event, reasons.get(event["event_id"]) if include_reasons else None)
            for event in kept
        ]
        return "\n\n".join(part for part in [HEADER, intro, *blocks] if part)
    
    # 嘗試三種版本：保留開場與推薦理由、移除開場，保留理由、開場與理由都移除
    for intro, include_reasons in ((opening, True), ("", True), ("", False)):
        text = compose(intro, include_reasons)
        # 只要不超過 500 字元就回傳
        if len(text) <= MAX_MESSAGE_CHARS:
            return text, [event["event_id"] for event in kept] 
    # 逐筆移除活動，直到剩下一筆或符合長度限制；若剩一筆仍太長，回傳空字串與空清單
    while len(kept) > 1:
        kept.pop()
        text = compose("", False)
        if len(text) <= MAX_MESSAGE_CHARS:
            return text, [event["event_id"] for event in kept]
    return "", []


def generate_push(
    events: list[dict],
    user_tags: list[str],
    *,
    now: datetime,
    writer: ActivityPushWriter | None = None,
    candidate_limit: int = 5,
) -> PushResult:
    """產生最多 5 場、完整訊息 500 Unicode code points 內的推播。

    Python len(str) 包含空白、換行、網址與 emoji code points，與報告計數方式一致。
    呼叫失敗／格式或 ID 驗證失敗時，使用 Python 已選定的最多 5 場活動套固定模板。
    不在這裡判斷 LINE 送達狀態、預算結算或保存使用者訂閱。
    """
    tags = normalize_user_tags(user_tags)
    candidates = select_push_candidates(events, tags, now=now, limit=candidate_limit)
    if not candidates:
        return PushResult("skipped", None, [], "no_candidates")

    sendable = []
    for event in candidates:
        try:
            block = _event_block(event)
            # 連單筆活動的必要資訊都放不下時先排除，避免浪費模型呼叫。
            if block and len(f"{HEADER}\n\n{block}") <= MAX_MESSAGE_CHARS:
                sendable.append(event)
        except (KeyError, TypeError, ValueError):
            continue
    if not sendable:
        return PushResult("skipped", None, [], "no_sendable_events")

    status: Literal["generated", "fallback"] = "fallback"
    fallback_reason = "ai_disabled" if writer is None else None
    selected = sendable[:MAX_RECOMMENDATIONS]
    opening, reasons = FALLBACK_OPENING, {}
    if writer is not None:
        try:
            request = build_push_input(selected, tags)
            response = writer.generate(request)
            # 不信任替代實作／model_construct 略過驗證的物件，再驗證輸出契約。
            draft = PushDraft.model_validate(
                response.model_dump() if isinstance(response, PushDraft) else response,
            )
            _validate_draft(draft, selected)
        except Exception as error:
            # 不記錄原文或錯誤訊息，避免將 API 金鑰／活動聯絡資訊寫入日誌。
            logger.warning("Push generation failed: %s", type(error).__name__)
            fallback_reason = "generation_failed"
        else:
            opening = draft.opening
            reasons = {item.event_id: item.reason for item in draft.recommendations}
            status = "generated"

    text, event_ids = _assemble(selected[:MAX_RECOMMENDATIONS], opening, reasons)
    if not event_ids:
        return PushResult("skipped", None, [], "message_too_long")
    return PushResult(status, text, event_ids, fallback_reason)
