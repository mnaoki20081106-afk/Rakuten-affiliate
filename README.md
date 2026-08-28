# Rakuten-affiliate — Threads × 楽天アフィリエイト SNS 自動運用システム

Threads API・Claude API・楽天商品検索 API を組み合わせ、**最大 10 アカウント規模の SNS 運用を完全自動化**する
サーバーレス構成のプロジェクトです。実行環境は **GitHub Actions**（公開リポジトリ想定）と
**Streamlit Community Cloud**（管理画面）で、状態はすべてリポジトリ内の JSON / YAML に保存し、
処理のたびに `git commit` & `git push` して永続化します。

```
                     ┌──────────────────────────┐
   Streamlit 管理画面 │ アカウント / テーマ / 設定 │──► config/accounts.json
   (Community Cloud)  └──────────────────────────┘        （GitHub API でコミット）
                                   │
   毎日 JST 23:00 ─► batch.yml ─► src/batch_generator.py
                                   │  楽天検索 → 重複除外 → 7件抽選 → 悩み生成 → 投稿生成
                                   │  → 時間割り当て（ゆらぎ + ゴールデンタイム優先）
                                   ├─► data/queue.json / data/used_items.json
                                   └─► .github/workflows/publisher*.yml を動的生成（cron）
                                   │
   翌日の予約時刻 ─► publisher*.yml ─► src/publisher.py
                                   │  親投稿 → 直後に「アフィリエイトURL + ※PR」を子投稿
                                   └─► data/post_history.json
                                   │
   月・水・金 19:30 ─► reposter.yml ─► src/reposter.py
                                      過去1週間のいいね上位3件を「8件目の投稿」として再投稿
```

---

## 1. 構成

| パス | 役割 |
| --- | --- |
| `app.py` | Streamlit 管理画面（アカウント登録・キュー確認・履歴・設定・プロンプト編集） |
| `src/batch_generator.py` | 【前日バッチ】リサーチ・生成・時間予約・配信用 YAML の動的生成 |
| `src/publisher.py` | 【当日配信】予約時刻に起動し、親投稿＋PR リプライを即時送信 |
| `src/reposter.py` | 【再投稿】過去にバズった投稿を月・水・金に再投稿 |
| `src/rakuten_api.py` | 楽天商品検索 API クライアント（ページング・順位付け） |
| `src/claude_api.py` | Claude API クライアント（悩み生成・投稿生成・「伸びる確率」パース） |
| `src/threads_api.py` | Threads API クライアント（投稿・リプライ・インサイト） |
| `src/scheduler.py` | 時間枠の分割・ゆらぎ・ゴールデンタイム優先割り当て・cron 変換 |
| `src/workflow_generator.py` | `publisher*.yml` の生成（cron 60 件ごとに自動分割） |
| `src/github_sync.py` | Streamlit から GitHub へ設定をコミットするクライアント |
| `config/accounts.json` | アカウント定義（テーマ・トークン参照先） |
| `config/settings.json` | 共通設定（投稿数・活動時間帯・ゆらぎ・除外日数など） |
| `data/queue.json` | 翌日分の予約投稿（アカウント別） |
| `data/used_items.json` | 紹介済み商品の履歴（重複防止用） |
| `data/post_history.json` | 投稿履歴（いいね数・再投稿の判定に使用） |
| `prompts/Claude×アフィリエイト投稿作成プロンプト.txt` | 投稿生成プロンプト（**空の場合は組み込みの既定プロンプトを使用**） |

---

## 2. セットアップ

### 2.1 必要な API 資格情報

