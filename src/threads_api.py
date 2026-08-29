"""Threads API クライアント（親投稿・リプライ投稿・インサイト取得）。"""

from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any, Iterable

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://graph.threads.net/v1.0"
# トークンの交換・更新はバージョン無しのルートに対して行う
GRAPH_ROOT = "https://graph.threads.net"
ENV_DRY_RUN = "DRY_RUN"


class ThreadsAPIError(RuntimeError):
    """Threads API 呼び出しの失敗。"""


def is_dry_run(env: dict[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return str(env.get(ENV_DRY_RUN, "")).strip().lower() in {"1", "true", "yes", "on"}


class ThreadsClient:
    """Threads Graph API の薄いラッパー。

    ``dry_run=True`` の場合は API を呼ばず擬似 ID を返す（動作確認用）。
    """

    def __init__(
        self,
        access_token: str,
        user_id: str = "",
        session: requests.Session | None = None,
        timeout: int = 30,
        dry_run: bool | None = None,
        publish_retries: int = 3,
        publish_backoff_sec: float = 5.0,
    ) -> None:
        self.access_token = (access_token or "").strip()
        self.user_id = (user_id or "").strip()
        self.session = session or requests.Session()
        self.timeout = timeout
        self.dry_run = is_dry_run() if dry_run is None else dry_run
        self.publish_retries = publish_retries
        self.publish_backoff_sec = publish_backoff_sec
        if not self.dry_run and not self.access_token:
            raise ThreadsAPIError("Threads アクセストークンが未設定です")

    # ------------------------------------------------------------------
    def _request(
        self, method: str, path: str, params: dict[str, Any] | None = None, retries: int = 3
    ) -> dict[str, Any]:
        url = f"{BASE_URL}/{path.lstrip('/')}"
        payload = dict(params or {})
        payload["access_token"] = self.access_token
        last_error: Exception | None = None
        for attempt in range(retries):
            try:
                if method.upper() == "GET":
                    resp = self.session.get(url, params=payload, timeout=self.timeout)
                else:
                    resp = self.session.post(url, data=payload, timeout=self.timeout)
                if resp.status_code >= 400:
                    detail = resp.text[:400]
                    # 4xx（認証・入力エラー）は再試行しても回復しない
                    if resp.status_code < 500 and resp.status_code != 429:
                        raise ThreadsAPIError(f"HTTP {resp.status_code}: {detail}")
                    raise requests.RequestException(f"HTTP {resp.status_code}: {detail}")
                return resp.json() if resp.content else {}
            except ThreadsAPIError:
                raise
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                logger.warning("Threads API 失敗 (%s/%s) %s: %s", attempt + 1, retries, path, exc)
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
        raise ThreadsAPIError(f"Threads API 呼び出しに失敗しました ({path}): {last_error}")

    # ------------------------------------------------------------------
    def resolve_user_id(self) -> str:
        """未設定ならトークンからユーザー ID を解決する。"""
        if self.user_id:
            return self.user_id
        if self.dry_run:
            self.user_id = "dry-run-user"
            return self.user_id
        data = self._request("GET", "me", {"fields": "id,username"})
        self.user_id = str(data.get("id", ""))
        if not self.user_id:
            raise ThreadsAPIError("Threads ユーザー ID を取得できませんでした")
        return self.user_id

    def create_container(
        self,
        text: str,
        reply_to_id: str | None = None,
        media_type: str = "TEXT",
        link_attachment: str | None = None,
    ) -> str:
        """投稿コンテナを作成し creation_id を返す。"""
        if self.dry_run:
            return f"dry-container-{uuid.uuid4().hex[:12]}"
        user_id = self.resolve_user_id()
        params: dict[str, Any] = {"media_type": media_type, "text": text}
        if reply_to_id:
            params["reply_to_id"] = reply_to_id
        if link_attachment:
            params["link_attachment"] = link_attachment
        data = self._request("POST", f"{user_id}/threads", params)
        creation_id = str(data.get("id", ""))
        if not creation_id:
            raise ThreadsAPIError(f"コンテナ作成に失敗しました: {data}")
        return creation_id

    def publish_container(self, creation_id: str) -> str:
        """コンテナを公開し media_id を返す。"""
        if self.dry_run:
            return f"dry-media-{uuid.uuid4().hex[:12]}"
        user_id = self.resolve_user_id()
        last_error: Exception | None = None
        for attempt in range(self.publish_retries):
            try:
                data = self._request(
                    "POST", f"{user_id}/threads_publish", {"creation_id": creation_id}, retries=1
                )
                media_id = str(data.get("id", ""))
                if media_id:
                    return media_id
                last_error = ThreadsAPIError(f"media_id が空です: {data}")
            except ThreadsAPIError as exc:
                # コンテナ生成直後は publish が失敗することがあるため短時間だけ再試行
                last_error = exc
            if attempt < self.publish_retries - 1:
                time.sleep(self.publish_backoff_sec)
        raise ThreadsAPIError(f"投稿の公開に失敗しました: {last_error}")

    def post_text(
        self, text: str, reply_to_id: str | None = None, link_attachment: str | None = None
    ) -> str:
        """テキスト投稿（またはリプライ）を送信し media_id を返す。"""
        creation_id = self.create_container(
            text, reply_to_id=reply_to_id, link_attachment=link_attachment
        )
        return self.publish_container(creation_id)

    # ------------------------------------------------------------------
    def get_insights(
        self, media_id: str, metrics: Iterable[str] = ("likes", "views", "replies", "reposts")
    ) -> dict[str, int]:
        """投稿のインサイト（いいね数など）を取得する。"""
        if self.dry_run or not media_id or str(media_id).startswith("dry-"):
            return {}
        try:
            data = self._request(
                "GET", f"{media_id}/insights", {"metric": ",".join(metrics)}, retries=2
            )
        except ThreadsAPIError as exc:
            logger.warning("インサイト取得に失敗しました media_id=%s: %s", media_id, exc)
            return {}
        result: dict[str, int] = {}
        for entry in data.get("data", []) or []:
            name = entry.get("name")
            value = 0
            values = entry.get("values") or []
            if values and isinstance(values[0], dict):
                value = values[0].get("value", 0)
            elif "total_value" in entry:
                value = (entry.get("total_value") or {}).get("value", 0)
            if name:
                try:
                    result[name] = int(value)
                except (TypeError, ValueError):
                    result[name] = 0
        return result


def refresh_long_lived_token(
    access_token: str,
    session: requests.Session | None = None,
    timeout: int = 30,
    retries: int = 3,
) -> dict[str, Any]:
    """長寿命アクセストークンを更新し、新しいトークンと有効期間を返す。

    Threads API の仕様上、対象のトークンは
    「発行から 24 時間以上経過していて、かつ未失効」である必要がある。
    戻り値: ``{"access_token": str, "expires_in": int}``
    """
    session = session or requests.Session()
    params = {"grant_type": "th_refresh_token", "access_token": access_token}
    last_error: Exception | None = None

    for attempt in range(retries):
        try:
            resp = session.get(
                f"{GRAPH_ROOT}/refresh_access_token", params=params, timeout=timeout
            )
            if resp.status_code >= 400:
                detail = resp.text[:400]
                # 4xx は失効・権限不足など、再試行しても回復しない
                if resp.status_code < 500 and resp.status_code != 429:
                    raise ThreadsAPIError(f"HTTP {resp.status_code}: {detail}")
                raise requests.RequestException(f"HTTP {resp.status_code}: {detail}")

            data = resp.json()
            token = str(data.get("access_token", ""))
            if not token:
                raise ThreadsAPIError(f"新しいトークンを取得できませんでした: {data}")
            return {
                "access_token": token,
                "expires_in": int(data.get("expires_in", 0) or 0),
                "token_type": str(data.get("token_type", "")),
            }
        except ThreadsAPIError:
            raise
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            logger.warning("トークン更新に失敗 (%s/%s): %s", attempt + 1, retries, exc)
            if attempt < retries - 1:
                time.sleep(2 ** attempt)

    raise ThreadsAPIError(f"トークンの更新に失敗しました: {last_error}")


def build_pr_reply(affiliate_url: str, pr_text: str = "※PR", lead: str = "") -> str:
    """コメント欄用のアフィリエイトリプライ本文を組み立てる。"""
    lines = []
    if lead:
        lines.append(lead)
    lines.append(pr_text)
    if affiliate_url:
        lines.append(affiliate_url)
    return "\n".join(line for line in lines if line)
