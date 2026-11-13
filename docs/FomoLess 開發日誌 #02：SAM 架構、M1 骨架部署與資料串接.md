---
title: 'FomoLess 開發日誌 #02：SAM 架構、M1 骨架部署與資料串接'

---

# FomoLess 開發日誌 #02：SAM 架構、M1 骨架部署與資料串接
 
###### *tags: `FomoLess` `AWS` `SAM` `Lambda` `DynamoDB` `CI/CD` `開發日誌`*
 
> **期間**：2026-10-29 ～ 2026-11-10　**撰寫**：朱禹澄 Pupu　**Region**：`ap-east-2`（台北）
> 接續 #01（AWS 帳號與 IAM）。這篇記錄從「帳號建好」到「第一版 LINE Bot 跑在 AWS 上、爬蟲資料進來」的過程，以及每個設計決策的理由。
 
## 今日摘要
 
- 用 **AWS SAM** 把整個後端寫成一份 `template.yaml`：LINE Webhook Lambda、HTTP API、DynamoDB、日誌保留設定，一個指令部署成 dev / prod 兩套互不干擾的環境。
- 完成 **M1 骨架**並部署到 dev：同學在 LINE 輸入「本週活動」，Bot 從 DynamoDB 查出接下來 7 天的活動並回覆。單元測試 18 個全數通過。
- 設計共用的**活動資料模型**（Pydantic v2），一筆資料等於一個場次，所有來源（Portal 爬蟲、社團投稿、未來的計中 API）都轉成同一個格式。
- 建立 **GitHub 協作流程**：組織、branch protection、PR 審查、GitHub Actions CI/CD（OIDC 免金鑰部署）。
- 和**電算中心**會談取得資料來源共識；爬蟲組抓到第一批 Portal 資料（68 個活動、166 個場次），並決定「本機爬取 → S3 → Lambda」的兩段式架構，避免把個人登入 cookie 放上雲端。

| 項目 | 狀態 |
| :-- | :-- |
| SAM 範本（Lambda、HTTP API、DynamoDB、LogGroup） | ✅ |
| LINE Webhook：簽章驗證、本週活動查詢、Reply API | ✅ |
| 活動資料模型 `shared/models.py`＋16 筆假資料＋seed 腳本 | ✅ |
| 單元測試 18 個（models 14、week 4） | ✅ |
| 部署到 dev（`fomoless-dev` stack）並串上 LINE 官方帳號 | ✅ |
| GitHub 組織、branch protection、Git 協作文件 | ✅ |
| CI/CD workflow（PR 跑測試、合併自動部署） | ✅ 已寫好，OIDC 角色與 repo 變數待確認 |
| PR #1（M1 骨架）合併到 develop | ⏳ 等待審查 |
| 社團活動投稿 Google 表單（Apps Script 一鍵產生） | ✅ |
| 電算中心會談、活動 API 欄位需求文件 | ✅ |
| Portal 爬蟲（爬蟲組，PR #2） | ⏳ 修正後合併 |
| S3 上傳 bucket＋爬蟲上傳專用 IAM 使用者 | ⏳ IAM 使用者已建，bucket 待部署 |
 
---
 
## 1. 專案背景與要解決的問題
 
**問題**：中大的校園活動散落在 Portal 活動報名系統、各系所粉專、社團 IG、課外組公告。學生常常錯過有興趣的講座，或是錯過學習護照時數（畢業門檻 100 小時）的活動。
 
**FomoLess 做的事**：把校內活動集中起來，依學生訂閱的興趣標籤，透過 LINE 推播。學生不用下載新 App，加 LINE 好友就能用。
 
**團隊**：10 人，我是發起人兼專案統籌，負責架構設計、AWS、CI/CD 與 PR 審查。其他分組：LINE Bot 後端、爬蟲、資料庫、前端、AI（海報辨識）、Discord。
 
:::info
**面試一句話版本**：「我帶 10 人團隊做一個 LINE Bot，把中大分散在各平台的活動整合起來，依興趣標籤推播。我負責整個 AWS Serverless 架構、用 SAM 寫基礎設施即程式碼、建 CI/CD，並制定團隊的 Git 協作規範。」
:::
 
---
 
## 2. 架構總覽
 
### 2-1 目前已部署（M1）
 
