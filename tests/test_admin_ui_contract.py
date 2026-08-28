"""管理画面(docs/app.js)とバックエンド(src/)の取り決めが崩れていないか検証する。

管理画面は静的サイトなので、Python側のデータ構造を変えると気付かないうちに
壊れてしまう。ここでJSの定数と突き合わせておく。
"""

import json
import re

from src.config import (
    ACCOUNTS_FILE,
    FORBIDDEN_ACCOUNT_KEYS,
    POST_PROMPT_FILE,
    QUEUE_FILE,
    ROOT,
    THREADS_TOKEN_PREFIX,
    Account,
)

DOCS = ROOT / "docs"
APP_JS = DOCS / "app.js"


def _js_object(name: str) -> str:
    """app.js から `const NAME = { ... };` のオブジェクト部分を取り出す。"""
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index(f"const {name} = {{")
    depth = 0
    for index in range(source.index("{", start), len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[source.index("{", start) : index + 1]
    raise AssertionError(f"{name} を解析できませんでした")


def _js_keys(name: str) -> set[str]:
    """オブジェクトのトップレベルのキー名を集める（1行に複数キーがあっても拾う）。"""
    body = _js_object(name)
    keys: set[str] = set()
    depth = 0
    index = 0
    while index < len(body):
        char = body[index]
        if char in "\"'":  # 文字列リテラルは読み飛ばす
            quote = char
            index += 1
            while index < len(body) and body[index] != quote:
                index += 2 if body[index] == "\\" else 1
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
        elif depth == 1 and (char.isalpha() or char == "_"):
            match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*:", body[index:])
            if match:
                keys.add(match.group(1))
                index += match.end() - 1
        index += 1
    return keys


def test_静的サイトのファイルが揃っている():
    for name in ("index.html", "style.css", "app.js"):
        assert (DOCS / name).is_file(), f"docs/{name} がありません"


def test_Streamlitの痕跡が残っていない():
    assert not (ROOT / "app.py").exists()
    assert not (ROOT / "src" / "github_sync.py").exists()
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    assert "streamlit" not in requirements


def test_管理画面のアカウント項目がAccountの定義と一致する():
    assert _js_keys("ACCOUNT_DEFAULTS") == set(Account.__dataclass_fields__)


def test_管理画面が参照するファイルパスが実際のパスと一致する():
    body = _js_object("PATHS")
    paths = dict(re.findall(r'(\w+):\s*"([^"]+)"', body))
    assert paths["accounts"] == str(ACCOUNTS_FILE.relative_to(ROOT))
    assert paths["queue"] == str(QUEUE_FILE.relative_to(ROOT))
    assert paths["prompt"] == str(POST_PROMPT_FILE.relative_to(ROOT))
    for path in paths.values():
        parent = (ROOT / path).parent
        assert parent.is_dir(), f"{path} の置き場所がありません"


def test_管理画面の既定設定がsettings_jsonと矛盾しない():
    js_keys = _js_keys("DEFAULT_SETTINGS")
    settings = json.loads((ROOT / "config" / "settings.json").read_text(encoding="utf-8"))
    assert js_keys == set(settings)


def test_トークンのシークレット名の規則がPython側と一致する():
    # JS の tokenSecretName() と Account.token_secret_name が同じ規則であること
    source = APP_JS.read_text(encoding="utf-8")
    assert "${THREADS_TOKEN_PREFIX}${slugify(accountId).toUpperCase()}" in source
    assert f'const THREADS_TOKEN_PREFIX = "{THREADS_TOKEN_PREFIX}"' in source
    assert Account(id="beauty_lab", name="x").token_secret_name == "THREADS_TOKEN_BEAUTY_LAB"


def test_管理画面が保存する項目に機密フィールドが無い():
    keys = _js_keys("ACCOUNT_DEFAULTS")
    assert not keys & FORBIDDEN_ACCOUNT_KEYS, keys & FORBIDDEN_ACCOUNT_KEYS


def test_管理画面はSecretsAPIで暗号化して保存している():
    source = APP_JS.read_text(encoding="utf-8")
    # 公開鍵の取得 → sealed box で暗号化 → Secrets API へ PUT
    assert "/actions/secrets/public-key" in source
    assert "crypto_box_seal" in source
    assert "encrypted_value" in source and "key_id" in source


def test_暗号化ライブラリが同梱されている():
    for name in ("libsodium.js", "libsodium-wrappers.js"):
        path = DOCS / "vendor" / name
        assert path.is_file() and path.stat().st_size > 1000, f"docs/vendor/{name} がありません"
    html = (DOCS / "index.html").read_text(encoding="utf-8")
    # CDN ではなく同梱ファイルを読み込んでいること
    assert 'src="vendor/libsodium.js"' in html
    assert 'src="vendor/libsodium-wrappers.js"' in html


def test_トークンがソースへ直書きされていない():
    for path in DOCS.iterdir():
        if path.suffix not in {".js", ".html", ".css"}:
            continue
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"gh[pousr]_[A-Za-z0-9]{16,}", text), f"{path.name} にトークンらしき文字列があります"
        assert not re.search(r"github_pat_[A-Za-z0-9_]{20,}", text), f"{path.name} にトークンらしき文字列があります"
