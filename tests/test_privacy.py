"""shared/privacy.py 的測試。範例改寫自 Portal 活動內文的常見寫法（值皆為假資料）。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.privacy import MASK, contains_pii, redact_pii  # noqa: E402


@pytest.mark.parametrize(
    "text, leaked",
    [
        ("聯絡資訊：電話：03-422-7151 分機 12345 陳小姐 Email： someone2026@g.ncu.edu.tw", ["7151", "12345", "陳小姐", "someone2026"]),
        ("※活動聯絡人：系辦公室 林專員 分機65000", ["林專員", "65000"]),
        ("歡迎來信test@ncu.edu.tw或電洽事務組張小姐03-4227151分機：50000", ["test@", "張小姐", "4227151", "50000"]),
        ("聯絡資訊： 研發處 (03)4227151#20000", ["4227151", "20000"]),
        ("洽詢電話： 03-4227151 轉 50001", ["4227151", "50001"]),
        ("手機 0912-345-678", ["0912", "345-678"]),
    ],
)
def test_redacts_contacts(text, leaked):
    assert contains_pii(text)
    out = redact_pii(text)
    for s in leaked:
        assert s not in out
    assert MASK in out
    assert not contains_pii(out)


@pytest.mark.parametrize(
    "text",
    [
        "日期：2026 年 12 月 4 日 時間：12:10 - 14:00",
        "115/11/18(三)12:00~13:00 地點：科學五館111教室",
        "報名短網址 https://pse.is/9v792b",
        "上課網址：https://ncu-edu.webex.com/j.php?MTID=m7d8dde8680943922673b356288ccfba7",
        "講者：楊承融先生 主持人：翁老師",
        "【講者】某某/彩虹生命教育協會專員 【時間】",
        "1151-1-AI與校園著作權",
    ],
)
def test_keeps_non_pii(text):
    assert redact_pii(text) == text
    assert not contains_pii(text)


def test_adjacent_masks_are_merged():
    assert redact_pii("葉小姐03-4227151") == MASK


def test_none_and_empty():
    assert redact_pii(None) is None
    assert redact_pii("") == ""
    assert not contains_pii(None)
    assert not contains_pii("")
    assert not contains_pii(MASK)
