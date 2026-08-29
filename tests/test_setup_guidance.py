"""管理画面に「取得先へすぐ飛べる案内」が備わっているかを検査する。

設定作業でつまずきやすいのは「どこで取るのか分からない」点なので、
取得先のリンクが画面内に存在することを機械的に確認する。
"""

import re

from src.config import ROOT

DOCS = ROOT / "docs"
HTML = (DOCS / "index.html").read_text(encoding="utf-8")
JS = (DOCS / "app.js").read_text(encoding="utf-8")


def test_ログイン画面にトークンの取得手順とリンクがある():
    login = HTML[HTML.index('id="login-screen"') : HTML.index('id="app"')]
    # 必要な権限にチェックが入った状態で開くリンク
    assert "github.com/settings/tokens/new?scopes=repo" in login
    assert "Generate token" in login
    assert "二度と表示されません" in login  # コピーし忘れの警告


def test_APIキーの取得先が4つとも用意されている():
    for host in (
        "console.anthropic.com",        # Claude
        "webservice.rakuten.co.jp",     # 楽天アプリID
        "affiliate.rakuten.co.jp",      # 楽天アフィリエイトID
    ):
        assert host in JS, f"{host} へのリンクがありません"
    # WORKFLOW_TOKEN は権限つきの作成URLを使う
    assert "scopes=repo,workflow" in JS


def test_APIキーごとに取得手順が書かれている():
    # GLOBAL_SECRETS の各項目に steps があること
    block = JS[JS.index("const GLOBAL_SECRETS = [") : JS.index("// src/config.py の DEFAULT_SETTINGS")]
    entries = block.count("name:")
    assert entries >= 4, entries
    assert block.count("steps: [") == entries, "取得手順が無いAPIキーがあります"
    assert "取得のしかた" in JS


def test_アカウント編集画面にThreadsトークンの取得案内がある():
    modal = HTML[HTML.index('id="account-modal"') :]
    assert "developers.facebook.com" in modal
    for scope in ("threads_basic", "threads_content_publish", "threads_manage_insights"):
        assert scope in modal, f"{scope} の案内がありません"
    # 短命トークンを長寿命へ交換する導線
    assert "exchange-secret" in modal and "exchange-short" in modal


def test_長寿命トークンの交換URLを組み立てている():
    assert "graph.threads.net/access_token" in JS
    assert "th_exchange_token" in JS
    # 入力値はブラウザ内だけで使い、どこにも送信しない
    assert "どこにも送信されません" in HTML


def test_接続先はページのURLから自動判定する():
    # https://<オーナー名>.github.io/<リポジトリ名>/ から求める
    assert "github\\.io$" in JS
    assert "location.pathname.split" in JS
    # フォークした人ごとに変わるため、決め打ちの値をプレースホルダに置かない
    login = HTML[HTML.index('id="login-screen"') : HTML.index('id="app"')]
    for field in ("login-owner", "login-repo"):
        tag = re.search(rf'<input id="{field}"[^>]*>', login).group(0)
        assert "placeholder=" not in tag, f"{field} に決め打ちのプレースホルダがあります"


def test_未完了のステップから該当画面へ直接飛べる():
    assert 'data-goto=' in JS                       # タブ切り替え
    assert "actions/workflows/batch.yml" in JS      # 初回バッチ
    assert "actions/workflows/token_refresh.yml" in JS


def test_案内用のタブは増やさない():
    # 慣れた利用者の邪魔になるため、手順は各画面の中に置く
    tabs = re.findall(r'data-view="([a-z]+)"', HTML)
    assert tabs == ["dashboard", "accounts", "secrets", "settings", "prompt"], tabs


def test_開発モードでの運用手順が案内されている():
    # 最初は開発モードのままで運用できることと、テスター追加が必要なことを明示する
    assert "テスターに追加" in JS
    assert "開発モード" in JS
    assert "自分のアカウントを運用するだけなら、開発モードのままで問題ありません" in JS
    # ライブモードにするには審査が要ることも書く
    assert "アプリレビュー" in JS


def test_審査に備えた文書ページが用意されている():
    for name, heading in (("privacy.html", "プライバシーポリシー"), ("terms.html", "利用規約")):
        path = DOCS / name
        assert path.is_file(), f"docs/{name} がありません"
        text = path.read_text(encoding="utf-8")
        assert heading in text
        # 記入が必要なひな形であることを明示しておく
        assert "ひな形" in text
        assert "記入" in text
    # 管理画面から URL を渡せるようにしてある
    assert "privacy.html" in JS and "terms.html" in JS


def test_リダイレクトURLはディレクトリに正規化される():
    # Meta は完全一致で照合するため、index.html の有無で揺れてはいけない
    assert 'location.pathname.replace(/[^/]*$/, "")' in JS
