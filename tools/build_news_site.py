# -*- coding: utf-8 -*-
"""統合ニュースサイト・ビルダー v5（当日のみ / 一般・生活・仕事の3カテゴリ）

同一フォルダの general/life/work-news-YYYY-MM-DD.json を走査し、**最新1日分のみ**を
カテゴリタブ（一般Top10 / 生活Top10 / 仕事Top20）+ 本日のTOP3 + カードで
自己完結HTML(news.html)に生成する。外部依存なし・UTF-8/LF。

前日分のJSONは「表示」はしないが、重複検出（続報の自動判定）のために読み込む。

各JSONスキーマ:
{"date":"YYYY-MM-DD","sub":"曜","cat":"work|general|life",
 "articles":[{
   "rank":1,                     # 掲載順位（1始まり・任意。無い場合は配列順）
   "field":"AI",                 # 仕事カテゴリのみ: AI|auto|econ|ad|india
   "sec":"小見出し",
   "badges":["us","jp"],         # new/prev はビルダー側で自動付与するため不要
   "title":"…","summary":"…","insight":"…(任意)","meta":["要点1",…],
   "source":"名","sourceUrl":"URL",
   "followup":{"since":"YYYY-MM-DD","whatsNew":"前日から何が新しいか"}  # 続報のみ
 }],
 "weekly":["本日のTOP候補文",…]}

旧形式（econ/ai/auto-news-*.json）は仕事カテゴリへ自動マッピングして後方互換。
"""
import os, re, glob, html, json, datetime
from collections import defaultdict

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "news.html")

# ---- カテゴリ定義 --------------------------------------------------------
# (ファイル接頭辞, 内部キー, 表示名, 掲載上限)
CATS = [("general-news", "general", "一般", 10),
        ("life-news",    "life",    "生活", 10),
        ("work-news",    "work",    "仕事", 20)]
ORDER = [c[1] for c in CATS]
CATLABEL = {c[1]: c[2] for c in CATS}
CAP = {c[1]: c[3] for c in CATS}

# 仕事カテゴリの分野（サブフィルタ）
FIELDS = [("AI", "AI"), ("auto", "自動車"), ("econ", "経済"),
          ("ad", "自動運転"), ("india", "インド")]
FIELDLABEL = dict(FIELDS)

# 旧形式ファイル → 仕事カテゴリの分野へのマッピング（後方互換）
LEGACY = {"econ-news": "econ", "ai-news": "AI", "auto-news": "auto"}

WD = ["月", "火", "水", "木", "金", "土", "日"]


def ri(text):
    t = html.escape(text or "")
    t = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
               lambda m: '<a href="%s" target="_blank" rel="noopener">%s</a>' % (m.group(2), m.group(1)), t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
    return t


def esc(s):
    return html.escape(s or "")


def badge_html(k):
    if k == "new":
        return '<span class="badge new">新</span>'
    if k == "fu":
        return '<span class="badge fu">続報</span>'
    if k == "us":
        return '<span class="badge geo">US</span>'
    if k == "jp":
        return '<span class="badge geo">JP</span>'
    return ""


def card_html(a, ck):
    badges = "".join(badge_html(b) for b in a.get("badges", []))
    bits = []
    if ck == "work" and a.get("field") in FIELDLABEL:
        bits.append('<span class="fchip">%s</span>' % esc(FIELDLABEL[a["field"]]))
    if a.get("sec"):
        bits.append('<span class="ctx">%s</span>' % esc(a["sec"]))
    meta = '<div class="cmeta">%s%s</div>' % (badges, "".join(bits)) if (badges or bits) else ""
    title = '<h3>%s</h3>' % ri(a.get("title", "")) if a.get("title") else ""

    fu = a.get("followup") or {}
    fubox = ""
    if fu:
        since = fu.get("since", "")
        sincetxt = ""
        if re.match(r"^\d{4}-\d{2}-\d{2}$", since or ""):
            _, mo, da = since.split("-")
            sincetxt = '<span class="fusince">初報 %d/%d</span>' % (int(mo), int(da))
        whats = ri(fu.get("whatsNew", "")) if fu.get("whatsNew") else ""
        fubox = '<div class="fubox"><span class="fulab">今日の新情報</span>%s%s</div>' % (
            ("<span>%s</span>" % whats) if whats else "", sincetxt)

    summ = '<p>%s</p>' % ri(a.get("summary", "")) if a.get("summary") else ""
    chips = ""
    if a.get("meta"):
        chips = '<div class="chips">%s</div>' % "".join(
            '<span class="mchip">%s</span>' % esc(m) for m in a["meta"])
    ins = '<div class="insight">💡 %s</div>' % ri(a["insight"]) if a.get("insight") else ""
    src = ""
    if a.get("source") and a.get("sourceUrl"):
        src = '<div class="src"><a href="%s" target="_blank" rel="noopener">出典: %s ↗</a></div>' % (
            esc(a["sourceUrl"]), esc(a["source"]))
    elif a.get("source"):
        src = '<div class="src"><span class="srcx">出典: %s</span></div>' % esc(a["source"])

    fattr = ' data-field="%s"' % esc(a.get("field", "")) if ck == "work" else ""
    rank = '<span class="rank">%d</span>' % a["_rank"]
    return '<article class="card"%s>%s%s%s%s%s%s%s%s</article>' % (
        fattr, rank, meta, title, fubox, summ, chips, ins, src)


