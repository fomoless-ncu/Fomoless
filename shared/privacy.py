"""共用的個資遮蔽與偵測，供活動匯入、Query 與 Push 使用。

規則刻意保守（寧可多遮），只處理自由文字欄位（例如 description），不碰講者、主辦單位。
"""

from __future__ import annotations

import re

MASK = "（已遮蔽）"

# 注意：Python 的 \w 會匹配中文，這裡一律用 ASCII 字元類別，免得把前後的中文一起吃掉
_PATTERNS: list[re.Pattern[str]] = [
    # 電子郵件：shiang2216@g.ncu.edu.tw
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"),
    # 手機：0912-345-678、0912345678
    re.compile(r"(?<![A-Za-z0-9_/=.])09\d{2}[-\s]?\d{3}[-\s]?\d{3}(?![A-Za-z0-9])"),
    # 市話：03-422-7151、03-4227151、(03)4227151；前後不能接英數，避免誤遮網址參數
    re.compile(r"(?<![A-Za-z0-9_/=.])\(?0\d{1,2}\)?[-\s]?\d{3,4}[-\s]?\d{3,4}(?![A-Za-z0-9])"),
    # 分機：分機65059、分機 66065、分機：57312、轉 57191、#27058
    re.compile(r"(?:分機|轉|#)\s*[:：]?\s*\d{2,6}(?!\d)"),
    # 承辦人稱謂：王小姐、葉專員（姓氏＋稱謂）。「姓氏」位置若是單位字尾就不遮，
    # 避免遮到講者頭銜（例如「教育協會專員」「某公司專員」）
    re.compile(r"(?![會司處組室院心所部館局])[一-鿿](?:小姐|專員)"),
]


def contains_pii(text: str | None) -> bool:
    """使用與 redact_pii 相同的規則，檢查文字是否含可辨識的聯絡資訊。

    用於 AI 輸出驗證，不修改原文；已遮蔽的文字不會再次被當成個資。
    規則只涵蓋上述格式，不能保證辨識所有姓名或所有形式的個資。
    """
    return bool(text) and any(pattern.search(text) for pattern in _PATTERNS)


def redact_pii(text: str | None) -> str | None:
    """把信箱、電話、分機、承辦人稱謂換成「（已遮蔽）」。None 原樣回傳。"""
    if not text:
        return text
    for pattern in _PATTERNS:
        text = pattern.sub(MASK, text)
    # 相鄰的多個遮蔽合併成一個，例如「03-4227151（已遮蔽）」→ 只留一個
    return re.sub(rf"(?:{re.escape(MASK)}\s*)+", MASK, text)
