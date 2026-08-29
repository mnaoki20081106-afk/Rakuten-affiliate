"""【当日の配信処理】予約時刻に起動し、該当する投稿を即時に Threads へ送信する。

動的生成された cron によってピンポイントで起動されるため、待機（sleep）は行わない。
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from src import config as cfg
from src.config import find_account, load_accounts, load_settings
from src.scheduler import parse_iso, to_jst
from src.storage import read_json, write_json
from src.threads_api import ThreadsAPIError, ThreadsClient, build_pr_reply, is_dry_run
from src.workflow_generator import iter_queue_posts

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3


def load_queue(path: Path) -> dict[str, Any]:
    queue = read_json(path, {}) or {}
    queue.setdefault("accounts", {})
    return queue


def load_history(path: Path) -> dict[str, Any]:
    history = read_json(path, {"posts": []}) or {}
    history.setdefault("posts", [])
    return history


def select_due_posts(
    queue: dict[str, Any],
    now_utc: datetime,
    window_before_minutes: int = 5,
    window_after_minutes: int = 60,
    account_id: str = "",
    post_id: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """起動時刻に対応する未送信の投稿を返す。

    GitHub Actions の cron は数分遅れて起動することがあるため、
    「起動時刻の少し先」から「一定時間前まで」を配信対象の窓とする。

    戻り値: (配信対象, 期限切れ)
    """
    lower = now_utc - timedelta(minutes=max(0, int(window_after_minutes)))
    upper = now_utc + timedelta(minutes=max(0, int(window_before_minutes)))

    due: list[dict[str, Any]] = []
    expired: list[dict[str, Any]] = []
    for post in iter_queue_posts(queue):
        if post_id and post.get("id") != post_id:
            continue
        if account_id and post.get("account_id") != account_id:
            continue
        if post.get("status") not in (None, "", "pending"):
            continue
        raw = post.get("scheduled_at_utc")
        if not raw:
            continue
        try:
            scheduled = parse_iso(raw)
        except (ValueError, TypeError):
            logger.warning("配信時刻を解釈できません post=%s value=%s", post.get("id"), raw)
            continue
        if scheduled > upper:
            continue
        if scheduled < lower:
            expired.append(post)
            continue
        due.append(post)

    due.sort(key=lambda p: p.get("scheduled_at_utc", ""))
    return due, expired


def publish_post(client: ThreadsClient, post: dict[str, Any]) -> dict[str, Any]:
    """親投稿を送信し、直後にアフィリエイト URL + ※PR の子投稿を送る。"""
    body = (post.get("body") or "").strip()
    if not body:
        raise ThreadsAPIError("投稿本文が空です")

    parent_id = client.post_text(body)
    logger.info("親投稿を送信しました post=%s media_id=%s", post.get("id"), parent_id)

    reply_id = ""
    reply_error = ""
    affiliate_url = post.get("affiliate_url") or (post.get("item") or {}).get("affiliate_url", "")
    reply_text = build_pr_reply(affiliate_url, post.get("pr_text") or "※PR")
    try:
        reply_id = client.post_text(reply_text, reply_to_id=parent_id)
        logger.info("PR リプライを送信しました post=%s media_id=%s", post.get("id"), reply_id)
    except Exception as exc:  # noqa: BLE001 - 親投稿は成功しているため送信済みとして扱う
        reply_error = str(exc)
        logger.error("PR リプライの送信に失敗しました post=%s: %s", post.get("id"), exc)

    return {
        "media_id": parent_id,
        "reply_media_id": reply_id,
        "reply_text": reply_text,
        "reply_error": reply_error,
    }


def history_entry(post: dict[str, Any], result: dict[str, Any], published_at: datetime) -> dict[str, Any]:
    return {
        "post_id": post.get("id", ""),
        "account_id": post.get("account_id", ""),
        "account_name": post.get("account_name", ""),
        "media_id": result.get("media_id", ""),
        "reply_media_id": result.get("reply_media_id", ""),
        "body": post.get("body", ""),
        "affiliate_url": post.get("affiliate_url", ""),
        "pr_text": post.get("pr_text", "※PR"),
        "worry": post.get("worry", ""),
        "probability": post.get("probability"),
        "item": post.get("item", {}),
        "scheduled_at_jst": post.get("scheduled_at_jst", ""),
        "published_at": published_at.astimezone(timezone.utc).isoformat(),
        "published_at_jst": to_jst(published_at).isoformat(),
        "is_repost": bool(post.get("is_repost")),
        "repost_of": post.get("repost_of", ""),
        "likes": 0,
        "insights": {},
        "insights_updated_at": "",
    }


def run(
    now_utc: datetime | None = None,
    data_dir: Path = cfg.DATA_DIR,
    accounts_file: Path = cfg.ACCOUNTS_FILE,
    settings_file: Path = cfg.SETTINGS_FILE,
    account_id: str = "",
    post_id: str = "",
    dry_run: bool | None = None,
) -> dict[str, Any]:
    now_utc = now_utc or datetime.now(tz=timezone.utc)
    settings = load_settings(settings_file)
    accounts = load_accounts(accounts_file)
    publisher_cfg = settings.get("publisher", {})

    queue_path = Path(data_dir) / cfg.QUEUE_FILE.name
    history_path = Path(data_dir) / cfg.POST_HISTORY_FILE.name
    queue = load_queue(queue_path)
    history = load_history(history_path)

    due, expired = select_due_posts(
        queue,
        now_utc,
        window_before_minutes=int(publisher_cfg.get("window_before_minutes", 5)),
        window_after_minutes=int(publisher_cfg.get("window_after_minutes", 60)),
        account_id=account_id,
        post_id=post_id,
    )
    logger.info(
        "起動時刻 %s / 配信対象 %s 件 / 期限切れ %s 件",
        to_jst(now_utc).strftime("%Y-%m-%d %H:%M JST"),
        len(due),
        len(expired),
    )

    summary: dict[str, Any] = {
        "executed_at": now_utc.isoformat(),
        "due_count": len(due),
        "sent": [],
        "failed": [],
        "expired": [post.get("id", "") for post in expired],
    }

    for post in expired:
        post["status"] = "expired"
        post["error"] = "配信可能な時間帯を過ぎたためスキップしました"

    clients: dict[str, ThreadsClient] = {}
    # 同一時刻に複数ある場合は予約時刻順に「順次」送信する
    for post in due:
        acc_id = post.get("account_id", "")
        account = find_account(accounts, acc_id)
        try:
            if acc_id not in clients:
                if account is None:
                    raise ThreadsAPIError(f"アカウント定義が見つかりません: {acc_id}")
                token = account.resolve_token()
                if not token and not (is_dry_run() if dry_run is None else dry_run):
                    raise ThreadsAPIError(
                        f"Threads トークンが未設定です。GitHub Secrets に "
                        f"{account.token_secret_name} を登録してください"
                    )
                clients[acc_id] = ThreadsClient(
                    access_token=token,
                    user_id=account.threads_user_id,
                    dry_run=is_dry_run() if dry_run is None else dry_run,
                )
            result = publish_post(clients[acc_id], post)
        except Exception as exc:  # noqa: BLE001 - 1 投稿の失敗で他を止めない
            post["attempts"] = int(post.get("attempts", 0)) + 1
            post["error"] = str(exc)
            if post["attempts"] >= MAX_ATTEMPTS:
                post["status"] = "failed"
            logger.error("投稿の送信に失敗しました post=%s: %s", post.get("id"), exc)
            summary["failed"].append({"post_id": post.get("id", ""), "error": str(exc)})
            continue

        post["status"] = "sent"
        post["media_id"] = result["media_id"]
        post["reply_media_id"] = result["reply_media_id"]
        post["published_at"] = now_utc.isoformat()
        post["attempts"] = int(post.get("attempts", 0)) + 1
        post["error"] = result.get("reply_error", "")
        history["posts"].append(history_entry(post, result, now_utc))
        summary["sent"].append({"post_id": post.get("id", ""), "media_id": result["media_id"]})

    write_json(queue_path, queue)
    write_json(history_path, history)
    return summary


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="予約時刻に対応する投稿を Threads へ配信する")
    parser.add_argument("--now", default="", help="起動時刻の上書き (ISO8601, 既定は現在時刻)")
    parser.add_argument("--account", default="", help="対象アカウント ID")
    parser.add_argument("--post-id", default="", help="対象の投稿 ID")
    parser.add_argument("--data-dir", default=str(cfg.DATA_DIR))
    parser.add_argument("--accounts-file", default=str(cfg.ACCOUNTS_FILE))
    parser.add_argument("--settings-file", default=str(cfg.SETTINGS_FILE))
    parser.add_argument("--dry-run", action="store_true", help="Threads API を呼ばずに動作確認する")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    summary = run(
        now_utc=parse_iso(args.now) if args.now else None,
        data_dir=Path(args.data_dir),
        accounts_file=Path(args.accounts_file),
        settings_file=Path(args.settings_file),
        account_id=args.account,
        post_id=args.post_id,
        dry_run=True if args.dry_run else None,
    )
    logger.info(
        "配信完了: 送信 %s 件 / 失敗 %s 件 / 期限切れ %s 件",
        len(summary["sent"]),
        len(summary["failed"]),
        len(summary["expired"]),
    )
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
