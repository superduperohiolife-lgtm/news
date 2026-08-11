# -*- coding: utf-8 -*-
"""統合ニュースサイト・ビルダー v4 (Claude Design 忠実再現・JSONデータ駆動)
同一フォルダの econ/ai/auto-news-YYYY-MM-DD.json を走査し、直近3稼働日を
すべて/カテゴリ別タブ + 本日のTOP3 + カテゴリ別セクション(カード内に出典リンク)で
自己完結HTML(news.html)に生成。外部依存なし・UTF-8/LF。

各JSONスキーマ: {"date":"YYYY-MM-DD","sub":"曜","articles":[{
  "sec":"小見出し","badges":["new","us","jp","prev"],"title":"…","summary":"…",
  "insight":"…(任意)","meta":["要点1","要点2"(任意)],"source":"名","sourceUrl":"URL"}],
  "weekly":["本日のTOP候補文",…]}"""
import os, re, glob, html, json, datetime

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "news.html")
DAYS = 3
ACCENT = "#3556b8"
CATS = [("econ-news","econ","経済"),("ai-news","AI","AI"),("auto-news","auto","自動車")]
CATLABEL = {"econ":"経済","AI":"AI","auto":"自動車"}
ORDER = ["econ","AI","auto"]
CATFILTERS = [("all","すべて"),("econ","経済"),("AI","AI"),("auto","自動車")]
WD = ["月","火","水","木","金","土","日"]

def ri(text):
    t = html.escape(text or "")
    t = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
               lambda m: '<a href="%s" target="_blank" rel="noopener">%s</a>'%(m.group(2),m.group(1)), t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
    return t

def esc(s): return html.escape(s or "")

def badge_html(k):
    if k=="new": return '<span class="badge new">新</span>'
    if k=="prev": return '<span class="badge prev">既報</span>'
    if k=="us": return '<span class="badge geo">US</span>'
    if k=="jp": return '<span class="badge geo">JP</span>'
    return ""

def card_html(a):
    badges="".join(badge_html(b) for b in a.get("badges",[]))
    ctx='<span class="ctx">%s</span>'%esc(a.get("sec","")) if a.get("sec") else ""
    meta='<div class="cmeta">%s%s</div>'%(badges,ctx) if (badges or ctx) else ""
    title='<h3>%s</h3>'%ri(a.get("title","")) if a.get("title") else ""
    summ='<p>%s</p>'%ri(a.get("summary","")) if a.get("summary") else ""
    chips=""
    if a.get("meta"):
        chips='<div class="chips">%s</div>'%"".join('<span class="mchip">%s</span>'%esc(m) for m in a["meta"])
    ins='<div class="insight">💡 %s</div>'%ri(a["insight"]) if a.get("insight") else ""
    src=""
    if a.get("source") and a.get("sourceUrl"):
        src='<div class="src"><a href="%s" target="_blank" rel="noopener">出典: %s ↗</a></div>'%(esc(a["sourceUrl"]),esc(a["source"]))
    elif a.get("source"):
        src='<div class="src"><span class="srcx">出典: %s</span></div>'%esc(a["source"])
    return '<article class="card">%s%s%s%s%s%s</article>'%(meta,title,summ,chips,ins,src)

def section_html(label,count,cards):
    return '<section class="sec"><h2 class="sech"><span class="bar"></span>%s<span class="scnt">%d</span></h2>%s</section>'%(esc(label),count,cards)

def weekly_box(items,dlabel):
    lis="".join("<li>%s</li>"%ri(w) for w in items)
    return '<section class="weekly"><h2>📅 本日のTOP3(%s時点)</h2><ol>%s</ol></section>'%(esc(dlabel),lis)

def balanced_weekly(day, present):
    lists={ck:list(day[ck].get("weekly",[])) for ck in present}
    out=[]; i=0
    while len(out)<3 and any(lists[ck] for ck in present):
        for ck in present:
            if i < len(lists[ck]):
                out.append(lists[ck][i])
                if len(out)>=3: break
        i+=1
        if i>20: break
    return out[:3]

def render_panel(day, present, dlabel, filt):
    show = present if filt=="all" else ([filt] if filt in present else [])
    if filt=="all":
        weekly = balanced_weekly(day, present)
    else:
        weekly = (day.get(filt,{}).get("weekly",[]) if filt in day else [])[:3]
    out=""
    if weekly: out += weekly_box(weekly, dlabel)
    if not show:
        out += '<div class="empty">このカテゴリの配信はありません</div>'
    for ck in show:
        arts=day[ck]["articles"]
        cards="".join(card_html(a) for a in arts)
        out += section_html(CATLABEL[ck], len(arts), cards)
    return out

