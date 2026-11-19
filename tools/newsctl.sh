#!/usr/bin/env bash
# newsctl.sh — 統合ニュースサイトの取得・追加・ビルド・公開
# 使い方: bash tools/newsctl.sh {today|timer|status|pull|prev|add|sync|build|publish|token}
#
# 重要: Cowork の git プロキシは本リポジトリへの push 時に認証情報を注入せず 403 を返す。
#       そのため push / GitHub API はプロキシをバイパスして直接接続する（_nop ラッパ）。
#
# 2026-11-20 改定（障害の一括対策）
#   - today  : 実時刻を GitHub API の Date ヘッダで取得（再開セッションの時計ずれ対策）。土日は exit 2
#   - timer  : 実行時間の上限を実時刻で管理（start <分> / check）
#   - add    : 0件・日付不一致を拒否／リモートの方が件数が多ければ上書きしない（同時実行の後勝ち対策）
#              push 後にリモートの件数を検証。05:15 JST 以降の到着なら publish まで自動実行
#   - sync   : rebase をやめ、fetch → reset → 再適用 → push の3回再試行（rebase 衝突で偽の pushOK が出る問題の対策）
#   - publish: 最新を取得してからビルド／生成時刻だけの差分は skip／push を3回再試行
#   - status : 件数と下限を表示、PAT の有効期限を確認
#   - prev   : カテゴリごとに「記事を持つ直近日」を選ぶ（他カテゴリの0件ファイルに引きずられる不具合の修正）
set -uo pipefail

# 実行中に git reset でこのファイル自体が書き換わっても壊れないよう、一時コピーから実行し直す
if [ -z "${NEWSCTL_SELF:-}" ]; then
  _self="$(mktemp)"; cp "${BASH_SOURCE[0]}" "$_self"
  NEWSCTL_SELF="$_self" NEWSCTL_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" exec bash "$_self" "$@"
fi
trap 'rm -f "$NEWSCTL_SELF"' EXIT

GH_OWNER="${GH_OWNER:-superduperohiolife-lgtm}"
GH_REPO="${GH_REPO:-news}"
GH_BRANCH="${GH_BRANCH:-main}"
PAT_FILE="${PAT_FILE:-$HOME/.news_pat}"
TIMER_FILE="${TIMER_FILE:-$HOME/.news_deadline}"

REPO_DIR="${NEWSCTL_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DATA_DIR="$REPO_DIR/data"
TOOLS_DIR="$REPO_DIR/tools"

# 下限件数（これ未満は失敗扱い）
declare -A MIN=([general]=5 [life]=5 [work]=8)

# --- プロキシをバイパスして実行するラッパ -------------------------------
_nop() {
  env -u https_proxy -u HTTPS_PROXY -u http_proxy -u HTTP_PROXY \
      NO_PROXY='*' no_proxy='*' "$@"
}

_remote() {
  # テスト用: NEWSCTL_REMOTE にローカルの bare リポジトリを指定できる
  if [ -n "${NEWSCTL_REMOTE:-}" ]; then printf '%s' "$NEWSCTL_REMOTE"; return 0; fi
  if [ ! -s "$PAT_FILE" ]; then
    echo "[未設定] PATファイルが見つからない: $PAT_FILE" >&2
    return 2
  fi
  printf 'https://x-access-token:%s@github.com/%s/%s.git' \
    "$(tr -d ' \r\n' < "$PAT_FILE")" "$GH_OWNER" "$GH_REPO"
}

# 出力からトークンを伏字にする
_mask() { sed -E 's/github_pat_[A-Za-z0-9_]+/***/g; s/gh[pousr]_[A-Za-z0-9]+/***/g; s#x-access-token:[^@]*@#x-access-token:***@#g'; }

_gitid() {
  git -C "$REPO_DIR" config user.name  "${GIT_AUTHOR_NAME:-news-bot}"
  git -C "$REPO_DIR" config user.email "${GIT_AUTHOR_EMAIL:-news-bot@users.noreply.github.com}"
}

