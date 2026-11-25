# AI Push Writer：訂閱活動推薦與推播文案

本模組接收活動清單及使用者訂閱標籤，由 Python 篩選、去重、排序並選定最多 5 筆活動，再由 Gemini 或 Azure OpenAI 為選定活動產生開場與推薦理由。LLM 不選擇活動；日期、地點、福利、認證條件與網址由程式從原始活動回填，最後回傳可供發送的純文字內容。

以下整理此次「新增」及「修改」檔案，涵蓋 AI Push Writer、Query／Push 共用個資遮罩、Layer 打包、測試與文件。

## 本次變更檔案與責任

### 推播與 Query

```text
backend/webhook/
├── app.py                    # 修改：Query 輸入使用共用個資遮罩
├── push_candidates.py        # 新增：訂閱標籤配對、有效性篩選、去重、排序
├── push_service.py           # 新增：選定活動、生成、驗證、備援與純文字組裝
└── llm/
    ├── activity_push.py      # 新增：輸入／輸出 Schema、提示詞、模型抽象介面
    ├── gemini_push.py        # 新增：Gemini 推播文案實作
    ├── azure_push.py         # 新增：Azure OpenAI 推播文案實作
    └── activity_query.py     # 修改：提示詞不得將遮罩標記當作查詢條件
```

| 檔案                                                         | 類型 | 修改內容與目的                                                                                                                                                |
| ------------------------------------------------------------ | ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [activity_push.py](../backend/webhook/llm/activity_push.py)   | 新增 | 定義精簡輸入、文案輸出、共用抽象介面及提示詞；要求為所有選定活動撰寫文案，限制最多 5 筆，拒絕重複 ID、空白理由及額外欄位                                      |
| [push_candidates.py](../backend/webhook/push_candidates.py)   | 新增 | 正規化傳入標籤，篩選有效活動、檢查報名與名額、場次去重、排序並限制候選筆數；不讀寫訂閱資料庫                                                                  |
| [push_service.py](../backend/webhook/push_service.py)         | 新增 | Python 選定最多 5 筆、建立 writer、精簡模型輸入、驗證回傳 ID 完整一致、從原始資料回填事實、固定模板備援及完整訊息 500 字元檢查；個資規則用 `shared.privacy` |
| [gemini_push.py](../backend/webhook/llm/gemini_push.py)       | 新增 | 實作 Gemini 推播介面，傳送共用 JSON Schema，驗證 JSON 回應，設定逾時及單次嘗試                                                                                |
| [azure_push.py](../backend/webhook/llm/azure_push.py)         | 新增 | 實作 Azure 推播介面，使用 Responses API 的 `PushDraft` 結構化輸出，設定逾時、不自動重試及 `store=False`                                                   |
| [app.py](../backend/webhook/app.py)                           | 修改 | 在 `respond_to_text()` 呼叫 Query provider 前使用 `redact_pii()`；原始文字僅留給本機關鍵字備援                                                            |
| [activity_query.py](../backend/webhook/llm/activity_query.py) | 修改 | 共用 Query 提示詞補充：不得把「（已遮蔽）」當成名稱、單位、講者或地點等查詢條件                                                                               |

### 共用規則、打包與 CI

| 檔案                                                         | 類型 | 修改內容與目的                                                                                                             |
| ------------------------------------------------------------ | ---- | -------------------------------------------------------------------------------------------------------------------------- |
| [shared/privacy.py](../shared/privacy.py)                     | 修改 | 沿用 `redact_pii()`，新增 `contains_pii()`；遮罩與偵測共用 `_PATTERNS`，避免 Query／Push 各自維護規則                |
| [shared/Makefile](../shared/Makefile)                         | 新增 | 提供 SAM 的 `build-SharedPythonLayer` target，呼叫 Python 建置腳本                                                       |
| [shared/build_layer.py](../shared/build_layer.py)             | 新增 | 以白名單將 `__init__.py`、`privacy.py`、`models.py` 複製到 Layer 的 `python/shared/`，排除腳本、測試資料及本機設定 |
| [template.yaml](../template.yaml)                             | 修改 | 新增 `SharedPythonLayer` 及共用掛載設定，維持函式的 `CodeUri: backend/webhook/`，讓部署後的函式可以匯入 `shared.*`   |
| [.github/workflows/ci-cd.yml](../.github/workflows/ci-cd.yml) | 修改 | 在既有測試流程加入 Makefile Layer 建置步驟，保留既有 `pytest -q`、範本檢查及部署流程                                     |