def weekly_box(items, dlabel):
    lis = "".join("<li>%s</li>" % ri(w) for w in items)
    return '<section class="weekly"><h2>📌 本日のTOP3(%s時点)</h2><ol>%s</ol></section>' % (esc(dlabel), lis)


# ---- 続報の自動判定（前日分との照合）------------------------------------
# 収集タスク側で followup を付けるのが原則だが、付け漏れの保険としてビルダーでも
# 前日の見出しと照合し、同一ストーリーと判定したものに続報フラグを立てる。
SIM_TH = 0.40
_STOP = set("トヨタ ホンダ 日産 スバル マツダ ダイハツ スズキ 三菱 レクサス フォード テスラ "
            "クライスラー ジープ シボレー キャデラック リコール マイナーチェンジ モデル ニュース "
            "発売 発表 全国 予定 本日 特別 記念 一部 改良 実施 拡大 開始 運行 規模 規制 対応 追加 "
            "ロボタクシ".split())


def _plain(t):
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t or "")
    return re.sub(r"\*\*([^*]+)\*\*", r"\1", t)


def _tokens(t):
    t = _plain(t)
    out = set()
    for x in re.findall(r"[ァ-ヴー]{3,}", t):
        x = x.strip("ー")
        if x and x not in _STOP:
            out.add("k:" + x)
    up = {s.upper() for s in _STOP}
    for x in re.findall(r"[A-Za-z][A-Za-z0-9\-]{2,}", t):
        x = re.sub(r"-", "", x).upper()
        if len(x) >= 3 and x not in up:
            out.add("l:" + x)
    return out


def _bg(t):
    t = re.sub(r"[\s、。・,\.\-—“~()（）「」]+", "", _plain(t))
    return set(t[i:i + 2] for i in range(len(t) - 1)) or ({t} if t else set())


def _jac(a, b):
    u = a | b
    return len(a & b) / len(u) if u else 0.0


def mark_followups(today, prev, prev_date):
    """today の記事のうち prev（前日）と同一ストーリーのものに followup を補完する。"""
    ptok = defaultdict(list)   # 同一カテゴリ内でのみ照合する
    for ck, j in (prev or {}).items():
        for a in j.get("articles", []):
            t = a.get("title", "")
            ptok[ck].append((_tokens(t), _bg(t)))
    for ck, j in today.items():
        for a in j.get("articles", []):
            if a.get("followup"):
                continue
            ti = a.get("title", "")
            tk, bg = _tokens(ti), _bg(ti)
            for stk, sbg in ptok.get(ck, []):
                if not (tk & stk):
                    continue
                if _jac(bg, sbg) >= SIM_TH:
                    a["followup"] = {"since": prev_date or "",
                                     "whatsNew": "前日にも関連報道あり（自動判定）"}
                    break


