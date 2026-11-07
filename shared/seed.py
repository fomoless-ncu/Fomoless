"""把 sample_events.json 寫入 DynamoDB（dev 資料表或 DynamoDB Local）。

用法（在 repo 根目錄執行）：
    # 寫入 AWS 上的 dev 資料表（需要 AWS 權限）
    python -m shared.seed --table fomoless-dev-events

    # 寫入本機 DynamoDB Local（先啟動：docker run -p 8000:8000 amazon/dynamodb-local）
    python -m shared.seed --table fomoless-dev-events --local --create

每次都會覆寫同 event_id 的資料，重複執行沒關係。
"""

import argparse

import boto3

from shared.models import load_sample_events


def create_local_table(dynamodb, name: str) -> None:
    """在 DynamoDB Local 建立與 template.yaml 相同結構的資料表。"""
    existing = [t.name for t in dynamodb.tables.all()]
    if name in existing:
        return
    dynamodb.create_table(
        TableName=name,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "event_id", "AttributeType": "S"},
            {"AttributeName": "month", "AttributeType": "S"},
            {"AttributeName": "start_at", "AttributeType": "S"},
        ],
        KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "month-start_at-index",
                "KeySchema": [
                    {"AttributeName": "month", "KeyType": "HASH"},
                    {"AttributeName": "start_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    ).wait_until_exists()
    print(f"已建立本機資料表 {name}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed FomoLess sample events")
    parser.add_argument("--table", required=True)
    parser.add_argument("--local", action="store_true", help="寫入 http://localhost:8000 的 DynamoDB Local")
    parser.add_argument("--create", action="store_true", help="本機資料表不存在時自動建立")
    args = parser.parse_args()

    if args.local:
        dynamodb = boto3.resource(
            "dynamodb", endpoint_url="http://localhost:8000", region_name="ap-east-2",
            aws_access_key_id="local", aws_secret_access_key="local",
        )
        if args.create:
            create_local_table(dynamodb, args.table)
    else:
        dynamodb = boto3.resource("dynamodb", region_name="ap-east-2")

    table = dynamodb.Table(args.table)
    events = load_sample_events()
    with table.batch_writer(overwrite_by_pkeys=["event_id"]) as batch:
        for e in events:
            batch.put_item(Item=e.to_dynamo_item())
    print(f"已寫入 {len(events)} 筆範例活動到 {args.table}")


if __name__ == "__main__":
    main()
