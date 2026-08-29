"""【Threads 連携】認可コードを長寿命トークンへ交換し、Secrets へ保存する。

管理画面で「Threadsでログイン」を押すと、Threads の認可画面へ移動し、
利用者がログインして「許可」を押すと認可コードが返ってくる。
そのコードを長寿命トークンへ交換する処理には**アプリシークレットが必要**なため、
ブラウザではなくこのワークフローの中だけで行う（ブラウザへ秘密を渡さない）。

認可コード自体も公開リポジトリの実行ログに残さないよう、ワークフローの入力ではなく
一時シークレット ``THREADS_OAUTH_CODE`` として受け取り、使い終わったら削除する。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

from src import config as cfg
from src.config import (
    ENV_THREADS_APP_ID,
    ENV_THREADS_APP_SECRET,
    THREADS_OAUTH_CODE_SECRET,
    find_account,
    load_accounts,
    load_settings,
)
from src.github_secrets import GitHubSecretsClient, GitHubSecretsError
from src.storage import read_json, write_json
from src.threads_api import (
    ThreadsAPIError,
    exchange_code_for_token,
    exchange_for_long_lived_token,
)

logger = logging.getLogger(__name__)


def _annotate(level: str, message: str) -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::{level}::{message}")


def run(
    account_id: str,
    redirect_uri: str,
    code: str = "",
    app_id: str = "",
    app_secret: str = "",
    now: datetime | None = None,
    data_dir: Path = cfg.DATA_DIR,
    accounts_file: Path = cfg.ACCOUNTS_FILE,
    settings_file: Path = cfg.SETTINGS_FILE,
    secrets_client: GitHubSecretsClient | None = None,
    dry_run: bool = False,
) -> dict[str, str]:
    """認可コードを長寿命トークンへ交換し、アカウントのシークレットへ保存する。"""
    now = now or datetime.now(tz=timezone.utc)
    env = os.environ
    code = (code or env.get(THREADS_OAUTH_CODE_SECRET, "")).strip()
    # アプリIDは公開値なので設定ファイルに置く（環境変数があればそちらを優先）
    settings = load_settings(settings_file)
    app_id = (app_id or env.get(ENV_THREADS_APP_ID, "")
              or (settings.get("threads", {}) or {}).get("app_id", "")).strip()
    app_secret = (app_secret or env.get(ENV_THREADS_APP_SECRET, "")).strip()

    account = find_account(load_accounts(accounts_file), account_id)
    if account is None:
        raise ValueError(f"アカウントが見つかりません: {account_id}")
    if not code:
        raise ValueError(
            f"認可コードがありません（シークレット {THREADS_OAUTH_CODE_SECRET} が未設定です）"
        )
    if not app_id or not app_secret:
        raise ValueError(
            "アプリ情報がありません（管理画面でThreadsアプリIDを保存し、"
            f"{ENV_THREADS_APP_SECRET} を登録してください）"
        )

    if dry_run:
        long_lived = {"access_token": "dry-run-token", "expires_in": 5183944}
        user_id = "dry-run-user"
    else:
        short = exchange_code_for_token(code, redirect_uri, app_id, app_secret)
        user_id = short.get("user_id", "")
        long_lived = exchange_for_long_lived_token(short["access_token"], app_secret)

    expires = now + timedelta(seconds=int(long_lived.get("expires_in") or 0) or 5183944)

    if not dry_run:
        if secrets_client is None:
            secrets_client = GitHubSecretsClient()
        secrets_client.put_secret(account.token_secret_name, long_lived["access_token"])
        # 認可コードは一度きりの使い捨て。残しておく理由がないので必ず消す
        try:
            secrets_client.delete_secret(THREADS_OAUTH_CODE_SECRET)
        except GitHubSecretsError as exc:
            _annotate("warning", f"一時シークレットの削除に失敗しました: {exc}")

    # 有効期限などの非機密情報だけを記録する（トークンの値は書かない）
    status_path = Path(data_dir) / cfg.TOKEN_STATUS_FILE.name
    status = read_json(status_path, {"accounts": {}}) or {}
    status.setdefault("accounts", {})
    status["accounts"][account.id] = {
        "secret_name": account.token_secret_name,
        "status": "refreshed",
        "last_refreshed_at": now.isoformat(),
        "expires_at": expires.isoformat(),
        "days_remaining": max(0, (expires - now).days),
        "error": "",
        "connected_by": "oauth",
    }
    status["updated_at"] = now.isoformat()
    write_json(status_path, status)

    logger.info(
        "Threads と連携しました account=%s secret=%s 期限=%s",
        account.id, account.token_secret_name, expires.isoformat()[:10],
    )
    return {
        "account_id": account.id,
        "secret_name": account.token_secret_name,
        "user_id": user_id,
        "expires_at": expires.isoformat(),
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Threads の認可コードをトークンへ交換する")
    parser.add_argument("--account", required=True, help="対象アカウント ID")
    parser.add_argument("--redirect-uri", required=True, help="認可時に使ったリダイレクトURL")
    parser.add_argument("--code", default="", help="認可コード（既定は環境変数から取得）")
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
    try:
        result = run(
            account_id=args.account,
            redirect_uri=args.redirect_uri,
            code=args.code,
            data_dir=Path(args.data_dir),
            accounts_file=Path(args.accounts_file),
            settings_file=Path(args.settings_file),
            dry_run=args.dry_run,
        )
    except (ValueError, ThreadsAPIError, GitHubSecretsError) as exc:
        _annotate("error", f"Threads との連携に失敗しました: {exc}")
        logger.error("連携に失敗しました: %s", exc)
        return 1

    logger.info("連携完了: %s → %s", result["account_id"], result["secret_name"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