```
學生在 LINE 傳「本週活動」
        │
        ▼
LINE 平台 ──POST /webhook──▶ API Gateway（HTTP API）
                                   │
                                   ▼
                    Lambda：fomoless-dev-line-webhook
                    （Python 3.12、arm64、512 MB、15 秒）
                     │ 1. 驗證 X-Line-Signature
                     │ 2. 讀金鑰 ←── SSM Parameter Store（SecureString）
                     │ 3. 查詢   ──▶ DynamoDB：fomoless-dev-events
                     │              （GSI：month-start_at-index）
                     │ 4. Reply API 回覆（免費，不計訊息額度）
                     ▼
              CloudWatch Logs（保留 14 天）
```
 
### 2-2 規劃中的完整架構
 
```
【資料進來】
Portal 爬蟲（組員電腦）──JSON──▶ S3 ingest bucket ──事件──▶ Lambda：轉換、驗證、upsert ──▶ DynamoDB
社團投稿 Google 表單 ─────────────────────────────────────────▶（同上）
計中活動 API（約 2 個月後）── EventBridge 每日 ──▶ Lambda ──▶（同上）
 
【推播出去】
EventBridge Scheduler（每週）──▶ Lambda：依標籤配對 ──▶ SQS ──▶ Lambda：LINE Push API
```
 
### 2-3 為什麼選 Serverless
 
:::spoiler 決策理由（面試會被問）
| 考量 | Serverless（Lambda＋DynamoDB） | 傳統（EC2＋RDS） |
| :-- | :-- | :-- |
| 流量型態 | 學生活動查詢集中在特定時段，大部分時間閒置。Lambda 依請求計費，閒置不收錢 | EC2、RDS 24 小時開著都在計費 |
| 成本 | 學生專案量級幾乎都在免費額度內 | RDS 最小規格每月就有固定費用；NAT Gateway 每月至少約 US$30 |
| 維運 | 不用管作業系統更新、擴展 | 要自己 patch、監控、擴容 |
| 團隊 | 10 個學生、時間有限，越少要維護的東西越好 | 需要有人懂伺服器維運 |
 
**取捨**：Lambda 有冷啟動（第一次呼叫較慢），LINE Webhook 要求快速回應。目前流量下影響可接受，真的有問題可以用 Provisioned Concurrency，但會產生固定費用，所以先不開。
:::
 
---
 
## 3. AWS SAM：基礎設施即程式碼
 
### 3-1 SAM 是什麼、為什麼用
 
**SAM（Serverless Application Model）** 是 AWS 的工具，用一份 YAML 描述需要的資源，部署時轉成 CloudFormation，由 AWS 建立一整組資源（稱為 **Stack**）。修改 YAML 再部署，AWS 會算出差異，只更新有變動的部分。
 
**為什麼不在 Console 手動點**：
- **可重現**：同一份範本，傳入 `Stage=dev` 或 `Stage=prod` 就建出兩套一模一樣的環境。
- **可審查**：基礎設施的改動跟程式碼一樣走 PR，組員可以 review。
- **不會漂移**：手動在 Console 改的東西，下次部署會被範本蓋回去，所以團隊約定「Console 只看不改，要改就改範本」。人員帳號、金鑰值、Budgets 這類帳號層級設定才在 Console 操作。
### 3-2 template.yaml 逐段說明
 
