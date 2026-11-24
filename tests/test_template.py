"""檢查 template.yaml 的 S3 ingest bucket 設定（不連 AWS，只解析範本）。"""

from pathlib import Path

import pytest
from cfnlint.decode.cfn_yaml import load

TEMPLATE = Path(__file__).resolve().parent.parent / "template.yaml"


@pytest.fixture(scope="module")
def resources():
    return load(str(TEMPLATE))["Resources"]


def test_ingest_bucket_name_matches_crawler_upload_policy(resources):
    # IAM 政策 FomoLess-CrawlerUpload 寫死 fomoless-dev-ingest-<帳號ID>/portal/*.json，
    # 改名會讓爬蟲組上傳失敗（AccessDenied）
    name = resources["IngestBucket"]["Properties"]["BucketName"]
    assert name == {"Fn::Sub": "fomoless-${Stage}-ingest-${AWS::AccountId}"}


def test_ingest_bucket_blocks_public_access(resources):
    block = resources["IngestBucket"]["Properties"]["PublicAccessBlockConfiguration"]
    assert block == {
        "BlockPublicAcls": True,
        "BlockPublicPolicy": True,
        "IgnorePublicAcls": True,
        "RestrictPublicBuckets": True,
    }


def test_ingest_bucket_is_retained_on_stack_delete(resources):
    assert resources["IngestBucket"]["DeletionPolicy"] == "Retain"


def test_ingest_bucket_expires_raw_json_only_under_portal(resources):
    # 原始 JSON 未遮蔽個資，30 天內刪除；規則只限 portal/，避免之後放進來的其他檔案被一起刪掉
    rules = resources["IngestBucket"]["Properties"]["LifecycleConfiguration"]["Rules"]
    expiring = [r for r in rules if r["Status"] == "Enabled" and "ExpirationInDays" in r]
    assert expiring
    for rule in expiring:
        assert rule.get("Prefix") == "portal/"
        assert rule["ExpirationInDays"] <= 30


def test_ingest_bucket_policy_denies_http(resources):
    statements = resources["IngestBucketPolicy"]["Properties"]["PolicyDocument"]["Statement"]
    deny = [s for s in statements if s["Effect"] == "Deny"]
    assert deny
    assert deny[0]["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}