### 測試檔案

| 檔案                                                         | 類型 | 修改內容與目的                                                                                                                                        |
| ------------------------------------------------------------ | ---- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| [tests/test_activity_push.py](../tests/test_activity_push.py) | 新增 | 84 個參數化後的推播案例，涵蓋篩選、Schema、文案與備援；API 使用替身，每個案例攔截 socket／DNS；包含兩家 SDK 的 401、429、逾時、解析失敗與惡意描述情境 |
| [tests/test_shared_layer.py](../tests/test_shared_layer.py)   | 新增 | 建立 Layer 產物，在禁止網路且不包含 repo 根目錄的隔離子程序中，驗證 Query／Push 皆能匯入同一個共用遮罩函式                                            |
| [tests/test_privacy.py](../tests/test_privacy.py)             | 修改 | 延伸既有遮罩測試，確認 `contains_pii()` 與遮罩結果一致，並保留正常日期、網址、單位與空值行為                                                        |
| [tests/test_lambda_local.py](../tests/test_lambda_local.py)   | 修改 | 修正共用套件搜尋路徑；固定一般案例時鐘、補上跨月／跨年查詢；新增 6 個 Query 遮罩案例，檢查兩家 SDK 實際收到的文字                                     |
| [tests/test_event_filter.py](../tests/test_event_filter.py)   | 修改 | 同時加入 repo 根目錄及 `backend/webhook` 搜尋路徑，讓直接執行 `pytest` 時也能匯入新增共用遮罩的 `app.py`                                        |

### 文件

| 檔案                                                     | 類型 | 修改內容與目的                                                              |
| -------------------------------------------------------- | ---- | --------------------------------------------------------------------------- |
| [README.md](../README.md)                                 | 修改 | 補上共用 Lambda Layer 的範本資源說明                                        |
| [shared/README.md](../shared/README.md)                   | 修改 | 說明共用遮罩／偵測 API、Query／Push 使用位置及 Layer 建置方式               |
| [docs/ai-provider-interface.md](ai-provider-interface.md) | 修改 | 更新 Query 流程、共用個資規則與測試匯入方式，說明 provider 本身不另維護遮罩 |
| [docs/ai-push-writer.md](ai-push-writer.md)               | 新增 | 本文件：記錄變更檔案、介面、篩選規則、離線測試方式、逐檔結果及尚未完成項目  |

未來 LINE 指令或每週排程入口讀取資料後，呼叫 `generate_push()` 即可。

## 處理流程

```text
呼叫端提供 events、user_tags、now、writer
    → select_push_candidates()：篩選、去重、排序，預設保留 5 筆
    → 排除無法組成合法訊息或單筆必要資訊已超過 500 字元的候選
    → Python 選定 selected：最多 5 筆；不足時使用所有可發送活動
    → build_push_input(selected, tags)：建立精簡欄位白名單
    → writer.generate()：呼叫模型並驗證輸出 Schema
    → 驗證回傳 ID 與 selected 完整一致，以及文案規則
    → 依 ID 從原始活動回填事實
    → 依日期組裝並檢查完整訊息長度
    → 回傳 PushResult
```

- 沒有合法候選或可發送活動：直接回傳 `skipped`，不呼叫模型。
- `writer=None`：不呼叫模型，使用固定模板。
- 準備模型輸入、模型呼叫或輸出驗證失敗：保留 Python 已選定的最多 5 筆活動，改套固定模板。
- `generated`、`fallback`、`skipped` 都是文案處理結果，不是 LINE 送達狀態。

## 輸入資料

### `generate_push()` 介面

```python
def generate_push(
    events: list[dict],
    user_tags: list[str],
    *,
    now: datetime,
    writer: ActivityPushWriter | None = None,
    candidate_limit: int = 5,
) -> PushResult:
    ...
```