:::spoiler Parameters 與 Globals
```yaml
Parameters:
  Stage:
    Type: String
    AllowedValues: [dev, prod]
    Default: dev
 
Globals:
  Function:
    Runtime: python3.12
    Architectures: [arm64]
    MemorySize: 512
    Timeout: 15
    LoggingConfig:
      LogFormat: JSON
    Environment:
      Variables:
        STAGE: !Ref Stage
        EVENTS_TABLE: !Ref EventsTable
        SSM_PREFIX: !Sub /fomoless/${Stage}
```
- **Stage 參數**：所有資源名稱都帶 `${Stage}`，例如 `fomoless-dev-events`、`fomoless-prod-events`，dev 和 prod 完全隔離。
- **Globals**：所有 Lambda 共用的設定，之後新增爬蟲、推播函式不用重寫。
- **arm64（Graviton）**：比 x86 便宜約兩成，Python 沒有相容性問題。
- **512 MB / 15 秒**：Lambda 依「記憶體 × 執行時間」計費。Timeout 設短，避免程式卡住一直計費。
- **JSON 日誌**：在 CloudWatch Logs Insights 可以直接用欄位查詢。
- **環境變數只放名稱和路徑，不放金鑰**。
:::
:::spoiler LineWebhookFunction（Lambda＋HTTP API）
```yaml
LineWebhookFunction:
  Type: AWS::Serverless::Function
  Properties:
    FunctionName: !Sub fomoless-${Stage}-line-webhook
    CodeUri: backend/webhook/
    Handler: app.handler
    Policies:
      - DynamoDBReadPolicy:
          TableName: !Ref EventsTable
      - SSMParameterReadPolicy:
          ParameterName: !Sub fomoless/${Stage}/*
    Events:
      Webhook:
        Type: HttpApi
        Properties:
          Path: /webhook
          Method: POST
```
- **最小權限**：SAM 會替這個函式自動建立專屬 IAM 角色，只能「讀」活動表、只能讀 `/fomoless/{Stage}/` 底下的參數。Webhook 被攻破也無法改資料或讀其他環境的金鑰。
- **HTTP API 而不是 REST API**：HTTP API 較便宜、延遲較低，Webhook 不需要 REST API 的進階功能（API key、請求驗證）。
- 這也解決了 #01 的問題：開發者群組不能建 IAM 角色，但透過 SAM 部署，角色由 CloudFormation 依範本建立，權限範圍寫死在範本裡、經過 review。
:::
:::spoiler WebhookLogGroup（日誌保留）
```yaml
WebhookLogGroup:
  Type: AWS::Logs::LogGroup
  Properties:
    LogGroupName: !Sub /aws/lambda/fomoless-${Stage}-line-webhook
    RetentionInDays: 14
```
- Lambda 預設日誌**永久保存**，會一直累積儲存費用。明確建立 LogGroup 並設 14 天保留。
- 名稱必須跟 Lambda 的 FunctionName 對應，否則 Lambda 會自己建一個永久保存的。
:::
:::spoiler EventsTable（DynamoDB）
```yaml
EventsTable:
  Type: AWS::DynamoDB::Table
  DeletionPolicy: Retain
  UpdateReplacePolicy: Retain
  Properties:
    TableName: !Sub fomoless-${Stage}-events
    BillingMode: PAY_PER_REQUEST
    PointInTimeRecoverySpecification:
      PointInTimeRecoveryEnabled: true
    AttributeDefinitions:
      - { AttributeName: event_id, AttributeType: S }
      - { AttributeName: month,    AttributeType: S }
      - { AttributeName: start_at, AttributeType: S }
    KeySchema:
      - { AttributeName: event_id, KeyType: HASH }
    GlobalSecondaryIndexes:
      - IndexName: month-start_at-index
        KeySchema:
          - { AttributeName: month,    KeyType: HASH }
          - { AttributeName: start_at, KeyType: RANGE }
        Projection: { ProjectionType: ALL }
```
- **On-Demand（PAY_PER_REQUEST）**：依讀寫次數計費，沒流量幾乎不收錢，不用預估容量。
- **Retain**：就算 `sam delete` 刪掉整個 stack，資料表也保留，避免誤刪資料。
- **PITR**：可以還原到過去 35 天內任一時間點。
- **只宣告當作鍵的欄位**：DynamoDB 是 schemaless，title、location 等欄位不用事先定義。
- GSI 設計見第 5 節。
:::
:::spoiler Outputs 與 samconfig.toml
```yaml
Outputs:
  WebhookUrl:
    Value: !Sub https://${ServerlessHttpApi}.execute-api.${AWS::Region}.amazonaws.com/webhook
```
部署完印出 Webhook 網址，貼到 LINE Developers Console。
 
`samconfig.toml` 把 dev / prod 的部署參數分開：
 
| 設定 | dev | prod |
| :-- | :-- | :-- |
| stack_name | `fomoless-dev` | `fomoless-prod` |
| parameter_overrides | `Stage=dev` | `Stage=prod` |
| confirm_changeset | false（直接部署） | **true**（部署前要人工確認變更） |
| use_container | true（在 Docker 裡打包 arm64 套件） | true |
 
部署指令：`sam build --config-env dev && sam deploy --config-env dev`
:::
 
---
 
## 4. LINE Webhook Lambda（backend/webhook/app.py）
 
### 4-1 處理流程
 
```
handler(event)
 ├─ 1. 取出 x-line-signature 與 body（API Gateway 可能 base64 編碼，先解碼）
 ├─ 2. WebhookParser 驗證簽章 → 不符回 400
 ├─ 3. 逐一處理事件 handle_event()
 │     ├─ FollowEvent（加好友）→ 歡迎訊息
 │     └─ 文字訊息
 │          ├─ 含「本週／這週／本周／這周／活動」→ 查 DynamoDB → 格式化 → 回覆
 │          └─ 其他 → 使用說明
 └─ 4. 一律回 200
```
 
### 4-2 設計重點
 
