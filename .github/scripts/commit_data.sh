#!/usr/bin/env bash
# data/ と config/ の差分をコミットしてプッシュする共通スクリプト。
# 3 つのワークフローの最後で必ず呼び出し、状態ファイルをリポジトリに保存する。
set -euo pipefail

MESSAGE="${1:-chore: update data}"
BRANCH="${GITHUB_REF_NAME:-main}"

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

if [ -z "$(git status --porcelain data config)" ]; then
  echo "変更がないためコミットをスキップします。"
  exit 0
fi

git add data config
git commit -m "${MESSAGE} [skip ci]"

# 他のワークフローが同時にプッシュした場合に備えてリベース再試行する
for attempt in 1 2 3 4 5; do
  if git pull --rebase --autostash origin "${BRANCH}" && git push origin "HEAD:${BRANCH}"; then
    echo "プッシュしました（${attempt} 回目）。"
    exit 0
  fi
  sleep $((2 ** attempt))
done

echo "プッシュに失敗しました。" >&2
exit 1