# --- 実時刻 ---------------------------------------------------------------
# GitHub API の Date ヘッダ（UTC）を実時刻とみなす。取れなければローカル時計。
_epoch() {
  local h e
  [ "${NEWSCTL_OFFLINE:-0}" = "1" ] && { date +%s; return; }
  h="$(_nop curl -sI -m 10 https://api.github.com 2>/dev/null | tr -d '\r' | sed -n 's/^[Dd]ate: //p' | head -1)"
  if [ -n "$h" ] && e="$(date -d "$h" +%s 2>/dev/null)"; then
    echo "$e"
  else
    date +%s
  fi
}
_jst() { TZ=Asia/Tokyo date -d "@$(_epoch)" "+${1:-%F}"; }

# 配信日(JST)を出力。土日は exit 2。ローカル時計と10分以上ずれていれば警告。
cmd_today() {
  local e l d w
  e="$(_epoch)"; l="$(date +%s)"
  d="$(TZ=Asia/Tokyo date -d "@$e" +%F)"
  w="$(TZ=Asia/Tokyo date -d "@$e" +%u)"
  if [ $(( e > l ? e - l : l - e )) -gt 600 ]; then
    echo "[時計ずれ] ローカル時計が実時刻と $(( (e - l) / 60 )) 分ずれている。実時刻 $(TZ=Asia/Tokyo date -d "@$e" '+%F %H:%M') JST を採用" >&2
  fi
  echo "$d"
  [ "$w" -le 5 ] || { echo "[休配日] $d は土日のため配信しない" >&2; return 2; }
}

# 実行時間の上限管理: timer start <分> / timer check
cmd_timer() {
  local now; now="$(_epoch)"
  case "${1:-check}" in
    start)
      local m="${2:-45}"
      echo $(( now + m * 60 )) > "$TIMER_FILE"
      echo "[timer] 上限 ${m}分（〜$(TZ=Asia/Tokyo date -d "@$(( now + m * 60 ))" +%H:%M) JST）"
      ;;
    check)
      [ -s "$TIMER_FILE" ] || { echo "[timer] 未開始"; return 0; }
      local dl; dl="$(cat "$TIMER_FILE")"
      local left=$(( (dl - now) / 60 ))
      if [ "$now" -ge "$dl" ]; then
        echo "[timer] 上限超過（$(( (now - dl) / 60 ))分超過）→ 収集を打ち切り、確定済みの記事だけで出力・push へ進むこと"
        return 1
      fi
      echo "[timer] 残り ${left}分"
      ;;
    *) echo "usage: timer {start <分>|check}" >&2; return 64 ;;
  esac
}

_count() {  # JSON の記事数（読めなければ -1）
  python3 -c "import json,sys
try: print(len(json.load(open(sys.argv[1],encoding='utf-8')).get('articles',[])))
except Exception: print(-1)" "$1"
}

_token_check() {
  [ -s "$PAT_FILE" ] || { echo "  PAT      : 未配置"; return 0; }
  local h code exp
  h="$(_nop curl -s -m 15 -o /dev/null -D - -w 'HTTP:%{http_code}\n' \
        -H "Authorization: Bearer $(tr -d ' \r\n' < "$PAT_FILE")" https://api.github.com/user 2>/dev/null | tr -d '\r')"
  code="$(printf '%s\n' "$h" | sed -n 's/^HTTP://p' | tail -1)"
  exp="$(printf '%s\n' "$h" | sed -n 's/^[Gg]ithub-[Aa]uthentication-[Tt]oken-[Ee]xpiration: //p' | head -1)"
  if [ "$code" != "200" ]; then
    echo "  PAT      : ⚠ 認証NG（HTTP ${code:-接続不可}）"
    return 1
  fi
  if [ -n "$exp" ]; then
    local days=$(( ( $(date -d "$exp" +%s 2>/dev/null || echo 0) - $(_epoch) ) / 86400 ))
    if [ "$days" -lt 14 ]; then
      echo "  PAT      : ⚠ 有効期限まで残り${days}日（$exp）→ 再発行が必要"
    else
      echo "  PAT      : OK（期限 $exp・残り${days}日）"
    fi
  else
    echo "  PAT      : OK（期限なし）"
  fi
}

cmd_token() { _token_check; }

