"""Streamlit Community Cloud から GitHub リポジトリへ設定を書き戻すためのクライアント。

Streamlit Cloud のファイルシステムは再デプロイで失われるため、管理画面での変更は
GitHub Contents API 経由でコミットして状態を維持する。
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)

API_ROOT = "https://api.github.com"


class GitHubSyncError(RuntimeError):
    """GitHub API 操作の失敗。"""


class GitHubSync:
    def __init__(
        self,
        token: str,
        repo: str,
        branch: str = "main",
        session: requests.Session | None = None,
        timeout: int = 30,
    ) -> None:
        """``repo`` は ``owner/name`` 形式。"""
        self.token = (token or "").strip()
        self.repo = (repo or "").strip()
        self.branch = (branch or "main").strip()
        self.session = session or requests.Session()
        self.timeout = timeout
        if not self.token or not self.repo:
            raise GitHubSyncError("GitHub トークンとリポジトリ名（owner/name）が必要です")

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _url(self, path: str) -> str:
        return f"{API_ROOT}/repos/{self.repo}/contents/{path.lstrip('/')}"

    def get_file(self, path: str) -> tuple[str, str]:
        """ファイル内容と blob SHA を返す。存在しない場合は ``("", "")``。"""
        resp = self.session.get(
            self._url(path), headers=self._headers, params={"ref": self.branch}, timeout=self.timeout
        )
        if resp.status_code == 404:
            return "", ""
        if resp.status_code >= 400:
            raise GitHubSyncError(f"取得に失敗しました ({resp.status_code}): {resp.text[:300]}")
        payload = resp.json()
        content = base64.b64decode(payload.get("content", "")).decode("utf-8")
        return content, payload.get("sha", "")

    def put_file(self, path: str, content: str, message: str, sha: str | None = None) -> dict[str, Any]:
        """ファイルを作成／更新する。``sha`` 未指定なら現在の SHA を取得して使う。"""
        if sha is None:
            _, sha = self.get_file(path)
        body: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": self.branch,
        }
        if sha:
            body["sha"] = sha
        resp = self.session.put(
            self._url(path), headers=self._headers, json=body, timeout=self.timeout
        )
        if resp.status_code >= 400:
            raise GitHubSyncError(f"保存に失敗しました ({resp.status_code}): {resp.text[:300]}")
        return resp.json()

    def put_json(self, path: str, data: Any, message: str) -> dict[str, Any]:
        content = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        return self.put_file(path, content, message)
