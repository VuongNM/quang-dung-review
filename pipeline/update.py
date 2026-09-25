"""Weekly incremental update: find new videos on the channel, pull restaurant
names out of their descriptions, geocode them via Google Maps, merge into
../data.js. Run from the repo root:  python pipeline/update.py

State lives in pipeline/seen.json (video ids already processed). Names that
could not be matched are appended to pipeline/unresolved.md for a human look.
"""
import json, math, os, random, re, sys, time, unicodedata, urllib.parse, urllib.request
from datetime import date

import yt_dlp

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data.js")
SEEN = os.path.join(HERE, "seen.json")
UNRESOLVED = os.path.join(HERE, "unresolved.md")
CHANNEL = "https://www.youtube.com/channel/UCLzOOrwtGtiM_C5g1SCx7hQ/videos"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"

# ---------- text helpers ----------
def fold(s):
    s = unicodedata.normalize("NFD", (s or "").lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn").replace("đ", "d")

CITIES = [  # (regex on video title, label, lat, lng, query suffix)
    (r"Hà Nội|Hanoi|Vinh to Hanoi", "Hà Nội", 21.03, 105.85, "Hà Nội"),
    (r"Đà Nẵng|Da Nang", "Đà Nẵng", 16.06, 108.22, "Đà Nẵng"),
    (r"Hội An|Hoi An", "Hội An", 15.88, 108.33, "Hội An"),
    (r"Vũng Tàu|Vung Tau", "Vũng Tàu", 10.35, 107.08, "Vũng Tàu"),
    (r"Nha Trang", "Nha Trang", 12.24, 109.19, "Nha Trang"),
    (r"Dalat|Đà Lạt", "Đà Lạt", 11.94, 108.44, "Đà Lạt"),
    (r"Di Linh", "Di Linh", 11.58, 108.07, "Di Linh Lâm Đồng"),
    (r"My Tho|Mỹ Tho", "Mỹ Tho", 10.36, 106.36, "Mỹ Tho"),
    (r"Foodtour Huế|Ở Huế|\bHue\b(?! Ky)", "Huế", 16.46, 107.59, "Huế"),
    (r"Hạ Long|Ha Long", "Hạ Long", 20.95, 107.07, "Hạ Long"),
    (r"Sa Pa|Sapa", "Sa Pa", 22.34, 103.84, "Sa Pa"),
    (r"Quy Nhơn|Quy Nhon", "Quy Nhơn", 13.78, 109.22, "Quy Nhơn"),
    (r"Phú Quốc|Phu Quoc", "Phú Quốc", 10.22, 103.96, "Phú Quốc"),
    (r"Phan Thiet|Phan Thiết", "Phan Thiết", 10.93, 108.10, "Phan Thiết"),
    (r"Bình Dương|Binh Duong", "Bình Dương", 10.98, 106.65, "Bình Dương"),
    (r"Cần Thơ|Can Tho", "Cần Thơ", 10.03, 105.78, "Cần Thơ"),
]
HCM = ("TP.HCM", 10.78, 106.70, "Hồ Chí Minh")

def city_of(title):
    for rx, c, la, ln, q in CITIES:
        if re.search(rx, title, re.I):
            return (c, la, ln, q)
    return HCM

SKIP_CHAPTER = re.compile(r"^(intro|outro|chào|kết|tổng hợp|thông tin|mở đầu|giới thiệu|ending|end\b|hello|opening|summary|general information|address information|collection of information)|thông tin (tổng hợp|các quán)|^(tổ |đi thăm|hậu trường|thư ký|di chuyển|chuyến bay|road to)", re.I)

def clean(n):
    n = re.sub(r"\((?![^)]*(quận|q\.|cn|chi nhánh|cơ sở|đường|thảo điền|phú mỹ|\d))[^)]*\)", "", n, flags=re.I)
    n = re.sub(r"[()]", " ", n)
    n = re.sub(r"\b(phần|part)\s*\d+", "", n, flags=re.I)
    return re.sub(r"\s+", " ", n).strip(" -–:")

def ts(s):
    t = 0
    for x in s.split(":"):
        t = t * 60 + int(x)
    return t

def chapters(desc):
    out = []
    for line in (desc or "").splitlines():
        m = re.match(r"^\s*(\d{1,2}(?::\d{2}){1,2})\s*[-–:]?\s*(.+)$", line)
        if m and not SKIP_CHAPTER.search(m.group(2).strip()):
            out.append((ts(m.group(1)), clean(m.group(2).strip())))
    return out

NAME_LEAD = re.compile(r"(?:quán|nhà hàng|tiệm|restaurant)\s+([^.,;:!?\n()\"“”]{2,60})", re.I)
NAME_STOP = set("là ở này nằm với rất cũng có mà thì và tại trên của đã được cho nên khá đang ngay gần sẽ is in at on with and the".split())

def single_candidates(title, desc):
    """Guess the restaurant name for a video without chapters: '<quán|nhà hàng> Proper Name'
    phrases from the description first, the video title last."""
    text = re.split(r"Video của Dũng|Dung's videos|Liên hệ|Contact", desc or "")[0]
    names = []
    for m in NAME_LEAD.finditer(text):
        taken = []
        for w in m.group(1).split():
            if w.lower() in NAME_STOP or len(taken) >= 6: break
            taken.append(w)
        caps = [w for w in taken if w[0].isupper() or w[0].isdigit()]
        if len(taken) >= 2 and caps and fold(" ".join(caps)) not in GENERIC:
            names.append(" ".join(taken))
    names.append(re.sub(r"\s*\|.*$", "", title).strip())
    seen, out = set(), []
    for n in names:
        if fold(n) not in seen:
            seen.add(fold(n)); out.append(n)
    return out[:4]

# ---------- Google Maps lookup ----------
PB = "!4m12!1m3!1d30000!2d{ln}!3d{la}!2m3!1f0!2f0!3f0!3m2!1i1024!2i768!4f13.1!7i20!10b1!12m6!2m3!5m1!6e2!20e3!10b1!16b1!19m4!2m3!1i360!2i120!4i8"

def gm_fetch(q, la, ln):
    url = ("https://www.google.com/search?tbm=map&hl=vi&gl=vn&q=" + urllib.parse.quote(q)
           + "&pb=" + PB.format(la=la, ln=ln))
    for attempt in range(4):
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "vi"}), timeout=20).read().decode()
            return json.loads(r[r.index("\n") + 1:])
        except Exception:
            time.sleep(2 + attempt * 3)
    return None