def collect():
    data={}
    for prefix,ck,_l in CATS:
        for p in glob.glob(os.path.join(BASE, prefix+"-*.json")):
            m=re.search(r"(\d{4}-\d{2}-\d{2})\.json$", os.path.basename(p))
            if not m: continue
            try: j=json.load(open(p,encoding="utf-8"))
            except Exception: continue
            data.setdefault(m.group(1),{})[ck]=j
    return data, sorted(data.keys(), reverse=True)[:DAYS]

# ---- 「新」判定・記事エイジング（同一ストーリーの検出）--------------------
# 日次JSONの "new" バッジは信頼せず、全履歴を横断してストーリーを同定し、
#  ・新 = そのストーリーがApp上で初めて取り上げられた日（初出）のみ
#  ・同一ストーリーは初出から最大3稼働日まで掲載（超過分は非表示）
# を builder 側で再計算する。同一判定は「固有名アンカーの共有 かつ 見出し類似度>=閾値」。
MAXDAYS=3          # 同一ストーリーの最大掲載日数（初出含む稼働日数）
SIM_TH=0.40        # 見出し文字bigramのJaccard類似度しきい値
_STOP=set("トヨタ ホンダ 日産 スバル マツダ ダイハツ スズキ 三菱 レクサス フォード テスラ クライスラー ジープ シボレー キャデラック リコール マイナーチェンジ モデル ニュース 発売 発表 全国 予定 本日 特別 記念 一部 改良 実施 拡大 開始 運行 規模 規制 対応 追加 ロボタクシ".split())

def _plain(t):
    t=re.sub(r"\[([^\]]+)\]\([^)]+\)",r"\1",t or "")
    return re.sub(r"\*\*([^*]+)\*\*",r"\1",t)

def _tokens(t):
    t=_plain(t); out=set()
    for x in re.findall(r"[ァ-ヴー]{3,}",t):
        x=x.strip("ー")
        if x and x not in _STOP: out.add("k:"+x)
    up={s.upper() for s in _STOP}
    for x in re.findall(r"[A-Za-z][A-Za-z0-9\-]{2,}",t):
        x=re.sub(r"-","",x).upper()
        if len(x)>=3 and x not in up: out.add("l:"+x)
    return out

def _bg(t):
    t=re.sub(r"[\s、。・,\.\-—“~()（）「」]+","",_plain(t))
    return set(t[i:i+2] for i in range(len(t)-1)) or ({t} if t else set())

def _jac(a,b):
    u=a|b
    return len(a&b)/len(u) if u else 0.0

def flag_and_age(data):
    """data(全履歴)を破壊的に更新: badges の new/prev を再計算し、
    初出から MAXDAYS を超えたストーリーの記事を各日から除去する。"""
    items=[]  # {date,ck,ai,tok,bg}
    for date in sorted(data.keys()):
        for ck,j in data[date].items():
            for ai,a in enumerate(j.get("articles",[])):
                ti=a.get("title","")
                items.append({"date":date,"ck":ck,"ai":ai,
                              "tok":_tokens(ti),"bg":_bg(ti)})
    parent=list(range(len(items)))
    def find(x):
        while parent[x]!=x: parent[x]=parent[parent[x]]; x=parent[x]
        return x
    for i,r in enumerate(items):
        best=(0.0,None)
        for j in range(i):
            s=items[j]
            if s["ck"]!=r["ck"] or s["date"]>=r["date"]: continue
            if not (r["tok"] & s["tok"]): continue
            v=_jac(r["bg"],s["bg"])
            if v>best[0]: best=(v,j)
        if best[1] is not None and best[0]>=SIM_TH:
            parent[find(i)]=find(best[1])
    # コンポーネント（=ストーリー）ごとに出現日を集約
    from collections import defaultdict
    comp=defaultdict(list)
    for i in range(len(items)): comp[find(i)].append(i)
    first={}; allowed={}
    for c,idxs in comp.items():
        ds=sorted({items[i]["date"] for i in idxs})
        fd=ds[0]; keep=set(ds[:MAXDAYS])
        for i in idxs:
            first[i]=fd; allowed[i]=items[i]["date"] in keep
    # 記事へ反映（除去は後ろから）
    drop=defaultdict(list)  # (date,ck) -> [ai...]
    for i,r in enumerate(items):
        a=data[r["date"]][r["ck"]]["articles"][r["ai"]]
        if not allowed[i]:
            drop[(r["date"],r["ck"])].append(r["ai"]); continue
        base=[b for b in a.get("badges",[]) if b not in ("new","prev")]
        geo=[b for b in base if b in ("us","jp")]
        other=[b for b in base if b not in ("us","jp")]
        isnew=(r["date"]==first[i])
        a["badges"]=(["new"] if isnew else [])+geo+other+([] if isnew else ["prev"])
    for (date,ck),ais in drop.items():
        arts=data[date][ck]["articles"]
        for ai in sorted(ais,reverse=True): del arts[ai]
    return data

