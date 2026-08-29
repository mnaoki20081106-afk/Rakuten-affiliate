"""Threads 長寿命トークンの自動更新のテスト。

暗号化は実際に行い、テスト側で秘密鍵を使って復号して中身を確認する
（GitHub 側で正しく復号できることの確認に相当する）。
"""

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
from nacl import encoding, public

from src.config import Account
from src.github_secrets import GitHubSecretsClient, GitHubSecretsError, encrypt_secret
from src.threads_api import ThreadsAPIError, refresh_long_lived_token
from src import token_refresher

NOW = datetime(2026, 8, 30, 19, 0, tzinfo=timezone.utc)


# ----------------------------------------------------------------------
# 補助
# ----------------------------------------------------------------------
class FakeResponse:
    def __init__(self, payload=None, status_code=200, text=""):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.text = text
        self.content = b"x"

    def json(self):
        return self._payload


class FakeSecretsSession:
    """GitHub Secrets API のスタブ。受け取った暗号文を保持する。"""

    def __init__(self, private_key):
        self.private_key = private_key
        self.stored = {}
        self.requests = []

    def request(self, method, url, headers=None, json=None, timeout=None):
        self.requests.append((method, url, json))
        if url.endswith("/actions/secrets/public-key"):
            return FakeResponse({
                "key_id": "key-1",
                "key": self.private_key.public_key.encode(encoding.Base64Encoder).decode(),
            })
        if "/actions/secrets/" in url and method == "PUT":
            name = url.rsplit("/", 1)[-1]
            self.stored[name] = json["encrypted_value"]
            return FakeResponse(status_code=204)
        return FakeResponse(status_code=404, text="not found")

    def decrypt(self, name):
        sealed = base64.b64decode(self.stored[name])
        return public.SealedBox(self.private_key).decrypt(sealed).decode("utf-8")


@pytest.fixture
def secrets(tmp_path):
    private_key = public.PrivateKey.generate()
    session = FakeSecretsSession(private_key)
    client = GitHubSecretsClient(token="ghp_test", repository="owner/repo", session=session)
    return session, client


def _accounts_file(tmp_path, ids=("beauty_lab",)):
    # 呼び出しごとに別ファイルにする（既定値が先に評価され上書きされるのを防ぐ）
    path = tmp_path / f"accounts_{'_'.join(ids)}.json"
    path.write_text(
        json.dumps({"accounts": [{"id": i, "name": i, "genre": "g"} for i in ids]}),
        encoding="utf-8",
    )
    return path


def _run(tmp_path, secrets_client, monkeypatch, env=None, **kwargs):
    for name, value in (env or {}).items():
        monkeypatch.setenv(name, value)
    return token_refresher.run(
        now=NOW,
        data_dir=tmp_path,
        accounts_file=kwargs.pop("accounts_file", None) or _accounts_file(tmp_path),
        settings_file=tmp_path / "settings.json",
        secrets_client=secrets_client,
        **kwargs,
    )


# ----------------------------------------------------------------------
# 暗号化
# ----------------------------------------------------------------------
def test_暗号化した値はGitHubの秘密鍵で復号できる():
    private_key = public.PrivateKey.generate()
    public_b64 = private_key.public_key.encode(encoding.Base64Encoder).decode()

    encrypted = encrypt_secret(public_b64, "THQ_新しいトークン_🎉")
    decrypted = public.SealedBox(private_key).decrypt(base64.b64decode(encrypted))

    assert decrypted.decode("utf-8") == "THQ_新しいトークン_🎉"
    assert "THQ" not in encrypted  # 暗号文に平文の痕跡がない


def test_リポジトリ名が不正なら初期化に失敗する():
    with pytest.raises(GitHubSecretsError, match="リポジトリ名"):
        GitHubSecretsClient(token="t", repository="不正な形式")


