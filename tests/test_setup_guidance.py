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
    assert "Threadsテスターの追加/削除" in JS       # Metaの画面にある実際の項目名
    assert "ウェブサイトのアクセス許可" in JS        # スマホ側での承認手順
    assert "Pending" in JS                           # 追加直後の状態
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


def test_Threads連携の手順が実際の画面の項目名で書かれている():
    """Metaの画面に実在する項目名で案内していること。

    以前は記憶に頼って書いたためクリック経路が実際と一致せず、
    手順どおりに進めない状態だった。実際の項目名で固定しておく。
    """
    for label in (
        "Threads APIの使用",              # アプリ作成時のユースケース
        "Threads API の設定画面（Settings）",
        "コールバックURL（Redirect URI）",
        "アプリID（Client ID）",
        "アプリシークレット（Client Secret）",
        "Threadsテスターの追加/削除",
        "ウェブサイトのアクセス許可",      # スマホ側での承認場所
    ):
        assert label in JS, f"「{label}」の案内がありません"


def test_連携の準備は1画面ずつ進むウィザードになっている():
    # 似た横文字が同時に並ぶと迷うため、1画面に1つの作業だけを出す
    assert "const SETUP_STEPS = [" in JS
    keys = re.findall(r'key: "(\w+)"', JS[JS.index("const SETUP_STEPS = ["):])
    assert keys[:7] == ["create", "callback", "appid", "appsecret", "tester", "approve", "done"], keys[:7]
    assert "renderWizard" in JS and "wiz-next" in JS and "wiz-prev" in JS


def test_手順の本文列は幅いっぱいに広がる():
    # 行ごとに幅が変わると左右がちぐはぐに見えるため
    css = (DOCS / "style.css").read_text(encoding="utf-8")
    assert ".steps li > :last-child" in css
    assert "flex: 1 1 auto" in css


def test_ウィザードは画面全体を占める():
    """背景のページが見えていると集中できないため、ダイアログではなく専用画面にする。"""
    css = (DOCS / "style.css").read_text(encoding="utf-8")
    block = css[css.index(".wizard {") : css.index(".wizard-inner")]
    assert "position: fixed" in block
    assert "inset: 0" in block
    assert "background: var(--bg)" in block          # 背後を透かさない
    # 上に進捗、下に移動ボタンを固定し、本文だけがスクロールする
    assert ".wizard-bar" in css and ".wizard-foot" in css
    assert "overflow-y: auto" in css[css.index(".wizard-scroll") : css.index(".wizard-foot {")]
    for slot in ("setup-count", "setup-dots", "setup-body", "setup-foot", "setup-scroll"):
        assert f'id="{slot}"' in HTML, f"{slot} がありません"


def test_通知が積み上がって画面を覆わない():
    assert "area.children.length > 3" in JS