cmd_status() {
  local d; d="$(cmd_today 2>/dev/null || true)"
  [ -n "$d" ] || d="$(_jst %F)"
  echo "repo   : $REPO_DIR"
  echo "today  : $d (JST・実時刻 $(_jst '%H:%M'))"
  local n=0 short=0 c f k
  for c in general life work; do
    f="$DATA_DIR/$c-news-$d.json"
    if [ -f "$f" ]; then
      k="$(_count "$f")"
      if [ "$k" -lt "${MIN[$c]}" ]; then
        echo "  [!] $c-news-$d.json ${k}件（下限${MIN[$c]}未満）"
        short=$((short+1))
      else
        echo "  [o] $c-news-$d.json ${k}件"
      fi
      [ "$k" -gt 0 ] && n=$((n+1))
    else
      echo "  [-] $c-news-$d.json (未着)"
      short=$((short+1))
    fi
  done
  echo "today_json_count=$n short=$short"
  _token_check
  [ "$n" -gt 0 ]
}

# 直近の過去掲載日の見出し一覧を出力する（重複判定・続報判定の材料）。
# カテゴリごとに「記事を1件以上持つ、今日より前の最新日」を選ぶ。
# 使い方: newsctl.sh prev [general|life|work]  … 省略時は全カテゴリ
cmd_prev() {
  local want="${1:-}" today
  today="$(cmd_today 2>/dev/null || true)"; [ -n "$today" ] || today="$(_jst %F)"
  python3 - "$DATA_DIR" "$want" "$today" <<'PY'
import sys, os, re, json, glob
data_dir, want, today = sys.argv[1], sys.argv[2], sys.argv[3]
CATS = {"general": "general", "life": "life", "work": "work",
        "econ": "work", "ai": "work", "auto": "work"}
best = {}
for p in glob.glob(os.path.join(data_dir, "*-news-*.json")):
    m = re.match(r"^([a-z]+)-news-(\d{4}-\d{2}-\d{2})\.json$", os.path.basename(p))
    if not m or m.group(1) not in CATS or m.group(2) >= today:
        continue
    ck, d = CATS[m.group(1)], m.group(2)
    if want and ck != want:
        continue
    try:
        arts = json.load(open(p, encoding="utf-8")).get("articles", [])
    except Exception:
        continue
    if not arts:
        continue
    if ck not in best or d > best[ck][0]:
        best[ck] = (d, arts)
    elif d == best[ck][0]:
        best[ck] = (d, best[ck][1] + arts)
if not best:
    print("[prev] 過去データなし")
    sys.exit(0)
for ck in ("general", "life", "work"):
    if ck not in best:
        continue
    d, arts = best[ck]
    print("[prev] %s 直近掲載日: %s（%d件）" % (ck, d, len(arts)))
    for a in arts:
        f = a.get("field", "")
        print("  - [%s%s] %s" % (ck, ("/" + f) if f else "", a.get("title", "")))
PY
}

cmd_pull() {
  local r; r="$(_remote)" || return $?
  _nop git -C "$REPO_DIR" fetch -q "$r" "$GH_BRANCH" > /tmp/newsctl_pull.log 2>&1 \
    && git -C "$REPO_DIR" reset -q --hard FETCH_HEAD >> /tmp/newsctl_pull.log 2>&1
  local rc=$?
  _mask < /tmp/newsctl_pull.log
  return $rc
}

