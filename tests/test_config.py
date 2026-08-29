"""アカウント設定・トークン解決・シークレット展開のテスト。"""

import json

from src.config import (
    Account,
    find_secret_fields,
    load_accounts,
    load_settings,
    save_accounts,
)


def test_テーマはジャンルと世界観と強みから組み立てられる():
    account = Account(name="t", genre="美容", worldview="夜の独白", strength="自腹レビュー")
    assert "【発信ジャンル】美容" in account.theme
    assert "【世界観】夜の独白" in account.theme
    assert "【強み】自腹レビュー" in account.theme


def test_IDが未指定なら自動採番される():
    assert Account(name="Beauty Lab").id == "beauty_lab"
    assert Account(name="美容ラボ").id.startswith("account_")  # 日本語のみは UUID 由来


def test_キーワードは文字列でも配列に正規化される():
    assert Account(name="t", search_keywords="化粧水, 美容液\nクリーム").search_keywords == [
        "化粧水",
        "美容液",
        "クリーム",
    ]


def test_キーワード未設定ならジャンルが使われる():
    assert Account(name="t", genre="美容").keywords == ["美容"]


def test_トークンはSecrets由来の環境変数からのみ解決される():
    account = Account(id="beauty_lab", name="t")
    assert account.token_secret_name == "THREADS_TOKEN_BEAUTY_LAB"
    assert account.resolve_token({"THREADS_TOKEN_BEAUTY_LAB": "秘密"}) == "秘密"
    assert account.resolve_token({}) == ""


def test_Accountにトークンを保持するフィールドが存在しない():
    fields = set(Account.__dataclass_fields__)
    assert not fields & {"threads_access_token", "access_token", "token", "api_key"}


def test_アカウントの保存と読み込み(tmp_path):
    path = tmp_path / "accounts.json"
    accounts = [Account(id="a1", name="1つ目", genre="美容"), Account(id="a2", name="2つ目")]
    save_accounts(accounts, path)
    loaded = load_accounts(path)
    assert [a.id for a in loaded] == ["a1", "a2"]
    assert loaded[0].genre == "美容"


def test_旧形式の配列JSONも読める(tmp_path):
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps([{"id": "a1", "name": "旧形式"}]), encoding="utf-8")
    assert load_accounts(path)[0].name == "旧形式"


def test_設定は既定値とマージされる(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"posts_per_day": 5, "claude": {"model": "custom"}}), encoding="utf-8")
    settings = load_settings(path)
    assert settings["posts_per_day"] == 5
    assert settings["claude"]["model"] == "custom"
    assert settings["claude"]["max_tokens"] == 2000  # 既定値が残る
    assert settings["active_hours"]["start"] == "07:00"


def test_accounts_jsonに混入した機密情報を検出する(tmp_path):
    path = tmp_path / "accounts.json"
    path.write_text(
        json.dumps({"accounts": [
            {"id": "leaky", "name": "x", "threads_access_token": "THDS_xxxxx"},
            {"id": "clean", "name": "y"},
        ]}),
        encoding="utf-8",
    )
    assert find_secret_fields(path) == ["leaky.threads_access_token"]


def test_空文字のトークン欄は検出しない(tmp_path):
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps({"accounts": [{"id": "a", "name": "x", "token": ""}]}), encoding="utf-8")
    assert find_secret_fields(path) == []


def test_旧形式のトークンは読み込み時に破棄される(tmp_path):
    path = tmp_path / "accounts.json"
    path.write_text(
        json.dumps({"accounts": [{"id": "a1", "name": "x", "threads_access_token": "THDS_leaked"}]}),
        encoding="utf-8",
    )
    account = load_accounts(path)[0]
    assert not hasattr(account, "threads_access_token")
    assert "THDS_leaked" not in json.dumps(account.to_dict(), ensure_ascii=False)


def test_保存したaccounts_jsonに機密情報が含まれない(tmp_path):
    path = tmp_path / "accounts.json"
    save_accounts([Account(id="a1", name="テスト", genre="美容")], path)
    saved = json.loads(path.read_text(encoding="utf-8"))
    for account in saved["accounts"]:
        assert not set(account) & {"threads_access_token", "access_token", "token", "api_key"}
    assert find_secret_fields(path) == []
