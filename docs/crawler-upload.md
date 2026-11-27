# 爬蟲上傳：S3 ingest bucket 與 IAM 權限

###### tags: `FomoLess` `S3` `IAM` `爬蟲`

Portal 要登入才看得到活動，所以爬蟲在組員電腦上跑（cookie 不離開本機），只把結果上傳到 S3，後續由 Lambda 處理。設計理由見開發日誌 #02 第 9-3 節。

```
組員電腦（爬蟲）──PutObject──▶ S3 fomoless-dev-ingest-<帳號ID> ──事件──▶ Lambda（規劃中）──▶ DynamoDB
```

## Bucket

| 項目 | 設定 |
| --- | --- |
| 名稱 | `fomoless-{stage}-ingest-{帳號ID}`，dev 是 `fomoless-dev-ingest-697438195832` |
| Region | `ap-east-2` |
| 公開存取 | 全部封鎖，ACL 停用 |
| 加密 | SSE-S3 |
| 連線 | 只接受 HTTPS |
| 刪除 stack 時 | 保留 bucket 和資料（Retain） |

| 路徑 | 內容 | 保存期限 |
| --- | --- | --- |
| `portal/` | 爬蟲 JSON | **30 天**後自動刪除（原始資料未遮蔽個資） |
| `posters/` | 海報圖片（Portal 要登入才看得到，所以要自己存） | 永久 |

## 上傳權限

上傳政策由 `template.yaml` 的 `CrawlerUploadPolicy` 定義，經 PR review 後由 CI 部署。目前內容：

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "UploadPortalJsonOnly",
      "Effect": "Allow",
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::fomoless-dev-ingest-697438195832/portal/*.json"
    },
    {
      "Sid": "UploadPosterImagesOnly",
      "Effect": "Allow",
      "Action": "s3:PutObject",
      "Resource": [
        "arn:aws:s3:::fomoless-dev-ingest-697438195832/posters/*.jpg",
        "arn:aws:s3:::fomoless-dev-ingest-697438195832/posters/*.jpeg",
        "arn:aws:s3:::fomoless-dev-ingest-697438195832/posters/*.png",
        "arn:aws:s3:::fomoless-dev-ingest-697438195832/posters/*.webp"
      ]
    }
  ]
}
```

- **只能上傳**，不能讀、列出、刪除。金鑰外流也只能塞檔案進來。
- 副檔名比對**有分大小寫**：`.JPG` 會被拒絕，爬蟲端要統一轉小寫。
- 同一個路徑再上傳會直接覆蓋。
- 只有 dev 有上傳政策。prod 沒有設定 `CrawlerUploaders`，所以不會建立，爬蟲帳號無法寫入 prod。

### 可以上傳的帳號

| IAM 使用者 | 使用者 |
| --- | --- |
| `Crawler-jenyang` | 爬蟲組 |
| `Crawler-Kaiyu` | 爬蟲組 |

帳號清單寫在 `samconfig.toml` 的 dev `parameter_overrides`（`CrawlerUploaders=...`）。

**IAM 使用者和 access key 不放進範本**：放進 CloudFormation 的話，key 的 secret 會留在 stack 裡。所以使用者由 Pupu 手動建立，範本只負責建政策、掛到這些使用者上。

### 新增或移除上傳帳號

1. Pupu 在 IAM 手動建使用者（不給主控台登入、不加群組），建立 access key，**私下**交給本人。
2. 在 `samconfig.toml` 的 `CrawlerUploaders` 加上或刪掉帳號名稱，發 PR。
3. 合併後 CI 部署，政策自動掛上或拔掉。
4. 移除帳號時，記得也停用並刪除那個使用者的 access key。

**金鑰外流**：Pupu 馬上在 IAM 把那把 key 設成 Inactive，再發新的給本人。

### 驗證權限

```sh
B=arn:aws:s3:::fomoless-dev-ingest-697438195832
aws iam simulate-principal-policy \
  --policy-source-arn arn:aws:iam::697438195832:user/Crawler-jenyang \
  --action-names s3:PutObject --resource-arns $B/posters/2026/11/2590.webp \
  --query 'EvaluationResults[0].EvalDecision'
# "allowed"；換成 posters/x.exe、portal/x.webp 應該是 "implicitDeny"
```

一次只測一個路徑。傳多個 `--resource-arns` 時，結果會合併成一筆，看不出每個路徑各自的結果。

## 上傳規則（給爬蟲組）

| 檔案 | 路徑 | ContentType |
| --- | --- | --- |
| 爬蟲 JSON | `portal/YYYY-MM-DD-HHMM.json`（爬取時間） | `application/json` |
| 海報 | `posters/YYYY/MM/<活動 pk>.<副檔名>`，例如 `posters/2026/11/2590.webp` | `image/webp` 等 |

- 海報路徑會寫進 `Event.poster_key`（見 `shared/models.py`），所以 JSON 裡要記錄每個活動的海報路徑。
- 每天跑完整的爬取，不要用 `--new-only`。
- JSON 不要放承辦人姓名、個人信箱。
- access key 不要 commit、不要貼到群組。

```sh
aws configure --profile fomoless-crawler   # region 填 ap-east-2
aws s3 cp output.json s3://fomoless-dev-ingest-697438195832/portal/2026-11-24-0900.json --profile fomoless-crawler
```

```python
import boto3

s3 = boto3.Session(profile_name="fomoless-crawler").client("s3")
s3.upload_file("output.json", "fomoless-dev-ingest-697438195832",
               "portal/2026-11-24-0900.json",
               ExtraArgs={"ContentType": "application/json"})
s3.upload_file("2590.webp", "fomoless-dev-ingest-697438195832",
               "posters/2026/11/2590.webp",
               ExtraArgs={"ContentType": "image/webp"})
```

## 變更紀錄

| 日期 | 變更 |
| --- | --- |
| 11/09 | 手動建立 `Crawler-jenyang`、`Crawler-Kaiyu` 和政策 `FomoLess-CrawlerUpload`（只能上傳 `portal/*.json`） |
| 11/24 | 手動政策更新 v2：開放 `posters/` 的 jpg、jpeg、png、webp |
| 11/24 | 上傳政策移進 `template.yaml`（`CrawlerUploadPolicy`），刪除手動的 `FomoLess-CrawlerUpload`（當時 bucket 還沒部署，不影響上傳） |
