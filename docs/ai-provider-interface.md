# Azure／Gemini 活動查詢介面

## 功能目的

將使用者輸入的自然語言轉成統一的活動查詢條件，支援 Azure OpenAI 與 Gemini，並串接 LINE webhook 的活動查詢流程。

例如：

> 下週有資工系辦的活動，而且有便當嗎？

系統先解析時間、承辦單位與福利條件，再查詢及篩選活動，最後產生 LINE 回覆。

本次查詢欄位以 `shared/models.py` 既有活動欄位為依據。

## 處理流程

一般自然語言查詢的流程：

使用者文字
→ respond_to_text()
→ 依設定取得 Azure 或 Gemini 解析器
→ 產生 ActivityQuery
→ 查詢 DynamoDB 指定期間的活動
→ filter_events() 篩選
→ 格式化活動清單
→ LINE 回覆

所有非空白文字查詢（包含「本週活動」）都先經過 LLM 解析。空白文字回覆使用提示。

AI 設定缺少、API 呼叫失敗或回應無法解析時，若文字包含「本週」「這週」「本周」「這周」或「活動」，則以關鍵字備援提供接下來 7 天的活動清單。回覆會明確說明未套用其他篩選條件，因此不保證符合原本指定的下週、福利或單位等條件。

沒有備援關鍵字時，回覆暫時無法查詢。LLM 正常回傳不支援的查詢時，仍回覆不支援提示；資料庫查詢失敗時直接回報暫時無法查詢，不再次執行備援查詢。

## 主要檔案

| 檔案                                      | 用途                                         |
| ----------------------------------------- | -------------------------------------------- |
| `backend/webhook/llm/__init__.py`       | 將 llm 標示為 Python 套件，目前留空          |
| `backend/webhook/llm/activity_query.py` | 查詢模型、解析器抽象介面與共用提示詞         |
| `backend/webhook/llm/azure_query.py`    | Azure OpenAI 解析器                          |
| `backend/webhook/llm/gemini_query.py`   | Gemini 解析器                                |
| `backend/webhook/llm/factory.py`        | 依環境設定建立解析器                         |
| `backend/webhook/app.py`                | 串接文字解析、資料查詢、活動篩選及 LINE 回覆 |

兩個供應商都實作 `ActivityQueryParser.parse()`，並回傳 `ActivityQuery`，讓 app.py 使用相同方式處理解析結果。

## 支援的查詢條件

| 欄位                            | 說明                                                         |
| ------------------------------- | ------------------------------------------------------------ |
| `period`                      | 查詢期間：this_week、next_week；不支援的查詢使用 unsupported |
| `title`                       | 活動名稱                                                     |
| `host`                        | 承辦單位                                                     |
| `speaker`                     | 講者                                                         |
| `location`                    | 地點                                                         |
| `mode`                        | 活動形式：onsite、online、hybrid                             |
| `registration_method`         | 報名方式：portal、external、none                             |
| `perks`                       | 使用者明確要求的福利清單                                     |
| `learning_passport_category`  | 學習護照時數類別                                             |
| `learning_passport_condition` | 認證方式：sign_in、sign_out、both、other                     |
| `soft_skill_category`         | 軟實力時數類別                                               |

### 日期範圍

- `this_week`：依既有查詢規則，從台灣時間今天零時起算 7 天。
- `next_week`：台灣時間下週一零時至再下一週一零時之前。
- 未指定時間時，解析器使用 `this_week`。

### 篩選規則

- 同一場活動必須符合所有指定條件。
- 名稱、承辦單位、講者與地點採不分大小寫的文字包含比對。
- 活動形式、報名方式與護照認證方式採精確比對。
- 多個福利條件必須全部符合。
- 福利會比對活動的福利說明與既有標籤，並排除部分明確否定福利的文字。
- 時數類別必須相同，且該類別時數大於 0。
- 未指定的條件不參與篩選。

ActivityQuery 使用 Pydantic 驗證資料，禁止未定義欄位並啟用嚴格型別驗證。