:::spoiler 簽章驗證：為什麼一定要做
Webhook 網址是公開的，任何人都能 POST 假事件過來。LINE 會用 Channel secret 對 body 做 HMAC-SHA256，放在 `X-Line-Signature`。我們用同一個 secret 重算並比對，不符就拒絕，確保請求真的來自 LINE。
:::
 
:::spoiler 金鑰管理：SSM Parameter Store＋快取
- Channel secret 和 access token 存在 SSM Parameter Store 的 **SecureString**（KMS 加密），路徑 `/fomoless/{Stage}/line-channel-secret`。
- 程式碼和 GitHub 裡**完全沒有金鑰**。
- 用 `@lru_cache` 快取：同一個 Lambda 執行環境只讀一次 SSM，後續請求直接用記憶體裡的值，減少延遲和 API 呼叫。
- 本機測試時優先讀環境變數（`env.json`，已列在 `.gitignore`），不用連 AWS。
:::
:::spoiler 為什麼一律回 200
如果某個事件處理失敗就回 500，LINE 會**重送**整批事件，學生可能收到重複回覆。所以個別事件出錯時只記 log（`logger.exception`），繼續處理其他事件，最後一律回 200。錯誤由 CloudWatch 監控。
:::
 
:::spoiler Reply API vs Push API：成本設計
| | Reply API | Push API |
| :-- | :-- | :-- |
| 觸發 | 回覆使用者剛傳來的訊息（用 reply token） | 主動推播 |
| 費用 | **免費**，不計入訊息額度 | 計入每月額度，超過要付費 |
| 用途 | 查詢「本週活動」 | 每週推播 |
 
LINE 官方帳號 2026/11 起的方案：中用量 NT$1,000／3,000 則，高用量 NT$1,400／6,000 則，超過每則 NT$0.2。所以設計上**查詢一律走 Reply**，推播要控制頻率（規劃每週彙整一次），不做「每有新活動就推」。
:::
 
:::spoiler 訊息格式
```
📅 接下來 7 天的活動：
• 11-18 12:00 生科系職涯講座｜科學五館111
  ⏱ 自我探索與生涯規劃 1h｜👥 已額滿｜🎁 餐券
```
- 第二行只在有學習護照時數、名額、福利時才顯示。
- 最多列 8 筆，超過顯示「…還有 N 個活動」，避免訊息太長。
- DynamoDB 讀出的數字是 `Decimal`，用 `_num()` 轉成 `2`、`0.66` 這種好讀的格式。
:::
---
 
## 5. 資料模型與 DynamoDB 查詢設計
 
### 5-1 為什麼選 DynamoDB 而不是 RDS
 
組員問過這個問題，我的回答：
- **存取模式固定且簡單**：主要是「查某段時間的活動」「用 ID 取單筆」「依標籤找活動」，不需要複雜 JOIN。
- **成本**：RDS 即使沒流量也按小時計費，DynamoDB On-Demand 沒流量幾乎不收錢。
- **Serverless 整合**：Lambda 連 RDS 要處理連線池（或 RDS Proxy），DynamoDB 是 HTTP API，沒有連線數問題。
- **取捨**：DynamoDB 要**先想好查詢再設計索引**，臨時想做複雜統計會比較麻煩。真的需要分析，可以匯出到 S3 用 Athena 查。
### 5-2 GSI：month-start_at-index
 
**需求**：查「接下來 7 天的活動」。
 
**為什麼不用 Scan**：Scan 會讀整張表再過濾，資料越多越慢、越貴。
 
**設計**：
- Partition key：`month`（例如 `2026-11`），由 `start_at` 自動計算。
- Sort key：`start_at`（ISO 8601 帶 `+08:00` 的字串，字串排序就等於時間排序）。
- 查詢：`month = '2026-11' AND start_at BETWEEN 起 AND 訖`。
- **跨月處理**：本週如果是 11/28～12/4，`months_between()` 回傳 `['2026-11', '2026-12']`，兩個月各查一次再合併排序。
:::spoiler 細節：FilterExpression 的成本陷阱
查詢時加了 `FilterExpression: status = published`，草稿不給學生看。要注意 **Filter 是在讀出來之後才過濾**，被濾掉的資料一樣消耗讀取容量。目前資料量小沒關係；如果草稿很多，應該把 status 放進索引鍵。
:::
 
:::spoiler 細節：為什麼用 month 當 partition key
如果用 `start_at` 當 partition key，每個時間都是不同分區，無法做範圍查詢。用月份分組，一次 Query 就能拿到一個月內某段時間的活動。一個月的活動量（幾百筆）遠低於單一分區的限制，不會有熱分區問題。
:::
 
