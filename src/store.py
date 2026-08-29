"""``data/queue.json`` と ``data/post_history.json`` の読み書き。

GitHub Actions が実行のたびにコミット・プッシュする「状態ファイル」を扱う層。
サーバーレス設計のため、ここが唯一の永続化先になる。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .utils import HISTORY_FILE, QUEUE_FILE, iso, load_json, now_jst, parse_iso, save_json

EMPTY_QUEUE: dict[str, Any] = {"generated_at": None, "target_date": None, "accounts": {}}
EMPTY_HISTORY: dict[str, Any] = {"updated_at": None, "posts": []}


# --- queue.json -----------------------------------------------------------
def load_queue() -> dict[str, Any]:
    """配信キューを読み込む。"""
    queue = load_json(QUEUE_FILE, default=None)
    if not isinstance(queue, dict):
        return dict(EMPTY_QUEUE)
    queue.setdefault("accounts", {})
    if not isinstance(queue["accounts"], dict):
        queue["accounts"] = {}
    return queue


def save_queue(queue: dict[str, Any]) -> None:
    """配信キューを保存する。"""
    save_json(QUEUE_FILE, queue)


def queue_items(queue: dict[str, Any], account_id: str | None = None) -> list[dict[str, Any]]:
    """キューの投稿予定を平坦なリストとして返す。"""
    accounts = queue.get("accounts", {})
    if account_id is not None:
        return list(accounts.get(account_id, []))
    items: list[dict[str, Any]] = []
    for entries in accounts.values():
        items.extend(entries)
    return items


def due_items(queue: dict[str, Any], reference: datetime | None = None) -> list[tuple[str, dict[str, Any]]]:
    """予約時刻を過ぎた未配信アイテムを、時刻の早い順に返す。"""
    reference = reference or now_jst()
    due: list[tuple[str, dict[str, Any]]] = []
    for account_id, entries in queue.get("accounts", {}).items():
        for item in entries:
            if item.get("status") != "pending":
                continue
            scheduled_at = parse_iso(item.get("scheduled_at"))
            if scheduled_at and scheduled_at <= reference:
                due.append((account_id, item))
    due.sort(key=lambda pair: pair[1].get("scheduled_at") or "")
    return due


# --- post_history.json ----------------------------------------------------
def load_history() -> dict[str, Any]:
    """投稿履歴を読み込む。"""
    history = load_json(HISTORY_FILE, default=None)
    if not isinstance(history, dict):
        return dict(EMPTY_HISTORY)
    history.setdefault("posts", [])
    if not isinstance(history["posts"], list):
        history["posts"] = []
    return history


def save_history(history: dict[str, Any]) -> None:
    """投稿履歴を保存する。"""
    history["updated_at"] = iso(now_jst())
    save_json(HISTORY_FILE, history)


def append_history(history: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    """履歴を 1 件追加する（同じ post_id は上書き）。"""
    posts = history.setdefault("posts", [])
    for i, existing in enumerate(posts):
        if existing.get("post_id") and existing.get("post_id") == record.get("post_id"):
            posts[i] = {**existing, **record}
            return history
    posts.append(record)
    return history


def recent_posts(
    history: dict[str, Any],
    account_id: str,
    days: int = 7,
    reference: datetime | None = None,
    exclude_reposts: bool = True,
) -> list[dict[str, Any]]:
    """指定アカウントの直近 ``days`` 日分の投稿履歴を返す。"""
    reference = reference or now_jst()
    since = reference - timedelta(days=days)
    results: list[dict[str, Any]] = []
    for post in history.get("posts", []):
        if post.get("account_id") != account_id:
            continue
        if exclude_reposts and post.get("source") == "repost":
            continue
        published_at = parse_iso(post.get("published_at"))
        if published_at and since <= published_at <= reference:
            results.append(post)
    return results


def likes_of(post: dict[str, Any]) -> int:
    """履歴レコードからいいね数を取り出す。"""
    metrics = post.get("metrics") or {}
    try:
        return int(metrics.get("likes", 0) or 0)
    except (TypeError, ValueError):
        return 0