## 環境設定

### Gemini

| 環境變數           | 說明               |
| ------------------ | ------------------ |
| `AI_PROVIDER`    | 設為 gemini        |
| `GEMINI_MODEL`   | 實際使用的模型名稱 |
| `GEMINI_API_KEY` | Gemini API 金鑰    |

### Azure OpenAI

| 環境變數                    | 說明                      |
| --------------------------- | ------------------------- |
| `AI_PROVIDER`             | 設為 azure                |
| `AZURE_OPENAI_BASE_URL`   | Azure OpenAI API 基底網址 |
| `AZURE_OPENAI_DEPLOYMENT` | 實際使用的部署名稱        |
| `AZURE_OPENAI_API_KEY`    | Azure OpenAI API 金鑰     |

### 部署參數

`template.yaml` 已將以下部署參數連接到 webhook 的環境變數：

| 部署參數                  | Lambda 環境變數             | 用途                              |
| ------------------------- | --------------------------- | --------------------------------- |
| `AIProvider`            | `AI_PROVIDER`             | 選擇 azure 或 gemini，預設 gemini |
| `GeminiModel`           | `GEMINI_MODEL`            | 使用 Gemini 時填入有效模型名稱    |
| `AzureOpenAIBaseUrl`    | `AZURE_OPENAI_BASE_URL`   | 使用 Azure 時填入 API 基底網址    |
| `AzureOpenAIDeployment` | `AZURE_OPENAI_DEPLOYMENT` | 使用 Azure 時填入部署名稱         |

部署預設使用 Gemini，模型為 `gemini-3.1-flash-lite`，沿用目前本機設定。Azure 網址與部署名稱預設為空。現有 `samconfig.toml` 只傳入 Stage，因此部署時會使用上述 Gemini 預設值；仍須準備對應環境的 Parameter Store 金鑰。

切換供應商或模型時，在對應環境的 `parameter_overrides` 保留 Stage，另外加入所選供應商的參數：

- Gemini：`AIProvider=gemini` 與 `GeminiModel=實際模型名稱`。
- Azure：`AIProvider=azure`、`AzureOpenAIBaseUrl=實際API基底網址` 與 `AzureOpenAIDeployment=實際部署名稱`。

以上為設定格式，模型與網址需換成實際有效值。只需填寫所選供應商的設定；缺少必要設定時，AI 查詢會失敗。

範本也宣告了空的 `GEMINI_API_KEY` 與 `AZURE_OPENAI_API_KEY`，供本機 SAM 透過 `env.json` 覆寫。雲端依下節的 Parameter Store 路徑讀取金鑰。

### 金鑰讀取方式

程式優先讀取環境變數。環境變數未提供金鑰時，會透過既有的 get_secret() 從 AWS Systems Manager Parameter Store 讀取：

- Gemini：`{SSM_PREFIX}/gemini-api-key`
- Azure：`{SSM_PREFIX}/azure-openai-api-key`

SSM_PREFIX 預設為 `/fomoless/dev`。使用 Parameter Store 時，執行身分必須具備對應的讀取與解密權限。

目前程式不會自動載入 `.env`；僅建立 `.env` 檔案，不會直接變成程式的環境變數。

實際查詢及回覆還需要既有的 EVENTS_TABLE、LINE_CHANNEL_SECRET、LINE_CHANNEL_ACCESS_TOKEN 等設定。

金鑰不得寫入原始碼、文件或 PR 說明。`.env` 與 `env.json` 應保留在本機，不提交至 GitHub。

## 本機測試

在專案根目錄執行以下 PowerShell 指令。

