"""パス定義・設定ファイル・アカウント定義の読み書き。"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from src.storage import read_json, write_json

logger = logging.getLogger(__name__)

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
TOKEN_STATUS_FILE = DATA_DIR / "token_status.json"
POST_PROMPT_FILE = PROMPT_DIR / "Claude×アフィリエイト投稿作成プロンプト.txt"

# 環境変数名
ENV_RAKUTEN_APP_ID = "RAKUTEN_APP_ID"
ENV_RAKUTEN_AFFILIATE_ID = "RAKUTEN_AFFILIATE_ID"
ENV_ANTHROPIC_API_KEY = "ANTHROPIC_API_KEY"

# 各アカウントの Threads トークンを入れる GitHub Secrets 名の接頭辞
THREADS_TOKEN_PREFIX = "THREADS_TOKEN_"

# accounts.json に入っていてはいけないキー（過去バージョンからの移行検出用）
FORBIDDEN_ACCOUNT_KEYS = frozenset(
    {"threads_access_token", "access_token", "token", "api_key", "anthropic_api_key",
     "rakuten_app_id", "secret", "password"}
)

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
    # Threads のユーザー ID（公開情報。アクセストークンはここには持たせない）
    threads_user_id: str = ""
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
    # アクセストークンは accounts.json に保存しない。
    # 公開リポジトリを前提とするため、GitHub Actions Secrets にのみ保存し、
    # 実行時に「THREADS_TOKEN_<アカウントID大文字>」という環境変数として受け取る。
    @property
    def token_secret_name(self) -> str:
        return f"{THREADS_TOKEN_PREFIX}{slugify(self.id).upper()}"

    def resolve_token(self, env: dict[str, str] | None = None) -> str:
        """Secrets 由来の環境変数からアクセストークンを取得する。"""
        env = os.environ if env is None else env
        return (env.get(self.token_secret_name) or "").strip()

    def resolve_affiliate_id(self, env: dict[str, str] | None = None) -> str:
        """楽天アフィリエイト ID を返す。

        アフィリエイト ID は投稿するリンクに必ず現れる公開情報のため、
        非機密データとして accounts.json に保持することを許容している。
        """
        env = os.environ if env is None else env
        return (self.rakuten_affiliate_id or env.get(ENV_RAKUTEN_AFFILIATE_ID, "")).strip()

    # -- 直列化 -------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Account":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in (data or {}).items() if k in known})


def find_secret_fields(path: str | Path = ACCOUNTS_FILE) -> list[str]:
    """``accounts.json`` に機密情報が混入していないか調べる。

    このリポジトリは公開前提のため、トークン類がファイルに書かれていたら
    「値が既に漏れている」ものとして扱い、呼び出し側で警告する。
    戻り値は ``"アカウントID.キー名"`` のリスト（値そのものは返さない）。
    """
    raw = read_json(path, {}) or {}
    items = raw if isinstance(raw, list) else raw.get("accounts", [])
    found: list[str] = []
    for index, item in enumerate(items or []):
        if not isinstance(item, dict):
            continue
        account_id = item.get("id") or f"#{index + 1}"
        for key, value in item.items():
            if key.lower() in FORBIDDEN_ACCOUNT_KEYS and str(value or "").strip():
                found.append(f"{account_id}.{key}")
    return found


def load_accounts(path: str | Path = ACCOUNTS_FILE) -> list[Account]:
    """``config/accounts.json`` を読み込む。

    未知のキー（旧バージョンのトークン欄など）は ``Account`` へ取り込まれず破棄される。
    """
    leaked = find_secret_fields(path)
    if leaked:
        logger.warning(
            "accounts.json に機密情報らしき項目があります: %s / "
            "公開リポジトリでは既に漏洩している可能性があります。"
            "該当のトークンを失効させ、GitHub Secrets へ登録し直してください。",
            ", ".join(leaked),
        )
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
