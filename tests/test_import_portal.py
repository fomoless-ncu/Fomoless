"""shared/import_portal.py 的測試：用 moto 模擬 DynamoDB，不連真的 AWS。"""

import json
import logging
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared import import_portal  # noqa: E402
from shared.import_portal import (  # noqa: E402
    convert_items,
    delete_events,
    load_items,
    main,
    sample_event_ids,
    write_events,
)
from shared.models import Event, Source, Status, load_sample_events, make_event_id  # noqa: E402
from shared.seed import create_local_table  # noqa: E402

TABLE = "fomoless-test-events"
REPO = Path(__file__).resolve().parent.parent


def fake_convert(item: dict) -> Event:
    """代替 from_portal_crawler()，只用到 pk / title / start_at / description。"""
    return Event(
        event_id=make_event_id(Source.CRAWLER_PORTAL, item["pk"]),
        title=item["title"],
        description=item.get("description"),
        host="測試單位",
        start_at=item["start_at"],
        source=Source.CRAWLER_PORTAL,
        status=Status.PUBLISHED,
    )


def crawler_item(n: int, **overrides) -> dict:
    item = {"pk": f"portal.activity#{n}#115A{n:05d}", "title": f"活動 {n}", "start_at": "2026-11-18T12:00:00+08:00"}
    item.update(overrides)
    return item


@pytest.fixture
def table(monkeypatch):
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(k, "testing")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name=import_portal.REGION)
        create_local_table(dynamodb, TABLE)
        yield dynamodb.Table(TABLE)


def all_ids(table) -> set[str]:
    return {i["event_id"] for i in table.scan(ProjectionExpression="event_id")["Items"]}


def write_json(tmp_path: Path, data) -> Path:
    path = tmp_path / "crawl.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


# ---- 讀檔 ----
def test_load_items_accepts_list_and_single_dict(tmp_path):
    assert len(load_items(write_json(tmp_path, [crawler_item(1), crawler_item(2)]))) == 2
    assert load_items(write_json(tmp_path, crawler_item(1))) == [crawler_item(1)]


def test_load_items_rejects_other_types(tmp_path):
    with pytest.raises(ValueError):
        load_items(write_json(tmp_path, "not a list"))


# ---- 轉換 ----
def test_convert_skips_failures_with_warning(caplog):
    items = [crawler_item(1), crawler_item(2, start_at="沒有時區"), {"title": "缺 pk"}, "不是 dict", crawler_item(3)]
    with caplog.at_level(logging.WARNING, logger="shared.import_portal"):
        events, skipped = convert_items(items, fake_convert)

    assert [e.title for e in events] == ["活動 1", "活動 3"]
    assert skipped == 3
    warnings = [r.getMessage() for r in caplog.records]
    assert len(warnings) == 3
    assert "第 2 筆" in warnings[0] and "portal.activity#2#115A00002" in warnings[0]


def test_convert_dedupes_same_event_id_keeping_last(caplog):
    items = [crawler_item(1, title="舊標題"), crawler_item(1, title="新標題")]
    with caplog.at_level(logging.WARNING, logger="shared.import_portal"):
        events, skipped = convert_items(items, fake_convert)

    assert [e.title for e in events] == ["新標題"]
    assert skipped == 1
    assert "重複" in caplog.records[0].getMessage()


def test_convert_redacts_description():
    item = crawler_item(1, description="報名請洽王小姐 分機12345，email: a1@g.ncu.edu.tw")
    events, _ = convert_items([item], fake_convert)

    desc = events[0].description
    assert "王小姐" not in desc and "12345" not in desc and "a1@" not in desc
    assert desc.startswith("報名請洽")


# ---- 寫入 ----
def test_write_events_round_trip(table):
    events, _ = convert_items([crawler_item(1)], fake_convert)
    assert write_events(table, events) == 1

    item = table.get_item(Key={"event_id": events[0].event_id})["Item"]
    assert Event.from_dynamo_item(item).title == "活動 1"
    assert item["month"] == "2026-11"  # GSI 分區鍵有寫進去


def test_rerun_does_not_duplicate(table):
    items = [crawler_item(n) for n in range(1, 4)]
    for _ in range(2):
        events, _ = convert_items(items, fake_convert)
        write_events(table, events)
    assert table.scan(Select="COUNT")["Count"] == 3


# ---- 刪除假資料 ----
def test_sample_event_ids_match_sample_file():
    ids = sample_event_ids()
    assert len(ids) == 16
    assert ids == [e.event_id for e in load_sample_events()]


def test_delete_samples_keeps_real_data(table):
    write_events(table, load_sample_events())
    real, _ = convert_items([crawler_item(1)], fake_convert)
    write_events(table, real)

    assert delete_events(table, sample_event_ids()) == 16
    assert all_ids(table) == {real[0].event_id}
    # 再刪一次：已經不存在，回報 0 筆
    assert delete_events(table, sample_event_ids()) == 0


# ---- CLI ----
def test_main_imports_and_prints_summary(table, tmp_path, capsys):
    write_events(table, load_sample_events())
    path = write_json(tmp_path, [crawler_item(1), crawler_item(2), crawler_item(3, start_at="壞掉")])

    assert main([str(path), "--table", TABLE, "--delete-samples"], convert=fake_convert) == 0

    out = capsys.readouterr().out
    assert "已刪除 16 筆假資料" in out
    assert "成功 2 筆、跳過 1 筆" in out
    assert table.scan(Select="COUNT")["Count"] == 2


def test_main_dry_run_does_not_touch_aws(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(import_portal.boto3, "resource", lambda *a, **k: pytest.fail("dry-run 不應連 AWS"))
    path = write_json(tmp_path, [crawler_item(1), {"pk": "x"}])

    assert main([str(path), "--dry-run"], convert=fake_convert) == 0
    assert "成功 1 筆、跳過 1 筆" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [
        ["crawl.json"],  # 沒有 --table 也沒有 --dry-run
        ["crawl.json", "--table", "fomoless-prod-events"],  # prod 要明確允許
    ],
)
def test_main_rejects_unsafe_args(argv):
    with pytest.raises(SystemExit):
        main(argv, convert=fake_convert)


# ---- 搭配真正的轉換函式（PR #7 合併後才會執行）----
def test_real_adapter_converts_crawler_samples():
    pytest.importorskip("shared.adapters.portal")
    samples = sorted((REPO / "crawler" / "samples").glob("*.json"))
    items = [item for p in samples for item in load_items(p)]

    events, skipped = convert_items(items, import_portal._default_converter())
    assert skipped == 0
    assert len(events) == len(items) == 3
