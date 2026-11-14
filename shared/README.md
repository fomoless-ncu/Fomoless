# shared：共用資料格式

所有模組對「活動資料」的唯一標準。**修改欄位或標籤請發 PR，並在群組通知所有組。**

| 檔案 | 用途 |
| --- | --- |
| `models.py` | `Event` 資料模型（一筆 = 一個場次）、標籤目錄 `TAG_CATALOG`、學習護照類別、Gemini 用的 `PosterExtraction` |
| `sample_events.json` | 16 筆假場次（2026-11、12 月），含名額、學習護照與軟實力時數，開發時當假資料庫 |
| `seed.py` | 把假活動寫進 DynamoDB（dev 或本機 DynamoDB Local） |

## 各組怎麼用

**爬蟲、電算中心 API 轉接器：產生場次**

```python
from shared.models import Event, HourEntry, RegistrationMethod, Source, make_event_id

event = Event(
    event_id=make_event_id(Source.CRAWLER_PORTAL, session_url),  # 同一場次永遠同 id，可去重
    activity_id="A0001",                       # 同一活動的場次共用
    title="115-1學期客家學院週會",
    host="客家學院",
    start_at="2026-11-10T10:00:00+08:00",      # 一定要帶時區
    location="客家學院1樓國際會議廳",
    registration_method=RegistrationMethod.PORTAL,
    capacity=120, registered_count=120,
    learning_passport_hours=[HourEntry(category="院週會", hours=2)],  # 會自動加入「院週會」標籤
    soft_skill_hours=[HourEntry(category="批判思考", hours=0.66)],
    tags=["講座"],                              # 只能從 TAG_CATALOG 選
    source=Source.CRAWLER_PORTAL,
    source_url=session_url,
)
print(event.is_full)  # True
```

格式不對會丟出 `ValidationError`，訊息會說哪個欄位有問題。

**AI 海報辨識：讓 Gemini 照格式輸出**

```python
from shared.models import PosterExtraction, TAG_CATALOG

schema = PosterExtraction.model_json_schema()   # 當作 Gemini 的 response schema
result = PosterExtraction.model_validate_json(gemini_response_text)
```

Prompt 裡要列出 `TAG_CATALOG` 的標籤，請 Gemini 只能從中選。結果存成 `status=draft`，等主辦方確認後才改成 `published`。

**資料庫組：存取 DynamoDB**

```python
table.put_item(Item=event.to_dynamo_item())          # 寫入
event = Event.from_dynamo_item(resp["Item"])          # 讀出
```

`to_dynamo_item()` 會自動處理時間格式、Decimal 轉換、省略空值，並帶上 `month`、`categories`。

**LINE Bot、Discord Bot、前端：先用假資料**

```python
from shared.models import load_sample_events, CATEGORY_LABELS

for e in load_sample_events():
    print(e.start_at, e.title, [CATEGORY_LABELS[c] for c in e.categories])
```

前端可以直接讀 `sample_events.json` 當假 API 回應。

## 重要規則

| 規則 | 原因 |
| --- | --- |
| 時間一律帶時區，存成 `+08:00` | 依月份查詢用字串比對時間，格式不一致會查不到 |
| 標籤只能從 `TAG_CATALOG` 選 | 學生訂閱靠標籤配對，自創標籤不會被推播 |
| `event_id` 用 `make_event_id()` 產生 | 同一場次重複爬到時會覆寫，不會出現兩筆 |
| 一筆資料 = 一個場次 | 同一活動的多個場次各自一筆，用 `activity_id` 關聯 |
| 學習護照類別用 `LEARNING_PASSPORT_CATEGORIES` 的名稱 | 才會自動變成訂閱標籤；正式清單待電算中心提供 |
| AI 和擴充功能產生的資料先存 `draft` | 人工確認後才 `published`，草稿不會出現在查詢和推播 |

## 寫入假資料

```bash
# 本機 DynamoDB Local（不需 AWS 帳號）
docker run -d -p 8000:8000 amazon/dynamodb-local
python -m shared.seed --table fomoless-dev-events --local --create

# AWS dev 資料表（管理員）
python -m shared.seed --table fomoless-dev-events
```