| 參數                | 說明                                                                                                                |
| ------------------- | ------------------------------------------------------------------------------------------------------------------- |
| `events`          | 由呼叫端提供的活動 dict 清單；欄位沿用共用 `Event` 模型。此函式不查詢 DynamoDB，也不完整驗證整份 `Event` Schema |
| `user_tags`       | 使用者已訂閱的標籤，必須為 `list[str]`。移除前後空白、空字串及重複值後配對                                        |
| `now`             | 必須帶時區；同時作為七天範圍、活動結束與報名期限的判斷依據                                                          |
| `writer`          | 實作 `ActivityPushWriter` 的模型介面；預設 `None` 表示固定模板模式                                              |
| `candidate_limit` | 候選活動上限，預設 5，必須是正整數；即使調高此值，送給 LLM 的選定活動仍最多 5 筆                                    |

以下活動欄位由 Python 使用，並非全部送進模型：

| 用途           | 欄位                                                                                                                                                            |
| -------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 識別與去重     | `event_id`、`activity_id`                                                                                                                                   |
| 標籤配對       | `tags`                                                                                                                                                        |
| 活動狀態與日期 | `status`、`start_at`、`end_at`                                                                                                                            |
| 報名與名額篩選 | `registration_method`、`registration_status`、`registration_start`、`registration_end`、`capacity`、`registered_count`                              |
| 推薦與訊息內容 | `title`、`description`、`location`、`mode`、`audience`、`perks`、`learning_passport_hours`、`learning_passport_condition`、`soft_skill_hours` |
| 訊息連結       | `registration_url`、`info_url`                                                                                                                              |

目前沒有使用者訂閱資料表或訂閱指令的完整實作。`TAG_CATALOG` 的合法性驗證及訂閱儲存由外層負責；本模組只正規化標籤並做精確配對。

### 訂閱標籤在輸入與輸出中的用途

| 資料位置                 | 欄位                                                                      | 實際用途                                                              |
| ------------------------ | ------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| `generate_push()` 輸入 | `user_tags: list[str]`                                                  | Python 用來配對、排序活動；沒有有效標籤時不呼叫模型，回傳 `skipped` |
| LLM 輸入 `PushInput`   | `user_tags`                                                             | 供模型理解訂閱興趣、撰寫相關理由；不授權模型重新選擇活動              |
| LLM 輸入的每筆活動       | `events[].tags`                                                         | 活動自身的標籤，與使用者訂閱標籤是不同欄位                            |
| LLM 輸出 `PushDraft`   | `opening`、`recommendations[].event_id`、`recommendations[].reason` | 不回傳訂閱標籤；以 ID 對應每筆活動的文案                              |
| 最終輸出 `PushResult`  | `status`、`text`、`event_ids`、`fallback_reason`                  | 不包含訂閱標籤或收件者 ID；提供文案處理結果                           |

保留 `user_tags` 作為模型輸入，是為了生成與興趣有關的理由，不代表模型仍負責篩選。`PushRecommendation` 與 `recommendations` 目前只承載已選定活動的文案。`event_id` 則用於完整性驗證與對應原始資料，不需要由 AI 回傳使用者資訊。

活動時間接受帶時區的 ISO 8601 字串或 `datetime`；名額接受包括 DynamoDB `Decimal` 在內的數字。缺少 `event_id`、`title`、`start_at` 或無法配對的活動不會成為候選。完整共用格式見 [shared/README.md](../shared/README.md) 及 [shared/models.py](../shared/models.py)。

### 候選活動篩選規則