### 5-3 活動資料模型（shared/models.py，Pydantic v2）
 
**核心原則：一筆資料 = 一個場次。** 所有來源都轉成這個格式，下游（Webhook、推播、前端）只要認一種。
 
:::spoiler 主要欄位與驗證規則
| 類別 | 欄位 |
| :-- | :-- |
| 識別 | `event_id`、`activity_id`、`source`（NCU_API / CRAWLER_PORTAL / HOST_SUBMISSION…） |
| 內容 | `title`、`host`、`speaker`、`description`（上限 3000 字）、`audience` |
| 時間地點 | `start_at`、`end_at`、`location`、`mode`（實體／線上／混合） |
| 報名 | `registration_method`（portal / external / none）、`registration_url`、`capacity`、`registered_count` |
| 標籤與時數 | `tags`、`learning_passport_hours`（list of {category, hours}）、`soft_skill_hours`、`perks` |
| 狀態 | `status`（draft / published / cancelled / postponed）、`created_at`、`updated_at` |
| 計算欄位 | `month`、`categories`、`is_full` |
 
**驗證規則（寫進 model，不靠各組自律）**：
- 時間**必須帶時區**，一律轉成台北時間。
- `tags` 必須來自 `TAG_CATALOG`，避免各組亂打標籤。
- 有學習護照時數，自動把類別加進 tags（方便學生訂閱「人文藝術」之類的時數標籤）。
- 至少一個標籤；`registered_count ≤ capacity`；外部報名必須有網址。
:::
:::spoiler 踩過的坑
- **Decimal**：DynamoDB 不接受 Python float，寫入前 `_floats_to_decimal()`，讀出後轉回來。
- **微秒**：`created_at` 預設值帶微秒，存進去再讀出來比對會不一樣，測試失敗。改用 `now_taipei()` 去掉微秒。
- **計算欄位重新載入失敗**：`month`、`is_full` 是 computed field，從 JSON 重新載入時會被當成多餘欄位。加 `_drop_computed` before-validator 先移除。
:::
**學習護照**：中大畢業門檻 100 小時（生活知能 35、服務學習 40、人文藝術 20、國際視野 5），這是 FomoLess 對學生最有吸引力的功能之一，所以時數是一級欄位，不是放在描述裡。
 
---
 
## 6. 測試
 
- `tests/test_models.py`（14 個）：時區轉換、標籤驗證、時數自動加標籤、名額檢查、DynamoDB 轉換來回一致、假資料全部可載入。
- `tests/test_week.py`（4 個）：一週範圍、跨月、跨年、時區邊界（UTC 晚上等於台北隔天）。
- `events/make_event.py`：產生帶正確簽章的假 LINE 事件，搭配 `sam local invoke` 在本機測試整個 Lambda。
- CI 在每個 PR 自動跑 `pytest` 和 `cfn-lint`（檢查範本語法）。
---
 
## 7. CI/CD：GitHub Actions
 
### 7-1 流程
 
```
發 PR 到 develop / main ──▶ test job：pytest＋cfn-lint
合併到 develop ──▶ test ──▶ deploy job（environment: development）──▶ fomoless-dev
合併到 main    ──▶ test ──▶ deploy job（environment: production，需指定審查者核准）──▶ fomoless-prod
```
 
### 7-2 OIDC：不在 GitHub 存任何 AWS 金鑰
 
:::spoiler 運作方式與信任政策
傳統做法是把 IAM access key 存成 GitHub Secret，金鑰長期有效，外洩風險高。
 
**OIDC 做法**：
1. AWS 建立 GitHub 的 OIDC 身分提供者。
2. 建立部署角色 **==實際格式以 CloudTrail 看到的為準==**(見10第一點) `FomoLess-GitHubDeploy`，信任政策限定**只有這個 repo 的特定 environment** 能扮演：
   - "repo:fomoless-ncu@352312213/Fomoless@1445303768:environment:development"
    - "repo:fomoless-ncu@352312213/Fomoless@1445303768:environment:production"
3. workflow 設 `permissions: id-token: write`，執行時向 GitHub 取得短期 token，換 AWS 臨時憑證（1 小時內有效）。
4. 角色 ARN 放 repo 變數 `AWS_DEPLOY_ROLE_ARN`（不是 secret，ARN 不敏感）。
**好處**：沒有長期金鑰可以外洩；fork 的 PR 拿不到 token，無法部署。
:::
 
:::spoiler arm64 打包
Lambda 是 arm64，GitHub 的機器是 x86。用 `docker/setup-qemu-action` 模擬 arm64，`sam build --use-container` 在容器裡安裝套件，確保打包出來的二進位檔能在 Lambda 上執行。
:::
 