def build():
    data,_=collect()
    data=flag_and_age(data)
    dates=sorted([d for d in data if any(data[d][ck].get("articles") for ck in data[d])],
                 reverse=True)[:DAYS]
    now=datetime.datetime.utcnow()+datetime.timedelta(hours=9)
    gen=now.strftime("%Y-%m-%d %H:%M JST")
    if not dates:
        open(OUT,"w",encoding="utf-8",newline="\n").write(
            TEMPLATE.replace("__DAYTABS__","").replace("__CATTABS__","")
            .replace("__PANELS__",'<div class="empty">表示できるニュースがありません</div>')
            .replace("__COUNTS__","{}").replace("__GEN__",gen))
        print("生成: %s (対象日: なし)"%OUT); return
    day_tabs=""; panels=""; counts={}
    for di,date in enumerate(dates):
        day=data[date]
        y,mo,da=map(int,date.split("-")); dlabel="%d/%d"%(mo,da); wd=WD[datetime.date(y,mo,da).weekday()]
        present=[ck for ck in ORDER if ck in day and day[ck].get("articles")]
        a=" active" if di==0 else ""
        day_tabs+='<button class="daytab%s" data-day="%d"><span>%s</span><span class="dsub">%s</span></button>'%(a,di,dlabel,wd)
        total=sum(len(day[ck]["articles"]) for ck in present)
        counts[di]={0:total,1:len(day.get("econ",{}).get("articles",[])),2:len(day.get("AI",{}).get("articles",[])),3:len(day.get("auto",{}).get("articles",[]))}
        for fi,(fk,flabel) in enumerate(CATFILTERS):
            show=" active" if (di==0 and fi==0) else ""
            panels+='<div class="panel%s" data-day="%d" data-cat="%d">%s</div>'%(show,di,fi,render_panel(day,present,dlabel,fk))
    cat_tabs=""
    for fi,(fk,flabel) in enumerate(CATFILTERS):
        a=" active" if fi==0 else ""
        cat_tabs+='<button class="cattab%s" data-cat="%d">%s<span class="cnt">%d</span></button>'%(a,fi,flabel,counts[0][fi])
    open(OUT,"w",encoding="utf-8",newline="\n").write(
        TEMPLATE.replace("__DAYTABS__",day_tabs).replace("__CATTABS__",cat_tabs)
        .replace("__PANELS__",panels).replace("__COUNTS__",json.dumps(counts)).replace("__GEN__",gen))
    print("生成: %s (対象日: %s)"%(OUT,", ".join(dates)))

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
:root{--bg:#f6f5f1;--ink:#201e1b;--muted:#8a857c;--muted2:#a39d92;--line:#e6e3dc;--line2:#dcd8d0;--body:#514d46;--accent:#3556b8;--card:#fff;--chip:#f1efe9;--dark:#232120;}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent;}
html{scroll-behavior:smooth;}
body{margin:0;background:var(--bg);color:var(--ink);line-height:1.7;
 font-family:'Noto Sans JP',system-ui,-apple-system,sans-serif;-webkit-font-smoothing:antialiased;}
a{color:var(--accent);text-decoration:none;}a:hover{text-decoration:underline;}
.wrap{max-width:720px;margin:0 auto;padding:0 20px 90px;}
header.top{display:flex;align-items:baseline;justify-content:space-between;gap:12px;padding:26px 0 14px;}
header.top h1{margin:0;font-size:20px;font-weight:900;letter-spacing:.02em;}
header.top .sub{font-size:12px;color:var(--muted);font-weight:400;margin-left:10px;}
header.top .gen{font-size:11.5px;color:var(--muted);white-space:nowrap;}
nav.tabs{position:sticky;top:0;z-index:10;background:rgba(246,245,241,.94);
 backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);padding:8px 0 12px;border-bottom:1px solid var(--line);}
.trow,.crow{display:flex;gap:8px;overflow-x:auto;scrollbar-width:none;}
.trow::-webkit-scrollbar,.crow::-webkit-scrollbar{display:none;}
.crow{gap:6px;margin-top:10px;}
.daytab{flex:0 0 auto;display:inline-flex;align-items:baseline;gap:5px;border:1px solid var(--line2);
 background:var(--card);color:var(--body);border-radius:10px;padding:8px 16px;font:inherit;font-size:14px;cursor:pointer;white-space:nowrap;}
