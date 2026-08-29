"""Threads の OAuth 連携（認可コード → 長寿命トークン → Secrets 保存）のテスト。"""

import base64
import json

import pytest
from nacl import encoding, public

from src.github_secrets import GitHubSecretsClient
from src.threads_api import (
    ThreadsAPIError,
    build_authorize_url,
    exchange_code_for_token,
    exchange_for_long_lived_token,
)
from src import threads_connect
from datetime import datetime, timezone

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
REDIRECT = "https://taro.github.io/Rakuten-affiliate/"


class FakeResponse:
    def __init__(self, payload=None, status_code=200, text=""):
        self._payload = payload or {}
        self.status_code = status_code
        self.text = text
        self.content = b"x"

    def json(self):
        return self._payload


class FakeThreadsSession:
    """Threads のトークン発行エンドポイントのスタブ。"""

    def __init__(self):
        self.calls = []

    def post(self, url, data=None, timeout=None):
        self.calls.append(("POST", url, data))
        if data.get("code") == "bad-code":
            return FakeResponse(status_code=400, text='{"error":{"message":"Invalid code"}}')
        return FakeResponse({"access_token": "SHORT_TOKEN", "user_id": 987})

    def get(self, url, params=None, timeout=None):
        self.calls.append(("GET", url, params))
        return FakeResponse({"access_token": "LONG_TOKEN", "expires_in": 5183944})


class FakeSecretsSession:
    def __init__(self, private_key):
        self.private_key = private_key
        self.stored = {}
        self.deleted = []

    def request(self, method, url, headers=None, json=None, timeout=None):
        if url.endswith("/actions/secrets/public-key"):
            return FakeResponse({
                "key_id": "k1",
                "key": self.private_key.public_key.encode(encoding.Base64Encoder).decode(),
            })
        name = url.rsplit("/", 1)[-1]
        if method == "PUT":
            self.stored[name] = json["encrypted_value"]
            return FakeResponse(status_code=204)
        if method == "DELETE":
            self.deleted.append(name)
            self.stored.pop(name, None)
            return FakeResponse(status_code=204)
        return FakeResponse(status_code=404, text="not found")

    def decrypt(self, name):
        sealed = base64.b64decode(self.stored[name])
        return public.SealedBox(self.private_key).decrypt(sealed).decode("utf-8")


@pytest.fixture
def secrets():
    key = public.PrivateKey.generate()
    session = FakeSecretsSession(key)
    return session, GitHubSecretsClient(token="ghp_x", repository="owner/repo", session=session)


def _files(tmp_path, app_id="1234567890"):
    (tmp_path / "accounts.json").write_text(
        json.dumps({"accounts": [{"id": "beauty_lab", "name": "コスメ", "genre": "g"}]}),
        encoding="utf-8",
    )
    (tmp_path / "settings.json").write_text(
        json.dumps({"threads": {"app_id": app_id}}), encoding="utf-8"
    )
    return tmp_path / "accounts.json", tmp_path / "settings.json"


# ----------------------------------------------------------------------
# 認可URL
# ----------------------------------------------------------------------
def test_認可URLに必要な項目が入る():
    url = build_authorize_url("123", REDIRECT, state="nonce-1")
    assert url.startswith("https://threads.net/oauth/authorize?")
    assert "client_id=123" in url
    assert "response_type=code" in url
    assert "state=nonce-1" in url
    for scope in ("threads_basic", "threads_content_publish", "threads_manage_insights"):
        assert scope in url


# ----------------------------------------------------------------------
# トークン交換
# ----------------------------------------------------------------------
def test_認可コードを短命トークンへ交換する():
    session = FakeThreadsSession()
    result = exchange_code_for_token("CODE", REDIRECT, "123", "SECRET", session=session)

    method, url, data = session.calls[0]
    assert (method, url) == ("POST", "https://graph.threads.net/oauth/access_token")
    assert data["grant_type"] == "authorization_code"
    assert data["client_secret"] == "SECRET" and data["redirect_uri"] == REDIRECT
    assert result == {"access_token": "SHORT_TOKEN", "user_id": "987"}


def test_無効な認可コードは失敗する():
    with pytest.raises(ThreadsAPIError, match="400"):
        exchange_code_for_token("bad-code", REDIRECT, "123", "SECRET", session=FakeThreadsSession())


def test_短命トークンを長寿命トークンへ交換する():
    session = FakeThreadsSession()
    result = exchange_for_long_lived_token("SHORT_TOKEN", "SECRET", session=session)
    _, url, params = session.calls[0]
    assert url == "https://graph.threads.net/access_token"
    assert params["grant_type"] == "th_exchange_token"
    assert result["access_token"] == "LONG_TOKEN"


# ----------------------------------------------------------------------
# 連携処理全体
# ----------------------------------------------------------------------
def test_連携するとトークンが暗号化して保存される(tmp_path, secrets, monkeypatch):
    session, client = secrets
    accounts, settings = _files(tmp_path)
    monkeypatch.setattr(threads_connect, "exchange_code_for_token",
                        lambda *a, **k: {"access_token": "SHORT", "user_id": "42"})
    monkeypatch.setattr(threads_connect, "exchange_for_long_lived_token",
                        lambda *a, **k: {"access_token": "LONG_TOKEN", "expires_in": 5183944})
    monkeypatch.setenv("THREADS_OAUTH_CODE", "AUTH_CODE")
    monkeypatch.setenv("THREADS_APP_SECRET", "APP_SECRET")

    result = threads_connect.run(
        account_id="beauty_lab", redirect_uri=REDIRECT, now=NOW,
        data_dir=tmp_path, accounts_file=accounts, settings_file=settings,
        secrets_client=client,
    )

    assert session.decrypt("THREADS_TOKEN_BEAUTY_LAB") == "LONG_TOKEN"
    assert result["secret_name"] == "THREADS_TOKEN_BEAUTY_LAB"
    assert result["user_id"] == "42"