### 安裝測試依賴

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
```

### 執行全部測試

```powershell
python -X utf8 -m pytest -q
```

最新回歸驗證結果（本機 PowerShell）：

- `111 passed in 2.07s`。
- 已驗證「本週活動」優先呼叫 LLM、缺少供應商設定或 AI 逾時時使用關鍵字備援，以及資料庫失敗時不會重複查詢。
- `cfn-lint template.yaml` 通過。

### 查看 Lambda handler 模擬回覆

```powershell
python -X utf8 -m pytest tests/test_lambda_local.py -v -s
```

`-s` 會顯示測試印出的模擬回覆；`-X utf8` 用於避免 Windows 終端機輸出中文字或符號時的編碼問題。

### 新增測試的範圍

| 測試檔案                         | 驗證內容                                                      |
| -------------------------------- | ------------------------------------------------------------- |
| `tests/test_activity_query.py` | 查詢模型、型別驗證、供應商解析器與工廠設定                    |
| `tests/test_event_filter.py`   | 文字、代碼、福利、時數及複合條件篩選                          |
| `tests/test_lambda_local.py`   | LINE 簽章、一般與 Base64 請求、解析與查詢流程、回覆及錯誤處理 |

Lambda handler 測試模擬 AI、DynamoDB 與 LINE API，不會實際呼叫這些外部服務，也不需要真實金鑰。

以上 pytest 測試是在本機 Python 執行；先前完成的 SAM 容器驗證結果如下。

### SAM 容器驗證紀錄

已通過上述 pytest，並完成 `sam build --config-env dev`。以測試金鑰及空的 LINE 事件清單呼叫 `app.handler` 時，仍出現 `timed out after 15 seconds`，尚未取得回應；獨立 120 秒期限的啟動測試待確認。此測試不呼叫 AI、DynamoDB 或 LINE API。

以下保留先前版本在 Python 3.12.16／ARM64（aarch64）容器內的驗證紀錄，不代表目前版本已完成相同檢查。

當時使用臨時測試入口與獨立範本，涵蓋兩個供應商各自的一般與 Base64 訊息。測試執行真實的 `app.handler`、LINE 簽章解析、供應商解析器與篩選程式；AI API、DynamoDB 與 LINE 發送 API 使用 mock。

另外直接呼叫 `app.handler` 驗證簽章。這三個事件的 LINE 事件清單為空，只驗證啟動與簽章，不會發送訊息或查詢資料庫。

已完成的容器驗證結果：

| 情境                               | 實際結果                               |
| ---------------------------------- | -------------------------------------- |
| 正確簽章、一般本文                 | HTTP 200                               |
| 正確簽章、Base64 本文              | HTTP 200                               |
| 錯誤簽章                           | HTTP 400，Invalid signature            |
| Azure／Gemini × 一般／Base64 訊息 | 4 項全部通過，回覆僅包含符合條件的活動 |

共完成 7 項容器檢查，不需要真實金鑰，也未部署至 AWS。

#### 本機逾時限制

原本建置範本的 15 秒期限下，兩次本機呼叫都逾時。當時的獨立測試範本使用 120 秒期限：直接呼叫 handler 約需 44～62 秒，包含兩個供應商的 4 項流程約需 97 秒。

本機是 x86 電腦執行 ARM64 容器，這些耗時包含本機環境的啟動與套件載入成本，不能直接視為 AWS 上的執行時間。正式 `template.yaml` 的 15 秒設定未修改，仍須驗證正式期限及雲端實際耗時。

## 目前限制與待驗證事項

- 目前只支援上述日期範圍，不支援任意日期或「明天」等其他時間條件。
- 尚不支援「至少幾小時」的數值條件，以及福利擇一符合的查詢。
- 文字與福利主要採文字比對，尚未處理完整的同義詞或所有否定語句。
- DynamoDB 查詢目前尚未處理分頁結果。
- AWS SAM Python 3.12 ARM64 建置映像已下載完成。
- 本次版本已重新建置成功，但容器啟動測試仍在正式 15 秒期限下逾時，獨立 120 秒期限的啟動結果待確認。先前版本的 7 項容器檢查紀錄不代表本次版本已通過。
- 尚未完成 AWS DynamoDB 與 LINE 真實服務的完整串接驗證。
- `template.yaml` 預設使用 Gemini；部署前需確認模型可用，以及 Parameter Store 金鑰與讀取權限。切換 Azure 時需提供對應的網址與部署名稱。
