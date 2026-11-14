"""
FomoLess 爬蟲組：中央大學 iNCU「活動報名系統」爬蟲（用自己的登入 session）

※ 暫時措施：計中活動 API 上線後移除。
  cookie 是個人登入身分，不放雲端；只在組員自己的電腦排程執行，輸出 JSON 再上傳 S3。

欄位依「FomoLess 活動 API 欄位需求（草案）」一、二節的表格，
從「活動列表」和「活動詳細資料」兩個頁面抓：
  列表頁  https://cis.ncu.edu.tw/iNCU/messageNotice/activityManagement/activity?page=N
          每則活動一張 div.card[data-activity-id]：狀態、海報、瀏覽次數、SDGs
  內頁    https://cis.ncu.edu.tw/iNCU/messageNotice/activityManagement/activity/{id}
          dl.row 的 dt/dd：名稱、承辦單位、e-mail、參與對象、活動內容、
          場次區塊（div.tab-pane）：講師、地點、時間、名額、時數、報名按鈕（已截止…）

準備 cookie：
  瀏覽器登入 → 活動報名頁 → F12 → Network → 點 activity 那筆 → Request Headers 的 Cookie 值
  存成同資料夾的 ncu_cookies.txt（一行）。cookie 等於你的登入身分，不要分享、不要 commit。

用法（Windows 用 py）：
  py -m pip install requests beautifulsoup4
  py portal_activity_crawler.py crawl              # 所有還沒結束的活動 → output/portal_activities_日期.json
  py portal_activity_crawler.py crawl -n 5         # 測試：只抓 5 則
  py portal_activity_crawler.py crawl --new-only   # 測試用：只抓 seen_ids.json 裡沒有的（每天上傳請跑完整 crawl）
  py portal_activity_crawler.py samples output/portal_activities_日期.json   # 挑 3 筆範例到 samples/
  py portal_activity_crawler.py probe <網址>        # 存下頁面原始碼到 debug/，看結構用

在程式裡用：
  from portal_activity_crawler import crawl
  activities = crawl()   # list[dict]，所有還沒結束的活動
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

LIST_URL = os.getenv("NCU_ACTIVITY_LIST_URL", "https://cis.ncu.edu.tw/iNCU/messageNotice/activityManagement/activity")
ID_RE = re.compile(r"activityManagement/activity/(\d+)(?:[?#]|$)")
COOKIE_FILE = Path(os.getenv("NCU_COOKIE_FILE", "ncu_cookies.txt"))
DELAY_SEC = 1.0  # 每個 request 間隔 1 秒，對學校主機友善
HEADERS = {"User-Agent": "Mozilla/5.0 (FomoLess NCU student project)"}
LOGIN_HINTS = ("login", "signin", "/cas/", "oauth", "authorize", "sso")
TZ = "+08:00"
PORTAL_BTN_RE = re.compile(r"報名|截止|額滿|未開放|備取|候補")  # 場次的 Portal 報名按鈕（含已截止、額滿等停用狀態）
FORM_RE = re.compile(r"https?://(?:docs\.google\.com/forms|forms\.gle|forms\.office\.com|www\.surveycake\.com)[^\s\"'<>]*")


class SessionExpired(RuntimeError):
    pass


# ---- 登入 session --------------------------------------------------------
def load_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    raw = os.getenv("NCU_COOKIE")
    if not raw and COOKIE_FILE.exists():
        text = COOKIE_FILE.read_text(encoding="utf-8").strip()
        if text.startswith("# Netscape") or "\t" in text:
            jar = MozillaCookieJar(str(COOKIE_FILE))
            jar.load(ignore_discard=True, ignore_expires=True)
            s.cookies.update(jar)
            return s
        raw = text
    if not raw:
        sys.exit(f"找不到 cookie：請建立 {COOKIE_FILE} 或設定環境變數 NCU_COOKIE")
    s.headers["Cookie"] = re.sub(r"^cookie:\s*", "", raw, flags=re.I)
    return s


def fetch(s: requests.Session, url: str) -> requests.Response:
    # 不讓 requests 自動轉址：cookie 放在 header，自動轉址到別的網域會把 cookie 一起送出去
    resp = s.get(url, timeout=20, allow_redirects=False)
    time.sleep(DELAY_SEC)
    for _ in range(5):
        if not resp.is_redirect:
            break
        nxt = urljoin(resp.url, resp.headers.get("Location", ""))
        if any(h in nxt.lower() for h in LOGIN_HINTS) or urlparse(nxt).netloc != urlparse(url).netloc:
            raise SessionExpired(f"被轉址到登入頁或其他網域（{nxt}），為了不外洩 cookie 先停止 → cookie 可能過期，請重新複製")
        resp = s.get(nxt, timeout=20, allow_redirects=False)
        time.sleep(DELAY_SEC)
    if any(h in resp.url.lower() for h in LOGIN_HINTS) or resp.status_code in (401, 403):
        raise SessionExpired(f"被導到登入頁或拒絕存取（{resp.status_code} {resp.url}）→ cookie 過期，請重新複製")
    resp.raise_for_status()
    resp.encoding = "utf-8"
    return resp


# ---- 小工具 ---------------------------------------------------------------
def text(el) -> str | None:
    return el.get_text(" ", strip=True) if el else None


def dedupe(urls) -> list[str]:
    seen, out = set(), []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def iso_range(value: str | None) -> tuple[str | None, str | None]:
    """'2026-11-23 13:00 ~ 2026-11-23 17:00' → ('2026-11-23T13:00:00+08:00', '2026-11-23T17:00:00+08:00')"""
    found = re.findall(r"(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})", value or "")
    out = [f"{y}-{int(m):02d}-{int(d):02d}T{int(hh):02d}:{mm}:00{TZ}" for y, m, d, hh, mm in found]
    return (out[0] if out else None), (out[1] if len(out) > 1 else None)


def to_int(value: str | None) -> int | None:
    m = re.search(r"\d+", value or "")
    return int(m.group()) if m else None


def count_pair(label: str, blob: str) -> tuple[int | None, int | None]:
    """'報名人數：10 / 200' → (10, 200)"""
    m = re.search(label + r"\s*[:：]\s*(\d+)\s*/\s*(\d+)", blob)
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


def parse_hours(value: str | None) -> list[dict]:
    """'跨域整合 :  1.33小時、科技應用 :  1.33小時' → [{category, hours}]；'本場次不提供時數' → []"""
    if not value or "不提供" in value:
        return []
    value = re.sub(r"[（(]※.*?[)）]", "", value)
    return [{"category": c.strip(), "hours": float(h)}
            for c, h in re.findall(r"([^:：、,，]+?)\s*[:：]\s*([\d.]+)\s*小時", value)]


def dl_pairs(dl) -> dict:
    """dl.row 裡的 <dt>標籤</dt><dd>值</dd> → dict（只看這一層，不往場次區塊裡面找）"""
    pairs = {}
    for dt in dl.find_all("dt", recursive=False):
        dd = dt.find_next_sibling()
        if dd is not None and dd.name == "dd":
            pairs.setdefault(dt.get_text(strip=True), dd)
    return pairs


def images_in(el, base: str) -> list[str]:
    return [urljoin(base, i["src"]) for i in el.find_all("img", src=True) if not i["src"].startswith("data:")] if el else []


# ---- 活動列表 ---------------------------------------------------------------
def parse_card(card, base: str) -> dict:
    """列表頁一張卡片 → 狀態、標題、瀏覽次數、SDGs、海報"""
    blob = card.get_text(" ", strip=True)

    def badges(label):
        div = next((d for d in card.find_all("div", class_="card-text") if d.get_text(strip=True).startswith(label)), None)
        return [b.get_text(strip=True) for b in div.find_all("span")] if div else []

    views = re.search(r"瀏覽次數\s*[:：]\s*([\d,]+)", blob)
    status = card.find("span", class_="badge", recursive=False)
    title = card.find(class_="card-title")
    return {
        "activity_id": card["data-activity-id"],
        "title": (title.get("title") or text(title)) if title else None,
        "list_status": text(status),  # 報名中／已截止／未開放報名
        "views": int(views.group(1).replace(",", "")) if views else None,
        "sdgs_tag": badges("SDGs標籤"),
        "images": images_in(card, base),
    }


def collect_list(s: requests.Session, url: str) -> list[dict]:
    soup = BeautifulSoup(fetch(s, url).text, "html.parser")
    items = [parse_card(c, url) for c in soup.select("div.card[data-activity-id]")]
    carousel = {}  # 上方輪播圖的海報，跟卡片上的網址不一樣，也一起收
    for a in soup.select(".carousel a[href]"):
        m = ID_RE.search(a["href"])
        if m:
            carousel.setdefault(m.group(1), []).extend(images_in(a, url))
    for it in items:
        it["images"] = dedupe(it["images"] + carousel.get(it["activity_id"], []))
    return items


def collect_new(s, max_pages: int, store) -> list[dict]:
    """?page=1,2,3… 往後翻，只留沒抓過的；某頁全部抓過（或沒東西）就停。"""
    items, this_run = [], set()
    for page in range(1, max_pages + 1):
        rows = [it for it in collect_list(s, f"{LIST_URL}?page={page}") if it["activity_id"] not in this_run]
        this_run.update(it["activity_id"] for it in rows)
        new = [it for it in rows if not (store and store.has(it["activity_id"]))]
        print(f"[list] 第 {page} 頁：{len(rows)} 則，其中 {len(new)} 則沒抓過")
        items += new
        if not new:
            break
    return items


# ---- 活動詳細資料 -------------------------------------------------------------
def scrape_detail(s: requests.Session, url: str) -> list[dict]:
    """內頁 → 每個場次一個 dict（活動共用的欄位每場都會帶）"""
    soup = BeautifulSoup(fetch(s, url).text, "html.parser")
    info_dl = soup.select_one("div.container > dl.row")
    info = dl_pairs(info_dl) if info_dl else {}

    # 參與對象：<label>教職員：</label><span>【全部教職員】</span>
    audience = {}
    if info.get("參與對象"):
        for div in info["參與對象"].find_all("div"):
            label, span = div.find("label"), div.find("span")
            if label and span:
                name = label.get_text(strip=True).rstrip("：:")
                key = {"教職員": "staff", "學生": "students", "校外人士": "public"}.get(name, name)
                audience[key] = span.get_text(strip=True).strip("【】")

    # 活動內容：「活動內容」後面那格是空的，真正內容在下一個 dd.col-sm-12（<hr> 後面）
    desc_dd = info_dl.find("dd", class_="col-sm-12", recursive=False) if info_dl else None

    # 不存承辦人姓名、個人信箱（組長 11/7：只存必要資料）
    activity_raw = {k: text(v) for k, v in info.items() if "承辦人" not in k and "mail" not in k.lower()}
    activity_raw["活動內容"] = text(desc_dd)

    # 場次：#myTabContent 底下每個 div.tab-pane 是一場，一個活動可能有很多場
    panes = soup.select("#myTabContent > div.tab-pane") or [soup]
    sessions = []
    for i, pane in enumerate(panes):
        pane_dl = pane.find("dl", class_="row")
        sess = dl_pairs(pane_dl) if pane_dl else {}
        # 報名按鈕：可能是 <button>（例：已截止，disabled）或 <a class="btn">（例：報名 → /activity/{場次}/edit）
        buttons = pane.select("button, a.btn")
        button = next((b for b in buttons if PORTAL_BTN_RE.search(b.get_text())), None) or pane.find("button")
        blob = pane.get_text(" ", strip=True)
        registered, capacity_shown = count_pair("報名人數", blob)
        waitlisted, waitlist_shown = count_pair("備取人數", blob)
        session_desc = pane_dl.find("dd", class_="col-sm-12", recursive=False) if pane_dl else None
        title_h3 = pane_dl.find("h3") if pane_dl else None
        pane_id = pane.get("id") or ""
        if not pane_id.startswith("event"):
            # 找不到場次編號時用流水號，場次順序一變 id 就會變，需要留意
            print(f"  [warning] {url} 第 {i + 1} 個場次找不到場次編號，暫用流水號 {i + 1}")

        reg_start, reg_end = iso_range(text(sess.get("報名時間")))
        start_at, end_at = iso_range(text(sess.get("場次時間")))
        # 表單連結：先找這個場次自己的說明，找不到才用活動內容（活動內容的連結會套到每一場）
        forms = FORM_RE.findall(str(session_desc or "")) or FORM_RE.findall(str(desc_dd or ""))

        # raw_fields：內頁畫面上顯示的「標籤 → 文字」原樣保留（活動內容也放這裡）
        raw = dict(activity_raw)
        if title_h3:
            raw["場次名稱"] = text(title_h3)
        raw.update({k: text(v) for k, v in sess.items() if k not in raw})
        if session_desc:
            raw["場次說明"] = text(session_desc)
        for label in ("報名人數", "備取人數"):
            m = re.search(label + r"\s*[:：]\s*(\d+\s*/\s*\d+)", blob)
            if m:
                raw[label] = m.group(1)
        if button:
            raw["報名按鈕"] = text(button)

        sessions.append({
            "session_id": pane_id.removeprefix("event") or f"{i + 1}",  # 例：115A02237
            "session_name": text(title_h3),
            "raw_fields": raw,
            "title": text(info.get("活動名稱")),
            "organizer": text(info.get("承辦單位")),
            "audience": audience,
            "button_status": text(button),
            "has_portal_button": bool(button and PORTAL_BTN_RE.search(text(button) or "")),
            "speaker": text(sess.get("講師")) or None,
            "location": text(sess.get("地點")),
            "start_at": start_at, "end_at": end_at,
            "registration_start": reg_start, "registration_end": reg_end,
            "registration_url": forms[0] if forms else None,
            "capacity": to_int(text(sess.get("報名人數上限"))) or capacity_shown,
            "registered_count": registered,
            "waitlist_capacity": to_int(text(sess.get("備取人數上限"))) if sess.get("備取人數上限") else waitlist_shown,
            "waitlist_count": waitlisted,
            "hour_tags": parse_hours(text(sess.get("時數標籤"))),
            "learning_passport_hours": parse_hours(text(sess.get("學習護照時數標籤"))),
            "learning_passport_condition": text(sess.get("學習護照認證條件")),
            "soft_skill_hours": parse_hours(text(sess.get("軟實力時數標籤"))),
            "images": images_in(desc_dd, url) + images_in(session_desc, url),
        })
    return sessions


def registration_status(status: str | None, registered, capacity) -> str | None:
    """轉成草案的四種：開放報名、已截止、已額滿、尚未開始"""
    if not status:
        return None
    if "截止" in status:
        return "已截止"
    if "未開放" in status or "尚未" in status:
        return "尚未開始"
    if "額滿" in status or (capacity and registered is not None and registered >= capacity):
        return "已額滿"
    if "報名" in status:
        return "開放報名"
    return status


def build_record(item: dict, d: dict) -> dict:
    """一則活動 → 草案一、二節的欄位（攤平成一層）"""
    # 每一場自己的報名按鈕：「已截止」「未開放報名」直接用；「我要報名」這類算報名中；沒有按鈕才用列表卡片的標籤
    btn = d["button_status"] or ""
    if re.search(r"截止|未開放|額滿", btn):
        status = btn
    elif "報名" in btn:  # 「我要報名」「取消報名」→ 這一場還能報名
        # 注意：按鈕文字跟 cookie 主人有關（自己報過名會顯示「取消報名」）；
        # 額滿與否另外用報名人數／上限判斷（registration_status），不只看按鈕
        status = "報名中"
    else:
        status = item["list_status"] or btn or None
    # 報名方式：場次有 Portal 報名按鈕 → portal（就算內文也有校外表單，學生走 Portal）
    #           沒有按鈕、但有表單連結 → external；兩種都沒有（免報名）→ none
    if d["has_portal_button"]:
        method, reg_url = "portal", None
    elif d["registration_url"]:
        method, reg_url = "external", d["registration_url"]
    else:
        method, reg_url = "none", None
    return {
        "pk": f"portal.activity#{item['activity_id']}#{d['session_id']}",  # 唯一鍵：活動＋場次
        # 一、活動資料
        "activity_id": item["activity_id"],
        "session_id": d["session_id"],  # 場次編號；一個活動有幾場就輸出幾筆
        "session_name": d["session_name"],
        "title": d["title"] or item["title"],
        "organizer": d["organizer"],
        "contact_email": None,  # 不存個人信箱
        "audience": d["audience"],
        "url": item["url"],
        "image_url": dedupe(item["images"] + d["images"]),  # 海報圖網址 list
        "status": status,  # 網頁原文：報名中／已截止／未開放報名
        "updated_at": None,  # 前端抓不到
        "views": item["views"],  # 瀏覽次數（列表頁）
        # 二、活動內容詳細資料
        "speaker": d["speaker"],
        "location": d["location"],
        "location_code": None,  # 網頁上沒有
        "start_at": d["start_at"], "end_at": d["end_at"],
        "registration_start": d["registration_start"], "registration_end": d["registration_end"],
        "registration_method": method,  # portal／external／none
        "registration_url": reg_url,
        "capacity": d["capacity"], "registered_count": d["registered_count"],
        "waitlist_capacity": d["waitlist_capacity"], "waitlist_count": d["waitlist_count"],
        "registration_status": registration_status(status, d["registered_count"], d["capacity"]),
        "hour_tags": d["hour_tags"],
        "learning_passport_hours": d["learning_passport_hours"],
        "learning_passport_condition": d["learning_passport_condition"],
        "soft_skill_hours": d["soft_skill_hours"],
        "sdgs_tag": item["sdgs_tag"],
        "raw_fields": d["raw_fields"],  # 內頁畫面上顯示的原始資料（含活動內容）
        "scraped_at": datetime.now(timezone.utc).isoformat(),  # 這筆資料是什麼時候爬的（UTC）
    }


# ---- 去重：記住抓過的活動編號 -------------------------------------------------
class FileSeenStore:
    """本機版：抓過的 activity_id 存在 seen_ids.json"""

    def __init__(self, path: str = "seen_ids.json"):
        self.path = Path(path)
        self.ids = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def has(self, aid: str) -> bool:
        return aid in self.ids

    def add(self, rec: dict):
        self.ids[rec["activity_id"]] = rec["scraped_at"][:10]
        self.path.write_text(json.dumps(self.ids, ensure_ascii=False, indent=1), encoding="utf-8")


def not_ended(rec: dict, now: datetime) -> bool:
    """活動還沒結束：end_at（沒有就用 start_at）還在現在之後；兩個都沒有就保留。"""
    t = rec["end_at"] or rec["start_at"]
    return t is None or datetime.fromisoformat(t) >= now


def crawl(limit: int | None = None, max_pages: int = 50, store=None, include_ended: bool = False) -> list[dict]:
    """抓 iNCU 活動 → list[dict]。
    預設：翻完全部列表頁，回傳「所有還沒結束的活動」。
    store：測試用，有給 FileSeenStore 就只抓沒抓過的；每天上傳請跑完整 crawl（不給 store）。"""
    s = load_session()
    items = collect_new(s, max_pages, store)
    print(f"[list] 這次要抓 {len(items)} 則活動內頁")
    now = datetime.now(timezone.utc)
    results = []
    for it in items[:limit] if limit else items:
        it["url"] = f"{LIST_URL}/{it['activity_id']}"
        try:
            recs = [build_record(it, d) for d in scrape_detail(s, it["url"])]
            kept = [r for r in recs if include_ended or not_ended(r, now)]
            if store and recs:
                store.add(recs[0])  # 抓成功才記，失敗的明天再試
            results += kept
            title = recs[0]["title"] if recs else it["title"]
            print(f"  [ok] {it['activity_id']} {title}：{len(recs)} 場，還沒結束 {len(kept)} 場")
        except SessionExpired:
            raise
        except Exception as e:  # 單則失敗不中斷
            print(f"  [fail] {it['activity_id']}: {e}")
    return results


def pick_samples(input_path: str, out_dir: str = "samples"):
    """挑 3 種範例：有學習護照時數、外部報名連結、已額滿；各存一個 JSON 檔。"""
    data = json.loads(Path(input_path).read_text(encoding="utf-8"))
    rules = [
        ("learning_passport", lambda r: r["learning_passport_hours"]),
        ("external_registration", lambda r: r["registration_method"] == "external"),
        ("full", lambda r: r["registration_status"] == "已額滿"),
    ]
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    used = set()
    for name, rule in rules:  # 盡量挑不同的活動
        rec = next((r for r in data if rule(r) and r["activity_id"] not in used), None) \
            or next((r for r in data if rule(r)), None)
        if rec is None:
            print(f"  [沒找到] {name}：這次資料裡沒有符合的活動")
            continue
        used.add(rec["activity_id"])
        path = Path(out_dir, f"{name}_{rec['activity_id']}.json")
        path.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  [ok] {name} → {path}（{rec['title']}）")


def probe(url: str):
    resp = fetch(load_session(), url)
    Path("debug").mkdir(exist_ok=True)
    name = re.sub(r"\W+", "_", urlparse(resp.url).path.strip("/"))[:60] or "index"
    Path("debug", f"{name}.html").write_text(resp.text, encoding="utf-8")
    print(f"[probe] {resp.status_code} {resp.url} → 已存到 debug/{name}.html（裡面有你的姓名，分享前看一下）")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="NCU iNCU 活動報名爬蟲")
    sub = p.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("probe", help="存下頁面原始碼")
    pp.add_argument("url", nargs="?", default=f"{LIST_URL}?page=1")
    pc = sub.add_parser("crawl", help="抓活動列表＋內頁")
    pc.add_argument("-n", "--limit", type=int, help="最多抓幾則（測試用）")
    pc.add_argument("--pages", type=int, default=50, help="最多翻幾頁")
    pc.add_argument("--new-only", action="store_true", help="測試用：只抓 seen_ids.json 裡沒有的活動（每天上傳請跑完整 crawl）")
    pc.add_argument("--include-ended", action="store_true", help="已結束的活動也輸出")
    pc.add_argument("--state", default="seen_ids.json")
    pc.add_argument("-o", "--output", help="預設 output/portal_activities_YYYY-MM-DD.json")
    ps = sub.add_parser("samples", help="從輸出檔挑 3 筆範例到 samples/")
    ps.add_argument("input", help="crawl 輸出的 JSON 檔")
    ps.add_argument("--out-dir", default="samples")
    args = p.parse_args()

    try:
        if args.cmd == "probe":
            probe(args.url)
        elif args.cmd == "samples":
            pick_samples(args.input, args.out_dir)
        else:
            store = FileSeenStore(args.state) if args.new_only else None
            data = crawl(args.limit, args.pages, store, args.include_ended)
            if not data and not args.new_only:
                sys.exit("[error] 沒有抓到任何活動，不輸出檔案（請檢查 cookie 或網頁結構）")
            out = Path(args.output or f"output/portal_activities_{datetime.now():%Y-%m-%d}.json")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"完成！{len(data)} 則 → {out}")
    except SessionExpired as e:
        sys.exit(f"[session] {e}")
