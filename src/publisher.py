"""【当日の配信処理】親投稿と PR リプライの実行。

毎日 JST 7:00〜23:00 の間、15 分間隔で GitHub Actions から起動される想定。
``data/queue.json`` を読み、予約時刻を過ぎた未配信データを Threads へ送信する。

送信の流れ:
1. 親投稿（Claude が生成した本文）を送信する
2. 返ってきた投稿 ID を ``reply_to_id`` に指定し、コメント欄へ
   「楽天アフィリエイト URL + ※PR」の子投稿を送信する
3. ``data/queue.json`` のステータスを更新し、``data/post_history.json`` に記録する
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from .accounts import load_accounts, resolve_threads_token
from .store import (
    append_history,
    due_items,
    load_history,
    load_queue,
    save_history,
    save_queue,
)
from .threads_api import ThreadsAPIError, ThreadsClient, build_pr_reply
from .utils import env, iso, now_jst, setup_logging

logger = setup_logging("publisher")

# 連続失敗時に諦める回数
MAX_ATTEMPTS = 3


def build_client(account: dict[str, Any]) -> ThreadsClient:
    """アカウント設定から Threads クライアントを作る。"""
    token = resolve_threads_token(account)
    if not token:
        raise ThreadsAPIError(
            f"[{account['id']}] Threads のアクセストークンが未設定です。"
            " 管理画面で登録するか GitHub Secrets に追加してください。"
        )
    return ThreadsClient(token, user_id=(account.get("threads") or {}).get("user_id", ""))


def history_record(account: dict[str, Any], item: dict[str, Any], result: dict[str, str]) -> dict[str, Any]:
    """投稿履歴に残すレコードを組み立てる。"""
    product = item.get("product") or {}
    return {
        "id": item.get("id"),
        "account_id": account["id"],
        "account_name": account.get("name", ""),
        "post_id": result["post_id"],
        "reply_id": result.get("reply_id", ""),
        "body": item.get("body", ""),
        "affiliate_url": item.get("affiliate_url", ""),
        "product": {
            "name": product.get("name", ""),
            "rank": product.get("rank"),
            "price": product.get("price"),
            "item_code": product.get("item_code", ""),
        },
        "worry": item.get("worry", ""),
        "probability": item.get("probability"),
        "slot_type": item.get("slot_type", ""),
        "scheduled_at": item.get("scheduled_at"),
        "published_at": iso(now_jst()),
        "source": item.get("source", "queue"),
        "origin_post_id": item.get("origin_post_id", ""),
        "metrics": {},
        "metrics_updated_at": None,
    }


def publish_item(
    client: ThreadsClient, account: dict[str, Any], item: dict[str, Any], history: dict[str, Any]
) -> bool:
    """1 件の投稿（親 + PR リプライ）を実行し、ステータスを更新する。"""
    item["attempts"] = int(item.get("attempts", 0)) + 1
    reply_text = build_pr_reply(
        item.get("affiliate_url", ""), env("PR_REPLY_TEMPLATE")
    )
    try:
        result = client.publish_with_pr_reply(item.get("body", ""), reply_text)
    except ThreadsAPIError as exc:
        item["error"] = str(exc)
        item["status"] = "failed" if item["attempts"] >= MAX_ATTEMPTS else "pending"
        logger.error(
            "[%s] 配信に失敗しました id=%s (%s回目): %s",
            account["id"],
            item.get("id"),
            item["attempts"],
            exc,
        )
        return False

    item["status"] = "published"
    item["post_id"] = result["post_id"]
    item["reply_id"] = result.get("reply_id", "")
    item["published_at"] = iso(now_jst())
    item["error"] = result.get("reply_error", "")
    append_history(history, history_record(account, item, result))
    logger.info(
        "[%s] 配信完了 id=%s post_id=%s reply_id=%s",
        account["id"],
        item.get("id"),
        result["post_id"],
        result.get("reply_id") or "(失敗)",
    )
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="予約時刻を過ぎた投稿を Threads へ配信する")
    parser.add_argument("--account", help="指定した ID のアカウントだけ配信する")
    parser.add_argument("--limit", type=int, default=0, help="1 回の実行で配信する最大件数（0 は無制限）")
    parser.add_argument("--dry-run", action="store_true", help="送信せずに対象だけ表示する")
    args = parser.parse_args(argv)

    queue = load_queue()
    pending = due_items(queue)
    if args.account:
        pending = [(aid, item) for aid, item in pending if aid == args.account]
    if args.limit > 0:
        pending = pending[: args.limit]

    if not pending:
        logger.info("配信対象はありません（現在時刻: %s）。", iso(now_jst()))
        return 0

    logger.info("配信対象 %s 件を処理します。", len(pending))
    if args.dry_run:
        for account_id, item in pending:
            logger.info("[dry-run] %s %s %s", account_id, item.get("scheduled_at"), item.get("id"))
        return 0

    accounts = {account["id"]: account for account in load_accounts()}
    history = load_history()
    clients: dict[str, ThreadsClient] = {}
    published = 0
    failed = 0

    for account_id, item in pending:
        account = accounts.get(account_id)
        if account is None:
            item["status"] = "skipped"
            item["error"] = "アカウント設定が見つかりません。"
            logger.warning("アカウント %s の設定が見つからないためスキップします。", account_id)
            continue
        if not account.get("enabled", True):
            item["status"] = "skipped"
            item["error"] = "アカウントが無効化されています。"
            continue

        if account_id not in clients:
            try:
                clients[account_id] = build_client(account)
            except ThreadsAPIError as exc:
                failed += 1
                item["error"] = str(exc)
                logger.error("%s", exc)
                continue

        if publish_item(clients[account_id], account, item, history):
            published += 1
        else:
            failed += 1

    save_queue(queue)
    save_history(history)
    logger.info("配信結果: 成功 %s 件 / 失敗 %s 件", published, failed)
    return 1 if failed and published == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
