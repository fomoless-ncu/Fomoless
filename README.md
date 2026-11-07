# FomoLess

FomoLess｜NCU campus events, pushed to you on LINE by interest tags. 校內活動智慧推播 LINE Bot

**目前進度：M1 骨架**：學生在 LINE 輸入「本週活動」，Bot 從 DynamoDB 查詢接下來 7 天的活動，回覆時間、地點、學習護照時數與名額。

- 第一次部署、串 LINE、設定自動部署：[docs/deploy-guide.md](docs/deploy-guide.md)
- 活動資料格式與各組用法：[shared/README.md](shared/README.md)
- Git 協作流程：見團隊 HackMD「FomoLess Git 協作入門」

## 專案結構

```
.
├── template.yaml              # SAM 範本：描述所有 AWS 資源
├── samconfig.toml             # 部署設定（dev / prod，區域 ap-east-2）
├── backend/webhook/           # LINE Webhook Lambda
│   ├── app.py                 #   驗證簽章、查詢活動、回覆
│   ├── week.py                #   「本週」日期計算（台灣時間）
│   └── requirements.txt
├── shared/                    # 共用活動資料格式
│   ├── models.py              #   Event 模型（一筆 = 一個場次）
│   ├── sample_events.json     #   16 筆假場次
│   └── seed.py                #   把假資料寫進 DynamoDB
├── tests/                     # 單元測試（pytest）
├── events/make_event.py       # 產生已簽章的 LINE 測試事件（sam local 用）
├── env.json.example           # 本機測試金鑰範本（複製成 env.json，勿 commit）
├── .github/workflows/ci-cd.yml  # PR 自動測試、合併後自動部署
└── docs/deploy-guide.md       # 部署教學
```

## 建立的 AWS 資源（dev）

| 資源 | 名稱 | 說明 |
| --- | --- | --- |
| Lambda | `fomoless-dev-line-webhook` | Python 3.12、arm64、512 MB |
| API Gateway（HTTP API） | 自動產生 | `POST /webhook` |
| DynamoDB | `fomoless-dev-events` | On-Demand；GSI `month-start_at-index`；刪 Stack 時保留資料 |
| CloudWatch Logs | `/aws/lambda/fomoless-dev-line-webhook` | 保留 14 天 |
| IAM 角色 | 自動產生 | 只能讀活動表與 `/fomoless/dev/*` 參數 |

## 常用指令

| 指令 | 用途 |
| --- | --- |
| `pytest` | 單元測試 |
| `sam validate --lint` | 檢查範本 |
| `sam build --config-env dev` | 打包（需開 Docker） |
| `sam deploy --config-env dev` | 部署 dev |
| `sam logs -n LineWebhookFunction --stack-name fomoless-dev --tail` | 即時看日誌 |
| `python -m shared.seed --table fomoless-dev-events` | 寫入假資料 |

## 本機執行 Lambda（選用）

```bash
cp env.json.example env.json        # 填入 dev 的 LINE 金鑰；env.json 不會被 commit
python events/make_event.py "本週活動" > events/text_message.json
sam build
sam local invoke LineWebhookFunction -e events/text_message.json
```

本機測試的 reply token 是假的，最後呼叫 LINE 回覆時出現錯誤是正常的；只要日誌沒有 `Invalid LINE signature` 就代表流程正確。沒有 AWS 權限時，把 `env.json` 的 `EVENTS_TABLE` 設成空字串即可略過資料庫查詢。