1. **日期範圍**：活動開始時間必須位於台灣時間今天零時起算的七天範圍，採 `[start, end)`，不包含第八天零時。這不是固定週一至週日。
2. **發布狀態**：只保留 `status="published"`，草稿、取消、延期及缺少狀態的活動都不納入。
3. **訂閱配對**：活動 `tags` 與 `user_tags` 有任一交集即可；沒有訂閱標籤時回傳空清單。
4. **結束時間**：`end_at <= now` 或 `end_at < start_at` 的場次排除。缺少 `end_at` 時無法確定是否已結束，保留；因此今天稍早開始、沒有結束時間的活動也可能被納入。
5. **名額**：`capacity=0`、已知 `registered_count >= capacity` 或 `registration_status="full"` 時排除。未知名額不視為 0，目前不推薦候補。
6. **需要報名**：方式為 `portal` 或 `external` 時，排除 `closed`、`not_started`、報名開始時間晚於現在、報名截止時間小於等於現在，以及起訖顛倒的資料。報名方式缺值時沿用 `Event` 的 `none` 預設；免報名活動不套用報名日期與 `closed`／`not_started` 篩選。
7. **場次去重**：依時間早到晚處理，保留同一 `activity_id` 最早的有效場次；缺少或空白值 `activity_id=""` 時改依 `event_id` 去重。相同 `event_id` 不會重複納入。
8. **排序與數量**：先按命中訂閱標籤數多到少，再按開始時間早到晚，最後以 `event_id` 排序，取 `candidate_limit` 筆。

無效日期、不合法名額及部分欄位錯誤會略過該筆資料。錯誤的 `now`、`user_tags` 型別或 `candidate_limit` 則由函式拋出例外，交由呼叫端處理。

只有標籤不足以判斷院系、年級或學生身分資格，因此目前不根據 `audience` 自動排除活動；有提供參與對象時會在最終文字中顯示，供使用者確認。

### 傳送給 LLM 的欄位

`build_push_input()` 建立 `PushInput`，最外層只有 `user_tags`、`events`。每筆活動使用 `PushCandidate`：

| 欄位                          | 型別／限制                         | 來源                                                  |
| ----------------------------- | ---------------------------------- | ----------------------------------------------------- |
| `event_id`                  | `str`，1～80 字元                | 原始場次 ID                                           |
| `title`                     | `str`，1～120 字元               | 清理後的活動標題                                      |
| `tags`                      | `list[str]`                      | 活動標籤                                              |
| `summary`                   | `str`，最多 180 字元，可為空字串 | `description` 去 HTML、遮蔽聯絡方式、移除網址並縮短 |
| `perks`                     | `str \| None`                     | 清理後的福利說明，保留限制條件，不截短                |
| `learning_passport_summary` | `str \| None`                     | 學習護照類別、時數與認證條件                          |
| `soft_skill_summary`        | `str \| None`                     | 軟實力類別、時數與認證條件提示                        |

`summary` 及兩種時數摘要是傳輸欄位，不需要加入 `Event`。選用欄位為 `None` 時，`to_prompt()` 會省略；缺值不代表已確認沒有福利或不予認列。

`summary` 是清理後描述的前 180 個字元，不是 AI 摘要。福利與兩種認證摘要不套用這個截短限制，避免刪掉「前 50 名」或簽到／簽退等條件。目前有福利或時數資料就傳入，尚未依個別福利偏好決定是否傳送。

認證條件對應如下：

| 原始值         | 摘要及訊息文字           |
| -------------- | ------------------------ |
| `sign_in`    | 需簽到                   |
| `sign_out`   | 需簽退                   |
| `both`       | 需簽到及簽退             |
| `other`      | 其他認證條件請見活動資訊 |
| 缺值或未知代碼 | 認證條件待確認           |

不傳送整份 `Event`、`contact_email`、學生姓名、學號或 LINE userId。日期、地點及報名網址由程式保留，不列入 `PushCandidate`。目前也沒有把主辦單位或講者納入模型輸入。

## 共用個資遮罩與 Lambda 打包

`push_service.py` 使用 [shared/privacy.py](../shared/privacy.py) 的兩個函式：

- `redact_pii()`：遮蔽模型輸入中的個資。Query 的 `app.respond_to_text()` 及既有活動匯入流程也使用此函式。
- `contains_pii()`：檢查 AI 開場或推薦理由，若含可辨識的個資，驗證失敗並改用固定模板。

共用模組讓匯入、Query 與 Push 使用同一套辨識規則，修改規則時只需維護一處；Push 再檢查 AI 輸出是否含可辨識的個資。

### 什麼是打包，為什麼需要 Layer？

打包是將部署需要的程式整理成 Lambda 可使用的產物。Lambda 不會自動讀取本機整個專案，只新增 `from shared.privacy import redact_pii`，可能在本機成功、部署後卻找不到套件。