---
 
## 8. 團隊協作流程
 
### 8-1 GitHub 組織與分支策略
 
- 組織 `fomoless-ncu`，repo `Fomoless`（public）。
- 分支：`main`（正式）、`develop`（整合，自動部署 dev）、`feature/*`、`fix/*`。
- **Branch protection**：`main`、`develop` 不能直接 push，必須 PR＋1 人 Approve，**Squash and merge** 保持歷史乾淨。
- 免費組織只有 public repo 能用 branch protection，這是選 public 的原因之一，所以 repo 裡**絕對不能有金鑰**。
### 8-2 權限設計
 
| 角色 | GitHub 權限 | 說明 |
| :-- | :-- | :-- |
| Pupu | Admin | 可以略過規則，建議也勾「Do not allow bypassing」 |
| 組員 | Write（透過 `developers` team） | 可以 push feature 分支，但 develop / main 一樣要 PR |
| 組織 Base permission | Read | 權限只來自 team，不會因為是成員就自動有 Write |
 
**學到的事**：Write 權限管「能不能 push 到 repo」，branch protection 管「能不能直接改受保護分支」，兩者獨立。爬蟲組員一開始 push 出現 403，是因為還沒給 Write，他改用 fork 發 PR；給權限後就能直接在組織 repo 開分支。
 
### 8-3 文件
 
- **Git 協作入門**（Google Doc）：第一次設定、每天的開發流程、commit 規範（feat / fix / docs…）、衝突處理、絕對不能做的事（force push、commit 金鑰）。
- **M1 部署教學**：從安裝工具到串上 LINE 的完整步驟。
- **連結總整理**（HackMD）：所有連結一頁找到。
---
 
## 9. 資料來源：電算中心與 Portal 爬蟲
 
### 9-1 電算中心會談（11/05）
 
- 計中預計約 2 個月內提供活動 API；在那之前允許每日爬取。
- Portal 登入（SSO）需透過技術研發組。
- 我寫了一份**活動 API 欄位需求**給計中，欄位依 Portal 現有資料整理，時間格式統一 ISO 8601 帶時區。
- **風險**：如果 API 要求 IP 白名單，Lambda 沒有固定 IP，要加 NAT Gateway（每月至少約 US$30），會超出預算。優先爭取 API key 或 Open Data。
### 9-2 Portal 爬蟲與資料對齊
 
- 爬蟲組抓到 11/8 資料：68 個活動、166 個場次（例如大一 CPR 考試一個活動就有 52 場），所以改成**每個場次一筆**，正好符合我們「一筆 = 一個場次」的設計。
- 我 review 欄位時發現：
  - 範例的場次時間和報名時間一樣（填錯），請他們修正。
  - `registration_method` 規則太寬：內文有 Google 表單就判成外部報名，但很多活動是「校內走 Portal、校外填表單」。改成「有 Portal 報名按鈕就是 portal」。
- 個資：`contact_email` 固定 null，不存承辦人姓名與信箱（個資最少化）。
### 9-3 關鍵決策：cookie 不上雲端，改兩段式架構
 
:::spoiler 問題、選項與決定（很好的面試題材）
**問題**：Portal 要登入才能看活動，爬蟲需要組員的登入 cookie。原本計畫把爬蟲包成 Lambda，用 EventBridge 每天觸發。
 
**評估**：
- 放 SSM SecureString 被外人偷的機率其實不高，但 cookie 是**整個 NCU 帳號的身分**，包含成績、選課、個資，無法只開放活動頁的權限。
- cookie 會過期，還是要人手動更新，自動化效益有限。
- 從 AWS IP 用學生帳號自動登入，可能被視為帳號共用或異常登入，風險落在那位同學身上。
- GitHub Actions 一樣是雲端；self-hosted runner 裝在組員電腦上，對 public repo 有安全風險（fork PR 可在那台電腦執行程式），GitHub 官方也不建議。
**決定：兩段式**
```
組員電腦（排程，cookie 不離開本機）──只上傳 JSON──▶ S3 ──事件──▶ Lambda 處理
```
- 上傳用的 IAM 使用者**只能 `s3:PutObject` 到 `portal/*.json`**，不能讀、刪、列出，也不能登入主控台。一人一把金鑰，外洩只停用那一把。
- 後半段（轉換、驗證、寫入、推播）仍是 serverless、event-driven。
- 計中 API 開放後，只要把前段換成 EventBridge＋Lambda，後段完全不用改。
**學到的事**：安全決策不只是「會不會被駭」，還要考慮**責任歸屬和使用者帳號的風險**。好的架構會把「需要特殊權限的部分」隔離出來，讓它容易替換。
:::
 