def test_トークンが無ければ初期化に失敗する(monkeypatch):
    monkeypatch.delenv("WORKFLOW_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(GitHubSecretsError, match="トークン"):
        GitHubSecretsClient(repository="owner/repo")


# ----------------------------------------------------------------------
# Threads API のトークン更新
# ----------------------------------------------------------------------
def test_更新エンドポイントに正しいパラメータで問い合わせる():
    calls = []

    class Session:
        def get(self, url, params=None, timeout=None):
            calls.append((url, params))
            return FakeResponse({"access_token": "THQ_new", "expires_in": 5183944})

    result = refresh_long_lived_token("THQ_old", session=Session())
    url, params = calls[0]
    assert url == "https://graph.threads.net/refresh_access_token"
    assert params == {"grant_type": "th_refresh_token", "access_token": "THQ_old"}
    assert result["access_token"] == "THQ_new"
    assert result["expires_in"] == 5183944


def test_失効したトークンは再試行せず即座に失敗する():
    calls = []

    class Session:
        def get(self, url, params=None, timeout=None):
            calls.append(url)
            return FakeResponse(status_code=400, text='{"error":{"message":"Session expired"}}')

    with pytest.raises(ThreadsAPIError, match="400"):
        refresh_long_lived_token("THQ_expired", session=Session())
    assert len(calls) == 1  # 4xx は再試行しない


# ----------------------------------------------------------------------
# 更新処理全体
# ----------------------------------------------------------------------
def test_更新した新しいトークンが暗号化してSecretsへ保存される(tmp_path, secrets, monkeypatch):
    session, client = secrets
    monkeypatch.setattr(
        token_refresher, "refresh_long_lived_token",
        lambda token: {"access_token": f"{token}_refreshed", "expires_in": 5183944},
    )

    summary = _run(tmp_path, client, monkeypatch, env={"THREADS_TOKEN_BEAUTY_LAB": "THQ_current"})

    assert summary["counts"] == {"refreshed": 1}
    # 正しいシークレット名へ、復号可能な形で保存されている
    assert session.decrypt("THREADS_TOKEN_BEAUTY_LAB") == "THQ_current_refreshed"

    entry = summary["accounts"]["beauty_lab"]
    assert entry["status"] == "refreshed"
    assert entry["days_remaining"] == 59  # 約60日
    assert entry["expires_at"] > NOW.isoformat()


def test_状態ファイルにトークンの値が書かれない(tmp_path, secrets, monkeypatch):
    session, client = secrets
    monkeypatch.setattr(
        token_refresher, "refresh_long_lived_token",
        lambda token: {"access_token": "THQ_SECRET_VALUE", "expires_in": 5183944},
    )
    _run(tmp_path, client, monkeypatch, env={"THREADS_TOKEN_BEAUTY_LAB": "THQ_current"})

    text = (tmp_path / "token_status.json").read_text(encoding="utf-8")
    assert "THQ_SECRET_VALUE" not in text
    assert "THQ_current" not in text
    status = json.loads(text)
    assert status["accounts"]["beauty_lab"]["secret_name"] == "THREADS_TOKEN_BEAUTY_LAB"
    assert "expires_at" in status["accounts"]["beauty_lab"]


def test_シークレット未登録のアカウントは失敗ではなく未登録として扱う(tmp_path, secrets, monkeypatch):
    _, client = secrets
    monkeypatch.delenv("THREADS_TOKEN_BEAUTY_LAB", raising=False)

    summary = _run(tmp_path, client, monkeypatch)

    assert summary["counts"] == {"missing": 1}
    assert "THREADS_TOKEN_BEAUTY_LAB" in summary["accounts"]["beauty_lab"]["error"]


def test_失効していたらfailedとして記録される(tmp_path, secrets, monkeypatch):
    _, client = secrets

    def boom(token):
        raise ThreadsAPIError("HTTP 400: Session has expired")

    monkeypatch.setattr(token_refresher, "refresh_long_lived_token", boom)
    summary = _run(tmp_path, client, monkeypatch, env={"THREADS_TOKEN_BEAUTY_LAB": "THQ_old"})

    assert summary["counts"] == {"failed": 1}
    assert "expired" in summary["accounts"]["beauty_lab"]["error"]


def test_1アカウントが失敗しても他は更新される(tmp_path, secrets, monkeypatch):
    session, client = secrets

    def flaky(token):
        if token == "THQ_bad":
            raise ThreadsAPIError("HTTP 400: expired")
        return {"access_token": f"{token}_new", "expires_in": 5183944}

    monkeypatch.setattr(token_refresher, "refresh_long_lived_token", flaky)
    summary = _run(
        tmp_path, client, monkeypatch,
        accounts_file=_accounts_file(tmp_path, ("good_one", "bad_one")),
        env={"THREADS_TOKEN_GOOD_ONE": "THQ_good", "THREADS_TOKEN_BAD_ONE": "THQ_bad"},
    )

    assert summary["counts"] == {"refreshed": 1, "failed": 1}
    assert session.decrypt("THREADS_TOKEN_GOOD_ONE") == "THQ_good_new"
    assert "THREADS_TOKEN_BAD_ONE" not in session.stored


def test_直近に更新済みなら見送る(tmp_path, secrets, monkeypatch):
    session, client = secrets
    monkeypatch.setattr(
        token_refresher, "refresh_long_lived_token",
        lambda token: {"access_token": "THQ_new", "expires_in": 5183944},
    )
    (tmp_path / "token_status.json").write_text(json.dumps({"accounts": {"beauty_lab": {
        "secret_name": "THREADS_TOKEN_BEAUTY_LAB", "status": "refreshed",
        "last_refreshed_at": (NOW - timedelta(hours=3)).isoformat(),
        "expires_at": (NOW + timedelta(days=59)).isoformat(),
    }}}), encoding="utf-8")

    summary = _run(tmp_path, client, monkeypatch, env={"THREADS_TOKEN_BEAUTY_LAB": "THQ_current"})
    assert summary["counts"] == {"skipped": 1}
    assert not session.stored

    # --force なら見送らない
    summary = _run(
        tmp_path, client, monkeypatch, force=True,
        env={"THREADS_TOKEN_BEAUTY_LAB": "THQ_current"},
    )
    assert summary["counts"] == {"refreshed": 1}
    assert session.decrypt("THREADS_TOKEN_BEAUTY_LAB") == "THQ_new"


def test_1日以上経過していれば更新する(tmp_path, secrets, monkeypatch):
    session, client = secrets
    monkeypatch.setattr(
        token_refresher, "refresh_long_lived_token",
        lambda token: {"access_token": "THQ_new", "expires_in": 5183944},
    )
    (tmp_path / "token_status.json").write_text(json.dumps({"accounts": {"beauty_lab": {
        "last_refreshed_at": (NOW - timedelta(days=7)).isoformat(),
    }}}), encoding="utf-8")

    summary = _run(tmp_path, client, monkeypatch, env={"THREADS_TOKEN_BEAUTY_LAB": "THQ_current"})
    assert summary["counts"] == {"refreshed": 1}


def test_ドライランでは外部APIもSecretsも触らない(tmp_path, secrets, monkeypatch):
    session, client = secrets

    def must_not_call(token):
        raise AssertionError("ドライランなのに Threads API を呼んだ")

    monkeypatch.setattr(token_refresher, "refresh_long_lived_token", must_not_call)
    summary = _run(
        tmp_path, client, monkeypatch, dry_run=True,
        env={"THREADS_TOKEN_BEAUTY_LAB": "THQ_current"},
    )
    assert summary["counts"] == {"refreshed": 1}
    assert not session.stored


def test_失敗があれば終了コード1を返す(tmp_path, secrets, monkeypatch):
    monkeypatch.setattr(
        token_refresher, "refresh_long_lived_token",
        lambda token: (_ for _ in ()).throw(ThreadsAPIError("expired")),
    )
    monkeypatch.setenv("THREADS_TOKEN_BEAUTY_LAB", "THQ_old")
    monkeypatch.setenv("WORKFLOW_TOKEN", "ghp_x")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    session, client = secrets
    monkeypatch.setattr(token_refresher, "GitHubSecretsClient", lambda *a, **k: client)

    code = token_refresher.main([
        "--now", NOW.isoformat(), "--data-dir", str(tmp_path),
        "--accounts-file", str(_accounts_file(tmp_path)),
        "--settings-file", str(tmp_path / "settings.json"), "--log-level", "CRITICAL",
    ])
    assert code == 1


def test_残り日数の計算():
    assert token_refresher.days_until((NOW + timedelta(days=30, hours=1)).isoformat(), NOW) == 30
    assert token_refresher.days_until((NOW - timedelta(days=5)).isoformat(), NOW) == 0
    assert token_refresher.days_until("", NOW) is None
    assert token_refresher.days_until("壊れた日付", NOW) is None


def test_シークレット名はアカウントIDから決まる():
    assert Account(id="beauty_lab", name="x").token_secret_name == "THREADS_TOKEN_BEAUTY_LAB"