這次保留原本的 `CodeUri`，另外以 `SharedPythonLayer` 提供共用程式，讓本機與雲端維持相同匯入名稱。

Python Layer 需要正確的套件路徑；此專案將 `shared` 放在最上層 `python/` 下，部署到 AWS 後，Lambda 會把 Layer 裡的檔案放在 `/opt/python/shared`。

### 三個檔案的打包原因

| 檔案            | 為什麼放入 Layer                                                                             |
| --------------- | -------------------------------------------------------------------------------------------- |
| `privacy.py`  | 提供 Query／Push 需要的 `redact_pii()` 與 `contains_pii()`                               |
| `models.py`   | 提供共用的 `Event`、`TAG_CATALOG` 等模型及定義，供引用者使用；不是遮罩功能本身的必要依賴 |
| `__init__.py` | 將 `shared` 作為一般 Python 套件，支援 `from shared...` 匯入                             |

### 目前的建置流程與檔案責任

[template.yaml](../template.yaml) 新增 `SharedPythonLayer`，在 `Globals.Function.Layers` 掛載給函式，並維持 `CodeUri: backend/webhook/`。Layer 以 `ContentUri: shared/` 及 `BuildMethod: makefile` 建置；[shared/Makefile](../shared/Makefile) 呼叫 [build_layer.py](../shared/build_layer.py)，只複製 `__init__.py`、`privacy.py`、`models.py` 至以下結構：

```text
SharedPythonLayer/
└── python/
    └── shared/
        ├── __init__.py
        ├── privacy.py
        └── models.py
```

部署後套件位於 `/opt/python/shared`，可直接 `from shared.privacy import contains_pii, redact_pii`。Layer 使用檔案白名單，不包含匯入腳本、測試資料或本機設定；新增共用模組時需同步更新 `LAYER_MODULES`。`models.py` 的 Pydantic 依賴沿用函式的 `requirements.txt`。

SAM 已設定在 Docker 的 Python 3.12 ARM64 建置容器內執行 Makefile。完整建置使用 `sam build --config-env dev`；只建置 Layer 可使用 `sam build SharedPythonLayer --config-env dev`。若只想在本機檢查 Layer 目錄，可執行 `python shared/build_layer.py .aws-sam/shared-layer-preview`，但此步不等同完整 SAM 建置驗證。

```text
sam build 建置 SharedPythonLayer
    → 依 BuildMethod: makefile 呼叫 shared/Makefile
    → 執行 build-SharedPythonLayer target
    → 執行 python3 build_layer.py "$(ARTIFACTS_DIR)"
    → 在 SAM 指定的輸出目錄建立 python/shared/
    → 複製白名單中的三個共用模組
```

| 檔案                            | 責任                                                                         |
| ------------------------------- | ---------------------------------------------------------------------------- |
| `template.yaml`               | 宣告 Layer、來源目錄、建置方式，並掛載到函式                                 |
| `shared/Makefile`             | 提供 SAM 指定的 `build-SharedPythonLayer` 工作入口，將輸出目錄傳給 Python  |
| `shared/build_layer.py`       | 用 `Path` 建立目錄、用 `shutil.copy2()` 複製白名單檔案；可直接在本機執行 |
| `.github/workflows/ci-cd.yml` | 在 CI 執行相同的 Makefile target，之後再跑 pytest 與範本檢查                 |

## LLM 輸出與驗證

兩家供應商共用 `ActivityPushWriter.generate(request: PushInput) -> PushDraft`。

```json
{
  "opening": "這週有符合你興趣的活動！",
  "recommendations": [
    {
      "event_id": "event-1",
      "reason": "適合喜歡動手實作的你。"
    }
  ]
}
```

| 欄位                           | 限制                                                                    |
| ------------------------------ | ----------------------------------------------------------------------- |
| `opening`                    | 1～120 字元；提示詞要求不寫確切活動筆數，避免縮減清單後不一致           |
| `recommendations`            | Schema 允許 1～5 筆；服務層要求完整涵蓋本次 Python 選定的活動，不得少寫 |
| `recommendations[].event_id` | 1～80 字元，回傳集合必須與選定清單完全一致，且不得重複                  |
| `recommendations[].reason`   | 1～100 字元，只描述主題與訂閱興趣的關聯                                 |

