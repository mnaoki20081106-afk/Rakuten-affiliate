"""【トークン自動更新】Threads の長寿命アクセストークンを更新し、Secrets へ保存し直す。

Threads の長寿命トークンは発行から 60 日で失効する。失効すると全アカウントの投稿が
止まるため、定期的に更新して GitHub Secrets を上書きする。

Threads API の制約:
- 更新できるのは「発行から 24 時間以上経過し、まだ失効していない」トークンのみ
- 更新すると有効期限が再び約 60 日先へ延びる

トークンの値そのものはログにもファイルにも出さない。
``data/token_status.json`` に残すのは有効期限などの非機密情報だけ。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from src import config as cfg
from src.config import Account, load_accounts, load_settings
from src.github_secrets import GitHubSecretsClient, GitHubSecretsError
from src.scheduler import parse_iso, to_jst
from src.storage import read_json, write_json
from src.threads_api import ThreadsAPIError, refresh_long_lived_token

logger = logging.getLogger(__name__)

# 期限がこの日数を切ったら警告する
WARN_DAYS_REMAINING = 14


def load_status(path: Path) -> dict[str, Any]:
    status = read_json(path, {"accounts": {}}) or {}
    status.setdefault("accounts", {})
    return status


def days_until(expires_at: str, now: datetime) -> int | None:
    """有効期限までの残り日数。解釈できなければ None。"""
    if not expires_at:
        return None
    try:
        return max(0, (parse_iso(expires_at) - now).days)
    except (ValueError, TypeError):
        return None


def should_skip(entry: dict[str, Any], now: datetime, min_interval_days: float) -> bool:
    """直近に更新済みなら見送る（24 時間制約と API の無駄打ちを避ける）。"""
    last = entry.get("last_refreshed_at")
    if not last:
        return False
    try:
        elapsed = now - parse_iso(last)
    except (ValueError, TypeError):
        return False
    return elapsed < timedelta(days=min_interval_days)


def _annotate(level: str, message: str) -> None:
    """GitHub Actions のログに注釈を出す（ローカル実行時はただの出力）。"""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::{level}::{message}")


def write_step_summary(summary: dict[str, Any]) -> None:
    """GitHub Actions の実行サマリーへ結果の表を書き出す。"""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [
        "## Threads トークンの更新結果",
        "",
        "| アカウント | 結果 | 有効期限 (JST) | 残り |",
        "| --- | --- | --- | --- |",
    ]
    labels = {
        "refreshed": "✅ 更新した",
        "skipped": "⏭ 見送り（更新済み）",
        "missing": "⚠️ 未登録",
        "failed": "❌ 失敗",
    }
    for account_id, entry in summary["accounts"].items():
        expires = entry.get("expires_at", "")
        expires_label = to_jst(parse_iso(expires)).strftime("%Y-%m-%d") if expires else "-"
        remaining = entry.get("days_remaining")
        lines.append(
            f"| {account_id} | {labels.get(entry['status'], entry['status'])} "
            f"| {expires_label} | {'-' if remaining is None else str(remaining) + ' 日'} |"
        )
        if entry.get("error"):
            lines.append(f"| | {entry['error']} | | |")
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError as exc:  # pragma: no cover - 環境依存
        logger.warning("実行サマリーを書き出せませんでした: %s", exc)


def refresh_account(
    account: Account,
    secrets_client: GitHubSecretsClient | None,
    now: datetime,
    dry_run: bool,
) -> dict[str, Any]:
    """1 アカウント分のトークンを更新して Secrets へ保存し直す。"""
    entry: dict[str, Any] = {
        "secret_name": account.token_secret_name,
        "status": "failed",
        "last_refreshed_at": "",
        "expires_at": "",
        "days_remaining": None,
        "error": "",
    }

    token = account.resolve_token()
    if not token:
        entry["status"] = "missing"
        entry["error"] = f"{account.token_secret_name} が未登録です"
        return entry

    if dry_run:
        expires = now + timedelta(days=60)
        entry.update(
            status="refreshed",
            last_refreshed_at=now.isoformat(),
            expires_at=expires.isoformat(),
            days_remaining=60,
            error="",
        )
        return entry

    result = refresh_long_lived_token(token)  # 失敗時は ThreadsAPIError
    new_token = result["access_token"]
    expires_in = result.get("expires_in") or 0
    expires = now + timedelta(seconds=int(expires_in)) if expires_in else now + timedelta(days=60)

    if secrets_client is None:
        raise GitHubSecretsError("Secrets クライアントが初期化されていません")
    secrets_client.put_secret(account.token_secret_name, new_token)

    entry.update(
        status="refreshed",
        last_refreshed_at=now.isoformat(),
        expires_at=expires.isoformat(),
        days_remaining=days_until(expires.isoformat(), now),
        error="",
    )
    return entry


def run(
    now: datetime | None = None,
    data_dir: Path = cfg.DATA_DIR,
    accounts_file: Path = cfg.ACCOUNTS_FILE,
    settings_file: Path = cfg.SETTINGS_FILE,
    account_id: str = "",
    dry_run: bool = False,
    force: bool = False,
    min_interval_days: float = 1.0,
    secrets_client: GitHubSecretsClient | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(tz=timezone.utc)
    load_settings(settings_file)  # 設定ファイルの妥当性確認を兼ねる

    accounts = [a for a in load_accounts(accounts_file) if a.enabled]
    if account_id:
        accounts = [a for a in accounts if a.id == account_id]

    status_path = Path(data_dir) / cfg.TOKEN_STATUS_FILE.name
    status = load_status(status_path)

    if not dry_run and secrets_client is None and accounts:
        try:
            secrets_client = GitHubSecretsClient()
        except GitHubSecretsError as exc:
            # 全アカウントが失敗するため、ここで打ち切って理由を明示する
            _annotate("error", f"Secrets へ書き込めません: {exc}")
            raise

    summary: dict[str, Any] = {"executed_at": now.isoformat(), "accounts": {}, "counts": {}}

    for account in accounts:
        previous = status["accounts"].get(account.id, {})
        if not force and should_skip(previous, now, min_interval_days):
            entry = {**previous, "status": "skipped", "error": ""}
            entry["days_remaining"] = days_until(entry.get("expires_at", ""), now)
            summary["accounts"][account.id] = entry
            logger.info("直近に更新済みのため見送ります: %s", account.id)
            continue

        try:
            entry = refresh_account(account, secrets_client, now, dry_run)
        except (ThreadsAPIError, GitHubSecretsError) as exc:
            entry = {
                **previous,
                "secret_name": account.token_secret_name,
                "status": "failed",
                "error": str(exc)[:300],
            }
            entry["days_remaining"] = days_until(entry.get("expires_at", ""), now)
            logger.error("トークン更新に失敗しました account=%s: %s", account.id, exc)

        summary["accounts"][account.id] = entry
        status["accounts"][account.id] = entry

        if entry["status"] == "refreshed":
            logger.info(
                "トークンを更新しました account=%s 期限=%s",
                account.id,
                entry.get("expires_at", "")[:10],
            )
        elif entry["status"] == "missing":
            _annotate("warning", f"{account.id}: {entry['error']}")
        elif entry["status"] == "failed":
            _annotate(
                "error",
                f"{account.id}: トークンを更新できませんでした。"
                f"失効している場合は手動で取り直してください（{entry['error']}）",
            )

    # 期限が近いものを警告する
    for account_id_, entry in summary["accounts"].items():
        remaining = entry.get("days_remaining")
        if remaining is not None and remaining <= WARN_DAYS_REMAINING and entry["status"] != "refreshed":
            _annotate("warning", f"{account_id_}: トークンの残り {remaining} 日です")

    counts: dict[str, int] = {}
    for entry in summary["accounts"].values():
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    summary["counts"] = counts

    status["updated_at"] = now.isoformat()
    write_json(status_path, status)
    write_step_summary(summary)
    return summary


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Threads の長寿命トークンを更新する")
    parser.add_argument("--now", default="", help="実行時刻の上書き (ISO8601)")
    parser.add_argument("--account", default="", help="対象アカウント ID")
    parser.add_argument("--dry-run", action="store_true", help="外部 API を呼ばずに動作確認する")
    parser.add_argument("--force", action="store_true", help="直近に更新済みでも実行する")
    parser.add_argument(
        "--min-interval-days", type=float, default=1.0,
        help="この日数以内に更新済みならスキップする (既定 1)",
    )
    parser.add_argument("--data-dir", default=str(cfg.DATA_DIR))
    parser.add_argument("--accounts-file", default=str(cfg.ACCOUNTS_FILE))
    parser.add_argument("--settings-file", default=str(cfg.SETTINGS_FILE))
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    summary = run(
        now=parse_iso(args.now) if args.now else None,
        data_dir=Path(args.data_dir),
        accounts_file=Path(args.accounts_file),
        settings_file=Path(args.settings_file),
        account_id=args.account,
        dry_run=args.dry_run,
        force=args.force,
        min_interval_days=args.min_interval_days,
    )
    counts = summary["counts"]
    logger.info(
        "トークン更新完了: 更新 %s / 見送り %s / 未登録 %s / 失敗 %s",
        counts.get("refreshed", 0), counts.get("skipped", 0),
        counts.get("missing", 0), counts.get("failed", 0),
    )
    # 失敗があればワークフローを赤くして気付けるようにする（未登録は設定途中なので許容）
    return 1 if counts.get("failed") else 0


if __name__ == "__main__":
    sys.exit(main())