def collect():
    """{date: {catkey: json}} を返す。"""
    data = {}
    for prefix, ck, _l, _cap in CATS:
        for p in glob.glob(os.path.join(BASE, prefix + "-*.json")):
            m = re.search(r"(\d{4}-\d{2}-\d{2})\.json$", os.path.basename(p))
            if not m:
                continue
            try:
                j = json.load(open(p, encoding="utf-8"))
            except Exception:
                continue
            data.setdefault(m.group(1), {})[ck] = j
    # 旧形式（econ/ai/auto）を仕事カテゴリへ合流（新形式が無い日のみ）
    legacy = defaultdict(list)
    for prefix, field in LEGACY.items():
        for p in glob.glob(os.path.join(BASE, prefix + "-*.json")):
            m = re.search(r"(\d{4}-\d{2}-\d{2})\.json$", os.path.basename(p))
            if not m:
                continue
            try:
                j = json.load(open(p, encoding="utf-8"))
            except Exception:
                continue
            legacy[m.group(1)].append((field, j))
    for date, items in legacy.items():
        if "work" in data.get(date, {}):
            continue
        arts, weekly = [], []
        for field, j in items:
            for a in j.get("articles", []):
                a = dict(a)
                a["field"] = field
                a["badges"] = [b for b in a.get("badges", []) if b in ("us", "jp")]
                arts.append(a)
            weekly.extend(j.get("weekly", []))
        if arts:
            sub = items[0][1].get("sub", "")
            data.setdefault(date, {})["work"] = {
                "date": date, "sub": sub, "cat": "work",
                "articles": arts, "weekly": weekly[:3]}
    return data


def prepare(day):
    """順位ソート・上限カット・new/fuバッジ付与。"""
    for ck in list(day.keys()):
        j = day[ck]
        arts = [a for a in j.get("articles", []) if a.get("title")]
        arts.sort(key=lambda a: a.get("rank", 10 ** 6))
        arts = arts[:CAP.get(ck, 20)]
        for i, a in enumerate(arts, 1):
            a["_rank"] = i
            base = [b for b in a.get("badges", []) if b in ("us", "jp")]
            head = ["fu"] if a.get("followup") else ["new"]
            a["badges"] = head + base
        j["articles"] = arts


def build():
    data = collect()
    dates = sorted([d for d in data if any(data[d][ck].get("articles") for ck in data[d])],
                   reverse=True)
    now = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
    gen = now.strftime("%Y-%m-%d %H:%M JST")

    if not dates:
        open(OUT, "w", encoding="utf-8", newline="\n").write(
            TEMPLATE.replace("__CATTABS__", "").replace("__FIELDTABS__", "")
            .replace("__PANELS__", '<div class="empty">表示できるニュースがありません</div>')
            .replace("__DATE__", "").replace("__DEFCAT__", "general").replace("__GEN__", gen))
        print("生成: %s (対象日: なし)" % OUT)
        return

    date = dates[0]
    prev_date = dates[1] if len(dates) > 1 else None
    day = data[date]
    mark_followups(day, data.get(prev_date) if prev_date else None, prev_date)
    prepare(day)

    y, mo, da = map(int, date.split("-"))
    dlabel = "%d/%d" % (mo, da)
    wd = WD[datetime.date(y, mo, da).weekday()]
    datelabel = "%d月%d日(%s)" % (mo, da, wd)

    present = [ck for ck in ORDER if ck in day and day[ck].get("articles")]
    counts = {ck: len(day[ck]["articles"]) if ck in day else 0 for ck in ORDER}

    defcat = present[0] if present else ORDER[0]

    cat_tabs = ""
    for i, ck in enumerate(ORDER):
        a = " active" if ck == defcat else ""
        cat_tabs += '<button class="cattab%s" data-cat="%s">%s<span class="cnt">%d</span></button>' % (
            a, ck, esc(CATLABEL[ck]), counts[ck])

    fcounts = defaultdict(int)
    for a in day.get("work", {}).get("articles", []):
        fcounts[a.get("field", "")] += 1
    field_tabs = '<button class="ftab active" data-field="all">すべて<span class="cnt">%d</span></button>' % counts["work"]
    for fk, flabel in FIELDS:
        field_tabs += '<button class="ftab" data-field="%s">%s<span class="cnt">%d</span></button>' % (
            fk, esc(flabel), fcounts.get(fk, 0))

    panels = ""
    for i, ck in enumerate(ORDER):
        show = " active" if ck == defcat else ""
        body = ""
        if ck in present:
            weekly = (day[ck].get("weekly") or [])[:3]
            if weekly:
                body += weekly_box(weekly, dlabel)
            body += '<section class="sec">%s</section>' % "".join(
                card_html(a, ck) for a in day[ck]["articles"])
        else:
            body = '<div class="empty">このカテゴリの配信はありません</div>'
        panels += '<div class="panel%s" data-cat="%s">%s</div>' % (show, ck, body)

    open(OUT, "w", encoding="utf-8", newline="\n").write(
        TEMPLATE.replace("__CATTABS__", cat_tabs)
        .replace("__FIELDTABS__", field_tabs)
        .replace("__PANELS__", panels)
        .replace("__DATE__", esc(datelabel))
        .replace("__DEFCAT__", defcat)
        .replace("__GEN__", gen))
    print("生成: %s (対象日: %s / 一般%d 生活%d 仕事%d)" % (
        OUT, date, counts["general"], counts["life"], counts["work"]))