Schema 啟用嚴格型別並禁止額外欄位。服務層會再次驗證回應，檢查回傳 ID 與選定 ID 完整一致，並拒絕開場或理由中的網址、部分聯絡資訊、阿拉伯數字、部分中文數值事實，以及常見福利／認證／參加條件關鍵字。`event_id` 用來對應文案，不授權模型變更活動清單；不足 5 筆時仍應為實際收到的每筆活動提供文案。

目前關鍵字檢查包含「免費、便當、餐點、點心、贈品、抽獎、時數、小時、學分、認列、名額、報名、簽到、簽退、地點、保證」。即使文字與原始資料相符，出現這些詞仍會觸發固定模板備援；目的是將相關事實統一交由程式呈現。這是保守規則，可能誤擋合理理由，也不等於完整的語意事實驗證。

### 最終輸出

| 欄位                | 型別                               | 說明                                                |
| ------------------- | ---------------------------------- | --------------------------------------------------- |
| `status`          | `generated \| fallback \| skipped` | 文案處理結果                                        |
| `text`            | `str \| None`                     | 最終純文字；`skipped` 時為 `None`               |
| `event_ids`       | `list[str]`                      | 最終訊息保留的活動 ID，最多 5 筆，順序與訊息一致    |
| `fallback_reason` | `str \| None`                     | 固定原因代碼；正常生成為 `None`，略過也會提供原因 |

| 狀態／原因                          | 行為                                                        |
| ----------------------------------- | ----------------------------------------------------------- |
| `generated`／`None`             | AI 回應及業務規則檢查通過，組裝後可發送                     |
| `fallback`／`ai_disabled`       | 呼叫端未提供 writer，使用固定模板                           |
| `fallback`／`generation_failed` | 模型輸入準備、API 呼叫、Schema 或業務驗證失敗，使用固定模板 |
| `skipped`／`no_candidates`      | 無訂閱或沒有符合條件的候選                                  |
| `skipped`／`no_sendable_events` | 候選均缺少所需連結、必要資訊不合法或單筆內容已超長          |
| `skipped`／`message_too_long`   | 最終組裝仍無法產生合法長度的活動訊息                        |

`generation_failed` 不代表一定已呼叫模型，也不代表沒有費用；目前沒有取得用量後進行預算結算的功能。錯誤日誌只記錄例外類別，不記錄原始錯誤內容、金鑰或模型回應。

## 測試方式與結果

### 執行環境與命令

於專案根目錄完成本機完整回歸，使用直接執行的 pytest CLI，環境為 Windows、Python 3.14.5、pytest 9.2.0。

本次實際執行的完整測試指令：

```powershell
.\.venv\Scripts\pytest.exe tests -q -p no:cacheprovider
```

已啟用專案虛擬環境時，可直接執行等效指令；若只核對推播相關功能，可使用最後一行：

```powershell
pytest tests -q -p no:cacheprovider

pytest tests/test_activity_push.py tests/test_lambda_local.py tests/test_privacy.py tests/test_shared_layer.py -q -p no:cacheprovider
```

測試明確設定 repo 根目錄與 `backend/webhook` 的搜尋路徑，避免只在 `python -m pytest` 下成功。`-p no:cacheprovider` 僅停用 pytest 快取，測試案例不變。

### 離線測試資料與外部服務替身

