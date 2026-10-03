"""
川普 OGE 278-T 交易申報爬蟲
流程：找出申報 PDF → 下載 → 讀出 OCR 文字 → 解析成交易紀錄 → 存成 JSON / CSV
解析不了的行會放進 data/needs_review.json，方便人工檢查。
"""
import csv
import io
import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin

import pdfplumber
import requests

DATA_DIR = Path("data")
HEADERS = {"User-Agent": "trump-trade-tracker (research; contact: your-email@example.com)"}

# 已確認存在的申報 PDF，可以自行增加
PDF_URLS = [
    "https://extapps2.oge.gov/201/Presiden.nsf/PAS+Index/E590116FC9631E9885258E7A002DE209/$FILE/Donald-J-Trump-09.8.2026-278T.pdf",
    "https://extapps2.oge.gov/201/Presiden.nsf/PAS+Index/405E4EC4E27BE8D185258DF7002DD1C0/$FILE/Trump,%20Donald%20J.-05.08.2026-278T(2).pdf",
]

# OGE 列出總統所有申報的索引頁。請先用瀏覽器找到該頁，把網址貼進來；
# 留空就只處理上面的 PDF_URLS。
INDEX_URL = ""

# OGE 固定的金額區間：key 是「下限+上限」去掉符號後的數字
AMOUNT_BUCKETS = {
    "2500000150000000": "$25,000,001 - $50,000,000",
    "500000125000000": "$5,000,001 - $25,000,000",
    "10000015000000": "$1,000,001 - $5,000,000",
    "5000011000000": "$500,001 - $1,000,000",
    "250001500000": "$250,001 - $500,000",
    "100001250000": "$100,001 - $250,000",
    "50001100000": "$50,001 - $100,000",
    "1500150000": "$15,001 - $50,000",
    "100115000": "$1,001 - $15,000",
    "50000000": "Over $50,000,000",
}

# 日期允許少一個斜線（OCR 常見錯誤，例如 3/212026）
DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})(/?)(20\d{2})")
ROW_NO_RE = re.compile(r"^\s*\d{1,4}\s+")


def detect_type(text):
    t = text.lower()
    if re.search(r"ur[ce]has", t):          # purchase / ourchaso / purehase
        return "purchase"
    if re.search(r"\bsa\s?l[eo]\b", t):     # sale / salo / sa le
        return "sale"
    if "xchang" in t:
        return "exchange"
    return None


def detect_amount(text):
    digits = re.sub(r"\D", "", text)
    for key, label in AMOUNT_BUCKETS.items():   # 長的先比對
        if digits.endswith(key):
            return label
    return None


def clean_description(text):
    text = ROW_NO_RE.sub("", text)
    text = re.sub(r"\b(I|l)\s*$", "", text)     # OCR 把表格線讀成 I
    return text.strip(" .-•~|")


def parse_line(line, prev_line=""):
    """解析一行，回傳 (紀錄, 需要人工檢查的原因)。"""
    tx_type = detect_type(line)
    dates = list(DATE_RE.finditer(line))
    if not tx_type and not dates:
        return None, None

    if not tx_type or not dates:
        return None, "缺少交易類型或日期"

    date_m = dates[-1]
    month, day, slash, year = date_m.groups()
    if not (1 <= int(month) <= 12 and 1 <= int(day) <= 31):
        return None, "日期不合理"

    amount = detect_amount(line[date_m.end():])
    if not amount:
        return None, "找不到金額區間"

    type_m = re.search(r"\b\S*(ur[ce]has\S*|sa\s?l[eo]|xchang\S*)", line, re.I)
    desc = clean_description(line[: type_m.start()] if type_m else line[: date_m.start()])
    if len(desc) < 2:
        desc = clean_description(prev_line)    # 名稱在上一行的情況

    return {
        "description": desc,
        "type": tx_type,
        "date": f"{year}-{int(month):02d}-{int(day):02d}",
        "amount": amount,
        "date_ocr_fixed": slash == "",
    }, None


def discover_pdf_links(index_url):
    resp = requests.get(index_url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    links = re.findall(r'href="([^"]+)"', resp.text, re.I)
    found = []
    for link in links:
        low = link.lower()
        if ".pdf" in low and "278" in low:
            found.append(urljoin(index_url, link))
    return sorted(set(found))


def download(url, retries=3):
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=60)
            resp.raise_for_status()
            return resp.content
        except requests.RequestException as err:
            print(f"  下載失敗（第 {attempt + 1} 次）：{err}")
            time.sleep(5)
    return None


def parse_filing(url, pdf_bytes):
    records, review = [], []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            prev = ""
            for line in (page.extract_text() or "").splitlines():
                rec, problem = parse_line(line, prev)
                if rec:
                    rec.update({"source_url": url, "page": page_no})
                    records.append(rec)
                elif problem:
                    review.append({"source_url": url, "page": page_no, "line": line, "reason": problem})
                prev = line
    return records, review


def load_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    DATA_DIR.mkdir(exist_ok=True)
    seen_path = DATA_DIR / "seen_urls.json"
    trades_path = DATA_DIR / "trades.json"
    review_path = DATA_DIR / "needs_review.json"

    seen = set(load_json(seen_path, []))
    trades = load_json(trades_path, [])
    review = load_json(review_path, [])

    urls = list(PDF_URLS)
    if INDEX_URL:
        try:
            urls += discover_pdf_links(INDEX_URL)
        except requests.RequestException as err:
            print(f"索引頁讀取失敗：{err}")

    new_urls = [u for u in dict.fromkeys(urls) if u not in seen]
    print(f"共 {len(urls)} 份申報，其中 {len(new_urls)} 份是新的")

    for url in new_urls:
        print(f"處理：{url}")
        pdf_bytes = download(url)
        if not pdf_bytes:
            continue
        recs, rev = parse_filing(url, pdf_bytes)
        print(f"  解析出 {len(recs)} 筆，{len(rev)} 行待檢查")
        trades += recs
        review += rev
        seen.add(url)
        time.sleep(2)   # 對 OGE 伺服器客氣一點

    save_json(trades_path, trades)
    save_json(review_path, review)
    save_json(seen_path, sorted(seen))

    with open(DATA_DIR / "trades.csv", "w", newline="", encoding="utf-8-sig") as f:
        fields = ["date", "type", "description", "amount", "date_ocr_fixed", "page", "source_url"]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(trades)

    print(f"完成：目前共 {len(trades)} 筆交易")


if __name__ == "__main__":
    main()