def test_使い終わった認可コードは削除される(tmp_path, secrets, monkeypatch):
    session, client = secrets
    accounts, settings = _files(tmp_path)
    monkeypatch.setattr(threads_connect, "exchange_code_for_token",
                        lambda *a, **k: {"access_token": "SHORT", "user_id": "42"})
    monkeypatch.setattr(threads_connect, "exchange_for_long_lived_token",
                        lambda *a, **k: {"access_token": "LONG", "expires_in": 100})
    monkeypatch.setenv("THREADS_OAUTH_CODE", "AUTH_CODE")
    monkeypatch.setenv("THREADS_APP_SECRET", "APP_SECRET")

    threads_connect.run(
        account_id="beauty_lab", redirect_uri=REDIRECT, now=NOW,
        data_dir=tmp_path, accounts_file=accounts, settings_file=settings,
        secrets_client=client,
    )
    assert "THREADS_OAUTH_CODE" in session.deleted


def test_状態ファイルにトークンの値が残らない(tmp_path, secrets, monkeypatch):
    _, client = secrets
    accounts, settings = _files(tmp_path)
    monkeypatch.setattr(threads_connect, "exchange_code_for_token",
                        lambda *a, **k: {"access_token": "SHORT_SECRET_VALUE", "user_id": "42"})
    monkeypatch.setattr(threads_connect, "exchange_for_long_lived_token",
                        lambda *a, **k: {"access_token": "LONG_SECRET_VALUE", "expires_in": 5183944})
    monkeypatch.setenv("THREADS_OAUTH_CODE", "AUTH_CODE_SECRET")
    monkeypatch.setenv("THREADS_APP_SECRET", "APP_SECRET")

    threads_connect.run(
        account_id="beauty_lab", redirect_uri=REDIRECT, now=NOW,
        data_dir=tmp_path, accounts_file=accounts, settings_file=settings,
        secrets_client=client,
    )
    text = (tmp_path / "token_status.json").read_text(encoding="utf-8")
    for secret in ("LONG_SECRET_VALUE", "SHORT_SECRET_VALUE", "AUTH_CODE_SECRET", "APP_SECRET"):
        assert secret not in text, f"{secret} が状態ファイルに漏れています"
    status = json.loads(text)["accounts"]["beauty_lab"]
    assert status["connected_by"] == "oauth"
    assert status["days_remaining"] == 59


def test_アプリIDは設定ファイルから読む(tmp_path, secrets, monkeypatch):
    _, client = secrets
    accounts, settings = _files(tmp_path, app_id="9876543210")
    captured = {}

    def fake_exchange(code, redirect_uri, app_id, app_secret, **kwargs):
        captured.update(code=code, app_id=app_id, app_secret=app_secret)
        return {"access_token": "SHORT", "user_id": "1"}

    monkeypatch.setattr(threads_connect, "exchange_code_for_token", fake_exchange)
    monkeypatch.setattr(threads_connect, "exchange_for_long_lived_token",
                        lambda *a, **k: {"access_token": "LONG", "expires_in": 100})
    monkeypatch.delenv("THREADS_APP_ID", raising=False)
    monkeypatch.setenv("THREADS_OAUTH_CODE", "CODE")
    monkeypatch.setenv("THREADS_APP_SECRET", "APP_SECRET")

    threads_connect.run(
        account_id="beauty_lab", redirect_uri=REDIRECT, now=NOW,
        data_dir=tmp_path, accounts_file=accounts, settings_file=settings,
        secrets_client=client,
    )
    assert captured["app_id"] == "9876543210"
    assert captured["app_secret"] == "APP_SECRET"


def test_設定が足りなければ分かりやすく失敗する(tmp_path, secrets, monkeypatch):
    _, client = secrets
    accounts, settings = _files(tmp_path, app_id="")
    monkeypatch.setenv("THREADS_OAUTH_CODE", "CODE")
    monkeypatch.delenv("THREADS_APP_ID", raising=False)
    monkeypatch.delenv("THREADS_APP_SECRET", raising=False)

    with pytest.raises(ValueError, match="アプリ情報がありません"):
        threads_connect.run(
            account_id="beauty_lab", redirect_uri=REDIRECT, now=NOW,
            data_dir=tmp_path, accounts_file=accounts, settings_file=settings,
            secrets_client=client,
        )


def test_認可コードが無ければ失敗する(tmp_path, secrets, monkeypatch):
    _, client = secrets
    accounts, settings = _files(tmp_path)
    monkeypatch.delenv("THREADS_OAUTH_CODE", raising=False)
    monkeypatch.setenv("THREADS_APP_SECRET", "APP_SECRET")

    with pytest.raises(ValueError, match="認可コードがありません"):
        threads_connect.run(
            account_id="beauty_lab", redirect_uri=REDIRECT, now=NOW,
            data_dir=tmp_path, accounts_file=accounts, settings_file=settings,
            secrets_client=client,
        )


def test_存在しないアカウントは失敗する(tmp_path, secrets, monkeypatch):
    _, client = secrets
    accounts, settings = _files(tmp_path)
    monkeypatch.setenv("THREADS_OAUTH_CODE", "CODE")
    monkeypatch.setenv("THREADS_APP_SECRET", "APP_SECRET")

    with pytest.raises(ValueError, match="アカウントが見つかりません"):
        threads_connect.run(
            account_id="unknown", redirect_uri=REDIRECT, now=NOW,
            data_dir=tmp_path, accounts_file=accounts, settings_file=settings,
            secrets_client=client,
        )
