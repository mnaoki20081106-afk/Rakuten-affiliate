"""楽天市場 商品検索 API クライアント。

「売れ筋ランキング順位」は、人気順（既定は ``-reviewCount``）で取得した検索結果
の並び順（1 始まり）を順位として扱う。
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Iterable

import requests

from src.config import ENV_RAKUTEN_AFFILIATE_ID, ENV_RAKUTEN_APP_ID

logger = logging.getLogger(__name__)

SEARCH_ENDPOINT = "https://app.rakuten.co.jp/services/api/IchibaItem/Search/20220601"
MAX_HITS_PER_REQUEST = 30  # 楽天 API の 1 リクエスト上限
REQUEST_INTERVAL_SEC = 1.0  # 楽天 API のレート制限対策（1 リクエスト/秒）


class RakutenAPIError(RuntimeError):
    """楽天 API 呼び出しの失敗。"""


class RakutenClient:
    def __init__(
        self,
        application_id: str | None = None,
        affiliate_id: str | None = None,
        session: requests.Session | None = None,
        timeout: int = 20,
        request_interval: float = REQUEST_INTERVAL_SEC,
    ) -> None:
        self.application_id = (application_id or os.environ.get(ENV_RAKUTEN_APP_ID, "")).strip()
        self.affiliate_id = (affiliate_id or os.environ.get(ENV_RAKUTEN_AFFILIATE_ID, "")).strip()
        self.session = session or requests.Session()
        self.timeout = timeout
        self.request_interval = request_interval
        self._last_request_at = 0.0

    # ------------------------------------------------------------------
    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if self._last_request_at and elapsed < self.request_interval:
            time.sleep(self.request_interval - elapsed)
        self._last_request_at = time.monotonic()

    def _request(self, params: dict[str, Any], retries: int = 3) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(retries):
            self._throttle()
            try:
                resp = self.session.get(SEARCH_ENDPOINT, params=params, timeout=self.timeout)
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise RakutenAPIError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                if resp.status_code >= 400:
                    raise RakutenAPIError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                return resp.json()
            except (requests.RequestException, RakutenAPIError, ValueError) as exc:
                last_error = exc
                wait = 2 ** attempt
                logger.warning("楽天 API 呼び出し失敗 (%s/%s): %s", attempt + 1, retries, exc)
                if attempt < retries - 1:
                    time.sleep(wait)
        raise RakutenAPIError(f"楽天 API 呼び出しに失敗しました: {last_error}")

    # ------------------------------------------------------------------
    def search_items(
        self,
        keyword: str,
        hits: int = 50,
        sort: str = "-reviewCount",
        min_price: int = 0,
        max_price: int = 0,
        ng_keywords: Iterable[str] | None = None,
        affiliate_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """``hits`` 件になるまでページングして商品を取得し、正規化して返す。

        戻り値の各要素には ``rank``（1 始まりの売れ筋ランキング順位）を含む。
        """
        if not self.application_id:
            raise RakutenAPIError(
                f"楽天アプリ ID が未設定です（環境変数 {ENV_RAKUTEN_APP_ID} を設定してください）"
            )
        if not keyword:
            return []

        affiliate = (affiliate_id or self.affiliate_id).strip()
        ng = " ".join(k for k in (ng_keywords or []) if k)
        collected: list[dict[str, Any]] = []
        seen: set[str] = set()
        page = 1
        while len(collected) < hits and page <= 10:
            params: dict[str, Any] = {
                "applicationId": self.application_id,
                "keyword": keyword,
                "hits": min(MAX_HITS_PER_REQUEST, hits - len(collected)),
                "page": page,
                "sort": sort,
                "format": "json",
                "formatVersion": 2,
                "imageFlag": 1,
                "availability": 1,
            }
            if affiliate:
                params["affiliateId"] = affiliate
            if min_price:
                params["minPrice"] = int(min_price)
            if max_price:
                params["maxPrice"] = int(max_price)
            if ng:
                params["NGKeyword"] = ng

            payload = self._request(params)
            items = payload.get("Items") or []
            if not items:
                break
            for raw in items:
                item = normalize_item(raw, keyword=keyword, sort=sort)
                code = item.get("item_code")
                if not code or code in seen:
                    continue
                seen.add(code)
                item["rank"] = len(collected) + 1
                collected.append(item)
                if len(collected) >= hits:
                    break
            if len(items) < MAX_HITS_PER_REQUEST:
                break
            page += 1
        return collected


def normalize_item(raw: dict[str, Any], keyword: str = "", sort: str = "") -> dict[str, Any]:
    """楽天 API のレスポンス（formatVersion=2）を内部形式へ変換する。"""
    # formatVersion=1 で渡された場合にも対応する
    if "Item" in raw and isinstance(raw["Item"], dict):
        raw = raw["Item"]

    images = raw.get("mediumImageUrls") or raw.get("smallImageUrls") or []
    image_url = ""
    if images:
        first = images[0]
        image_url = first.get("imageUrl", "") if isinstance(first, dict) else str(first)

    def _to_int(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def _to_float(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    return {
        "item_code": str(raw.get("itemCode", "")),
        "item_name": str(raw.get("itemName", "")),
        "catch_copy": str(raw.get("catchcopy", "")),
        "caption": str(raw.get("itemCaption", ""))[:800],
        "price": _to_int(raw.get("itemPrice")),
        "shop_name": str(raw.get("shopName", "")),
        "review_count": _to_int(raw.get("reviewCount")),
        "review_average": _to_float(raw.get("reviewAverage")),
        "item_url": str(raw.get("itemUrl", "")),
        "affiliate_url": str(raw.get("affiliateUrl") or raw.get("itemUrl") or ""),
        "image_url": image_url,
        "genre_id": str(raw.get("genreId", "")),
        "keyword": keyword,
        "rank_source": sort,
        "rank": 0,
    }
