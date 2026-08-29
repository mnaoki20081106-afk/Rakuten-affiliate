"""``config/accounts.json`` の読み書きと正規化。

Streamlit の管理画面（app.py）とバッチ処理の双方から使う。
1 アカウント = 「テーマ（発信ジャンル + 世界観 + 強み）」 + 「Threads API トークン」 +
リサーチ条件 + 配信スケジュール設定、という構成。
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

from .utils import ACCOUNTS_FILE, env, iso, load_json, now_jst, save_json

# 既定のスケジュール設定（システム仕様書 3. のスケジュール予約に対応）
DEFAULT_SCHEDULE: dict[str, Any] = {
    "active_start": "07:00",
    "active_end": "23:00",
    "slot_count": 7,
    # ゴールデンタイム: 通勤時間帯と帰宅後
    "golden_windows": [["07:00", "09:00"], ["20:00", "23:00"]],
    "golden_slot_count": 4,
    # 各枠に付与する「ゆらぎ」の大きさ（分）
    "jitter_min_minutes": 15,
    "jitter_max_minutes": 30,
    # 枠同士が近づきすぎないための最小間隔（分）
    "min_gap_minutes": 30,
}

DEFAULT_THEME: dict[str, Any] = {
    "genre": "",
    "worldview": "",
    "strength": "",
    "tone": "やさしく親しみやすい丁寧語",
    "target": "",
}

DEFAULT_RAKUTEN: dict[str, Any] = {
    "keyword": "",
    "genre_id": "",
    "min_price": None,
    "max_price": None,
    # 楽天商品検索 API の sort。売れ筋の近似としてレビュー件数の多い順を既定にする。
    "sort": "-reviewCount",
}

DEFAULT_THREADS: dict[str, Any] = {
    "user_id": "",
    "token": "",
    "token_env": "",
}


def slugify(value: str, fallback: str = "account") -> str:
    """アカウント ID として使える文字列に整形する。"""
    slug = re.sub(r"[^0-9A-Za-z_-]+", "_", (value or "").strip()).strip("_").lower()
    return slug or fallback


def _merged(defaults: dict[str, Any], value: Any) -> dict[str, Any]:
    merged = copy.deepcopy(defaults)
    if isinstance(value, dict):
        for key, item in value.items():
            merged[key] = item
    return merged


def normalize_account(raw: dict[str, Any], index: int = 0) -> dict[str, Any]:
    """欠けているキーを既定値で補い、常に同じ形の dict を返す。"""
    account = dict(raw or {})
    name = str(account.get("name") or f"account_{index + 1}").strip()
    account_id = slugify(str(account.get("id") or name), fallback=f"account_{index + 1}")
    return {
        "id": account_id,
        "name": name,
        "enabled": bool(account.get("enabled", True)),
        "theme": _merged(DEFAULT_THEME, account.get("theme")),
        "rakuten": _merged(DEFAULT_RAKUTEN, account.get("rakuten")),
        "threads": _merged(DEFAULT_THREADS, account.get("threads")),
        "schedule": _merged(DEFAULT_SCHEDULE, account.get("schedule")),
        "created_at": account.get("created_at") or iso(now_jst()),
        "updated_at": account.get("updated_at") or iso(now_jst()),
    }


def load_accounts(enabled_only: bool = False) -> list[dict[str, Any]]:
    """``config/accounts.json`` を読み込んで正規化済みのリストを返す。"""
    payload = load_json(ACCOUNTS_FILE, default={"accounts": []}) or {}
    raw_accounts = payload.get("accounts") if isinstance(payload, dict) else payload
    if not isinstance(raw_accounts, list):
        raw_accounts = []
    accounts = [normalize_account(item, i) for i, item in enumerate(raw_accounts)]
    if enabled_only:
        accounts = [a for a in accounts if a["enabled"]]
    return accounts


def save_accounts(accounts: list[dict[str, Any]]) -> None:
    """アカウント一覧を ``config/accounts.json`` に保存する。"""
    normalized = [normalize_account(item, i) for i, item in enumerate(accounts)]
    save_json(ACCOUNTS_FILE, {"updated_at": iso(now_jst()), "accounts": normalized})


def default_token_env(account_id: str) -> str:
    """アカウント ID から推奨のトークン用環境変数名を組み立てる。"""
    return "THREADS_TOKEN_" + slugify(account_id, "account").upper()


def resolve_threads_token(account: dict[str, Any]) -> str:
    """Threads のアクセストークンを解決する。

    優先順位は以下のとおり。トークンを JSON に直接書かず、GitHub Secrets /
    Streamlit Secrets の環境変数で渡せるようにするための仕組み。

    1. ``THREADS_TOKEN_<ACCOUNT_ID>`` 環境変数
    2. アカウント設定の ``threads.token_env`` が指す環境変数
    3. ``THREADS_TOKENS_JSON`` 環境変数（``{"beauty": "THAA...", ...}`` 形式）
    4. アカウント設定に直接保存された ``threads.token``
    """
    threads = account.get("threads") or {}
    account_id = str(account.get("id", ""))

    for name in (default_token_env(account_id), str(threads.get("token_env") or "")):
        if name:
            value = env(name)
            if value:
                return value

    bundle = env("THREADS_TOKENS_JSON")
    if bundle:
        try:
            tokens = json.loads(bundle)
        except json.JSONDecodeError:
            tokens = {}
        if isinstance(tokens, dict):
            value = str(tokens.get(account_id) or "").strip()
            if value:
                return value

    return str(threads.get("token") or "").strip()


def account_by_id(account_id: str) -> dict[str, Any] | None:
    """ID でアカウントを 1 件取得する。"""
    for account in load_accounts():
        if account["id"] == account_id:
            return account
    return None
