"""【再投稿機能】過去 1 週間でいいね数が多かった投稿を「8 件目の投稿」として再投稿する。

毎週 月・水・金 の JST 19:30 (UTC 10:30) に起動する。
過去 1 週間の上位 3 件を、月曜=1 位 / 水曜=2 位 / 金曜=3 位 の順で 1 日 1 件ずつ再投稿する。
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from src import config as cfg
from src.config import Account, load_accounts, load_settings
from src.publisher import load_history
from src.scheduler import parse_iso, to_jst
from src.storage import write_json
from src.threads_api import ThreadsClient, build_pr_reply, is_dry_run

logger = logging.getLogger(__name__)


def _published_at(entry: dict[str, Any]) -> datetime | None:
    try:
        return parse_iso(entry.get("published_at", ""))
    except (ValueError, TypeError):
        return None


def refresh_likes(
    history: dict[str, Any],
    accounts: Sequence[Account],
    since: datetime,
    dry_run: bool = False,
) -> int:
    """直近の投稿のいいね数を Threads インサイトで更新する。"""
    by_account = {a.id: a for a in accounts}
    clients: dict[str, ThreadsClient] = {}
    updated = 0
    for entry in history.get("posts", []) or []:
        published = _published_at(entry)
        if not published or published < since:
            continue
        media_id = entry.get("media_id", "")
        account = by_account.get(entry.get("account_id", ""))
        if not media_id or account is None:
            continue
        try:
            if account.id not in clients:
                clients[account.id] = ThreadsClient(
                    access_token=account.resolve_token(),
                    user_id=account.threads_user_id,
                    dry_run=dry_run,
                )
            insights = clients[account.id].get_insights(media_id)
        except Exception as exc:  # noqa: BLE001 - 集計失敗で処理全体を止めない
            logger.warning("インサイト取得に失敗しました media_id=%s: %s", media_id, exc)
            continue
        if insights:
            entry["insights"] = insights
            entry["likes"] = int(insights.get("likes", entry.get("likes", 0)) or 0)
            entry["insights_updated_at"] = datetime.now(tz=timezone.utc).isoformat()
            updated += 1
    return updated


def recent_reposted_media_ids(
    history: dict[str, Any], account_id: str, since: datetime
) -> set[str]:
    """クールダウン期間内に再投稿済みの元投稿 media_id 集合。"""
    ids: set[str] = set()
    for entry in history.get("posts", []) or []:
        if not entry.get("is_repost") or entry.get("account_id") != account_id:
            continue
        published = _published_at(entry)
        if published and published >= since and entry.get("repost_of"):
            ids.add(str(entry["repost_of"]))
    return ids


def top_posts(
    history: dict[str, Any],
    account_id: str,
    since: datetime,
    top_n: int = 3,
) -> list[dict[str, Any]]:
    """過去一定期間のいいね数上位の投稿を返す（再投稿分は除く）。"""
    candidates = []
    for entry in history.get("posts", []) or []:
        if entry.get("account_id") != account_id or entry.get("is_repost"):
            continue
        if not entry.get("media_id"):
            continue
        published = _published_at(entry)
        if not published or published < since:
            continue
        if not (entry.get("body") or "").strip():
            continue
        candidates.append(entry)
    candidates.sort(
        key=lambda e: (int(e.get("likes", 0) or 0), e.get("published_at", "")), reverse=True
    )
    return candidates[: max(1, int(top_n))]


def pick_for_weekday(
    candidates: Sequence[dict[str, Any]],
    weekday: int,
    weekday_rank_map: dict[str, int],
    excluded_media_ids: set[str] | None = None,
) -> dict[str, Any] | None:
    """曜日に対応する順位の投稿を選ぶ（月=1 位 / 水=2 位 / 金=3 位）。

    順位は「上位 3 件」の並びそのものを指す。既に再投稿済み（クールダウン中）の
    ものが割り当たった場合のみ、残りの中で最上位のものへ繰り上げる。
    """
    excluded_media_ids = excluded_media_ids or set()
    available = [c for c in candidates if c.get("media_id") not in excluded_media_ids]
    if not available:
        return None

    index = weekday_rank_map.get(str(weekday), 0)
    try:
        index = int(index)
    except (TypeError, ValueError):
        index = 0
    if 0 <= index < len(candidates):
        target = candidates[index]
        if target.get("media_id") not in excluded_media_ids:
            return target
    return available[0]


def repost_entry(
    client: ThreadsClient, entry: dict[str, Any], pr_text: str = "※PR"
) -> dict[str, Any]:
    """元の本文とまったく同じ内容で親投稿＋PR リプライを再送信する。"""
    body = entry.get("body", "")
    parent_id = client.post_text(body)
    reply_id = ""
    reply_error = ""
    reply_text = build_pr_reply(entry.get("affiliate_url", ""), entry.get("pr_text") or pr_text)
    try:
        reply_id = client.post_text(reply_text, reply_to_id=parent_id)
    except Exception as exc:  # noqa: BLE001
        reply_error = str(exc)
        logger.error("再投稿の PR リプライに失敗しました media_id=%s: %s", parent_id, exc)
    return {"media_id": parent_id, "reply_media_id": reply_id, "reply_error": reply_error}


def run(
    now_utc: datetime | None = None,
    data_dir: Path = cfg.DATA_DIR,
    accounts_file: Path = cfg.ACCOUNTS_FILE,
    settings_file: Path = cfg.SETTINGS_FILE,
    account_id: str = "",
    rank_index: int | None = None,
    skip_insights: bool = False,
    dry_run: bool | None = None,
) -> dict[str, Any]:
    now_utc = now_utc or datetime.now(tz=timezone.utc)
    dry_run = is_dry_run() if dry_run is None else dry_run
    settings = load_settings(settings_file)
    repost_cfg = settings.get("repost", {})
    pr_text = settings.get("pr_text", "※PR")

    accounts = [a for a in load_accounts(accounts_file) if a.enabled]
    if account_id:
        accounts = [a for a in accounts if a.id == account_id]

    history_path = Path(data_dir) / cfg.POST_HISTORY_FILE.name
    history = load_history(history_path)

    lookback = int(repost_cfg.get("lookback_days", 7))
    cooldown = int(repost_cfg.get("cooldown_days", 14))
    since = now_utc - timedelta(days=lookback)
    cooldown_since = now_utc - timedelta(days=cooldown)
    weekday = to_jst(now_utc).weekday()  # JST の曜日で判定する

    if not skip_insights:
        updated = refresh_likes(history, accounts, since, dry_run=dry_run)
        logger.info("いいね数を更新しました: %s 件", updated)

    summary: dict[str, Any] = {
        "executed_at": now_utc.isoformat(),
        "weekday_jst": weekday,
        "reposted": [],
        "skipped": [],
        "failed": [],
    }

    for account in accounts:
        excluded = recent_reposted_media_ids(history, account.id, cooldown_since)
        candidates = top_posts(
            history, account.id, since, top_n=int(repost_cfg.get("top_n", 3))
        )
        if rank_index is not None:
            target = candidates[rank_index] if 0 <= rank_index < len(candidates) else None
        else:
            target = pick_for_weekday(
                candidates,
                weekday,
                repost_cfg.get("weekday_rank_map", {}) or {},
                excluded_media_ids=excluded,
            )
        if target is None:
            logger.info("再投稿候補がありません: %s", account.id)
            summary["skipped"].append({"account_id": account.id, "reason": "候補なし"})
            continue

        try:
            if not account.resolve_token() and not dry_run:
                raise RuntimeError(
                    f"Threads トークンが未設定です。GitHub Secrets に "
                    f"{account.token_secret_name} を登録してください"
                )
            client = ThreadsClient(
                access_token=account.resolve_token(),
                user_id=account.threads_user_id,
                dry_run=dry_run,
            )
            result = repost_entry(client, target, pr_text=pr_text)
        except Exception as exc:  # noqa: BLE001 - 1 アカウントの失敗で他を止めない
            logger.error("再投稿に失敗しました account=%s: %s", account.id, exc)
            summary["failed"].append({"account_id": account.id, "error": str(exc)})
            continue

        history["posts"].append(
            {
                "post_id": f"repost-{account.id}-{now_utc.strftime('%Y%m%d%H%M')}",
                "account_id": account.id,
                "account_name": account.name,
                "media_id": result["media_id"],
                "reply_media_id": result["reply_media_id"],
                "body": target.get("body", ""),
                "affiliate_url": target.get("affiliate_url", ""),
                "pr_text": target.get("pr_text", pr_text),
                "worry": target.get("worry", ""),
                "probability": target.get("probability"),
                "item": target.get("item", {}),
                "scheduled_at_jst": to_jst(now_utc).isoformat(),
                "published_at": now_utc.isoformat(),
                "published_at_jst": to_jst(now_utc).isoformat(),
                "is_repost": True,
                "repost_of": target.get("media_id", ""),
                "source_post_id": target.get("post_id", ""),
                "source_likes": int(target.get("likes", 0) or 0),
                "likes": 0,
                "insights": {},
                "insights_updated_at": "",
            }
        )
        summary["reposted"].append(
            {
                "account_id": account.id,
                "media_id": result["media_id"],
                "source_media_id": target.get("media_id", ""),
                "source_likes": int(target.get("likes", 0) or 0),
            }
        )
        logger.info(
            "再投稿しました account=%s source_likes=%s media_id=%s",
            account.id,
            target.get("likes", 0),
            result["media_id"],
        )

    write_json(history_path, history)
    return summary


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="過去にバズった投稿を再投稿する")
    parser.add_argument("--now", default="", help="実行時刻の上書き (ISO8601)")
    parser.add_argument("--account", default="", help="対象アカウント ID")
    parser.add_argument("--rank-index", type=int, default=None, help="使用する順位 (0 始まり)")
    parser.add_argument("--skip-insights", action="store_true", help="いいね数の再取得を行わない")
    parser.add_argument("--data-dir", default=str(cfg.DATA_DIR))
    parser.add_argument("--accounts-file", default=str(cfg.ACCOUNTS_FILE))
    parser.add_argument("--settings-file", default=str(cfg.SETTINGS_FILE))
    parser.add_argument("--dry-run", action="store_true")
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
        rank_index=args.rank_index,
        skip_insights=args.skip_insights,
        dry_run=True if args.dry_run else None,
    )
    logger.info(
        "再投稿完了: 成功 %s 件 / スキップ %s 件 / 失敗 %s 件",
        len(summary["reposted"]),
        len(summary["skipped"]),
        len(summary["failed"]),
    )
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