| 項目              | 本次使用方式                                                                                                  | 測試位置                                                                        |
| ----------------- | ------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| 共用活動樣本      | 讀取 [shared/sample_events.json](../shared/sample_events.json)，檢查活動模型、樣本 ID 與匯入流程，不查線上活動 | `test_models.py`、`test_import_portal.py`                                   |
| Portal 樣本       | 讀取 `tests/fixtures/portal/` 的三個 JSON，包括學習護照、額滿及報名表單情境                                 | `test_portal_adapter.py`                                                      |
| 推播與 Query 活動 | 使用測試內建立的 dict、固定日期、假網址及假標籤；尚未接入真實使用者訂閱                                       | `test_activity_push.py`、`test_lambda_local.py`、`test_event_filter.py`   |
| DynamoDB          | 匯入測試使用 moto 的 `mock_aws()` 與假 AWS 金鑰；webhook 查詢另外以 mock 取代 `boto3.resource()`          | `test_import_portal.py`、`test_lambda_local.py`                             |
| Gemini／Azure     | 以 mock 取代 SDK client，模擬合法結果、逾時、401、429 與解析失敗；金鑰為 `test-key` 等假值                  | `test_activity_push.py`、`test_activity_query.py`、`test_lambda_local.py` |
| LINE              | 以 mock 取代 `MessagingApi`，只檢查傳入的訊息，不發送真實 LINE 訊息                                         | `test_lambda_local.py`                                                        |
| 爬蟲              | 假 HTML、`FakeResponse` 與被替換的 `fetch()`，不連 Portal 或表單網址                                      | `test_portal_crawler.py`                                                      |
| Layer             | 在本機暫存目錄建立產物，子程序禁止網路，使用隔離匯入驗證，不部署 AWS                                          | `test_shared_layer.py`                                                        |

本次完整執行另外使用臨時的根目錄 `conftest.py` 防護：在收集測試前替換 socket 連線、DNS 與 UDP 發送入口，任何觸發都會拋出錯誤並讓整輪測試失敗。防護同時注入假 AWS／模型／LINE 金鑰、停用 AWS EC2 metadata，並讓 AWS 設定與憑證檔路徑指向空裝置，避免讀取本機真實 AWS 設定。

防護的主程序計數為 **`blocked_network_attempts: 0`**。推播模組原有的 autouse fixture 及 Layer 隔離子程序也各自保留網路阻擋。臨時 `conftest.py` 已於執行後移除，沒有納入正式變更；因此全域阻擋與空憑證檔設定是本輪額外驗證措施，不是一般 `pytest` 指令自帶的功能。

### 2026-11-26 完整回歸結果

本次結果為 **266 passed in 5.19s**，程序結束碼為 **0**，沒有失敗、錯誤或略過案例。下列筆數由同一次完整執行的收集結果與全數通過結果核對，各檔案已包含在 266 項總數內，不重複相加。

| 測試檔案                                                 | 結果                 | 驗證內容                                                                   |
| -------------------------------------------------------- | -------------------- | -------------------------------------------------------------------------- |
| [test_activity_push.py](../tests/test_activity_push.py)   | **84 passed**  | 候選篩選、輸入最小化、Schema、模型替身、文案驗證、固定模板、字元限制與個資 |
| [test_activity_query.py](../tests/test_activity_query.py) | **32 passed**  | Query Schema、供應商選擇與兩家模型的請求／回應契約                         |
| [test_event_filter.py](../tests/test_event_filter.py)     | **22 passed**  | 既有 Query 條件篩選及共用模組匯入                                          |
| [test_import_portal.py](../tests/test_import_portal.py)   | **14 passed**  | 樣本讀取、轉換、moto DynamoDB 寫入／刪除與 dry-run                         |
| [test_lambda_local.py](../tests/test_lambda_local.py)     | **19 passed**  | LINE 簽章入口、Query 遮罩、mock 回覆、固定時鐘及跨月／跨年查詢             |
| [test_models.py](../tests/test_models.py)                 | **17 passed**  | 共用活動模型與 sample_events 樣本                                          |
| [test_portal_adapter.py](../tests/test_portal_adapter.py) | **30 passed**  | 本機 Portal fixtures 的轉換、報名與時數資料                                |
| [test_portal_crawler.py](../tests/test_portal_crawler.py) | **26 passed**  | 假 HTML／假回應的爬蟲解析與報名方式判斷                                    |
| [test_privacy.py](../tests/test_privacy.py)               | **15 passed**  | 共用遮罩與偵測、正常文字／網址保留及空值                                   |
| [test_shared_layer.py](../tests/test_shared_layer.py)     | **1 passed**   | 本機 Layer 產物、白名單、禁止網路的隔離匯入與 Query／Push 共用函式來源     |
| [test_week.py](../tests/test_week.py)                     | **6 passed**   | 既有七天範圍、跨月／跨年、查詢邊界及活動結束判斷；`week.py` 本次未修改   |
| **完整測試**                                       | **266 passed** | **5.19 秒，結束碼 0**                                                |

