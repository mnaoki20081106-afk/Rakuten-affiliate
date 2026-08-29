"""管理画面のデザイン規約（白黒ミニマル・ソリッド）が守られているか検査する。

見た目の決まりごとはレビューで見落とされやすいため、機械的に確認する。
"""

import re

from src.config import ROOT

DOCS = ROOT / "docs"
CSS = (DOCS / "style.css").read_text(encoding="utf-8")
HTML = (DOCS / "index.html").read_text(encoding="utf-8")
JS = (DOCS / "app.js").read_text(encoding="utf-8")

# 例外として認めている色（アクセシビリティ向上のため）
ALLOWED_COLORS = {
    "#ffffff", "#fff", "#000000", "#000",   # 白と黒
    "#d50000",                              # 赤: エラー・必須・削除
    "#0077b6", "#00a5e0",                   # 水色: リンク・選択中・進行中
}


def _rule(selector: str) -> str:
    """``selector { ... }`` の宣言部分だけを取り出す（結合セレクタは対象外）。"""
    css = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    match = re.search(rf"(?m)^{re.escape(selector)}\s*\{{([^}}]*)\}}", css)
    assert match, f"{selector} のルールが見つかりません"
    return match.group(1)


def _declarations(prop: str) -> list[str]:
    """style.css から指定プロパティの値を集める（コメントは除く）。"""
    css = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    return [m.group(1).strip() for m in re.finditer(rf"(?<![-\w]){prop}\s*:\s*([^;{{}}]+)", css)]


def test_外部CSSフレームワークに依存しない():
    # 角丸・影・グレーの既定値と衝突するため、CDNのフレームワークは使わない
    assert "tailwindcss.com" not in HTML
    assert "cdn." not in HTML.replace("cdn.", "", 0) or "tailwind" not in HTML
    assert "no-tailwind" not in CSS and "no-tailwind" not in JS


def test_角は丸めない():
    for value in _declarations("border-radius"):
        assert value.rstrip("0123456789 ") in ("", "0", "0px") or value in ("0", "0px"), (
            f"border-radius に 0 以外が使われています: {value}"
        )


def test_立体感の影を使わない():
    for value in _declarations("box-shadow"):
        assert value in ("none", "0"), f"box-shadow が使われています: {value}"
    assert "text-shadow" not in CSS


def test_使用している色は白黒と例外色だけ():
    css = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    used = {c.lower() for c in re.findall(r"#[0-9a-fA-F]{3,8}\b", css)}
    unexpected = used - ALLOWED_COLORS
    assert not unexpected, f"想定外の色が使われています: {sorted(unexpected)}"


def test_例外色が意図した用途で定義されている():
    assert "--danger: #d50000" in CSS       # エラー・必須・削除
    assert "--link: #0077b6" in CSS         # リンク・選択中
    assert ".btn-danger" in CSS and "var(--danger)" in CSS
    assert ".tab.active" in CSS and "var(--link)" in CSS
    assert ".alert-error" in CSS and "var(--danger)" in CSS


def test_入力欄は下線だけで表現する():
    block = _rule(".input")
    assert "border: 0" in block
    assert "border-bottom: var(--line-thick)" in block


def test_メインとサブのボタンの塗り分け():
    primary = _rule(".btn-primary")
    ghost = _rule(".btn-ghost")
    # メインは黒背景に白文字
    assert "background: var(--fg)" in primary and "color: var(--bg)" in primary
    # サブは白背景に黒文字（枠線は共通指定で黒）
    assert "background: var(--bg)" in ghost and "color: var(--fg)" in ghost


def test_サンセリフ体を指定している():
    assert "sans-serif" in CSS
    assert "serif;" not in CSS.replace("sans-serif;", "")


def test_白基調のためダークモードには追従しない():
    assert "color-scheme: light" in CSS
    assert "prefers-color-scheme" not in CSS
    assert 'content="light"' in HTML


def test_同梱ライブラリを読み込んでいる():
    assert 'src="vendor/libsodium.js"' in HTML
    assert 'src="vendor/libsodium-wrappers.js"' in HTML