# data/ のファイルを公開ブランチへ反映する。
# リモートに同名ファイルがあり、件数がこちら以上なら上書きしない（後勝ちによる件数減の防止）。
# 使い方: _push_data <data/ 内のファイル名>...
_push_data() {
  local r; r="$(_remote)" || return $?
  _gitid
  local stage; stage="$(mktemp -d)"
  local b
  for b in "$@"; do cp "$DATA_DIR/$b" "$stage/$b"; done
  local i rc=1 changed
  for i in 1 2 3; do
    if ! _nop git -C "$REPO_DIR" fetch -q "$r" "$GH_BRANCH" > /tmp/newsctl_push.log 2>&1; then
      _mask < /tmp/newsctl_push.log; sleep 5; continue
    fi
    git -C "$REPO_DIR" reset -q --hard FETCH_HEAD
    changed=0
    for b in "$@"; do
      local mine theirs
      mine="$(_count "$stage/$b")"
      if [ -f "$DATA_DIR/$b" ]; then
        theirs="$(_count "$DATA_DIR/$b")"
        if [ "$theirs" -ge "$mine" ] && [ "${FORCE:-0}" != "1" ]; then
          if cmp -s "$stage/$b" "$DATA_DIR/$b"; then
            echo "[skip] data/$b はリモートと同一"
          else
            echo "[keep-remote] data/$b はリモート${theirs}件 ≥ こちら${mine}件のため上書きしない"
          fi
          continue
        fi
      fi
      cp "$stage/$b" "$DATA_DIR/$b"
      git -C "$REPO_DIR" add "data/$b"
      changed=1
    done
    if [ "$changed" -eq 0 ]; then rm -rf "$stage"; echo "[skip] 変更なし"; return 0; fi
    git -C "$REPO_DIR" commit -q -m "add news data $(_jst %FT%H:%M)JST" || { rm -rf "$stage"; return 1; }
    _nop git -C "$REPO_DIR" push "$r" "HEAD:$GH_BRANCH" > /tmp/newsctl_push.log 2>&1
    rc=$?
    _mask < /tmp/newsctl_push.log
    [ "$rc" -eq 0 ] && break
    echo "[retry] push 競合のため最新を取得して再適用（${i}/3）" >&2
    sleep 5
  done
  rm -rf "$stage"
  if [ "$rc" -ne 0 ]; then
    echo "[push失敗] exit=$rc" >&2
    return "$rc"
  fi
  echo "[pushOK] $(git -C "$REPO_DIR" rev-parse --short HEAD)"
}

# リモート main 上の件数を確認して出力する（報告はこの行を根拠にする）
_verify() {
  local r; r="$(_remote)" || return $?
  _nop git -C "$REPO_DIR" fetch -q "$r" "$GH_BRANCH" >/dev/null 2>&1 || { echo "[verify] 取得失敗"; return 1; }
  local b n
  for b in "$@"; do
    n="$(git -C "$REPO_DIR" show "FETCH_HEAD:data/$b" 2>/dev/null | python3 -c "import json,sys
try: print(len(json.load(sys.stdin).get('articles',[])))
except Exception: print('未着')")"
    echo "[verify] リモート data/$b = ${n}件（$(git -C "$REPO_DIR" rev-parse --short FETCH_HEAD)）"
  done
}

cmd_add() {
  mkdir -p "$DATA_DIR"
  local f base names=() n fd jd
  for f in "$@"; do
    [ -f "$f" ] || { echo "[skip] not found: $f" >&2; continue; }
    base="$(basename "$f")"
    if ! [[ "$base" =~ ^(general|life|work)-news-([0-9]{4}-[0-9]{2}-[0-9]{2})\.json$ ]]; then
      echo "[NG] ファイル名が規約外: $base（<general|life|work>-news-YYYY-MM-DD.json）" >&2; return 1
    fi
    fd="${BASH_REMATCH[2]}"
    if ! python3 -c "import json,sys; json.load(open(sys.argv[1],encoding='utf-8'))" "$f"; then
      echo "[NG] 不正なJSONのため追加しない: $base" >&2
      return 1
    fi
    n="$(_count "$f")"
    jd="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1],encoding='utf-8')).get('date',''))" "$f")"
    if [ "$jd" != "$fd" ]; then
      echo "[NG] JSON の date($jd) とファイル名の日付($fd)が不一致: $base" >&2; return 1
    fi
    if [ "$n" -le 0 ] && [ "${ALLOW_EMPTY:-0}" != "1" ]; then
      echo "[NG] 記事0件のため追加しない: $base（0件の push は失敗扱い）" >&2; return 3
    fi
    python3 - "$f" "$DATA_DIR/$base" <<'PY'
import sys, io
src, dst = sys.argv[1], sys.argv[2]
t = io.open(src, encoding='utf-8', newline=None).read().replace('\r\n', '\n')
io.open(dst, 'w', encoding='utf-8', newline='\n').write(t)
PY
    echo "[add] data/$base（${n}件）"
    names+=("$base")
  done
  [ "${#names[@]}" -gt 0 ] || { echo "[NG] 追加対象なし" >&2; return 1; }
  _push_data "${names[@]}" || return $?
  _verify "${names[@]}"
  # 05:15 JST 以降に届いたデータは、本番リビルド(05:20)に乗らないので自分で公開する
  local hm; hm="$(_jst %H%M)"
  if [ "$hm" -ge 0515 ] && [ "$hm" -lt 1200 ]; then
    echo "[late] $(_jst %H:%M) JST の到着のため publish まで実行する"
    cmd_publish
  fi
}

