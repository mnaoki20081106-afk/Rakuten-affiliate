"""共通ユーティリティ。

パス解決 / JST 時刻 / JSON の安全な入出力 / ロギング / 環境変数の取得をまとめる。
GitHub Actions と Streamlit Community Cloud の両方から import される想定。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

# --- タイムゾーン ---------------------------------------------------------
JST = timezone(timedelta(hours=9), "JST")

# --- パス -----------------------------------------------------------------
ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT_DIR / "config"
DATA_DIR = ROOT_DIR / "data"
PROMPT_DIR = ROOT_DIR / "prompts"

ACCOUNTS_FILE = CONFIG_DIR / "accounts.json"
QUEUE_FILE = DATA_DIR / "queue.json"
HISTORY_FILE = DATA_DIR / "post_history.json"
POST_PROMPT_FILE = PROMPT_DIR / "Claude×アフィリエイト投稿作成プロンプト.txt"


# --- ロギング -------------------------------------------------------------
def setup_logging(name: str) -> logging.Logger:
    """GitHub Actions のログで読みやすい形式のロガーを返す。"""
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    return logging.getLogger(name)


# --- 時刻 -----------------------------------------------------------------
def now_jst() -> datetime:
    """現在時刻を JST の aware datetime で返す。"""
    return datetime.now(JST)


def to_jst(dt: datetime) -> datetime:
    """naive datetime は JST とみなし、aware datetime は JST に変換する。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=JST)
    return dt.astimezone(JST)


def iso(dt: datetime) -> str:
    """JST の ISO8601 文字列に整形する。"""
    return to_jst(dt).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    """ISO8601 文字列を JST の aware datetime に戻す。失敗したら None。"""
    if not value:
        return None
    try:
        return to_jst(datetime.fromisoformat(value))
    except ValueError:
        return None


def parse_hhmm(value: str, base_day: date) -> datetime:
    """``"07:00"`` のような文字列を、指定日の JST datetime にする。

    ``"24:00"`` は翌日 00:00 として扱う（活動終了時刻の指定に使う）。
    """
    hour_str, _, minute_str = value.partition(":")
    hour = int(hour_str)
    minute = int(minute_str or 0)
    extra_days, hour = divmod(hour, 24)
    return datetime.combine(base_day, time(hour, minute), tzinfo=JST) + timedelta(days=extra_days)


# --- JSON -----------------------------------------------------------------
def load_json(path: Path, default: Any = None) -> Any:
    """JSON を読み込む。存在しない / 壊れている場合は ``default`` を返す。"""
    try:
        with path.open(encoding="utf-8") as fp:
            return json.load(fp)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, payload: Any) -> None:
    """JSON をアトミックに書き出す（途中終了でファイルを壊さないため）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
    ) as tmp:
        json.dump(payload, tmp, ensure_ascii=False, indent=2)
        tmp.write("\n")
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


# --- 環境変数 -------------------------------------------------------------
def env(name: str, default: str = "") -> str:
    """環境変数を前後の空白を除いて取得する。"""
    return (os.environ.get(name) or default).strip()


def require_env(name: str) -> str:
    """必須の環境変数を取得する。未設定なら分かりやすい例外を投げる。"""
    value = env(name)
    if not value:
        raise RuntimeError(
            f"環境変数 {name} が設定されていません。"
            " GitHub Secrets（Actions）または Streamlit の Secrets に登録してください。"
        )
    return value