.daytab .dsub{font-size:11px;opacity:.7;}
.daytab.active{background:var(--accent);border-color:var(--accent);color:#fff;}
.cattab{flex:0 0 auto;display:inline-flex;align-items:center;gap:6px;border:1px solid transparent;
 background:transparent;color:var(--muted);border-radius:99px;padding:5px 12px;font:inherit;font-size:12.5px;font-weight:500;cursor:pointer;white-space:nowrap;}
.cattab .cnt{font-size:10.5px;font-weight:700;color:var(--muted);background:var(--line);border-radius:99px;padding:1px 6px;}
.cattab.active{border-color:var(--accent);background:#fff;color:var(--accent);font-weight:700;}
.cattab.active .cnt{background:var(--accent);color:#fff;}
.panel{display:none;}.panel.active{display:block;animation:fade .18s ease;}
@keyframes fade{from{opacity:0;transform:translateY(4px);}to{opacity:1;transform:none;}}
.weekly{background:var(--dark);border-radius:14px;padding:20px 24px;margin-top:24px;}
.weekly h2{margin:0 0 12px;font-size:12px;font-weight:700;letter-spacing:.12em;color:rgba(255,255,255,.7);}
.weekly ol{margin:0;padding:0 0 0 20px;display:flex;flex-direction:column;gap:9px;}
.weekly li{font-size:13px;line-height:1.65;color:rgba(255,255,255,.92);}
.weekly li strong{color:#fff;}.weekly a{color:#c7d2ff;}
.sec{margin-top:28px;}
.sech{display:flex;align-items:center;gap:10px;margin:0 0 12px;font-size:17px;font-weight:900;}
.sech .bar{display:inline-block;width:5px;height:20px;border-radius:3px;background:var(--accent);}
.sech .scnt{font-size:11.5px;font-weight:700;color:var(--muted);background:var(--line);border-radius:99px;padding:2px 9px;}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 20px;margin:0 0 10px;}
.cmeta{display:flex;flex-wrap:wrap;align-items:center;gap:6px;}
.badge{font-size:10.5px;border-radius:5px;padding:2px 7px;white-space:nowrap;}
.badge.new{font-weight:700;color:#fff;background:var(--accent);}
.badge.prev{font-weight:500;color:var(--muted);background:var(--chip);}
.badge.geo{font-weight:700;color:var(--body);background:var(--chip);}
.ctx{font-size:11px;color:var(--muted2);}
.card h3{margin:8px 0 0;font-size:15.5px;line-height:1.5;font-weight:700;color:var(--ink);}
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
 <header class="top"><h1>統合ニュース<span class="sub">経済 / AI / 自動車</span></h1><span class="gen">生成 __GEN__</span></header>
 <nav class="tabs"><div class="trow">__DAYTABS__</div><div class="crow">__CATTABS__</div></nav>
 __PANELS__
 <footer>※ 事実(数値・日付・金額)は各出典に基づく。予測・噂・未確認情報は本文中に「(要確認)」と明記。個人用。</footer>
</div>
<button id="toTop" aria-label="先頭へ">↑</button>
<script>
(function(){
 var C=__COUNTS__,day=0,cat=0;
 function q(s){return Array.prototype.slice.call(document.querySelectorAll(s));}
 function render(){
  q('.daytab').forEach(function(b){b.classList.toggle('active',+b.dataset.day===day);});
  q('.cattab').forEach(function(b){var c=+b.dataset.cat,n=(C[day]&&C[day][c])||0;
   b.classList.toggle('active',c===cat);var e=b.querySelector('.cnt');if(e)e.textContent=n;});
  q('.panel').forEach(function(p){p.classList.toggle('active',+p.dataset.day===day&&+p.dataset.cat===cat);});
  window.scrollTo({top:0});
 }
 q('.daytab').forEach(function(b){b.addEventListener('click',function(){day=+b.dataset.day;render();});});
 q('.cattab').forEach(function(b){b.addEventListener('click',function(){cat=+b.dataset.cat;render();});});
 render();
 var t=document.getElementById('toTop');
 window.addEventListener('scroll',function(){t.classList.toggle('show',window.scrollY>500);});
 t.addEventListener('click',function(){window.scrollTo({top:0,behavior:'smooth'});});
 if('serviceWorker' in navigator){window.addEventListener('load',function(){navigator.serviceWorker.register('sw.js').catch(function(){});});}
})();
</script>
</body></html>
"""

if __name__=="__main__":
    build()