TEMPLATE = r"""<!DOCTYPE html>
<html lang="ja"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>統合ニュース</title>
<link rel="manifest" href="manifest.webmanifest">
<meta name="theme-color" content="#f6f5f1">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="News">
<link rel="apple-touch-icon" href="apple-touch-icon.png">
<link rel="icon" type="image/png" sizes="192x192" href="icon-192.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@400;500;700;900&display=swap" rel="stylesheet">
<style>
:root{--bg:#f6f5f1;--ink:#201e1b;--muted:#8a857c;--muted2:#a39d92;--line:#e6e3dc;--line2:#dcd8d0;--body:#514d46;--accent:#3556b8;--fu:#8a6d1f;--card:#fff;--chip:#f1efe9;--dark:#232120;}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent;}
html{scroll-behavior:smooth;}
body{margin:0;background:var(--bg);color:var(--ink);line-height:1.7;
 font-family:'Noto Sans JP',system-ui,-apple-system,sans-serif;-webkit-font-smoothing:antialiased;}
a{color:var(--accent);text-decoration:none;}a:hover{text-decoration:underline;}
.wrap{max-width:720px;margin:0 auto;padding:0 20px 90px;}
header.top{display:flex;align-items:baseline;justify-content:space-between;gap:12px;padding:26px 0 14px;}
header.top h1{margin:0;font-size:20px;font-weight:900;letter-spacing:.02em;}
header.top .sub{font-size:12.5px;color:var(--muted);font-weight:500;margin-left:10px;}
header.top .gen{font-size:11.5px;color:var(--muted);white-space:nowrap;}
nav.tabs{position:sticky;top:0;z-index:10;background:rgba(246,245,241,.94);
 backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);padding:8px 0 12px;border-bottom:1px solid var(--line);}
.trow,.crow{display:flex;gap:8px;overflow-x:auto;scrollbar-width:none;}
.trow::-webkit-scrollbar,.crow::-webkit-scrollbar{display:none;}
.crow{gap:6px;margin-top:10px;}
.crow.hide{display:none;}
.cattab{flex:1 1 0;display:inline-flex;align-items:center;justify-content:center;gap:6px;border:1px solid var(--line2);
 background:var(--card);color:var(--body);border-radius:10px;padding:9px 10px;font:inherit;font-size:14.5px;font-weight:700;cursor:pointer;white-space:nowrap;}
.cattab .cnt{font-size:10.5px;font-weight:700;color:var(--muted);background:var(--line);border-radius:99px;padding:1px 6px;}
.cattab.active{background:var(--accent);border-color:var(--accent);color:#fff;}
.cattab.active .cnt{background:rgba(255,255,255,.25);color:#fff;}
.ftab{flex:0 0 auto;display:inline-flex;align-items:center;gap:4px;border:1px solid transparent;
 background:transparent;color:var(--muted);border-radius:99px;padding:5px 9px;font:inherit;font-size:12px;font-weight:500;cursor:pointer;white-space:nowrap;}
.ftab .cnt{font-size:10px;font-weight:700;color:var(--muted);background:var(--line);border-radius:99px;padding:1px 5px;}
.ftab.active{border-color:var(--accent);background:#fff;color:var(--accent);font-weight:700;}
.ftab.active .cnt{background:var(--accent);color:#fff;}
.panel{display:none;}.panel.active{display:block;animation:fade .18s ease;}
@keyframes fade{from{opacity:0;transform:translateY(4px);}to{opacity:1;transform:none;}}
.weekly{background:var(--dark);border-radius:14px;padding:20px 24px;margin-top:24px;}
.weekly h2{margin:0 0 12px;font-size:12px;font-weight:700;letter-spacing:.12em;color:rgba(255,255,255,.7);}
.weekly ol{margin:0;padding:0 0 0 20px;display:flex;flex-direction:column;gap:9px;}
.weekly li{font-size:13px;line-height:1.65;color:rgba(255,255,255,.92);}
.weekly li strong{color:#fff;}.weekly a{color:#c7d2ff;}
.sec{margin-top:24px;}
.card{position:relative;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 20px 16px 20px;margin:0 0 10px;}
.card.hide{display:none;}
.rank{position:absolute;top:14px;right:16px;font-size:11px;font-weight:700;color:var(--muted2);}
.cmeta{display:flex;flex-wrap:wrap;align-items:center;gap:6px;padding-right:28px;}
.badge{font-size:10.5px;border-radius:5px;padding:2px 7px;white-space:nowrap;}
.badge.new{font-weight:700;color:#fff;background:var(--accent);}
.badge.fu{font-weight:700;color:#fff;background:var(--fu);}
.badge.geo{font-weight:700;color:var(--body);background:var(--chip);}
.fchip{font-size:10.5px;font-weight:700;color:var(--body);background:var(--chip);border:1px solid var(--line);border-radius:5px;padding:1px 7px;}
.ctx{font-size:11px;color:var(--muted2);}
.card h3{margin:8px 0 0;font-size:15.5px;line-height:1.5;font-weight:700;color:var(--ink);}
.fubox{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-top:9px;font-size:12px;line-height:1.6;
 color:#6b5414;background:#fdf6e3;border:1px solid #f0e3c0;border-radius:8px;padding:7px 11px;}
.fubox .fulab{font-size:10.5px;font-weight:700;color:#fff;background:var(--fu);border-radius:5px;padding:1px 7px;}
.fubox .fusince{font-size:11px;color:#9a8654;}
.card p{margin:6px 0 0;font-size:13.5px;line-height:1.7;color:var(--body);}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px;}
.mchip{font-size:11px;color:var(--body);background:#f4f2ec;border:1px solid var(--line);border-radius:6px;padding:2px 8px;}
.insight{margin-top:10px;font-size:12.5px;line-height:1.65;color:#2f3e7a;background:#eef1fb;border-radius:8px;padding:8px 11px;}
.src{margin-top:12px;}.src a{font-size:12px;white-space:nowrap;}.srcx{font-size:12px;color:var(--muted2);}
.empty{text-align:center;color:var(--muted);font-size:13.5px;padding:64px 0;}
footer{margin-top:40px;font-size:11.5px;color:var(--muted2);line-height:1.7;}
#toTop{position:fixed;right:16px;bottom:18px;z-index:30;width:42px;height:42px;border:1px solid var(--line);
 border-radius:50%;background:var(--card);color:var(--ink);font-size:18px;box-shadow:0 3px 10px rgba(80,70,60,.18);
 opacity:0;pointer-events:none;transition:opacity .2s;cursor:pointer;}
#toTop.show{opacity:.96;pointer-events:auto;}
</style></head>
<body>
<div class="wrap">
 <header class="top"><h1>統合ニュース<span class="sub">__DATE__</span></h1><span class="gen">生成 __GEN__</span></header>
 <nav class="tabs"><div class="trow">__CATTABS__</div><div class="crow hide" id="frow">__FIELDTABS__</div></nav>
 __PANELS__
 <footer>※ 当日分のみ掲載。前日と同じ話題は「続報」として、新たに判明した点を明記。事実(数値・日付・金額)は各出典に基づく。予測・噂・未確認情報は本文中に「(要確認)」と明記。個人用。</footer>
</div>
<button id="toTop" aria-label="先頭へ">↑</button>
<script>
(function(){
 var cat='__DEFCAT__',field='all';
 function q(s){return Array.prototype.slice.call(document.querySelectorAll(s));}
 function render(){
  q('.cattab').forEach(function(b){b.classList.toggle('active',b.dataset.cat===cat);});
  q('.panel').forEach(function(p){p.classList.toggle('active',p.dataset.cat===cat);});
  var fr=document.getElementById('frow');
  if(fr) fr.classList.toggle('hide',cat!=='work');
  q('.ftab').forEach(function(b){b.classList.toggle('active',b.dataset.field===field);});
  q('.panel[data-cat="work"] .card').forEach(function(c){
   c.classList.toggle('hide',field!=='all'&&c.dataset.field!==field);});
  window.scrollTo({top:0});
 }
 q('.cattab').forEach(function(b){b.addEventListener('click',function(){cat=b.dataset.cat;render();});});
 q('.ftab').forEach(function(b){b.addEventListener('click',function(){field=b.dataset.field;render();});});
 render();
 var t=document.getElementById('toTop');
 window.addEventListener('scroll',function(){t.classList.toggle('show',window.scrollY>500);});
 t.addEventListener('click',function(){window.scrollTo({top:0,behavior:'smooth'});});
 if('serviceWorker' in navigator){window.addEventListener('load',function(){navigator.serviceWorker.register('sw.js').catch(function(){});});}
})();
</script>
</body></html>
"""

if __name__ == "__main__":
    build()