---
 
## 10. 部署過程的問題排除
 
:::spoiler GitHub Actions 部署失敗：Not authorized to perform sts:AssumeRoleWithWebIdentity
**現象**：PR #1 合併後，`test` 通過，`deploy-dev` 卡在 Configure AWS credentials，重試十幾次後失敗：`Could not assume role with OIDC: Not authorized to perform sts:AssumeRoleWithWebIdentity`。角色的 Last activity 一直是「-」，代表從來沒被成功扮演過。

**排查過程**：
1. 確認 workflow 有 `environment: development`、`permissions: id-token: write` ✅
2. 確認 GitHub 傳的 `role-to-assume` 與角色 ARN 一致、`audience` 是 `sts.amazonaws.com` ✅
3. 確認 OIDC 身分提供者存在、信任政策的 `aud` 正確 ✅
4. 錯誤訊息只說「不允許」，看不出哪裡不符 → 到 **CloudTrail（ap-east-2）→ Event history**，篩選 `AssumeRoleWithWebIdentity`，看失敗事件的 `userIdentity.userName`

**原因**：GitHub 實際送來的 OIDC `sub` 含有組織和 repo 的**不可變 ID**：
| | sub |
| --- | --- |
| 信任政策原本寫的 | `repo:fomoless-ncu/Fomoless:environment:development` |
| GitHub 實際送來的 | `repo:fomoless-ncu@352312213/Fomoless@1445303768:environment:development` |

信任政策用 `StringEquals` 要求完全相同，所以被拒絕。ID 的用意是防止 repo 被刪除後，有人用同名組織或 repo 冒用權限。

**解法**：信任政策的 `sub` 改成含 ID 的版本（development、production 各一行），維持 `StringEquals`，不改用萬用字元，保留 ID 帶來的保護。改完在失敗的 run 按 Re-run failed jobs，不用重新合併。

**學到的事**：
- IAM 的錯誤訊息刻意不透露細節（避免洩漏設定），**要用 CloudTrail 看實際的請求內容**才查得到原因。
- 不要假設外部系統送來的格式，**先觀察實際的值再寫規則**。
- 部署教學已更新：設定信任政策前，先用 CloudTrail 確認實際的 sub。
::: 
 
:::spoiler NoCredentials：aws sts get-caller-identity 失敗
**原因**：還沒設定 CLI 憑證，`aws login` 流程卡住。
**解法**：Ctrl+C 離開，建立 IAM access key 後用 `aws configure`，region 填 `ap-east-2`。
:::
 
:::spoiler sam build：requires a container runtime
**原因**：Mac 上 Docker Desktop 沒有開放預設 socket，SAM 找不到 Docker。
**解法**：Docker Desktop → Settings → Advanced → 勾選「Allow the default Docker socket to be used」，或設定 `export DOCKER_HOST=unix://$HOME/.docker/run/docker.sock`。
:::
 
:::spoiler gh pr create：unknown argument
**原因**：`--reviewer A, B` 逗號後多了空格，第二個帳號被當成另一個參數。
**解法**：`--reviewer ShiYu0318,GabrielChentw`（不能有空格）。
:::
 
:::spoiler 本週活動出現草稿
**原因**：Query 沒有過濾狀態。
**解法**：加上 `FilterExpression=Attr("status").eq("published")`。
:::
 
:::spoiler Homebrew 從原始碼編譯 python@3.12
macOS / Xcode 版本較舊屬於 Tier 3 支援，Homebrew 沒有預編譯版本，會自己編譯，時間長但正常。電腦上同時有 python.org 和 Homebrew 兩個 3.12，PATH 裡 python.org 優先，不影響使用。
:::
 
---
 
## 11. 其他產出
 
- **社團活動投稿表單**：用 Google Apps Script 一鍵產生 6 頁表單＋回覆試算表，選項文字與 `TAG_CATALOG`、學習護照類別完全一致，回覆可以直接轉成 Event 格式。有「是否提供學習護照時數」的分支頁。
- **場地協調功能研究**：社團要在 FB 私人社團協調場地，資料是兩份大型試算表。規劃先做「唯讀可視化＋空場地查詢＋撞場標示」，不改變現有規則；要先取得場協長同意，資料不對外公開。
---
 
## 12. 下一步（M2）
 
