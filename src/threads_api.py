"""Threads Graph API クライアント。

親投稿の作成 → 公開 → 同じ投稿へのリプライ（PR 表記付きアフィリエイト URL）と、
再投稿機能で使うインサイト（いいね数など）の取得を担当する。

Threads の投稿は 2 段階。
1. メディアコンテナを作成する (``POST /{user_id}/threads``)
2. コンテナを公開する (``POST /{user_id}/threads_publish``)
"""

from __future__ import annotations

import time
from typing import Any

import requests

from .utils import setup_logging

logger = setup_logging(__name__)

API_BASE = "https://graph.threads.net/v1.0"
REQUEST_TIMEOUT = 30
MAX_RETRY = 3
# コンテナ作成から公開までの待機秒数（Threads の仕様上、少し間隔を空ける必要がある）
PUBLISH_WAIT_SECONDS = 5
# Threads 本文の上限
TEXT_LIMIT = 500


class ThreadsAPIError(RuntimeError):
    """Threads API 呼び出しに失敗したときの例外。"""


class ThreadsClient:
    """1 アカウント分の Threads API クライアント。"""

    def __init__(self, access_token: str, user_id: str = "", publish_wait: int = PUBLISH_WAIT_SECONDS):
        if not access_token:
            raise ThreadsAPIError("Threads のアクセストークンが空です。")
        self.access_token = access_token
        self._user_id = str(user_id or "").strip()
        self.publish_wait = publish_wait

    # --- 低レベル ---------------------------------------------------------
    def _call(self, method: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{API_BASE}/{path.lstrip('/')}"
        payload = {**params, "access_token": self.access_token}
        last_error: Exception | None = None

        for attempt in range(1, MAX_RETRY + 1):
            try:
                if method == "GET":
                    response = requests.get(url, params=payload, timeout=REQUEST_TIMEOUT)
                else:
                    response = requests.post(url, data=payload, timeout=REQUEST_TIMEOUT)

                if response.status_code == 429 or response.status_code >= 500:
                    raise ThreadsAPIError(f"HTTP {response.status_code}: {response.text[:200]}")
                body = response.json() if response.content else {}
                if response.status_code >= 400:
                    message = (body.get("error") or {}).get("message", response.text[:300])
                    # 4xx は再試行しても直らないのでそのまま失敗させる
                    raise ThreadsAPIError(f"Threads API エラー HTTP {response.status_code}: {message}")
                return body
            except ThreadsAPIError as exc:
                if "HTTP 4" in str(exc):
                    raise
                last_error = exc
            except (requests.RequestException, ValueError) as exc:
                last_error = exc

            if attempt < MAX_RETRY:
                wait = 2**attempt
                logger.warning("Threads API 再試行 %s/%s (%s 秒待機): %s", attempt, MAX_RETRY, wait, last_error)
                time.sleep(wait)

        raise ThreadsAPIError(f"Threads API の呼び出しに失敗しました: {last_error}")

    # --- ユーザー ---------------------------------------------------------
    @property
    def user_id(self) -> str:
        """Threads のユーザー ID。未設定ならトークンから解決する。"""
        if not self._user_id:
            data = self._call("GET", "me", {"fields": "id,username"})
            self._user_id = str(data.get("id", ""))
            if not self._user_id:
                raise ThreadsAPIError("アクセストークンから Threads ユーザー ID を取得できませんでした。")
            logger.info("Threads ユーザー ID を解決しました: %s", self._user_id)
        return self._user_id

    def get_profile(self) -> dict[str, Any]:
        """接続確認用にプロフィールを取得する。"""
        return self._call("GET", "me", {"fields": "id,username,threads_profile_picture_url"})

    # --- 投稿 -------------------------------------------------------------
    def _create_container(self, text: str, reply_to_id: str = "") -> str:
        if not text.strip():
            raise ThreadsAPIError("投稿本文が空です。")
        if len(text) > TEXT_LIMIT:
            logger.warning("本文が %s 文字を超えているため末尾を切り詰めます。", TEXT_LIMIT)
            text = text[: TEXT_LIMIT - 1] + "…"

        params: dict[str, Any] = {"media_type": "TEXT", "text": text}
        if reply_to_id:
            params["reply_to_id"] = reply_to_id

        data = self._call("POST", f"{self.user_id}/threads", params)
        container_id = str(data.get("id", ""))
        if not container_id:
            raise ThreadsAPIError(f"メディアコンテナの作成に失敗しました: {data}")
        return container_id

    def _publish_container(self, container_id: str) -> str:
        data = self._call("POST", f"{self.user_id}/threads_publish", {"creation_id": container_id})
        post_id = str(data.get("id", ""))
        if not post_id:
            raise ThreadsAPIError(f"投稿の公開に失敗しました: {data}")
        return post_id

    def publish_text(self, text: str, reply_to_id: str = "") -> str:
        """テキスト投稿を作成して公開し、投稿 ID を返す。

        ``reply_to_id`` を渡すとその投稿へのリプライになる。
        """
        container_id = self._create_container(text, reply_to_id=reply_to_id)
        if self.publish_wait:
            time.sleep(self.publish_wait)
        post_id = self._publish_container(container_id)
        logger.info("投稿しました post_id=%s (reply_to=%s)", post_id, reply_to_id or "-")
        return post_id

    def publish_with_pr_reply(self, body: str, reply_text: str) -> dict[str, str]:
        """親投稿を送信し、続けてコメント欄に PR リプライを送信する。

        親投稿が成功していればリプライが失敗しても投稿自体は成功として扱い、
        エラー内容を戻り値に含める。
        """
        post_id = self.publish_text(body)
        result = {"post_id": post_id, "reply_id": "", "reply_error": ""}
        try:
            result["reply_id"] = self.publish_text(reply_text, reply_to_id=post_id)
        except ThreadsAPIError as exc:
            logger.error("PR リプライの送信に失敗しました post_id=%s: %s", post_id, exc)
            result["reply_error"] = str(exc)
        return result

    # --- インサイト -------------------------------------------------------
    def get_post_insights(self, post_id: str) -> dict[str, int]:
        """投稿のいいね数・表示数・返信数を取得する。"""
        data = self._call(
            "GET", f"{post_id}/insights", {"metric": "likes,views,replies,reposts,quotes"}
        )
        metrics: dict[str, int] = {}
        for entry in data.get("data", []) or []:
            name = entry.get("name")
            values = entry.get("values") or []
            value = entry.get("total_value", {}).get("value") if isinstance(entry.get("total_value"), dict) else None
            if value is None and values:
                value = values[0].get("value") if isinstance(values[0], dict) else values[0]
            if name is not None and value is not None:
                metrics[name] = int(value)
        return metrics


def build_pr_reply(affiliate_url: str, template: str = "") -> str:
    """コメント欄に投稿する PR リプライ本文を組み立てる。"""
    default = "▼詳細・購入はこちら\n{url}\n\n※PR"
    text = (template or default).strip() or default
    if "{url}" in text:
        text = text.replace("{url}", affiliate_url)
    else:
        text = f"{text}\n{affiliate_url}"
    if "※PR" not in text and "#PR" not in text:
        text = f"{text}\n\n※PR"
    return text