def gm_places(d):
    out = []
    def walk(x, depth=0):
        if depth > 8 or not isinstance(x, list):
            return
        if len(x) > 39 and isinstance(x[11], str) and isinstance(x[9], list) and len(x[9]) > 3 and isinstance(x[9][2], float):
            out.append(x); return
        for y in x:
            walk(y, depth + 1)
    walk(d)
    return out

def gm_info(i):
    try: rating = i[4][7]
    except Exception: rating = None
    blob = json.dumps(i, ensure_ascii=False).lower()
    return dict(gname=i[11], lat=i[9][2], lng=i[9][3], address=i[39] if isinstance(i[39], str) else i[18],
                gcats=i[13] or [], rating=rating, pid=i[10],
                closed="đóng cửa vĩnh viễn" in blob or "permanently closed" in blob)

STOP = set("quan nha hang tiem cn chi nhanh co so sai gon saigon tphcm hcm ho chi minh ha noi da nang the restaurant cafe coffee va and cua o tai moi phan so duong d q of in soup shop dung quang nguyen thich nhat ngon noi tieng dac biet lau doi hot review dao dien".split())
EN = [(r"broken rice", "com tam"), (r"steamed rice rolls?|rice rolls?", "banh cuon"), (r"claypot rice|pot rice", "com nieu"),
      (r"noodle soup|noodles?", "mi"), (r"chicken", "ga"), (r"beef", "bo"), (r"snail", "oc"), (r"porridge", "chao"),
      (r"fried dough", "bot chien"), (r"fish cake", "cha ca"), (r"\brice\b", "com"), (r"goat", "de"), (r"duck", "vit"), (r"offal", "long")]
GENERIC = set("rieu xeo khot quang mi i bun bo pho com tam hu tieu banh cuon uot xoi ga chao vit oc cua lau de mien luon canh nuong cha ca long bot chien hoanh thanh nieu gia truyen".split())

def toks(s):
    s = fold(s).replace("y", "i")
    for a, b in EN:
        s = re.sub(a, b, s)
    return [t for t in re.findall(r"[a-z0-9]+", s) if t not in STOP]

