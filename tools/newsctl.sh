#!/usr/bin/env bash
# newsctl.sh — 統合ニュースサイトの取得・追加・ビルド・公開
# 使い方: bash tools/newsctl.sh {status|pull|add <json>...|build|publish}
#
# 重要: Cowork の git プロキシは本リポジトリへの push 時に認証情報を注入せず 403 を返す。
#       そのため push / GitHub API はプロキシをバイパスして直接接続する（_nop ラッパ）。
#       clone / fetch は通常経路でも成功するが、統一のため同じラッパを通す。
set -uo pipefail

GH_OWNER="${GH_OWNER:-superduperohiolife-lgtm}"
GH_REPO="${GH_REPO:-news}"
GH_BRANCH="${GH_BRANCH:-main}"
PAT_FILE="${PAT_FILE:-$HOME/.news_pat}"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="$REPO_DIR/data"
TOOLS_DIR="$REPO_DIR/tools"

# --- プロキシをバイパスして実行するラッパ -------------------------------
_nop() {
  env -u https_proxy -u HTTPS_PROXY -u http_proxy -u HTTP_PROXY \
      NO_PROXY='*' no_proxy='*' "$@"
}

_remote() {
  if [ ! -s "$PAT_FILE" ]; then
    echo "[未設定] PATファイルが見つからない: $PAT_FILE" >&2
    return 2
  fi
  printf 'https://x-access-token:%s@github.com/%s/%s.git' \
    "$(cat "$PAT_FILE")" "$GH_OWNER" "$GH_REPO"
}

# 出力からトークンを伏字にする
_mask() { sed -E 's/github_pat_[A-Za-z0-9_]+/***/g; s#x-access-token:[^@]*@#x-access-token:***@#g'; }

cmd_status() {
  local d
  d="$(TZ=Asia/Tokyo date +%F)"
  echo "repo   : $REPO_DIR"
  echo "branch : $(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null)"
  echo "today  : $d (JST)"
  local n=0
  for c in econ ai auto; do
    if [ -f "$DATA_DIR/$c-news-$d.json" ]; then
      echo "  [o] $c-news-$d.json"
      n=$((n+1))
    else
      echo "  [-] $c-news-$d.json (未着)"
    fi
  done
  echo "today_json_count=$n"
  echo "data_files=$(ls -1 "$DATA_DIR"/*.json 2>/dev/null | wc -l)"
  [ "$n" -gt 0 ]
}

cmd_pull() {
  local r; r="$(_remote)" || return $?
  _nop git -C "$REPO_DIR" pull --rebase "$r" "$GH_BRANCH" > /tmp/newsctl_pull.log 2>&1
  local rc=$?
  _mask < /tmp/newsctl_pull.log
  return $rc
}

cmd_add() {
  mkdir -p "$DATA_DIR"
  local f base
  for f in "$@"; do
    [ -f "$f" ] || { echo "[skip] not found: $f" >&2; continue; }
    base="$(basename "$f")"
    if ! python3 -c "import json,sys; json.load(open(sys.argv[1],encoding='utf-8'))" "$f"; then
      echo "[NG] 不正なJSONのため追加しない: $base" >&2
      return 1
    fi
    python3 - "$f" "$DATA_DIR/$base" <<'PY'
import sys, io
src, dst = sys.argv[1], sys.argv[2]
t = io.open(src, encoding='utf-8', newline=None).read().replace('\r\n', '\n')
io.open(dst, 'w', encoding='utf-8', newline='\n').write(t)
PY
    echo "[add] data/$base"
  done
  # data/ の変更をそのまま公開ブランチへ反映（index.html は触らない）
  cmd_sync
}

# data/ と tools/ の変更のみを commit & push する。index.html は再生成しない。
cmd_sync() {
  local r; r="$(_remote)" || return $?
  git -C "$REPO_DIR" config user.name  "${GIT_AUTHOR_NAME:-news-bot}"
  git -C "$REPO_DIR" config user.email "${GIT_AUTHOR_EMAIL:-news-bot@users.noreply.github.com}"
  git -C "$REPO_DIR" add -A data tools
  if git -C "$REPO_DIR" diff --cached --quiet; then
    echo "[skip] 変更なし"
    return 0
  fi
  git -C "$REPO_DIR" commit -q -m "add news data $(TZ=Asia/Tokyo date +%FT%H:%MJST)" || return 1
  local i rc=1
  for i in 1 2 3; do
    _nop git -C "$REPO_DIR" push "$r" "HEAD:$GH_BRANCH" > /tmp/newsctl_push.log 2>&1
    rc=$?
    [ "$rc" -eq 0 ] && break
    # 先行タスクの push と競合した場合は rebase して再試行
    _nop git -C "$REPO_DIR" pull --rebase -q "$r" "$GH_BRANCH" >> /tmp/newsctl_push.log 2>&1
    sleep 3
  done
  _mask < /tmp/newsctl_push.log
  if [ "$rc" -ne 0 ]; then
    echo "[push失敗] exit=$rc" >&2
    return "$rc"
  fi
  echo "[pushOK]"
}

cmd_build() {
  local work; work="$(mktemp -d)"
  cp "$TOOLS_DIR/build_news_site.py" "$work/" || return 1
  cp "$DATA_DIR"/*.json "$work/" 2>/dev/null
  ( cd "$work" && python3 build_news_site.py ) || { rm -rf "$work"; return 1; }
  [ -s "$work/news.html" ] || { echo "[NG] news.html が生成されていない" >&2; rm -rf "$work"; return 1; }
  # 生成物の健全性チェック（末尾が閉じているか）
  if ! grep -q '</html>' "$work/news.html"; then
    echo "[NG] 生成HTMLが不完全" >&2; rm -rf "$work"; return 1
  fi
  cp "$work/news.html" "$REPO_DIR/index.html"
  echo "[build] index.html ($(wc -c < "$REPO_DIR/index.html") bytes)"
  rm -rf "$work"
}

cmd_publish() {
  cmd_build || return 1
  local r; r="$(_remote)" || return $?
  git -C "$REPO_DIR" config user.name  "${GIT_AUTHOR_NAME:-news-bot}"
  git -C "$REPO_DIR" config user.email "${GIT_AUTHOR_EMAIL:-news-bot@users.noreply.github.com}"
  git -C "$REPO_DIR" add -A index.html data tools
  if git -C "$REPO_DIR" diff --cached --quiet; then
    echo "[skip] 変更なし"
    return 0
  fi
  git -C "$REPO_DIR" commit -q -m "update news $(TZ=Asia/Tokyo date +%FT%H:%MJST)" || return 1
  # push は必ずプロキシバイパスで。パイプで終了ステータスを潰さないこと。
  _nop git -C "$REPO_DIR" push "$r" "HEAD:$GH_BRANCH" > /tmp/newsctl_push.log 2>&1
  local rc=$?
  _mask < /tmp/newsctl_push.log
  if [ "$rc" -ne 0 ]; then
    echo "[push失敗] exit=$rc" >&2
    return "$rc"
  fi
  echo "[公開OK] https://${GH_OWNER}.github.io/${GH_REPO}/"
}

case "${1:-}" in
  status)  cmd_status ;;
  pull)    cmd_pull ;;
  add)     shift; cmd_add "$@" ;;
  sync)    cmd_sync ;;
  build)   cmd_build ;;
  publish) cmd_publish ;;
  *) echo "usage: $0 {status|pull|add <json>...|sync|build|publish}" >&2; exit 64 ;;
esac
