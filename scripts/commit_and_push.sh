#!/usr/bin/env bash
# 更新された JSON / 生成されたワークフロー YAML をリポジトリへコミット & プッシュする。
# 同時刻に複数のワークフローが走ってもよいよう、失敗時は rebase して指数バックオフで再試行する。
set -euo pipefail

MESSAGE="${1:-chore: update generated data}"
BRANCH="${GITHUB_REF_NAME:-$(git rev-parse --abbrev-ref HEAD)}"

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

git add -A data config .github/workflows

if git diff --cached --quiet; then
  echo "コミット対象の変更はありません。"
  exit 0
fi

git commit -m "$MESSAGE"

for attempt in 1 2 3 4 5; do
  if git push -u origin "HEAD:${BRANCH}"; then
    echo "プッシュに成功しました (試行 ${attempt} 回目)"
    exit 0
  fi
  wait_sec=$((2 ** attempt))
  echo "プッシュに失敗しました。${wait_sec} 秒後に再試行します..." >&2
  sleep "$wait_sec"
  git pull --rebase origin "$BRANCH" || true
done

echo "プッシュに失敗しました。" >&2
exit 1
