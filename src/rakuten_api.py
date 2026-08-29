"""楽天商品検索 API クライアント。

アカウントのテーマに沿った「売れ筋商品」を必ず 7 件取得し、
売れ筋ランキング順位（1〜7 位）を付与して返す。
"""

from __future__ import annotations

import time
from typing import Any

import requests

from .utils import env, require_env, setup_logging

logger = setup_logging(__name__)

SEARCH_ENDPOINT = "https://app.rakuten.co.jp/services/api/IchibaItem/Search/20220601"
REQUEST_TIMEOUT = 30
MAX_RETRY = 3


class RakutenAPIError(RuntimeError):
    """楽天 API 呼び出しに失敗したときの例外。"""


def _unwrap(entry: Any) -> dict[str, Any]:
    """API のバージョン差（``{"Item": {...}}`` かフラットか）を吸収する。"""
    if isinstance(entry, dict) and isinstance(entry.get("Item"), dict):
        return entry["Item"]
    return entry if isinstance(entry, dict) else {}


def _first_image(item: dict[str, Any]) -> str:
    for key in ("mediumImageUrls", "smallImageUrls"):
        images = item.get(key) or []
        if images:
            first = images[0]
            url = first.get("imageUrl") if isinstance(first, dict) else first
            if url:
                # 楽天のサムネイル URL に付く ``?_ex=128x128`` を落として原寸にする
                return str(url).split("?")[0]
    return ""


def _to_product(item: dict[str, Any], rank: int) -> dict[str, Any]:
    """API のレスポンスを、以降の処理で使う形に整える。"""
    return {
        "rank": rank,
        "item_code": item.get("itemCode", ""),
        "name": item.get("itemName", ""),
        "catch_copy": item.get("catchcopy", ""),
        "price": item.get("itemPrice"),
        "shop": item.get("shopName", ""),
        "review_count": item.get("reviewCount", 0),
        "review_average": item.get("reviewAverage", 0),
        "item_url": item.get("itemUrl", ""),
        # affiliateId 未設定時は affiliateUrl が空なので通常 URL にフォールバックする
        "affiliate_url": item.get("affiliateUrl") or item.get("itemUrl", ""),
        "image_url": _first_image(item),
    }


def _request(params: dict[str, Any]) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRY + 1):
        try:
            response = requests.get(SEARCH_ENDPOINT, params=params, timeout=REQUEST_TIMEOUT)
            if response.status_code == 429 or response.status_code >= 500:
                raise RakutenAPIError(f"HTTP {response.status_code}: {response.text[:200]}")
            if response.status_code >= 400:
                raise RakutenAPIError(
                    f"楽天 API がエラーを返しました HTTP {response.status_code}: {response.text[:300]}"
                )
            return response.json()
        except (requests.RequestException, RakutenAPIError, ValueError) as exc:
            last_error = exc
            if attempt == MAX_RETRY:
                break
            wait = 2**attempt
            logger.warning("楽天 API 再試行 %s/%s (%s 秒待機): %s", attempt, MAX_RETRY, wait, exc)
            time.sleep(wait)
    raise RakutenAPIError(f"楽天 API の呼び出しに失敗しました: {last_error}")


def search_products(
    keyword: str,
    hits: int = 7,
    genre_id: str = "",
    min_price: int | None = None,
    max_price: int | None = None,
    sort: str = "-reviewCount",
) -> list[dict[str, Any]]:
    """売れ筋商品を ``hits`` 件取得する。

    ``sort`` は楽天商品検索 API の並び順。既定の ``-reviewCount``
    （レビュー件数の多い順）を「売れ筋」の指標として使う。
    返り値の並び順がそのまま売れ筋ランキング順位（1 位から）になる。
    """
    if not keyword and not genre_id:
        raise RakutenAPIError("楽天リサーチには keyword か genre_id のどちらかが必要です。")

    params: dict[str, Any] = {
        "applicationId": require_env("RAKUTEN_APP_ID"),
        "format": "json",
        "formatVersion": 1,
        # 1 リクエストの上限は 30 件。除外分を見込んで多めに取得する。
        "hits": min(30, max(hits * 3, hits)),
        "page": 1,
        "sort": sort,
        "imageFlag": 1,
        "availability": 1,
    }
    if keyword:
        params["keyword"] = keyword
    if genre_id:
        params["genreId"] = genre_id
    if min_price:
        params["minPrice"] = int(min_price)
    if max_price:
        params["maxPrice"] = int(max_price)

    affiliate_id = env("RAKUTEN_AFFILIATE_ID")
    if affiliate_id:
        params["affiliateId"] = affiliate_id
    else:
        logger.warning(
            "RAKUTEN_AFFILIATE_ID が未設定です。アフィリエイト URL の代わりに通常の商品 URL を使います。"
        )

    payload = _request(params)
    raw_items = payload.get("Items") or []

    products: list[dict[str, Any]] = []
    seen_shops: set[str] = set()
    leftovers: list[dict[str, Any]] = []
    for entry in raw_items:
        item = _unwrap(entry)
        if not item.get("itemName"):
            continue
        shop = item.get("shopName", "")
        # 同じ店舗ばかりにならないよう、まずは 1 店舗 1 商品で埋める
        if shop and shop in seen_shops:
            leftovers.append(item)
            continue
        seen_shops.add(shop)
        products.append(item)
        if len(products) >= hits:
            break

    # 店舗の重複を避けた結果 7 件に届かない場合は、除外した商品で補充する
    for item in leftovers:
        if len(products) >= hits:
            break
        products.append(item)

    if len(products) < hits:
        raise RakutenAPIError(
            f"売れ筋商品が {hits} 件必要ですが {len(products)} 件しか取得できませんでした。"
            " keyword や価格帯の条件を緩めてください。"
        )

    return [_to_product(item, rank=i + 1) for i, item in enumerate(products[:hits])]
