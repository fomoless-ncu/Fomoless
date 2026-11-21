"""把 Portal 爬蟲輸出的 JSON 匯入 DynamoDB。

流程：爬蟲 JSON → from_portal_crawler() 轉成 Event → to_dynamo_item() → batch_writer() 寫入

用法（在 repo 根目錄執行）：
    # 只轉換、不寫入，先看會成功幾筆（不需要 AWS 權限）
    python -m shared.import_portal output/portal_activities.json --dry-run

    # 匯入 dev 資料表（需要 AWS 權限）
    python -m shared.import_portal output/portal_activities.json --table fomoless-dev-events

    # 匯入前先刪掉 sample_events.json 的 16 筆假資料
    python -m shared.import_portal output/portal_activities.json --table fomoless-dev-events --delete-samples

    # 本機 DynamoDB Local（先啟動：docker run -p 8000:8000 amazon/dynamodb-local）
    python -m shared.import_portal output/portal_activities.json --table fomoless-dev-events --local --create

event_id 由爬蟲的 pk 固定產生，重跑只會覆寫同一筆，不會產生重複資料。
匯入資料不是部署，可以在本機執行；完整 JSON 可能含個資，不要 commit。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import boto3

from shared.models import Event
from shared.privacy import redact_pii

logger = logging.getLogger(__name__)

Converter = Callable[[dict], Event]

REGION = "ap-east-2"
SAMPLE_EVENTS_PATH = Path(__file__).parent / "sample_events.json"


def _default_converter() -> Converter:
    """延遲 import，讓沒有 adapters 的環境也能 import 這個模組（例如測試）。"""
    from shared.adapters.portal import from_portal_crawler

    return from_portal_crawler


# ---------------------------------------------------------------------------
# 讀取與轉換
# ---------------------------------------------------------------------------
def load_items(path: str | Path) -> list[dict]:
    """讀爬蟲 JSON。完整 crawl 輸出是 list；單筆範例檔是 dict，也一併接受。"""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return data
    raise ValueError(f"{path} 的最外層必須是 list 或 dict，實際是 {type(data).__name__}")


def _first_line(exc: Exception) -> str:
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text.splitlines()[0] if text else ''}"


def convert_items(items: Iterable[Any], convert: Converter) -> tuple[list[Event], int]:
    """逐筆轉換並遮蔽 description 的個資。失敗或 event_id 重複的記 warning 並跳過。

    回傳 (成功的 Event, 跳過筆數)。同一個 event_id 出現多次時保留最後一筆。
    """
    by_id: dict[str, Event] = {}
    skipped = 0
    for i, item in enumerate(items, start=1):
        pk = item.get("pk") if isinstance(item, dict) else None
        try:
            if not isinstance(item, dict):
                raise TypeError(f"每筆資料必須是 dict，實際是 {type(item).__name__}")
            event = convert(item)
            # 活動內文常夾帶承辦人姓名、分機、信箱：寫入前一律遮蔽（不存個資）
            event = event.model_copy(update={"description": redact_pii(event.description)})
        except Exception as exc:  # 任何一筆壞掉都不能中斷整批匯入
            logger.warning("第 %d 筆轉換失敗，跳過（pk=%s）：%s", i, pk, _first_line(exc))
            skipped += 1
            continue
        if event.event_id in by_id:
            logger.warning("第 %d 筆 event_id 重複，以後出現的為準（pk=%s）", i, pk)
            skipped += 1
        by_id[event.event_id] = event
    return list(by_id.values()), skipped


# ---------------------------------------------------------------------------
# DynamoDB
# ---------------------------------------------------------------------------
def write_events(table, events: Iterable[Event]) -> int:
    """批次寫入；同 event_id 直接覆寫。回傳寫入筆數。"""
    count = 0
    with table.batch_writer(overwrite_by_pkeys=["event_id"]) as batch:
        for event in events:
            batch.put_item(Item=event.to_dynamo_item())
            count += 1
    return count


def sample_event_ids(path: str | Path = SAMPLE_EVENTS_PATH) -> list[str]:
    """sample_events.json 的 event_id。假資料裡也有 crawler_portal 來源，所以只能靠 id 判斷。"""
    with open(path, encoding="utf-8") as f:
        return [e["event_id"] for e in json.load(f)]


def delete_events(table, event_ids: Iterable[str]) -> int:
    """刪除資料表中存在的 event_id，回傳實際刪掉的筆數。"""
    existing = [
        eid
        for eid in dict.fromkeys(event_ids)
        if "Item" in table.get_item(Key={"event_id": eid}, ProjectionExpression="event_id")
    ]
    with table.batch_writer() as batch:
        for eid in existing:
            batch.delete_item(Key={"event_id": eid})
    return len(existing)


def _dynamodb_resource(local: bool):
    if local:
        return boto3.resource(
            "dynamodb", endpoint_url="http://localhost:8000", region_name=REGION,
            aws_access_key_id="local", aws_secret_access_key="local",
        )
    return boto3.resource("dynamodb", region_name=REGION)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="匯入 Portal 爬蟲 JSON 到 DynamoDB")
    parser.add_argument("json_path", help="爬蟲輸出的 JSON 檔")
    parser.add_argument("--table", help="資料表名稱，例如 fomoless-dev-events（--dry-run 時可省略）")
    parser.add_argument("--dry-run", action="store_true", help="只轉換並印出結果，不連 AWS")
    parser.add_argument("--delete-samples", action="store_true", help="匯入前刪掉 sample_events.json 的假資料")
    parser.add_argument("--local", action="store_true", help="寫入 http://localhost:8000 的 DynamoDB Local")
    parser.add_argument("--create", action="store_true", help="本機資料表不存在時自動建立（搭配 --local）")
    parser.add_argument("--allow-prod", action="store_true", help="允許寫入名稱含 prod 的資料表")
    return parser


def main(argv: list[str] | None = None, convert: Converter | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.dry_run and not args.table:
        parser.error("沒有 --dry-run 時必須指定 --table")
    if args.table and "prod" in args.table and not args.allow_prod:
        parser.error(f"{args.table} 看起來是 prod 資料表；確定要寫入請加 --allow-prod")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    items = load_items(args.json_path)
    events, skipped = convert_items(items, convert or _default_converter())

    if args.dry_run:
        print(f"[dry-run] 共 {len(items)} 筆：成功 {len(events)} 筆、跳過 {skipped} 筆（未寫入）")
        return 0

    dynamodb = _dynamodb_resource(args.local)
    if args.local and args.create:
        from shared.seed import create_local_table

        create_local_table(dynamodb, args.table)
    table = dynamodb.Table(args.table)

    # 先刪假資料再寫入：萬一真實資料的 id 和假資料撞到，最後留下的是真實資料
    if args.delete_samples:
        deleted = delete_events(table, sample_event_ids())
        print(f"已刪除 {deleted} 筆假資料")

    written = write_events(table, events)
    print(f"匯入 {args.table}：成功 {written} 筆、跳過 {skipped} 筆")
    return 0


if __name__ == "__main__":
    sys.exit(main())
