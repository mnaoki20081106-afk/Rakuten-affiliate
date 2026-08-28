"""楽天 API クライアント（正規化・ページング・順位付け）のテスト。"""

from src.rakuten_api import RakutenClient, normalize_item


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.content = b"x"
        self.text = ""

    def json(self):
        return self._payload


class FakeSession:
    """hits=30 上限を模した楽天 API のスタブ。"""

    def __init__(self, total=70):
        self.total = total
        self.requests = []

    def get(self, url, params=None, timeout=None):
        self.requests.append(params)
        page = params["page"]
        hits = params["hits"]
        start = (page - 1) * 30
        items = [
            {"itemCode": f"shop:{i}", "itemName": f"商品{i}", "itemPrice": 1000 + i,
             "itemUrl": f"https://item/{i}", "affiliateUrl": f"https://afl/{i}"}
            for i in range(start, min(start + hits, self.total))
        ]
        return FakeResponse({"Items": items})


def _client(session):
    return RakutenClient(
        application_id="app", affiliate_id="afl", session=session, request_interval=0
    )


def test_レスポンスが内部形式へ正規化される():
    item = normalize_item(
        {
            "itemCode": "shop:1",
            "itemName": "テスト商品",
            "itemPrice": "1980",
            "reviewCount": "12",
            "reviewAverage": "4.5",
            "mediumImageUrls": ["https://img/1.jpg"],
            "itemUrl": "https://item/1",
            "affiliateUrl": "https://afl/1",
        },
        keyword="化粧水",
        sort="-reviewCount",
    )
    assert item["item_code"] == "shop:1"
    assert item["price"] == 1980
    assert item["review_count"] == 12
    assert item["review_average"] == 4.5
    assert item["image_url"] == "https://img/1.jpg"
    assert item["affiliate_url"] == "https://afl/1"
    assert item["keyword"] == "化粧水"


def test_アフィリエイトURLが無ければ商品URLを使う():
    assert normalize_item({"itemCode": "c", "itemUrl": "https://item/1"})["affiliate_url"] == (
        "https://item/1"
    )


def test_formatVersion1のネスト形式にも対応する():
    assert normalize_item({"Item": {"itemCode": "shop:9", "itemName": "n"}})["item_code"] == "shop:9"


def test_50件取得するために2ページ目まで取りに行く():
    session = FakeSession(total=70)
    items = _client(session).search_items("化粧水", hits=50)

    assert len(items) == 50
    assert [p["page"] for p in session.requests] == [1, 2]
    assert session.requests[0]["hits"] == 30  # 1 リクエスト上限
    assert session.requests[1]["hits"] == 20  # 残り分だけ要求する
    assert session.requests[0]["affiliateId"] == "afl"


def test_売れ筋ランキング順位が並び順で付与される():
    items = _client(FakeSession()).search_items("化粧水", hits=50)
    assert [it["rank"] for it in items[:5]] == [1, 2, 3, 4, 5]
    assert items[0]["rank_source"] == "-reviewCount"


def test_itemCodeが重複しても1件にまとめられる():
    class DuplicateSession(FakeSession):
        def get(self, url, params=None, timeout=None):
            self.requests.append(params)
            return FakeResponse(
                {"Items": [{"itemCode": "same", "itemName": "n"} for _ in range(30)]}
            )

    items = _client(DuplicateSession()).search_items("化粧水", hits=50)
    assert len(items) == 1


def test_在庫切れが少ない場合は取得できた分だけ返す():
    items = _client(FakeSession(total=10)).search_items("化粧水", hits=50)
    assert len(items) == 10