### 2026-11-26 SAM 容器建置結果

完整建置已完成，終端回報 **`Build Succeeded`**，程序結束碼為 **0**。本次使用 SAM CLI 1.172.1、Docker Engine 29.9.3，依 `samconfig.toml` 的 `dev.build.parameters.use_container = true` 在 Python 3.12 ARM64 容器中建置。

```powershell
sam build --config-env dev
```

本次初次執行仍等待映像，因此先使用以下指令取得映像並觀察下載進度，再重跑上述完整建置，最終成功：

```powershell
docker pull public.ecr.aws/sam/build-python3.12:latest-arm64
```

| 建置項目                | 結果與證據                                                                                                                                   |
| ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `SharedPythonLayer`   | 成功執行 `CustomMakeBuilder:CopySource`、`CustomMakeBuilder:MakeBuild`，Makefile 呼叫 `python3 build_layer.py "/tmp/samcli/artifacts"` |
| `LineWebhookFunction` | 成功完成 `PythonPipBuilder:ResolveDependencies` 與 `PythonPipBuilder:CopySource`                                                         |
| 共用模組產物            | `.aws-sam/build/SharedPythonLayer/python/shared/` 包含 `__init__.py`、`privacy.py`、`models.py`，內容與原始檔案一致                  |
| 函式產物                | `.aws-sam/build/LineWebhookFunction/` 包含 `app.py`、`push_service.py` 等程式與已解析的依賴                                            |
| ARM64 依賴              | 產物包含 `_pydantic_core.cpython-312-aarch64-linux-gnu.so`，與 Python 3.12／Linux ARM64 的目標一致                                         |
| 建置後範本              | 產生 `.aws-sam/build/template.yaml`                                                                                                        |

上述結果驗證建置流程與產物，不等同啟動 Lambda handler 或實際模型／LINE 串接驗證。

這次建置有下載公開容器映像及 Python 套件，與前述禁止真實網路／AWS 服務呼叫的 pytest 是分開的流程。本次沒有執行 `sam deploy`，沒有部署 AWS，也沒有發送 LINE 訊息或呼叫真實模型；沒有為完成建置修改功能程式、套件清單或部署架構。

### 本機測試以外的驗證狀態

| 項目                                       | 狀態與限制                                                                                          |
| ------------------------------------------ | --------------------------------------------------------------------------------------------------- |
| `sam validate --lint --region ap-east-2` | 先前已通過；本輪新增完整容器建置，未重跑 SAM 範本檢查                                               |
| Python 範例語法與文件連結                  | 文件更新後核對；語法解析不等同 Python 3.12 ARM64 執行                                               |
| `sam build --config-env dev`             | **2026-11-26 通過：Build Succeeded，結束碼 0**；Layer 與函式皆完成 Python 3.12 ARM64 容器建置 |
| 遠端 CI／真實模型／LINE 發送／AWS 部署     | 尚未驗證；建置僅下載公開映像及套件，未呼叫真實模型、LINE 或部署 AWS                                 |

### 尚未完成的執行與外部驗證

Python 3.12 ARM64 容器建置已完成；尚未以建置產物執行 `sam local invoke` 驗證 handler。既有 Layer 測試仍使用本機 Python 的隔離子程序，不應與完整 Lambda 執行驗證混為一談。

CI 檔案已新增 Makefile target，但尚未取得這批變更的遠端 CI 結果。未呼叫真實模型或 LINE、未進行雲端部署、真實費用結算或模型品質 Benchmark。

## 尚未完成與後續串接

目前完成的是可獨立呼叫的推播內容生成模組。接入每週推播時，外層仍需提供：

- 使用者訂閱的讀寫與 `TAG_CATALOG` 驗證。
- 活動資料查詢及完整 `Event` 驗證。
- 每週排程、收件者選取及 LINE Push API 發送。
- 發送結果、重試與去重紀錄。
- 模型用量、預算預留／結算、快取及 Benchmark。