def good_match(query, p, strict=False):
    """Name-overlap score >= 0.6, dish words must match, and at least one
    distinctive (non-dish) word must be in the Google place name."""
    q = toks(query)
    if not q:
        return 0
    name = set(toks(p["gname"])); hay = name | set(toks(p["address"] or ""))
    if not any(t in name for t in q):
        return 0
    score = sum(t in hay for t in q) / len(q)
    dist = [t for t in q if t not in GENERIC]; gen = [t for t in q if t in GENERIC]
    if dist and not any(t in name for t in dist):
        return 0
    if strict and (not dist or sum(t in name for t in dist) / len(dist) < 0.6):
        return 0
    if gen and sum(t in name for t in gen) / len(gen) < 0.5:
        return 0
    return score if score >= 0.6 else 0

NONFOOD_CAT = re.compile(r"Công ty|Văn phòng|Chung cư|Căn hộ|Nơi lưu trú|Sân |Trạm y tế|Trung tâm|Điểm thu hút|Người cung cấp", re.I)

def km(a, b, c, d):
    return math.hypot((a - c) * 111, (b - d) * 111 * math.cos(math.radians(a)))

def lookup(name, city, strict=False):
    c, la, ln, suf = city
    best = None
    for q in [f"{name} {suf}".strip(), name]:
        d = gm_fetch(q, la, ln)
        time.sleep(0.5 + random.random() * 0.5)
        if not d:
            continue
        for i in gm_places(d)[:6]:
            try: p = gm_info(i)
            except Exception: continue
            if km(p["lat"], p["lng"], la, ln) > 80 or not p["gcats"] or NONFOOD_CAT.search(p["gcats"][0]):
                continue
            s = good_match(name, p, strict)
            if s and (not best or s > best[0]):
                best = (s, p)
        if best:
            break
    return best[1] if best else None

# ---------- categories ----------
RULES = [("chay", r"\bchay\b|vegetarian|vegan|\bhum\b"),
         ("cafe", r"cafe|coffee|ca phe|\btra\b|tea|banh ngot|bakery|chocolate|marou|kem\b|gelato"),
         ("pho", r"\bpho\b"), ("banh-mi", r"banh mi|bo ne"),
         ("hai-san", r"hai san|\boc\b|\bcua\b|seafood|ghe|tom hum|lobster|sushi"),
         ("bun-mi-hu-tieu", r"\bbun\b|\bmi\b|hu tieu|mien|banh canh|chao|ramen|udon|noodle|cao lau|my quang|banh da"),
         ("lau-nuong", r"\blau\b|nuong|grill|bbq|hotpot|hot pot|\bde\b|yakiniku|steak|beef|bo tung xeo"),
         ("com", r"\bcom\b|xoi|com tam|rice"),
         ("an-vat", r"banh xeo|banh cuon|banh uot|hot vit|bot chien|banh khot|che\b|banh beo|xiu mai|dimsum|dim sum|goi cuon|banh trang|banh bao"),
         ("nhau", r"bia|beer|lai rai|nhau|sake|izakaya|\bpub\b|\bbar\b"),
         ("chau-a", r"nhat|japan|omakase|han quoc|korea|thai|trung hoa|hong kong|chinese|singapore|teppan"),
         ("a-au", r"french|phap|ital|pizza|pasta|burger|bistro|au\b|europe|hotel|resort")]
DISH = {"pho": "Phở", "banh-mi": "Bánh mì", "bun-mi-hu-tieu": "Bún · Mì · Hủ tiếu", "com": "Cơm · Xôi", "lau-nuong": "Lẩu · Nướng",
        "hai-san": "Hải sản", "an-vat": "Ăn vặt", "nhau": "Lai rai", "cafe": "Cà phê", "chay": "Chay", "a-au": "Món Âu",
        "chau-a": "Món Á", "khac": "Quán ăn"}

def classify(query, p):
    t = fold(query + " " + p["gname"] + " " + " ".join(p["gcats"]))
    for c, rx in RULES:
        if re.search(rx, t):
            return c
    g = p["gcats"][0] if p["gcats"] else ""
    if re.search(r"Trung Quốc|Quảng Đông|lai Á|Nhật|Hàn|Thái", g): return "chau-a"
    if re.search(r"\bý\b|Pháp|Âu|Mỹ", g, re.I): return "a-au"
    return "khac"

