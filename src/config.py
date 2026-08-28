"""パス定義・設定ファイル・アカウント定義の読み書き。"""

from __future__ import annotations

import json
import os
import re
import unicodedata
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from src.storage import read_json, write_json

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
PROMPT_DIR = ROOT / "prompts"
WORKFLOW_DIR = ROOT / ".github" / "workflows"

ACCOUNTS_FILE = CONFIG_DIR / "accounts.json"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
QUEUE_FILE = DATA_DIR / "queue.json"
USED_ITEMS_FILE = DATA_DIR / "used_items.json"
POST_HISTORY_FILE = DATA_DIR / "post_history.json"
RUN_LOG_FILE = DATA_DIR / "run_log.json"
POST_PROMPT_FILE = PROMPT_DIR / "Claude×アフィリエイト投稿作成プロンプト.txt"

# 環境変数名
ENV_RAKUTEN_APP_ID = "RAKUTEN_APP_ID"
ENV_RAKUTEN_AFFILIATE_ID = "RAKUTEN_AFFILIATE_ID"
ENV_ANTHROPIC_API_KEY = "ANTHROPIC_API_KEY"
# GitHub Actions から toJSON(secrets) をまとめて受け取るための環境変数
ENV_SECRET_BUNDLE = "ALL_SECRETS"

DEFAULT_SETTINGS: dict[str, Any] = {
    "timezone": "Asia/Tokyo",
    "posts_per_day": 7,
    "active_hours": {"start": "07:00", "end": "23:00"},
    "jitter_minutes": {"min": 15, "max": 30},
    "golden_time_ranges": [["07:00", "09:00"], ["20:00", "23:00"]],
    "min_gap_minutes": 20,
    "duplicate_exclusion_days": 14,
    "rakuten": {
        "fetch_hits": 50,
        "sort": "-reviewCount",
        "min_price": 0,
        "max_price": 0,
        "ng_keywords": [],
    },
    "claude": {
        "model": "claude-3-5-sonnet-latest",
        "max_tokens": 2000,
        "temperature": 1.0,
        "worry_max_tokens": 400,
    },
    "publisher": {
        "max_cron_per_file": 60,
        "window_before_minutes": 5,
        "window_after_minutes": 60,
        "workflow_basename": "publisher",
        "python_version": "3.11",
    },
    "repost": {
        "lookback_days": 7,
        "top_n": 3,
        "cooldown_days": 14,
        "weekday_rank_map": {"0": 0, "2": 1, "4": 2},
    },
    "pr_text": "※PR",
}


def apply_secret_bundle(
    env: dict[str, str] | None = None, prefixes: tuple[str, ...] = ("THREADS_TOKEN_",)
) -> int:
    """``ALL_SECRETS``（GitHub Actions の ``toJSON(secrets)``）を環境変数へ展開する。

    アカウント数が可変で静的な YAML に個々のシークレット名を書けないため、
    まとめて受け取り ``THREADS_TOKEN_*`` だけを取り込む。既存の環境変数は上書きしない。
    戻り値は取り込んだ件数（値はログに出さない）。
    """
    env = os.environ if env is None else env
    raw = env.get(ENV_SECRET_BUNDLE, "")
    if not raw:
        return 0
    try:
        bundle = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return 0
    if not isinstance(bundle, dict):
        return 0

    applied = 0
    for name, value in bundle.items():
        if not isinstance(value, str) or not value:
            continue
        if not any(name.startswith(prefix) for prefix in prefixes):
            continue
        if env.get(name):
            continue
        env[name] = value
        applied += 1
    return applied


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_settings(path: str | Path = SETTINGS_FILE) -> dict[str, Any]:
    """``config/settings.json`` を既定値にマージして返す。"""
    return _deep_merge(DEFAULT_SETTINGS, read_json(path, {}) or {})


def save_settings(settings: dict[str, Any], path: str | Path = SETTINGS_FILE) -> None:
    write_json(path, settings)


def slugify(text: str) -> str:
    """アカウント ID / 環境変数名に使える ASCII スラッグを作る。"""
    normalized = unicodedata.normalize("NFKC", text or "")
    slug = re.sub(r"[^0-9A-Za-z]+", "_", normalized).strip("_").lower()
    return slug


@dataclass
class Account:
    """1 つの Threads アカウント設定。"""

    id: str = ""
    name: str = ""
    enabled: bool = True
    # テーマ = 発信ジャンル + 世界観 + 強み
    genre: str = ""
    worldview: str = ""
    strength: str = ""
    tone: str = "親しみやすい丁寧な口調"
    target: str = ""
    search_keywords: list[str] = field(default_factory=list)
    # Threads API
    threads_user_id: str = ""
    threads_access_token: str = ""
    threads_token_env: str = ""
    # 投稿設定
    posts_per_day: int = 7
    rakuten_affiliate_id: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            base = slugify(self.name)
            self.id = base or f"account_{uuid.uuid4().hex[:8]}"
        if isinstance(self.search_keywords, str):
            self.search_keywords = [
                k.strip() for k in re.split(r"[\n,、,]+", self.search_keywords) if k.strip()
            ]

    # -- テーマ -------------------------------------------------------
    @property
    def theme(self) -> str:
        parts = [
            f"【発信ジャンル】{self.genre}" if self.genre else "",
            f"【世界観】{self.worldview}" if self.worldview else "",
            f"【強み】{self.strength}" if self.strength else "",
            f"【ターゲット】{self.target}" if self.target else "",
        ]
        return "\n".join(p for p in parts if p)

    @property
    def keywords(self) -> list[str]:
        """検索キーワード。未設定ならジャンルを使う。"""
        if self.search_keywords:
            return list(self.search_keywords)
        return [self.genre] if self.genre else []

    # -- 認証情報 -----------------------------------------------------
    @property
    def default_token_env(self) -> str:
        return f"THREADS_TOKEN_{slugify(self.id).upper()}"

    def resolve_token(self, env: dict[str, str] | None = None) -> str:
        """アクセストークンを解決する。

        優先順位は 環境変数(threads_token_env) → 環境変数(既定名) → accounts.json の平文。
        公開リポジトリでは環境変数（GitHub Secrets）の利用を推奨。
        """
        env = os.environ if env is None else env
        for name in (self.threads_token_env, self.default_token_env):
            if name and env.get(name):
                return env[name].strip()
        return (self.threads_access_token or "").strip()

    def resolve_affiliate_id(self, env: dict[str, str] | None = None) -> str:
        env = os.environ if env is None else env
        return (self.rakuten_affiliate_id or env.get(ENV_RAKUTEN_AFFILIATE_ID, "")).strip()

    # -- 直列化 -------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Account":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in (data or {}).items() if k in known})


def load_accounts(path: str | Path = ACCOUNTS_FILE) -> list[Account]:
    """``config/accounts.json`` を読み込む。"""
    raw = read_json(path, {"accounts": []}) or {}
    if isinstance(raw, list):  # 旧形式（配列のみ）にも対応
        items = raw
    else:
        items = raw.get("accounts", [])
    return [Account.from_dict(item) for item in items]


def save_accounts(accounts: list[Account], path: str | Path = ACCOUNTS_FILE) -> None:
    payload = {"accounts": [a.to_dict() if isinstance(a, Account) else a for a in accounts]}
    write_json(path, payload)


def enabled_accounts(accounts: list[Account]) -> list[Account]:
    return [a for a in accounts if a.enabled]


def find_account(accounts: list[Account], account_id: str) -> Account | None:
    for a in accounts:
        if a.id == account_id:
            return a
    return None
