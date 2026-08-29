"""GitHub Actions Secrets へ値を書き込むクライアント（暗号化つき）。

Secrets は「リポジトリの公開鍵で封をした（sealed box）」暗号文しか受け付けない。
更新後の Threads トークンを自動でしまい直すために使う。

必要な権限: classic PAT の ``repo`` スコープ
（fine-grained なら ``Secrets: Read and write``）。
既定の ``GITHUB_TOKEN`` では書き込めないため、``WORKFLOW_TOKEN`` を使う。
"""

from __future__ import annotations

import base64
import logging
import os
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

API_ROOT = "https://api.github.com"
ENV_WORKFLOW_TOKEN = "WORKFLOW_TOKEN"
ENV_GITHUB_TOKEN = "GITHUB_TOKEN"
ENV_GITHUB_REPOSITORY = "GITHUB_REPOSITORY"  # GitHub Actions が自動で設定する "owner/repo"


class GitHubSecretsError(RuntimeError):
    """Secrets API 操作の失敗。"""


def encrypt_secret(public_key_base64: str, value: str) -> str:
    """リポジトリの公開鍵で値を封をする（GitHub が要求する sealed box 方式）。"""
    try:
        from nacl import encoding, public
    except ImportError as exc:  # pragma: no cover - 依存未導入時のみ
        raise GitHubSecretsError(
            "PyNaCl が導入されていません（pip install -r requirements.txt）"
        ) from exc

    key = public.PublicKey(public_key_base64.encode("utf-8"), encoding.Base64Encoder())
    sealed = public.SealedBox(key).encrypt(value.encode("utf-8"))
    return base64.b64encode(sealed).decode("utf-8")


class GitHubSecretsClient:
    def __init__(
        self,
        token: str | None = None,
        repository: str | None = None,
        session: requests.Session | None = None,
        timeout: int = 30,
    ) -> None:
        """``repository`` は ``owner/repo`` 形式。"""
        env = os.environ
        self.token = (token or env.get(ENV_WORKFLOW_TOKEN) or env.get(ENV_GITHUB_TOKEN) or "").strip()
        self.repository = (repository or env.get(ENV_GITHUB_REPOSITORY, "")).strip()
        self.session = session or requests.Session()
        self.timeout = timeout
        self._public_key: dict[str, Any] | None = None

        if not self.token:
            raise GitHubSecretsError(
                f"GitHub トークンが未設定です（環境変数 {ENV_WORKFLOW_TOKEN}）"
            )
        if "/" not in self.repository:
            raise GitHubSecretsError(
                f"リポジトリ名が不正です（環境変数 {ENV_GITHUB_REPOSITORY} = "
                f"'{self.repository}'）。owner/repo 形式で指定してください"
            )

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _request(self, method: str, path: str, json_body: Any = None, retries: int = 3) -> Any:
        url = f"{API_ROOT}/repos/{self.repository}{path}"
        last_error: Exception | None = None
        for attempt in range(retries):
            try:
                resp = self.session.request(
                    method, url, headers=self._headers, json=json_body, timeout=self.timeout
                )
                if resp.status_code == 404:
                    raise GitHubSecretsError(
                        f"404 Not Found: {path} / リポジトリ名と、トークンの repo 権限を確認してください"
                    )
                if resp.status_code == 403:
                    raise GitHubSecretsError(
                        f"403 Forbidden: {path} / トークンに Secrets の書き込み権限がありません"
                    )
                if 400 <= resp.status_code < 500 and resp.status_code != 429:
                    raise GitHubSecretsError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                if resp.status_code >= 500 or resp.status_code == 429:
                    raise requests.RequestException(f"HTTP {resp.status_code}")
                return resp.json() if resp.content else None
            except GitHubSecretsError:
                raise
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                logger.warning("GitHub API 失敗 (%s/%s) %s: %s", attempt + 1, retries, path, exc)
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
        raise GitHubSecretsError(f"GitHub API 呼び出しに失敗しました ({path}): {last_error}")

    def get_public_key(self) -> dict[str, Any]:
        """リポジトリの公開鍵を取得する（1 インスタンスにつき 1 回）。"""
        if self._public_key is None:
            self._public_key = self._request("GET", "/actions/secrets/public-key")
        return self._public_key

    def put_secret(self, name: str, value: str) -> None:
        """シークレットを暗号化して作成／上書きする。"""
        public_key = self.get_public_key()
        payload = {
            "encrypted_value": encrypt_secret(public_key["key"], value),
            "key_id": public_key["key_id"],
        }
        self._request("PUT", f"/actions/secrets/{name}", json_body=payload)
        logger.info("シークレットを更新しました: %s", name)

    def delete_secret(self, name: str) -> None:
        """シークレットを削除する（存在しない場合は何もしない）。"""
        try:
            self._request("DELETE", f"/actions/secrets/{name}")
        except GitHubSecretsError as exc:
            if "404" not in str(exc):
                raise
            return
        logger.info("シークレットを削除しました: %s", name)