# ---------- YouTube ----------
YDL = dict(quiet=True, no_warnings=True, skip_download=True, ignore_no_formats_error=True,
           extractor_args={"youtube": {"player_skip": ["webpage", "configs"], "player_client": ["android_vr"]}})

def latest_video_ids(n=30):
    with yt_dlp.YoutubeDL(dict(YDL, extract_flat=True, playlistend=n)) as y:
        info = y.extract_info(CHANNEL, download=False)
    return [e["id"] for e in info.get("entries") or [] if e.get("id")]

def video_meta(vid):
    for attempt in range(3):
        try:
            with yt_dlp.YoutubeDL(YDL) as y:
                d = y.extract_info("https://youtu.be/" + vid, download=False, process=False)
            return dict(id=vid, title=d.get("title") or "", description=d.get("description") or "", date=d.get("upload_date") or "")
        except Exception as e:
            err = e; time.sleep(3)
    raise err

# ---------- main ----------
def load_places():
    s = open(DATA, encoding="utf-8").read()
    return json.loads(s[s.index("=") + 1:].rstrip().rstrip(";"))

def save_places(places):
    open(DATA, "w", encoding="utf-8").write("window.PLACES=" + json.dumps(places, ensure_ascii=False, separators=(",", ":")) + ";\n")

def main():
    seen = set(json.load(open(SEEN)))
    ids = latest_video_ids()
    print(f"Channel listing OK: {len(ids)} latest videos")
    test = int(os.environ.get("TEST_WITHHOLD", "0"))  # dry run: re-process the N newest, write nothing
    if test:
        seen -= set(ids[:test])
    new = [v for v in ids if v not in seen]
    if not new:
        print("No new videos."); return
    places = load_places()
    by_cid = {p.get("cid"): p for p in places if p.get("cid")}
    added, linked, unresolved = [], [], []
    for vid in reversed(new):  # oldest first
        meta = video_meta(vid)
        city = city_of(meta["title"])
        ch = chapters(meta["description"])
        jobs = [(t, [n]) for t, n in ch] if ch else [(0, single_candidates(meta["title"], meta["description"]))]
        print(f"{vid} {meta['title'][:70]!r}: {len(jobs)} place(s)")
        for t, names in jobs:
            hit = query = None
            for query in names:
                hit = lookup(query, city, strict=not ch)
                if hit: break
            if not hit:
                unresolved.append((meta, t, names)); continue
            cid = str(int(hit["pid"].split(":")[1], 16)) if ":" in (hit["pid"] or "") else None
            ref = {"v": vid, "t": t, "title": meta["title"], "date": meta["date"]}
            if cid and cid in by_cid:
                p = by_cid[cid]
                if not any(v["v"] == vid and v["t"] == t for v in p["videos"]):
                    p["videos"].insert(0, ref); linked.append(p["name"])
                continue
            cat = classify(query, hit)
            p = {"name": hit["gname"], "dish": DISH[cat], "category": cat,
                 "address": re.sub(r",?\s*Việt Nam$", "", hit["address"] or ""), "city": city[0],
                 "lat": round(hit["lat"], 6), "lng": round(hit["lng"], 6), "precision": "exact",
                 "rating": hit["rating"], "closed": hit["closed"], "gcat": hit["gcats"][0] if hit["gcats"] else None,
                 "cid": cid, "q": query, "videos": [ref]}
            places.append(p); by_cid[cid] = p; added.append(p["name"])
        seen.add(vid)
    print(f"{len(new)} new video(s): +{len(added)} places, +{len(linked)} links to existing, {len(unresolved)} unresolved")
    for n in added: print("  +", n)
    for meta, t, names in unresolved: print("  ?", " / ".join(names))
    if test:
        print("TEST_WITHHOLD set: nothing written."); return
    save_places(places)
    json.dump(sorted(seen), open(SEEN, "w"), indent=0)
    if unresolved:
        with open(UNRESOLVED, "a", encoding="utf-8") as f:
            for meta, t, names in unresolved:
                f.write(f"- {date.today()} [{meta['title']}](https://youtu.be/{meta['id']}?t={t}) — tried: {' / '.join(names)}\n")

if __name__ == "__main__":
    sys.exit(main())