# data/ の未コミット変更をすべて反映する（手動用）
cmd_sync() {
  local names=() p
  while IFS= read -r p; do names+=("$(basename "$p")"); done < <(
    git -C "$REPO_DIR" status --porcelain -- data | sed -E 's/^.. //' | grep -E '\.json$')
  [ "${#names[@]}" -gt 0 ] || { echo "[skip] 変更なし"; return 0; }
  _push_data "${names[@]}"
}

cmd_build() {
  local work; work="$(mktemp -d)"
  cp "$TOOLS_DIR/build_news_site.py" "$work/" || return 1
  cp "$TOOLS_DIR/track_config.json" "$work/" 2>/dev/null || true
  cp "$DATA_DIR"/*.json "$work/" 2>/dev/null
  ( cd "$work" && python3 -W ignore::DeprecationWarning build_news_site.py ) || { rm -rf "$work"; return 1; }
  [ -s "$work/news.html" ] || { echo "[NG] news.html が生成されていない" >&2; rm -rf "$work"; return 1; }
  if ! grep -q '</html>' "$work/news.html"; then
    echo "[NG] 生成HTMLが不完全" >&2; rm -rf "$work"; return 1
  fi
  cp "$work/news.html" "$REPO_DIR/index.html"
  echo "[build] index.html ($(wc -c < "$REPO_DIR/index.html") bytes)"
  rm -rf "$work"
}

# 生成時刻の行だけが違うなら「変更なし」とみなす
_only_timestamp_changed() {
  git -C "$REPO_DIR" diff --quiet HEAD -- data tools && \
  diff -q <(git -C "$REPO_DIR" show HEAD:index.html 2>/dev/null | sed -E 's/生成 [0-9: -]+JST//') \
          <(sed -E 's/生成 [0-9: -]+JST//' "$REPO_DIR/index.html") >/dev/null
}

cmd_publish() {
  local r; r="$(_remote)" || return $?
  _gitid
  local i rc=1
  for i in 1 2 3; do
    if ! _nop git -C "$REPO_DIR" fetch -q "$r" "$GH_BRANCH" > /tmp/newsctl_push.log 2>&1; then
      _mask < /tmp/newsctl_push.log; sleep 5; continue
    fi
    git -C "$REPO_DIR" reset -q --hard FETCH_HEAD
    cmd_build || return 1
    if _only_timestamp_changed; then
      git -C "$REPO_DIR" checkout -q -- index.html
      echo "[skip] 変更なし（紙面は最新）"
      return 0
    fi
    git -C "$REPO_DIR" add index.html
    git -C "$REPO_DIR" commit -q -m "update news $(_jst %FT%H:%M)JST" || return 1
    _nop git -C "$REPO_DIR" push "$r" "HEAD:$GH_BRANCH" > /tmp/newsctl_push.log 2>&1
    rc=$?
    _mask < /tmp/newsctl_push.log
    [ "$rc" -eq 0 ] && break
    echo "[retry] push 競合のため最新を取得して再ビルド（${i}/3）" >&2
    sleep 5
  done
  if [ "$rc" -ne 0 ]; then
    echo "[push失敗] exit=$rc" >&2
    return "$rc"
  fi
  echo "[公開OK] https://${GH_OWNER}.github.io/${GH_REPO}/ ($(git -C "$REPO_DIR" rev-parse --short HEAD))"
}

case "${1:-}" in
  today)   cmd_today ;;
  timer)   shift; cmd_timer "$@" ;;
  status)  cmd_status ;;
  token)   cmd_token ;;
  pull)    cmd_pull ;;
  prev)    shift; cmd_prev "${1:-}" ;;
  add)     shift; cmd_add "$@" ;;
  sync)    cmd_sync ;;
  build)   cmd_build ;;
  publish) cmd_publish ;;
  *) echo "usage: $0 {today|timer start <分>|timer check|status|token|pull|prev [cat]|add <json>...|sync|build|publish}" >&2; exit 64 ;;
esac
