"""公開リポジトリとして安全な状態が保たれているかを検査する。

このリポジトリはフォークして使われる公開テンプレートのため、
「コミットされているファイルに機密情報が無い」ことを常に確認する。
"""

import json
import re

from src.config import ACCOUNTS_FILE, ROOT, find_secret_fields

# 実際の資格情報にありがちな形。プレースホルダ（sk-ant-... 等）は検出しない
SECRET_PATTERNS = {
    "GitHub PAT (classic)": re.compile(r"ghp_[A-Za-z0-9]{30,}"),
    "GitHub PAT (fine-grained)": re.compile(r"github_pat_[A-Za-z0-9_]{50,}"),
    "Anthropic APIキー": re.compile(r"sk-ant-[A-Za-z0-9\-_]{30,}"),
    "Threads/Metaトークン": re.compile(r"\bTHQ[A-Za-z0-9_\-]{40,}"),
}

SCAN_SUFFIXES = {".py", ".js", ".json", ".yml", ".yaml", ".md", ".html", ".css", ".txt", ".sh"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "vendor"}


def _scan_files():
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in SCAN_SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        yield path


def test_リポジトリ内に資格情報らしき文字列が無い():
    found = []
    for path in _scan_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                found.append(f"{path.relative_to(ROOT)}: {label}")
    assert not found, f"機密情報らしき文字列が見つかりました: {found}"


def test_accounts_jsonに機密フィールドが無い():
    assert find_secret_fields(ACCOUNTS_FILE) == []


def test_設定ファイルに機密フィールドが無い():
    for name in ("accounts.json", "accounts.example.json", "settings.json"):
        path = ROOT / "config" / name
        if not path.exists():
            continue
        text = json.dumps(json.loads(path.read_text(encoding="utf-8")), ensure_ascii=False).lower()
        for word in ("access_token", "api_key", "password", '"secret"'):
            assert word not in text, f"{name} に {word} が含まれています"


def test_生成済みワークフローにトークンの実体が無い():
    for path in (ROOT / ".github" / "workflows").glob("*.yml"):
        text = path.read_text(encoding="utf-8")
        for label, pattern in SECRET_PATTERNS.items():
            assert not pattern.search(text), f"{path.name} に {label} が含まれています"
        # 参照は ${{ secrets.NAME }} / ${{ secrets['NAME'] }} の形だけであること
        for line in text.splitlines():
            if "THREADS_TOKEN_" in line and "secrets" not in line and not line.strip().startswith("#"):
                raise AssertionError(f"{path.name}: トークンが直接書かれている疑い: {line.strip()}")


def test_gitignoreが機密ファイルを除外している():
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in ignored


def test_管理画面のソースにトークンが直書きされていない():
    for path in (ROOT / "docs").iterdir():
        if path.suffix not in {".js", ".html", ".css"}:
            continue
        text = path.read_text(encoding="utf-8")
        for label, pattern in SECRET_PATTERNS.items():
            assert not pattern.search(text), f"docs/{path.name} に {label} が含まれています"
