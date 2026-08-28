"""アカウント設定・トークン解決・シークレット展開のテスト。"""

import json

from src.config import Account, apply_secret_bundle, load_accounts, load_settings, save_accounts


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


def test_トークンは環境変数を優先する():
    account = Account(id="a1", name="t", threads_access_token="平文", threads_token_env="TOK_A")
    assert account.resolve_token({}) == "平文"
    assert account.resolve_token({"TOK_A": "環境変数"}) == "環境変数"
    # 既定名の環境変数でも解決できる
    assert Account(id="a1", name="t").resolve_token({"THREADS_TOKEN_A1": "既定名"}) == "既定名"


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


def test_ALL_SECRETSからThreadsトークンだけを取り込む():
    env = {
        "ALL_SECRETS": json.dumps(
            {"THREADS_TOKEN_A": "a", "THREADS_TOKEN_B": "b", "ANTHROPIC_API_KEY": "秘密"}
        )
    }
    assert apply_secret_bundle(env) == 2
    assert env["THREADS_TOKEN_A"] == "a"
    assert "ANTHROPIC_API_KEY" not in env


def test_既存の環境変数は上書きしない():
    env = {"ALL_SECRETS": json.dumps({"THREADS_TOKEN_A": "新"}), "THREADS_TOKEN_A": "既存"}
    assert apply_secret_bundle(env) == 0
    assert env["THREADS_TOKEN_A"] == "既存"


def test_ALL_SECRETSが壊れていても例外にならない():
    assert apply_secret_bundle({"ALL_SECRETS": "not json"}) == 0
    assert apply_secret_bundle({}) == 0