- [ ] 合併 PR #1，通知組員從 develop 開分支
- [ ] 確認 OIDC 部署角色與 `AWS_DEPLOY_ROLE_ARN`，讓合併自動部署生效
- [ ] template.yaml 新增 S3 ingest bucket（30 天生命週期）＋處理 Lambda（S3 事件觸發）
- [ ] 資料庫組：models.py 新增爬蟲欄位、`from_portal_crawler()`、upsert 規則、新 GSI（同活動所有場次、依標籤）
- [ ] 逾時告警：36 小時沒有新檔案就通知（cookie 可能過期）
- [ ] 每週推播：EventBridge Scheduler＋SQS＋Push API，控制訊息額度
- [ ] 前端 LIFF、AI 海報辨識、Discord 服務
---
 
## 13. 面試準備：可以講的故事
 
:::spoiler 30 秒專案介紹
「FomoLess 是我發起的校園活動 LINE Bot，10 人團隊。中大活動散在 Portal、粉專、IG，學生常錯過講座和學習護照時數。我們把活動整合起來，依學生訂閱的標籤推播。我負責架構和雲端：全 Serverless，用 AWS SAM 寫基礎設施即程式碼，Lambda＋API Gateway＋DynamoDB，GitHub Actions 用 OIDC 免金鑰部署，dev / prod 兩套環境。」
:::
 
:::spoiler STAR 1：在成本和安全限制下設計架構
- **S**：學生專案沒有預算，10 個人共用一個 AWS 帳號，怕有人誤開昂貴資源。
- **T**：設計能長期跑、幾乎零成本、又不會被誤用的架構。
- **A**：選 Serverless 全依用量計費；用 IAM 政策鎖定台北區域、禁止 EC2 / RDS / NAT 等高成本服務；開 Budgets 和異常偵測告警（US$5）；日誌 14 天保留；Reply API 免費所以查詢一律走 Reply。
- **R**：M1 部署後費用維持在免費額度內，組員可以自己部署測試，不需要我每次幫忙。
:::
:::spoiler STAR 2：cookie 上雲端的安全取捨
- **S**：Portal 要登入才能爬，原本計畫把個人 cookie 放 Lambda 每天自動跑。
- **T**：在自動化和帳號安全之間做決定。
- **A**：分析風險不只是外洩，還有 cookie 代表整個學生帳號、會過期、可能違反帳號使用規範；比較 Lambda、GitHub Actions、self-hosted runner 三個選項；最後設計兩段式架構，本機爬取、只上傳結果，上傳金鑰只有 PutObject 權限。
- **R**：cookie 不離開本機，後段仍是 event-driven；計中 API 開放後只需替換前段。
:::
:::spoiler STAR 3：讓 10 個新手能協作
- **S**：多數組員第一次用 Git 協作。
- **T**：建立不會互相蓋掉、又不會太複雜的流程。
- **A**：設 GitHub 組織、branch protection、PR 審查；寫 Git 協作入門文件和部署教學；統一資料模型讓各組有共同介面；權限用 team 管理，組員 Write、規則對所有人生效。
- **R**：爬蟲組已經透過 PR 交付爬蟲和範例資料，我在 review 中發現欄位問題並在合併前修正。
:::
:::spoiler 可能被問的技術問題
- **為什麼用 DynamoDB 不用 RDS？** 存取模式簡單固定、On-Demand 閒置不收錢、Lambda 不用處理連線池；代價是要先設計查詢。
- **GSI 怎麼設計？** month 當 partition key、start_at 當 sort key，支援時間範圍查詢；跨月查兩次合併。
- **Lambda 冷啟動怎麼辦？** 目前可接受；需要時用 Provisioned Concurrency，但有固定成本。也可以減少套件、延遲載入。
- **金鑰怎麼管？** SSM SecureString＋最小權限讀取＋快取；CI/CD 用 OIDC，沒有長期金鑰。
- **Webhook 為什麼一律回 200？** 避免 LINE 重送造成重複回覆，錯誤靠 log 監控。
- **dev / prod 怎麼隔離？** 同一份範本傳不同 Stage，資源名稱、SSM 路徑、stack 都分開；prod 部署要人工核准。
- **如果要支援 10 萬人？** Lambda、DynamoDB 會自動擴展；瓶頸會是 LINE Push 額度和費用，推播要改成批次、依偏好頻率控制，推播用 SQS 削峰。
- **FilterExpression 有什麼要注意？** 過濾在讀取之後，被濾掉的資料仍計費；高比例過濾的條件應該放進索引鍵。
:::
---
 
> 下一篇預計記錄：S3 資料串接、資料庫組的 upsert 設計、每週推播與訊息額度控制。

