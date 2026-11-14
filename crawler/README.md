# iNCU 活動報名爬蟲

> ⚠️ 暫時措施：計中活動 API 上線後移除。
> cookie 是個人登入身分，不放雲端；爬蟲只在組員自己的電腦排程執行，輸出 JSON 再上傳 S3，後續驗證、寫入資料庫、推播由後端 Lambda 處理。

用自己瀏覽器已登入的 session，從「活動列表」＋「活動詳細資料」抓活動，
欄位依「FomoLess 活動 API 欄位需求（草案）」一、二節的表格（攤平成一層）。

## 準備 cookie（只放自己電腦，不要 commit、不要分享）
瀏覽器登入 → 活動報名頁 → F12 → Network → 點 `activity` 那筆 → Request Headers 的 Cookie 值，
存成同資料夾的 `ncu_cookies.txt`（一行）。

## 跑
```
py -m pip install requests beautifulsoup4
py portal_activity_crawler.py crawl              # 所有還沒結束的活動 → output/portal_activities_日期.json
py portal_activity_crawler.py crawl -n 5         # 測試：只抓 5 則
py portal_activity_crawler.py samples output/portal_activities_日期.json   # 挑 3 筆範例 → samples/
```

在程式裡用：
```python
from portal_activity_crawler import crawl
activities = crawl()   # list[dict]，所有還沒結束的活動
```

## 輸出欄位
**每個場次一筆**：一個活動有 N 場就輸出 N 筆，活動共用的欄位（名稱、承辦單位、海報…）每筆都會帶；已經結束的場次會濾掉。

| 欄位 | 來源 |
|---|---|
| activity_id, title, organizer, audience, url | 內頁上方 dl |
| session_id, session_name | 場次分頁（例：115A02237），pk = `portal.activity#活動編號#場次編號` |
| contact_email | 固定 null（不存承辦人姓名、個人信箱；raw_fields 也不放） |
| raw_fields | 內頁畫面上顯示的所有「標籤→文字」原樣保留，活動內容在 `raw_fields["活動內容"]` |
| scraped_at | 這筆資料爬下來的時間（UTC） |
| image_url（list） | 列表卡片＋輪播＋內頁內容裡的圖 |
| status | 內頁場次按鈕（已截止…），沒有就用列表卡片標籤 |
| views | 列表卡片「瀏覽次數」 |
| sdgs_tag | 列表卡片「SDGs標籤」 |
| speaker, location, start_at/end_at, registration_start/end, capacity, waitlist_capacity, hour_tags, learning_passport_hours/condition, soft_skill_hours | 內頁場次區塊 |
| registered_count, waitlist_count | 內頁「報名人數：0 / 80」「備取人數：0 / 0」 |
| registration_method/url | 場次有 Portal 報名按鈕（含已截止、額滿）→ `portal`，url 為 null；沒有按鈕但有 Google 表單等連結 → `external`，附 url（先找場次說明，沒有才用活動內容）；兩種都沒有 → `none` |
| registration_status | 由 status 轉成：開放報名／已截止／已額滿／尚未開始 |
| updated_at, location_code | 網頁上沒有，固定 null |

## 每天執行
- **每天上傳請跑完整的 `crawl`**（輸出所有還沒結束的場次），報名人數、額滿狀態、新增場次才會更新；資料庫組用「最後一次被爬到的時間」判斷下架
- `--new-only` 只在測試時使用，會跳過抓過的活動，不要用在每天上傳
- Windows 工作排程器：
  `schtasks /create /tn FomoLessPortal /sc daily /st 08:00 /tr "cmd /c cd /d <資料夾> && py portal_activity_crawler.py crawl >> crawl.log 2>&1"`
- cookie 會過期，過期時程式會停下來提示重新複製，不會產生 JSON；一筆都沒抓到時也不會輸出空檔案