| 名前 | 取得元 | 用途 |
| --- | --- | --- |
| `RAKUTEN_APP_ID` | [楽天ウェブサービス](https://webservice.rakuten.co.jp/) | 商品検索 |
| `RAKUTEN_AFFILIATE_ID` | 楽天アフィリエイト | アフィリエイト URL の生成 |
| `ANTHROPIC_API_KEY` | [Anthropic Console](https://console.anthropic.com/) | 悩み・投稿の生成 |
| `THREADS_TOKEN_<アカウントID大文字>` | Meta for Developers（Threads API） | 各アカウントの投稿 |
| `WORKFLOW_TOKEN` | GitHub PAT（`repo` + `workflow` スコープ） | 配信用 YAML の書き換えプッシュ |

> **`WORKFLOW_TOKEN` は必須です。** GitHub Actions の既定の `GITHUB_TOKEN` では
> `.github/workflows/` 配下の変更をプッシュできません（GitHub 側の制限）。
> `workflow` スコープを持つ PAT を `WORKFLOW_TOKEN` という名前で Secrets に登録してください。

### 2.2 GitHub Secrets の登録

リポジトリの **Settings → Secrets and variables → Actions** に上記をすべて登録します。
Threads トークンのシークレット名は管理画面の「アカウント管理」ページ下部に一覧表示されます。

```
RAKUTEN_APP_ID
RAKUTEN_AFFILIATE_ID
ANTHROPIC_API_KEY
WORKFLOW_TOKEN
THREADS_TOKEN_BEAUTY_LAB      # アカウントごとに 1 つ
THREADS_TOKEN_GADGET_NOTE
...
```

### 2.3 アカウントの登録

`config/accounts.example.json` を参考に、Streamlit 管理画面（または直接 JSON 編集）で登録します。

```jsonc
{
  "accounts": [
    {
      "id": "beauty_lab",              // 英数字。シークレット名にも使われる
      "name": "コスメ研究ノート",
      "enabled": true,
      "genre": "プチプラコスメ・スキンケア",     // テーマ①: 発信ジャンル
      "worldview": "深夜の洗面所でひとりごと",   // テーマ②: 世界観
      "strength": "全部自腹で試して正直に書く",   // テーマ③: 強み
      "tone": "親しみやすい丁寧語",
      "target": "20代後半〜30代前半の会社員",
      "search_keywords": ["プチプラ スキンケア", "毛穴 化粧水"],
      "threads_user_id": "",           // 空なら /me から自動取得
      "threads_token_env": "THREADS_TOKEN_BEAUTY_LAB",  // 推奨: Secrets 参照
      "threads_access_token": "",       // 非推奨: 公開リポジトリでは空にする
      "posts_per_day": 7
    }
  ]
}
```

> **公開リポジトリでのトークンの扱い**
> 仕様上 `config/accounts.json` にトークンを直接保存できますが、公開リポジトリではファイルごと
> 公開されてしまいます。本実装では `threads_token_env`（GitHub Secrets 名）での参照を既定の
> 運用方法とし、環境変数 → `accounts.json` の平文 の順で解決します。
> `threads_token_env` が空の場合は `THREADS_TOKEN_<ID大文字>` が自動的に参照されます。

### 2.4 Streamlit Community Cloud

1. Streamlit Cloud で本リポジトリを指定し、メインファイルに `app.py` を設定します。
2. アプリの **Secrets** に以下を登録すると、管理画面からの変更が GitHub へ直接コミットされます。

```toml
GITHUB_TOKEN = "ghp_..."          # repo スコープの PAT
GITHUB_REPO  = "owner/Rakuten-affiliate"
GITHUB_BRANCH = "main"
```

未設定の場合はローカル保存のみとなり、再デプロイで変更が失われます（画面上に警告が出ます）。

---

## 3. 動作の詳細

### 3.1 前日バッチ（`batch.yml` / 毎日 JST 23:00 = UTC 14:00）

登録済みの全アカウントを **1 アカウントずつ順番に** 処理します。

1. **リサーチ（重複防止）**
   楽天 API をキーワードごとに検索し、既定で **上位 50 件**（1 リクエスト 30 件上限のためページング）取得。
   `data/used_items.json` を参照し、**過去 14 日以内に同じアカウントで紹介した `itemCode` を除外**。
   残った候補から **ランダムに必ず 7 件** を選択し、履歴へ記録します。
   （フレッシュな候補が 7 件に満たない場合のみ、紹介済み商品を順位順で補充して 7 件を確保します。）
2. **売れ筋ランキング順位**
   人気順（既定 `-reviewCount`）で取得した検索結果の並び順を 1 始まりの順位として保持します。
3. **悩み生成** — Claude API に商品情報とテーマを渡し、ターゲットの具体的な悩みを推測させます。
4. **投稿生成** — `prompts/Claude×アフィリエイト投稿作成プロンプト.txt` を読み込み、
   `{ジャンル}` `{ターゲットの悩み}` `{商品名}` `{口調}` を置換して送信。
   返ってきた複数パターンのうち **「伸びる確率：〇〇％」が最も高いパターンの本文だけ** を抽出します。
5. **スケジュール割り当て**
   活動時間帯 JST 7:00〜23:00 を 7 つの時間枠に等間隔で分割し、各枠に **±15〜30 分のランダムなゆらぎ**を付与。
   **ランキング上位の商品はゴールデンタイム（朝 7〜8 時台 / 夜 20〜22 時台）へ優先的に割り当て**ます。
6. **動的トリガー生成**
   全配信時刻（分単位）を UTC の cron 式へ変換して `.github/workflows/publisher.yml` を上書き生成します。
   **1 ファイルにつき cron は 60 件までという GitHub の制限があるため、60 件を超える場合は
   `publisher_2.yml`, `publisher_3.yml` … へ自動分割**します（投稿数が減った日は余分なファイルを削除）。
   cron は対象日の日・月を固定するため、その日だけ起動します。

### 3.2 当日配信（`publisher*.yml`）

予約時刻に起動し、`data/queue.json` から**起動時刻に対応する未送信の投稿**を取得して、
**待機（sleep）せずに即座に**送信します。

- 親投稿を送信 → 返ってきた `media_id` を `reply_to_id` にして、コメント欄へ
  **「楽天アフィリエイト URL」＋「※PR」** の子投稿を即座に送信 → ステータスを `sent` に更新。
- 同一時刻に複数ある場合は予約時刻順に順次送信します。
- GitHub Actions の cron は数分遅れて起動することがあるため、
  「起動時刻の 5 分先」から「60 分前」までを配信対象の窓としています（`config/settings.json` で変更可）。
  窓を過ぎた投稿は `expired` として記録されます。

### 3.3 再投稿（`reposter.yml` / 月・水・金 JST 19:30 = UTC 10:30）

`data/post_history.json` のいいね数を Threads インサイト API で更新し、
**過去 1 週間の履歴からアカウントごとに上位 3 件**を抽出。
その 3 件を **月曜 = 1 位 / 水曜 = 2 位 / 金曜 = 3 位** の順に、その日の「8 件目の投稿」として
**元の本文とまったく同じ内容で**再投稿します（親投稿＋PR リプライ）。
直近 14 日以内に再投稿済みのものは繰り上げ対象から除外されます。

---

## 4. ローカルでの実行と検証

```bash
pip install -r requirements-dev.txt

# 単体テスト
python -m pytest tests -q

# 管理画面
streamlit run app.py

# 外部 API を呼ばないドライラン（data/ と .github/workflows/ を汚さないよう出力先を分ける）
mkdir -p /tmp/try/data /tmp/try/wf
echo '{"accounts":{}}' > /tmp/try/data/used_items.json
python -m src.batch_generator --dry-run --seed 1 \
  --data-dir /tmp/try/data --workflow-dir /tmp/try/wf

# 予約された時刻を指定して配信をドライラン
python -m src.publisher --dry-run --now 2026-08-29T22:47:00+00:00 --data-dir /tmp/try/data

# 再投稿をドライラン
python -m src.reposter --dry-run --skip-insights --data-dir /tmp/try/data
```

環境変数 `DRY_RUN=1` を設定すると、Threads API の呼び出しを擬似 ID で置き換えます。

---

## 5. 運用上の注意

- **cron はデフォルトブランチのものだけが有効です。** 生成された `publisher*.yml` が実際に起動するのは、
  デフォルトブランチ（通常 `main`）にコミットされたときだけです。
- **cron は UTC 基準**です。`0 14 * * *` が JST 23:00、`30 10 * * 1,3,5` が JST 月・水・金 19:30 に対応します。
- **GitHub Actions の cron は遅延します。** 高負荷時は数分〜十数分ずれることがあるため、
  配信側で「窓」を設けて取りこぼしを拾う設計にしています。分単位の厳密性が必要な場合は
  `settings.json` の `publisher.window_*` を調整してください。
- **楽天 API のレート制限**に配慮し、リクエスト間隔を 1 秒空けています。
- **1 アカウントの失敗は他アカウントに波及しません。** バッチは各アカウントを独立して処理し、
  失敗は `data/run_log.json` に記録されます。
- 実行結果は毎回リポジトリへコミットされるため、`data/*.json` の差分がそのまま運用ログになります。
