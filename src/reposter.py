"""【再投稿機能】過去バズの再利用。

毎週 月・水・金 の JST 19:00（UTC 10:00）に GitHub Actions から起動される想定。
``data/post_history.json`` を集計し、過去 1 週間の投稿履歴から
各アカウントにつき「いいね数」が高かった上位 3 件を抽出して、
その日の「8 件目の投稿」として元の本文と全く同じ内容で再投稿する
（親投稿 + PR リプライ）。
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from .accounts import load_accounts, resolve_threads_token
from .store import append_history, likes_of, load_history, recent_posts, save_history
from .threads_api import ThreadsAPIError, ThreadsClient, build_pr_reply
from .utils import env, iso, now_jst, setup_logging

logger = setup_logging("reposter")

TOP_N = 3
LOOKBACK_DAYS = 7


def build_client(account: dict[str, Any]) -> ThreadsClient:
    """アカウント設定から Threads クライアントを作る。"""
    token = resolve_threads_token(account)
    if not token:
        raise ThreadsAPIError(
            f"[{account['id']}] Threads のアクセストークンが未設定です。"
        )
    return ThreadsClient(token, user_id=(account.get("threads") or {}).get("user_id", ""))


def refresh_metrics(
    client: ThreadsClient, posts: list[dict[str, Any]], account_id: str
) -> None:
    """対象投稿のいいね数などを Threads API から取り直して履歴を更新する。"""
    for post in posts:
        post_id = post.get("post_id")
        if not post_id:
            continue
        try:
            metrics = client.get_post_insights(post_id)
        except ThreadsAPIError as exc:
            logger.warning("[%s] インサイト取得に失敗しました post_id=%s: %s", account_id, post_id, exc)
            continue
        post["metrics"] = metrics
        post["metrics_updated_at"] = iso(now_jst())


def pick_top_posts(posts: list[dict[str, Any]], top_n: int = TOP_N) -> list[dict[str, Any]]:
    """いいね数の多い順に上位 ``top_n`` 件を返す（いいね 0 件は除外）。"""
    ranked = sorted(posts, key=likes_of, reverse=True)
    return [post for post in ranked if likes_of(post) > 0][:top_n]


def repost(
    client: ThreadsClient, account: dict[str, Any], post: dict[str, Any], history: dict[str, Any]
) -> bool:
    """1 件を元の本文のまま再投稿する（親投稿 + PR リプライ）。"""
    body = post.get("body", "")
    if not body.strip():
        logger.warning("[%s] 本文が空のため再投稿をスキップします post_id=%s", account["id"], post.get("post_id"))
        return False

    reply_text = build_pr_reply(post.get("affiliate_url", ""), env("PR_REPLY_TEMPLATE"))
    try:
        result = client.publish_with_pr_reply(body, reply_text)
    except ThreadsAPIError as exc:
        logger.error("[%s] 再投稿に失敗しました post_id=%s: %s", account["id"], post.get("post_id"), exc)
        return False

    now = now_jst()
    append_history(
        history,
        {
            "id": f"{account['id']}-repost-{now.strftime('%Y%m%d%H%M')}-{post.get('post_id')}",
            "account_id": account["id"],
            "account_name": account.get("name", ""),
            "post_id": result["post_id"],
            "reply_id": result.get("reply_id", ""),
            "body": body,
            "affiliate_url": post.get("affiliate_url", ""),
            "product": post.get("product", {}),
            "worry": post.get("worry", ""),
            "probability": post.get("probability"),
            "slot_type": "repost",
            "scheduled_at": iso(now),
            "published_at": iso(now),
            "source": "repost",
            "origin_post_id": post.get("post_id", ""),
            "origin_likes": likes_of(post),
            "metrics": {},
            "metrics_updated_at": None,
        },
    )
    logger.info(
        "[%s] 再投稿しました（元いいね %s件）origin=%s new=%s",
        account["id"],
        likes_of(post),
        post.get("post_id"),
        result["post_id"],
    )
    return True


def process_account(account: dict[str, Any], history: dict[str, Any], dry_run: bool = False) -> int:
    """1 アカウント分の集計と再投稿を行い、再投稿できた件数を返す。"""
    logger.info("=== [%s] %s の再投稿処理 ===", account["id"], account["name"])
    client = build_client(account)

    candidates = recent_posts(history, account["id"], days=LOOKBACK_DAYS, exclude_reposts=True)
    if not candidates:
        logger.info("[%s] 過去 %s 日間の投稿履歴がありません。", account["id"], LOOKBACK_DAYS)
        return 0

    refresh_metrics(client, candidates, account["id"])
    top_posts = pick_top_posts(candidates)
    if not top_posts:
        logger.info("[%s] いいねが付いた投稿がないため再投稿をスキップします。", account["id"])
        return 0

    logger.info(
        "[%s] 再投稿候補: %s",
        account["id"],
        ", ".join(f"{p.get('post_id')}({likes_of(p)}いいね)" for p in top_posts),
    )
    if dry_run:
        return 0

    return sum(1 for post in top_posts if repost(client, account, post, history))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="過去 1 週間でいいねが多かった投稿を再投稿する")
    parser.add_argument("--account", help="指定した ID のアカウントだけ処理する")
    parser.add_argument("--dry-run", action="store_true", help="送信せずに候補だけ表示する")
    args = parser.parse_args(argv)

    accounts = load_accounts(enabled_only=True)
    if args.account:
        accounts = [a for a in accounts if a["id"] == args.account]
    if not accounts:
        logger.error("有効なアカウントがありません。config/accounts.json を確認してください。")
        return 1

    history = load_history()
    total = 0
    failures = 0
    for account in accounts:
        try:
            total += process_account(account, history, dry_run=args.dry_run)
        except ThreadsAPIError as exc:
            failures += 1
            logger.error("[%s] 再投稿処理に失敗しました: %s", account["id"], exc)

    save_history(history)
    logger.info("再投稿を %s 件実行しました。", total)
    return 1 if failures and total == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
