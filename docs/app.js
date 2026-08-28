/*
 * SNS自動運用 管理画面（GitHub Pages / Vanilla JS）
 *
 * 【設計方針】
 * このリポジトリは公開（Public）前提のテンプレートである。
 * したがって機密情報（Threadsトークン・APIキー）は、リポジトリ内のファイルへ
 * 一切保存しない。管理画面で入力された機密情報は、その場でブラウザ内において
 * libsodium の sealed box でリポジトリの公開鍵により暗号化し、
 * GitHub Actions Secrets API へ直接送信する。平文はネットワークにもJSONにも残らない。
 *
 * 非機密のメタデータ（テーマ・キーワード等）だけを Contents API 経由で
 * config/accounts.json へコミットする。
 *
 * 認証は利用者が入力した Personal Access Token を localStorage に保存して使う。
 * トークンをこのソースへ直書きしてはならない。
 */

"use strict";

// ======================================================================
// 定数
// ======================================================================
const STORAGE_KEY = "sns-admin-auth";
const API_ROOT = "https://api.github.com";
const THREADS_TOKEN_PREFIX = "THREADS_TOKEN_";

const PATHS = {
  accounts: "config/accounts.json",
  settings: "config/settings.json",
  queue: "data/queue.json",
  history: "data/post_history.json",
  used: "data/used_items.json",
  runLog: "data/run_log.json",
  prompt: "prompts/Claude×アフィリエイト投稿作成プロンプト.txt",
};

/** 全アカウント共通で必要になるシークレット。管理画面から登録できる。 */
const GLOBAL_SECRETS = [
  {
    name: "ANTHROPIC_API_KEY",
    label: "Claude APIキー",
    required: true,
    help: "投稿文と「ターゲットの悩み」の生成に使います。",
    link: "https://console.anthropic.com/",
    linkLabel: "Anthropic Console",
    placeholder: "sk-ant-...",
  },
  {
    name: "RAKUTEN_APP_ID",
    label: "楽天アプリID",
    required: true,
    help: "楽天市場の商品検索に使います。",
    link: "https://webservice.rakuten.co.jp/",
    linkLabel: "楽天ウェブサービス",
    placeholder: "10000000000000000000",
  },
  {
    name: "RAKUTEN_AFFILIATE_ID",
    label: "楽天アフィリエイトID",
    required: true,
    help: "投稿に貼るアフィリエイトリンクの生成に使います。",
    link: "https://affiliate.rakuten.co.jp/",
    linkLabel: "楽天アフィリエイト",
    placeholder: "0123abcd.4567efgh...",
  },
  {
    name: "WORKFLOW_TOKEN",
    label: "GitHub PAT（workflow権限つき）",
    required: true,
    help:
      "翌日分の配信スケジュール（ワークフローファイル）を書き換えるために必要です。" +
      "repo と workflow の両方にチェックを入れて発行したトークンを登録してください。",
    link: "https://github.com/settings/tokens/new?scopes=repo,workflow&description=sns-workflow",
    linkLabel: "この設定でトークンを作る",
    placeholder: "ghp_...",
  },
];

// src/config.py の DEFAULT_SETTINGS と対応させる
const DEFAULT_SETTINGS = {
  timezone: "Asia/Tokyo",
  posts_per_day: 7,
  active_hours: { start: "07:00", end: "23:00" },
  jitter_minutes: { min: 15, max: 30 },
  golden_time_ranges: [["07:00", "09:00"], ["20:00", "23:00"]],
  min_gap_minutes: 20,
  duplicate_exclusion_days: 14,
  rakuten: { fetch_hits: 50, sort: "-reviewCount", min_price: 0, max_price: 0, ng_keywords: [] },
  claude: { model: "claude-3-5-sonnet-latest", max_tokens: 2000, temperature: 1.0, worry_max_tokens: 400 },
  publisher: {
    max_cron_per_file: 60, window_before_minutes: 5, window_after_minutes: 60,
    workflow_basename: "publisher", python_version: "3.11",
  },
  repost: { lookback_days: 7, top_n: 3, cooldown_days: 14, weekday_rank_map: { 0: 0, 2: 1, 4: 2 } },
  pr_text: "※PR",
};

/**
 * config/accounts.json に保存する項目（すべて非機密）。
 * src/config.py の Account dataclass と 1 対 1 で対応させること。
 * ここに機密情報のフィールドを追加してはならない。
 */
const ACCOUNT_DEFAULTS = {
  id: "", name: "", enabled: true,
  genre: "", worldview: "", strength: "", tone: "", target: "",
  search_keywords: [],
  threads_user_id: "",
  posts_per_day: 7, rakuten_affiliate_id: "", note: "",
};

const STATUS_LABEL = { pending: "予約中", sent: "送信済み", failed: "失敗", expired: "期限切れ" };

// ======================================================================
// 状態
// ======================================================================
const state = {
  auth: null,            // { token, owner, repo, branch }
  accounts: [],
  settings: {},
  queue: {},
  history: { posts: [] },
  used: { accounts: {} },
  runLog: {},
  prompt: "",
  secrets: new Map(),    // 登録済みシークレット名 → { updated_at }
  secretsReadable: true, // 一覧を取得できたか（権限不足なら false）
  publicKey: null,       // { key_id, key }
  shas: {},
  view: "dashboard",
};

// ======================================================================
// 小さなユーティリティ
// ======================================================================
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

/** src/config.py の slugify() と同じ規則でIDを正規化する。 */
function slugify(text) {
  return String(text ?? "")
    .normalize("NFKC")
    .replace(/[^0-9A-Za-z]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .toLowerCase();
}

/** アカウントIDからシークレット名を求める（Account.token_secret_name と同じ規則）。 */
function tokenSecretName(accountId) {
  return `${THREADS_TOKEN_PREFIX}${slugify(accountId).toUpperCase()}`;
}

function toast(message, type = "info") {
  const node = document.createElement("div");
  node.className = `toast toast-${type}`;
  node.textContent = message;
  $("#toast-area").appendChild(node);
  setTimeout(() => {
    node.style.transition = "opacity .3s";
    node.style.opacity = "0";
    setTimeout(() => node.remove(), 300);
  }, type === "error" ? 7000 : 3500);
}

function formatDateTime(iso, withDate = true) {
  if (!iso) return "-";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return String(iso).slice(0, 16).replace("T", " ");
  const parts = new Intl.DateTimeFormat("ja-JP", {
    timeZone: "Asia/Tokyo", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  }).formatToParts(date);
  const get = (t) => parts.find((p) => p.type === t)?.value ?? "";
  const time = `${get("hour")}:${get("minute")}`;
  return withDate ? `${get("month")}/${get("day")} ${time}` : time;
}

// ---- UTF-8 対応の Base64 変換 ----------------------------------------
// btoa/atob は Latin-1 しか扱えないため、日本語を含む内容では必ずここを通す。
function encodeBase64(text) {
  const bytes = new TextEncoder().encode(text);
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

function decodeBase64(base64) {
  const binary = atob(String(base64).replace(/\s/g, ""));
  const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
  return new TextDecoder("utf-8").decode(bytes);
}

// ======================================================================
// 認証情報の保存
// ======================================================================
function loadAuth() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY) || sessionStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function saveAuth(auth, remember) {
  const store = remember ? localStorage : sessionStorage;
  const other = remember ? sessionStorage : localStorage;
  try {
    store.setItem(STORAGE_KEY, JSON.stringify(auth));
    other.removeItem(STORAGE_KEY);
  } catch {
    toast("ブラウザにトークンを保存できませんでした", "error");
  }
}

function clearAuth() {
  localStorage.removeItem(STORAGE_KEY);
  sessionStorage.removeItem(STORAGE_KEY);
}

// ======================================================================
// GitHub REST API（共通）
// ======================================================================
function repoUrl(suffix = "") {
  const { owner, repo } = state.auth;
  return `${API_ROOT}/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}${suffix}`;
}

function contentsUrl(path) {
  const encoded = path.split("/").map(encodeURIComponent).join("/");
  return repoUrl(`/contents/${encoded}`);
}

async function ghFetch(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    cache: "no-store",
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${state.auth.token}`,
      "X-GitHub-Api-Version": "2022-11-28",
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(options.headers || {}),
    },
  });

  if (response.status === 204) return null;
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }

  if (!response.ok) {
    const error = new Error(payload?.message || `HTTP ${response.status}`);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

// ---- ファイル（Contents API） ----------------------------------------
async function getFile(path) {
  const url = `${contentsUrl(path)}?ref=${encodeURIComponent(state.auth.branch)}&t=${Date.now()}`;
  try {
    const payload = await ghFetch(url);
    state.shas[path] = payload.sha;
    return decodeBase64(payload.content || "");
  } catch (error) {
    if (error.status === 404) {
      state.shas[path] = null;
      return null;
    }
    throw error;
  }
}

async function getJson(path, fallback) {
  const text = await getFile(path);
  if (text === null || text.trim() === "") return fallback;
  try {
    return JSON.parse(text);
  } catch {
    toast(`${path} のJSONを解釈できませんでした`, "error");
    return fallback;
  }
}

/** ファイルを上書きコミットする。SHAが古い場合は取り直して1度だけ再試行する。 */
async function putFile(path, text, message) {
  const body = { message, content: encodeBase64(text), branch: state.auth.branch };
  if (state.shas[path]) body.sha = state.shas[path];

  try {
    const payload = await ghFetch(contentsUrl(path), { method: "PUT", body: JSON.stringify(body) });
    state.shas[path] = payload.content.sha;
    return payload;
  } catch (error) {
    // 409/422 は GitHub Actions などから同時に更新されSHAがずれた場合
    if (error.status === 409 || error.status === 422) {
      await getFile(path);
      body.sha = state.shas[path] || undefined;
      const payload = await ghFetch(contentsUrl(path), { method: "PUT", body: JSON.stringify(body) });
      state.shas[path] = payload.content.sha;
      toast("他の処理と競合したため、最新の状態に対して保存し直しました", "info");
      return payload;
    }
    throw error;
  }
}

function putJson(path, data, message) {
  return putFile(path, `${JSON.stringify(data, null, 2)}\n`, message);
}

// ======================================================================
// GitHub Actions Secrets（暗号化して保存）
// ======================================================================
/** libsodium の準備を待つ。読み込めていなければ分かりやすい例外を投げる。 */
async function ensureSodium() {
  if (typeof window.sodium === "undefined") {
    throw new Error(
      "暗号化ライブラリ(libsodium)を読み込めませんでした。ページを再読み込みしてください。"
    );
  }
  await window.sodium.ready;
  return window.sodium;
}

/** リポジトリの公開鍵を取得する（1セッション1回だけ）。 */
async function getPublicKey() {
  if (!state.publicKey) {
    state.publicKey = await ghFetch(repoUrl("/actions/secrets/public-key"));
  }
  return state.publicKey;
}

/**
 * 値をリポジトリの公開鍵で暗号化する（GitHubが要求する sealed box 方式）。
 * 復号できるのはGitHubだけで、暗号文からは元の値を復元できない。
 */
async function encryptSecretValue(value, publicKeyBase64) {
  const sodium = await ensureSodium();
  const key = sodium.from_base64(publicKeyBase64, sodium.base64_variants.ORIGINAL);
  const sealed = sodium.crypto_box_seal(sodium.from_string(value), key);
  return sodium.to_base64(sealed, sodium.base64_variants.ORIGINAL);
}

/** 登録済みシークレットの一覧（名前と更新日時のみ。値は取得できない）。 */
async function loadSecrets() {
  try {
    const payload = await ghFetch(repoUrl("/actions/secrets?per_page=100"));
    state.secrets = new Map((payload.secrets || []).map((s) => [s.name, s]));
    state.secretsReadable = true;
  } catch (error) {
    state.secrets = new Map();
    state.secretsReadable = false;
    if (error.status !== 403 && error.status !== 404) throw error;
  }
}

/** シークレットを暗号化して保存（新規作成・上書きの両方）。 */
async function putSecret(name, value) {
  const publicKey = await getPublicKey();
  const encrypted = await encryptSecretValue(value, publicKey.key);
  await ghFetch(repoUrl(`/actions/secrets/${encodeURIComponent(name)}`), {
    method: "PUT",
    body: JSON.stringify({ encrypted_value: encrypted, key_id: publicKey.key_id }),
  });
  state.secrets.set(name, { name, updated_at: new Date().toISOString() });
}

async function deleteSecret(name) {
  try {
    await ghFetch(repoUrl(`/actions/secrets/${encodeURIComponent(name)}`), { method: "DELETE" });
  } catch (error) {
    if (error.status !== 404) throw error;
  }
  state.secrets.delete(name);
}

function secretStatus(name) {
  if (!state.secretsReadable) return { known: false, registered: false };
  const entry = state.secrets.get(name);
  return { known: true, registered: Boolean(entry), updatedAt: entry?.updated_at };
}

function secretBadge(name) {
  const status = secretStatus(name);
  if (!status.known) return '<span class="badge badge-pending">状態不明</span>';
  if (!status.registered) return '<span class="badge badge-expired">未登録</span>';
  return `<span class="badge badge-sent">登録済み</span>
    <span class="text-xs text-muted">${escapeHtml(formatDateTime(status.updatedAt))} 更新</span>`;
}

// ======================================================================
// ログイン
// ======================================================================
function guessRepoFromUrl() {
  const match = location.hostname.match(/^([^.]+)\.github\.io$/i);
  if (!match) return null;
  const segment = location.pathname.split("/").filter(Boolean)[0];
  return { owner: match[1], repo: segment || `${match[1]}.github.io` };
}

async function verifyAndStart(auth, remember) {
  state.auth = auth;
  const repo = await ghFetch(
    `${API_ROOT}/repos/${encodeURIComponent(auth.owner)}/${encodeURIComponent(auth.repo)}`
  );
  if (!repo.permissions?.push) {
    toast("このトークンには書き込み権限がありません。閲覧のみ可能です。", "error");
  }
  if (remember !== undefined) saveAuth(auth, remember);

  $("#login-screen").classList.add("hidden");
  $("#app").classList.remove("hidden");
  $("#repo-label").textContent = `${auth.owner}/${auth.repo} @ ${auth.branch}`;
  await reloadAll();
}

function setupLogin() {
  const guessed = guessRepoFromUrl();
  if (guessed) {
    $("#login-owner").value = guessed.owner;
    $("#login-repo").value = guessed.repo;
  }

  $("#login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = $("#login-submit");
    const errorBox = $("#login-error");
    errorBox.classList.add("hidden");
    button.disabled = true;
    button.textContent = "接続中...";

    const auth = {
      token: $("#login-token").value.trim(),
      owner: $("#login-owner").value.trim(),
      repo: $("#login-repo").value.trim(),
      branch: $("#login-branch").value.trim() || "main",
    };

    try {
      await verifyAndStart(auth, $("#login-remember").checked);
    } catch (error) {
      state.auth = null;
      const hint = error.status === 401
        ? "トークンが正しくないか、有効期限が切れています。"
        : error.status === 404
          ? "リポジトリが見つかりません。オーナー名・リポジトリ名と、トークンの repo 権限を確認してください。"
          : error.message;
      errorBox.textContent = `接続できませんでした: ${hint}`;
      errorBox.classList.remove("hidden");
    } finally {
      button.disabled = false;
      button.textContent = "接続する";
    }
  });
}

// ======================================================================
// データ読み込み
// ======================================================================
async function reloadAll() {
  $("#loading").classList.remove("hidden");
  $$(".view").forEach((el) => el.classList.add("hidden"));
  try {
    const [accountsRaw, settingsRaw, queue, history, used, runLog, prompt] = await Promise.all([
      getJson(PATHS.accounts, { accounts: [] }),
      getJson(PATHS.settings, {}),
      getJson(PATHS.queue, {}),
      getJson(PATHS.history, { posts: [] }),
      getJson(PATHS.used, { accounts: {} }),
      getJson(PATHS.runLog, {}),
      getFile(PATHS.prompt),
      loadSecrets(),
    ]);

    const list = Array.isArray(accountsRaw) ? accountsRaw : (accountsRaw.accounts || []);
    state.accounts = list.map((account) => sanitizeAccount(account));
    state.settings = mergeDeep(DEFAULT_SETTINGS, settingsRaw || {});
    state.queue = queue || {};
    state.history = history?.posts ? history : { posts: [] };
    state.used = used?.accounts ? used : { accounts: {} };
    state.runLog = runLog || {};
    state.prompt = prompt ?? "";
  } catch (error) {
    toast(`読み込みに失敗しました: ${error.message}`, "error");
  } finally {
    $("#loading").classList.add("hidden");
    render();
  }
}

/**
 * 既知の非機密フィールドだけを取り込む。
 * 旧バージョンのファイルにトークン欄が残っていても、ここで確実に落とす。
 */
function sanitizeAccount(raw) {
  const account = { ...ACCOUNT_DEFAULTS };
  for (const key of Object.keys(ACCOUNT_DEFAULTS)) {
    if (raw && raw[key] !== undefined) account[key] = raw[key];
  }
  return account;
}

function mergeDeep(base, override) {
  const result = { ...base };
  for (const [key, value] of Object.entries(override || {})) {
    result[key] = (value && typeof value === "object" && !Array.isArray(value)
      && result[key] && typeof result[key] === "object" && !Array.isArray(result[key]))
      ? mergeDeep(result[key], value)
      : value;
  }
  return result;
}

async function saveAccounts(message) {
  await putJson(PATHS.accounts, { accounts: state.accounts }, message);
}

// ======================================================================
// 画面切り替え
// ======================================================================
function render() {
  $$("#tabs .tab").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.view === state.view);
  });
  $$(".view").forEach((el) => el.classList.add("hidden"));
  const target = $(`#view-${state.view}`);
  if (!target) return;
  target.classList.remove("hidden");

  const renderers = {
    dashboard: renderDashboard,
    accounts: renderAccounts,
    secrets: renderSecrets,
    settings: renderSettings,
    prompt: renderPrompt,
  };
  (renderers[state.view] || renderDashboard)(target);
}

// ======================================================================
// ダッシュボード
// ======================================================================
function queuePosts() {
  const accounts = state.queue?.accounts || {};
  return Object.entries(accounts).flatMap(([accountId, entry]) =>
    (entry?.posts || []).map((post) => ({ account_id: accountId, ...post }))
  );
}

function statCard(value, label) {
  return `<div class="card p-4">
    <div class="stat-value">${escapeHtml(value)}</div>
    <div class="stat-label">${escapeHtml(label)}</div>
  </div>`;
}

/** セットアップの進み具合を示すチェックリスト（初心者向けの道しるべ）。 */
function renderSetupChecklist() {
  const missingGlobal = GLOBAL_SECRETS.filter(
    (secret) => secret.required && !secretStatus(secret.name).registered
  );
  const accountsWithoutToken = state.accounts.filter(
    (account) => account.enabled && !secretStatus(tokenSecretName(account.id)).registered
  );

  const steps = [
    {
      done: state.accounts.length > 0,
      label: "アカウントを登録する",
      hint: "「アカウント管理」タブから追加します",
    },
    {
      done: state.secretsReadable && missingGlobal.length === 0,
      label: "APIキーを登録する",
      hint: state.secretsReadable
        ? `未登録: ${missingGlobal.map((s) => s.label).join(" / ") || "なし"}`
        : "トークンの権限が足りず状態を確認できません",
    },
    {
      done: state.secretsReadable && state.accounts.length > 0 && accountsWithoutToken.length === 0,
      label: "各アカウントのThreadsトークンを登録する",
      hint: accountsWithoutToken.length
        ? `未登録: ${accountsWithoutToken.map((a) => a.name || a.id).join(" / ")}`
        : "すべて登録済みです",
    },
    {
      done: Boolean(state.runLog?.generated_at),
      label: "バッチを1回実行する",
      hint: "GitHubの Actions タブ →「Batch Generator」→ Run workflow",
    },
  ];

  if (steps.every((step) => step.done)) return "";

  return `<div class="card p-4">
    <h2 class="font-bold mb-1">セットアップの進み具合</h2>
    <p class="text-xs text-muted mb-3">すべて緑になれば自動運用が始まります。</p>
    <ol class="space-y-2">
      ${steps.map((step, index) => `<li class="flex items-start gap-3 text-sm">
        <span class="badge ${step.done ? "badge-sent" : "badge-pending"} shrink-0">
          ${step.done ? "✓" : index + 1}
        </span>
        <span class="min-w-0">
          <span class="${step.done ? "text-muted" : "font-medium"}">${escapeHtml(step.label)}</span>
          <span class="block text-xs text-muted">${escapeHtml(step.hint)}</span>
        </span>
      </li>`).join("")}
    </ol>
  </div>`;
}

function renderDashboard(root) {
  const posts = queuePosts();
  const sent = posts.filter((p) => p.status === "sent").length;
  const failed = posts.filter((p) => p.status === "failed").length;
  const history = state.history.posts || [];
  const errors = state.runLog?.errors || [];
  const workflows = state.runLog?.workflows;

  const banner = errors.length
    ? `<div class="alert-error">直近のバッチで ${errors.length} 件のエラーが発生しました:
        ${escapeHtml(errors.map((e) => `${e.account_id}: ${e.error}`).join(" / "))}</div>`
    : "";

  const workflowInfo = workflows
    ? `<p class="text-xs text-muted">配信ワークフロー: <code class="code">${escapeHtml((workflows.files || []).join(", "))}</code>
       （cron ${escapeHtml(workflows.cron_count ?? 0)} 件 / ${escapeHtml(workflows.file_count ?? 0)} ファイル）</p>`
    : "";

  root.innerHTML = `
    ${banner}
    ${renderSetupChecklist()}
    <div class="grid grid-cols-2 lg:grid-cols-4 gap-3">
      ${statCard(state.accounts.filter((a) => a.enabled).length, "有効なアカウント")}
      ${statCard(posts.length, `予約投稿（${state.queue?.target_date || "未生成"}）`)}
      ${statCard(sent, "送信済み")}
      ${statCard(history.length, "累計投稿数")}
    </div>

    <div class="card p-4 space-y-1">
      <div class="flex flex-wrap gap-x-6 gap-y-1 text-sm">
        <span class="text-muted">最終バッチ:</span>
        <span>${escapeHtml(state.runLog?.generated_at ? formatDateTime(state.runLog.generated_at) + " JST" : "未実行")}</span>
        <span class="text-muted">配信対象日:</span>
        <span>${escapeHtml(state.queue?.target_date || "-")}</span>
        ${failed ? `<span class="badge badge-failed">失敗 ${failed} 件</span>` : ""}
      </div>
      ${workflowInfo}
    </div>

    <div class="card p-4">
      <h2 class="font-bold mb-3">予約キュー</h2>
      ${renderQueueSection()}
    </div>

    <div class="card p-4">
      <h2 class="font-bold mb-3">投稿履歴</h2>
      ${renderHistoryTable()}
    </div>

    <div class="card p-4">
      <h2 class="font-bold mb-1">紹介済み商品</h2>
      <p class="text-xs text-muted mb-3">過去 ${escapeHtml(state.settings.duplicate_exclusion_days)} 日以内の商品は次回のリサーチから除外されます。</p>
      ${renderUsedItems()}
    </div>
  `;
}

function renderQueueSection() {
  const entries = Object.entries(state.queue?.accounts || {});
  if (!entries.length) {
    return `<p class="text-sm text-muted">キューが空です。GitHub Actions の <code class="code">Batch Generator</code> を実行すると翌日分が生成されます。</p>`;
  }

  return entries.map(([accountId, entry]) => {
    const posts = entry.posts || [];
    const rows = posts.map((post) => {
      const status = post.status || "pending";
      const item = post.item || {};
      return `<div class="border-t border-slate-200 dark:border-slate-800 pt-3 mt-3 space-y-2">
        <div class="flex flex-wrap items-center gap-2 text-sm">
          <span class="font-mono font-bold">${escapeHtml(formatDateTime(post.scheduled_at_jst, false))}</span>
          <span class="badge badge-${escapeHtml(status)}">${escapeHtml(STATUS_LABEL[status] || status)}</span>
          ${post.is_golden_time ? '<span class="badge badge-golden">ゴールデン</span>' : ""}
          <span class="text-xs text-muted">順位 ${escapeHtml(item.rank ?? "-")}</span>
          ${post.probability != null ? `<span class="text-xs text-muted">伸びる確率 ${escapeHtml(post.probability)}%</span>` : ""}
        </div>
        <div class="post-body">${escapeHtml(post.body)}</div>
        <div class="text-xs text-muted break-all">
          ${escapeHtml(item.item_name || "")}
          ${post.affiliate_url ? `<br /><a class="underline" href="${escapeHtml(post.affiliate_url)}" target="_blank" rel="noopener">${escapeHtml(post.affiliate_url)}</a>` : ""}
        </div>
        ${post.error ? `<div class="alert-error">${escapeHtml(post.error)}</div>` : ""}
      </div>`;
    }).join("");

    return `<details class="account-queue mb-2">
      <summary class="py-2 font-medium text-sm">
        ${escapeHtml(entry.account_name || accountId)}
        <span class="text-muted font-normal">（${posts.length} 件）</span>
      </summary>
      <div class="pl-2">${rows}</div>
    </details>`;
  }).join("");
}

function renderHistoryTable() {
  const posts = [...(state.history.posts || [])].reverse().slice(0, 100);
  if (!posts.length) return `<p class="text-sm text-muted">まだ投稿履歴がありません。</p>`;

  const rows = posts.map((post) => `<tr>
    <td class="whitespace-nowrap">${escapeHtml(formatDateTime(post.published_at_jst || post.published_at))}</td>
    <td>${escapeHtml(post.account_name || post.account_id)}</td>
    <td class="text-right font-mono">${escapeHtml(post.likes ?? 0)}</td>
    <td>${post.is_repost ? '<span class="badge badge-on">再投稿</span>' : ""}</td>
    <td class="max-w-md"><div class="truncate">${escapeHtml((post.body || "").replace(/\n/g, " "))}</div></td>
    <td class="max-w-xs"><div class="truncate text-muted">${escapeHtml(post.item?.item_name || "")}</div></td>
  </tr>`).join("");

  return `<div class="table-wrap"><table class="data">
    <thead><tr>
      <th>投稿日時(JST)</th><th>アカウント</th><th class="text-right">いいね</th>
      <th></th><th>本文</th><th>商品</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table></div>`;
}

function renderUsedItems() {
  const entries = Object.entries(state.used?.accounts || {});
  if (!entries.length) return `<p class="text-sm text-muted">記録がありません。</p>`;

  return entries.map(([accountId, items]) => {
    const rows = [...(items || [])].reverse().slice(0, 50).map((item) => `<tr>
      <td class="font-mono text-xs">${escapeHtml(item.item_code)}</td>
      <td><div class="truncate max-w-md">${escapeHtml(item.item_name)}</div></td>
      <td class="text-right">${escapeHtml(item.rank ?? "")}</td>
      <td class="whitespace-nowrap">${escapeHtml(item.target_date || "")}</td>
    </tr>`).join("");

    return `<details class="account-queue mb-2">
      <summary class="py-2 font-medium text-sm">
        ${escapeHtml(accountId)} <span class="text-muted font-normal">（${(items || []).length} 件）</span>
      </summary>
      <div class="table-wrap"><table class="data">
        <thead><tr><th>itemCode</th><th>商品名</th><th class="text-right">順位</th><th>使用日</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>
    </details>`;
  }).join("");
}

// ======================================================================
// APIキー（GitHub Secrets）
// ======================================================================
function renderSecrets(root) {
  const permissionNote = state.secretsReadable
    ? ""
    : `<div class="alert-warn">
         登録済みシークレットの一覧を取得できませんでした。トークンに <code class="code">repo</code> 権限
         （fine-grained なら <code class="code">Secrets: Read and write</code>）があるか確認してください。
         保存自体は試せますが、登録状況は表示できません。
       </div>`;

  const cards = GLOBAL_SECRETS.map((secret) => `
    <div class="card p-4 space-y-3">
      <div class="flex flex-wrap items-center justify-between gap-2">
        <div class="min-w-0">
          <h3 class="font-bold">${escapeHtml(secret.label)}</h3>
          <code class="code">${escapeHtml(secret.name)}</code>
        </div>
        <div class="flex items-center gap-2">${secretBadge(secret.name)}</div>
      </div>
      <p class="text-sm text-muted">${escapeHtml(secret.help)}</p>
      <p class="text-xs">
        取得先:
        <a class="underline" href="${escapeHtml(secret.link)}" target="_blank" rel="noopener">
          ${escapeHtml(secret.linkLabel)}
        </a>
      </p>
      <div class="flex flex-col sm:flex-row gap-2">
        <input type="password" class="input font-mono flex-1" autocomplete="off"
               data-secret-input="${escapeHtml(secret.name)}"
               placeholder="${escapeHtml(secret.placeholder)}" />
        <button class="btn-primary shrink-0" data-secret-save="${escapeHtml(secret.name)}">
          暗号化して保存
        </button>
      </div>
    </div>`).join("");

  root.innerHTML = `
    <div>
      <h2 class="font-bold">APIキー（GitHub Secrets）</h2>
      <p class="text-xs text-muted">
        入力した値はこのブラウザの中で暗号化されてから送信され、GitHub Secrets に保存されます。
        リポジトリのファイルには一切書き込まれません。
      </p>
    </div>
    ${permissionNote}
    <div class="alert-warn">
      一度保存した値は GitHub 側でも読み出せません（表示できるのは名前と更新日時だけです）。
      変更したいときは新しい値を入力して上書き保存してください。
    </div>
    ${cards}
    <div class="card p-4">
      <h3 class="font-bold text-sm mb-2">各アカウントのThreadsトークン</h3>
      <p class="text-xs text-muted mb-3">
        アカウントごとのトークンは「アカウント管理」タブの各アカウントの編集画面から登録します。
      </p>
      ${state.accounts.length ? `<div class="table-wrap"><table class="data">
        <thead><tr><th>アカウント</th><th>シークレット名</th><th>状態</th></tr></thead>
        <tbody>${state.accounts.map((account) => `<tr>
          <td>${escapeHtml(account.name || account.id)}</td>
          <td><code class="code">${escapeHtml(tokenSecretName(account.id))}</code></td>
          <td>${secretBadge(tokenSecretName(account.id))}</td>
        </tr>`).join("")}</tbody>
      </table></div>` : '<p class="text-sm text-muted">アカウントが未登録です。</p>'}
    </div>
  `;

  root.querySelectorAll("[data-secret-save]").forEach((button) => {
    button.addEventListener("click", async () => {
      const name = button.dataset.secretSave;
      const input = root.querySelector(`[data-secret-input="${name}"]`);
      const value = input.value.trim();
      if (!value) {
        toast("値を入力してください", "error");
        return;
      }
      button.disabled = true;
      button.textContent = "保存中...";
      try {
        await putSecret(name, value);
        input.value = "";  // 平文を画面に残さない
        toast(`${name} を暗号化して保存しました`, "success");
        render();
      } catch (error) {
        toast(`保存に失敗しました: ${error.message}`, "error");
      } finally {
        button.disabled = false;
        button.textContent = "暗号化して保存";
      }
    });
  });
}

// ======================================================================
// アカウント管理
// ======================================================================
function renderAccounts(root) {
  const cards = state.accounts.map((account, index) => {
    const secretName = tokenSecretName(account.id);
    return `<div class="card p-4 space-y-3">
      <div class="flex items-start gap-3">
        <div class="min-w-0 flex-1">
          <div class="flex flex-wrap items-center gap-2">
            <h3 class="font-bold truncate">${escapeHtml(account.name || account.id)}</h3>
            <span class="badge ${account.enabled ? "badge-on" : "badge-off"}">${account.enabled ? "有効" : "無効"}</span>
          </div>
          <p class="text-xs text-muted font-mono">${escapeHtml(account.id)}</p>
        </div>
        <div class="flex gap-1 shrink-0">
          <button class="btn-ghost" data-action="edit" data-index="${index}">編集</button>
          <button class="btn-danger" data-action="delete" data-index="${index}">削除</button>
        </div>
      </div>

      <dl class="grid grid-cols-1 sm:grid-cols-3 gap-3 text-sm">
        <div><dt class="stat-label">ジャンル</dt><dd>${escapeHtml(account.genre || "-")}</dd></div>
        <div><dt class="stat-label">世界観</dt><dd>${escapeHtml(account.worldview || "-")}</dd></div>
        <div><dt class="stat-label">強み</dt><dd>${escapeHtml(account.strength || "-")}</dd></div>
      </dl>

      <div class="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted">
        <span>1日 ${escapeHtml(account.posts_per_day)} 投稿</span>
        <span>キーワード: ${escapeHtml((account.search_keywords || []).join(" / ") || account.genre || "-")}</span>
      </div>
      <div class="flex flex-wrap items-center gap-2 text-xs">
        <span class="text-muted">Threadsトークン:</span>
        ${secretBadge(secretName)}
        <code class="code">${escapeHtml(secretName)}</code>
      </div>
    </div>`;
  }).join("");

  root.innerHTML = `
    <div class="flex items-center justify-between gap-3">
      <div>
        <h2 class="font-bold">アカウント管理</h2>
        <p class="text-xs text-muted">
          テーマは <code class="code">config/accounts.json</code> に、トークンは暗号化して GitHub Secrets に保存されます。
        </p>
      </div>
      <button id="add-account" class="btn-primary shrink-0">＋ 追加</button>
    </div>

    ${cards || '<div class="card p-8 text-center text-muted text-sm">アカウントがまだ登録されていません。「＋ 追加」から登録してください。</div>'}
  `;

  $("#add-account")?.addEventListener("click", () => openAccountModal(-1));
  root.querySelectorAll("[data-action]").forEach((button) => {
    button.addEventListener("click", () => {
      const index = Number(button.dataset.index);
      if (button.dataset.action === "edit") openAccountModal(index);
      else deleteAccount(index);
    });
  });
}

async function deleteAccount(index) {
  const account = state.accounts[index];
  if (!account) return;
  const secretName = tokenSecretName(account.id);
  const hasSecret = secretStatus(secretName).registered;
  const message = hasSecret
    ? `アカウント「${account.name || account.id}」を削除します。\n登録済みのシークレット ${secretName} も削除します。よろしいですか？`
    : `アカウント「${account.name || account.id}」を削除します。よろしいですか？`;
  if (!confirm(message)) return;

  const backup = [...state.accounts];
  state.accounts.splice(index, 1);
  try {
    await saveAccounts(`chore(accounts): remove ${account.id}`);
    if (hasSecret) {
      try {
        await deleteSecret(secretName);
      } catch (error) {
        toast(`シークレットの削除に失敗しました（手動で削除してください）: ${error.message}`, "error");
      }
    }
    toast("削除しました", "success");
    render();
  } catch (error) {
    state.accounts = backup;
    toast(`削除に失敗しました: ${error.message}`, "error");
  }
}

function openAccountModal(index) {
  const isNew = index < 0;
  const account = isNew ? { ...ACCOUNT_DEFAULTS } : state.accounts[index];

  $("#account-modal-title").textContent = isNew ? "アカウントを追加" : "アカウントを編集";
  $("#acc-index").value = String(index);
  $("#acc-original-id").value = isNew ? "" : account.id;
  $("#acc-name").value = account.name || "";
  $("#acc-id").value = account.id || "";
  $("#acc-genre").value = account.genre || "";
  $("#acc-worldview").value = account.worldview || "";
  $("#acc-strength").value = account.strength || "";
  $("#acc-target").value = account.target || "";
  $("#acc-tone").value = account.tone || "";
  $("#acc-keywords").value = (account.search_keywords || []).join("\n");
  $("#acc-posts").value = account.posts_per_day || 7;
  $("#acc-affiliate").value = account.rakuten_affiliate_id || "";
  $("#acc-user-id").value = account.threads_user_id || "";
  $("#acc-note").value = account.note || "";
  $("#acc-enabled").checked = account.enabled !== false;
  $("#acc-token").value = "";

  updateSecretPreview();
  $("#account-modal").classList.remove("hidden");
  document.body.style.overflow = "hidden";
}

function closeAccountModal() {
  $("#acc-token").value = "";  // 平文をDOMに残さない
  $("#account-modal").classList.add("hidden");
  document.body.style.overflow = "";
}

function updateSecretPreview() {
  const id = $("#acc-id").value.trim();
  const name = id ? tokenSecretName(id) : "";
  $("#acc-secret-name").textContent = name || "アカウントIDを入力してください";

  const statusBox = $("#acc-secret-status");
  if (!name) {
    statusBox.innerHTML = "";
    return;
  }
  const status = secretStatus(name);
  statusBox.innerHTML = status.registered
    ? `${secretBadge(name)} <span class="text-xs text-muted">変更する場合のみ入力してください</span>`
    : `${secretBadge(name)} <span class="text-xs text-muted">投稿するにはトークンの登録が必要です</span>`;
}

function setupAccountModal() {
  $("#account-modal-close").addEventListener("click", closeAccountModal);
  $("#account-cancel").addEventListener("click", closeAccountModal);
  $("#acc-id").addEventListener("input", updateSecretPreview);
  $("#account-modal").addEventListener("click", (event) => {
    if (event.target === $("#account-modal")) closeAccountModal();
  });

  $("#account-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const index = Number($("#acc-index").value);
    const isNew = index < 0;
    const originalId = $("#acc-original-id").value;

    const id = slugify($("#acc-id").value);
    if (!id) {
      toast("アカウントIDには半角英数字を含めてください", "error");
      return;
    }
    if (state.accounts.some((a, i) => a.id === id && i !== index)) {
      toast(`アカウントIDが重複しています: ${id}`, "error");
      return;
    }

    const token = $("#acc-token").value.trim();
    const secretName = tokenSecretName(id);

    // IDを変えるとシークレット名も変わるが、既存の値は読み出せないため引き継げない
    if (!isNew && originalId && originalId !== id && !token) {
      const oldName = tokenSecretName(originalId);
      if (secretStatus(oldName).registered) {
        toast(
          `アカウントIDを変更するとシークレット名が ${secretName} に変わります。` +
          "トークンを入力し直してください。",
          "error"
        );
        return;
      }
    }

    const account = {
      ...ACCOUNT_DEFAULTS,
      id,
      name: $("#acc-name").value.trim(),
      enabled: $("#acc-enabled").checked,
      genre: $("#acc-genre").value.trim(),
      worldview: $("#acc-worldview").value.trim(),
      strength: $("#acc-strength").value.trim(),
      tone: $("#acc-tone").value.trim(),
      target: $("#acc-target").value.trim(),
      search_keywords: $("#acc-keywords").value
        .split(/[\n,、]+/).map((k) => k.trim()).filter(Boolean),
      threads_user_id: $("#acc-user-id").value.trim(),
      posts_per_day: Number($("#acc-posts").value) || 7,
      rakuten_affiliate_id: $("#acc-affiliate").value.trim(),
      note: $("#acc-note").value.trim(),
    };

    const button = $("#account-form button[type=submit]");
    button.disabled = true;
    const backup = [...state.accounts];
    try {
      // トークンを先に保存する。失敗した場合はメタデータもコミットしない。
      if (token) {
        button.textContent = "トークンを暗号化中...";
        await putSecret(secretName, token);
      }
      button.textContent = "保存中...";
      if (isNew) state.accounts.push(account);
      else state.accounts[index] = account;
      await saveAccounts(`chore(accounts): ${isNew ? "add" : "update"} ${id}`);

      toast(token ? "保存し、トークンを暗号化して登録しました" : "保存しました", "success");
      closeAccountModal();
      render();
    } catch (error) {
      state.accounts = backup;
      toast(`保存に失敗しました: ${error.message}`, "error");
    } finally {
      button.disabled = false;
      button.textContent = "保存する";
    }
  });
}

// ======================================================================
// 共通設定
// ======================================================================
function renderSettings(root) {
  const s = state.settings;
  const golden = (s.golden_time_ranges || []).map((r) => `${r[0]}-${r[1]}`).join("\n");

  root.innerHTML = `
    <div class="card p-4 space-y-4">
      <div>
        <h2 class="font-bold">共通設定</h2>
        <p class="text-xs text-muted"><code class="code">config/settings.json</code> を編集します。全アカウント共通の設定です。</p>
      </div>

      <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <div>
          <label class="label" for="set-posts">1日の投稿数（時間枠の数）</label>
          <input id="set-posts" type="number" min="1" max="20" class="input" value="${escapeHtml(s.posts_per_day)}" />
        </div>
        <div>
          <label class="label" for="set-exclusion">重複除外の日数</label>
          <input id="set-exclusion" type="number" min="0" max="365" class="input" value="${escapeHtml(s.duplicate_exclusion_days)}" />
          <p class="hint">この日数以内に紹介した商品は再度選ばれません。</p>
        </div>
        <div>
          <label class="label" for="set-start">活動開始（JST）</label>
          <input id="set-start" class="input font-mono" value="${escapeHtml(s.active_hours?.start)}" placeholder="07:00" />
        </div>
        <div>
          <label class="label" for="set-end">活動終了（JST）</label>
          <input id="set-end" class="input font-mono" value="${escapeHtml(s.active_hours?.end)}" placeholder="23:00" />
        </div>
        <div>
          <label class="label" for="set-jitter-min">投稿時刻のゆらぎ 最小（分）</label>
          <input id="set-jitter-min" type="number" min="0" max="120" class="input" value="${escapeHtml(s.jitter_minutes?.min)}" />
        </div>
        <div>
          <label class="label" for="set-jitter-max">投稿時刻のゆらぎ 最大（分）</label>
          <input id="set-jitter-max" type="number" min="0" max="120" class="input" value="${escapeHtml(s.jitter_minutes?.max)}" />
        </div>
        <div>
          <label class="label" for="set-hits">楽天の取得件数</label>
          <input id="set-hits" type="number" min="10" max="300" class="input" value="${escapeHtml(s.rakuten?.fetch_hits)}" />
          <p class="hint">多いほど重複を避けやすくなります（既定 50）。</p>
        </div>
        <div>
          <label class="label" for="set-model">Claudeのモデル</label>
          <input id="set-model" class="input font-mono" value="${escapeHtml(s.claude?.model)}" />
        </div>
        <div>
          <label class="label" for="set-pr">PR表記</label>
          <input id="set-pr" class="input" value="${escapeHtml(s.pr_text)}" />
        </div>
        <div>
          <label class="label" for="set-window-after">配信の取りこぼし許容（分）</label>
          <input id="set-window-after" type="number" min="0" max="720" class="input" value="${escapeHtml(s.publisher?.window_after_minutes)}" />
          <p class="hint">cronの遅延で送れなかった投稿を、何分前まで遡って送るか。</p>
        </div>
      </div>

      <div>
        <label class="label" for="set-golden">ゴールデンタイム（1行1区間、<code class="code">開始-終了</code>）</label>
        <textarea id="set-golden" class="input font-mono" rows="3">${escapeHtml(golden)}</textarea>
        <p class="hint">売れ筋ランキング上位の商品がこの時間帯へ優先的に割り当てられます。</p>
      </div>

      <button id="save-settings" class="btn-primary w-full sm:w-auto">保存してコミット</button>
    </div>
  `;

  $("#save-settings").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    const ranges = $("#set-golden").value.split("\n")
      .map((line) => line.trim()).filter((line) => line.includes("-"))
      .map((line) => {
        const [low, high] = line.split("-");
        return [low.trim(), high.trim()];
      });

    const updated = mergeDeep(state.settings, {
      posts_per_day: Number($("#set-posts").value) || 7,
      duplicate_exclusion_days: Number($("#set-exclusion").value) || 0,
      active_hours: { start: $("#set-start").value.trim(), end: $("#set-end").value.trim() },
      jitter_minutes: { min: Number($("#set-jitter-min").value) || 0, max: Number($("#set-jitter-max").value) || 0 },
      golden_time_ranges: ranges,
      rakuten: { fetch_hits: Number($("#set-hits").value) || 50 },
      claude: { model: $("#set-model").value.trim() },
      publisher: { window_after_minutes: Number($("#set-window-after").value) || 0 },
      pr_text: $("#set-pr").value,
    });

    button.disabled = true;
    button.textContent = "保存中...";
    try {
      await putJson(PATHS.settings, updated, "chore(settings): update from admin UI");
      state.settings = updated;
      toast("保存してGitHubへコミットしました", "success");
    } catch (error) {
      toast(`保存に失敗しました: ${error.message}`, "error");
    } finally {
      button.disabled = false;
      button.textContent = "保存してコミット";
    }
  });
}

// ======================================================================
// プロンプト
// ======================================================================
function renderPrompt(root) {
  root.innerHTML = `
    <div class="card p-4 space-y-4">
      <div>
        <h2 class="font-bold">投稿生成プロンプト</h2>
        <p class="text-xs text-muted"><code class="code">${escapeHtml(PATHS.prompt)}</code> を編集します。</p>
      </div>

      <div class="alert-warn">
        <code class="code">{ジャンル}</code> <code class="code">{ターゲットの悩み}</code>
        <code class="code">{商品名}</code> <code class="code">{口調}</code> が実際の値へ置換されます。<br />
        Claudeには複数パターンを出力させ、各パターンの末尾に
        <code class="code">伸びる確率：〇〇％</code> を書かせてください。最も高いパターンの本文だけが投稿されます。<br />
        空のまま保存すると、組み込みの既定プロンプトが使われます。
      </div>

      <textarea id="prompt-text" class="input font-mono" rows="20">${escapeHtml(state.prompt)}</textarea>

      <div class="flex gap-2">
        <button id="save-prompt" class="btn-primary">保存してコミット</button>
        <button id="reset-prompt" class="btn-ghost">編集を取り消す</button>
      </div>
    </div>
  `;

  $("#reset-prompt").addEventListener("click", () => {
    $("#prompt-text").value = state.prompt;
  });

  $("#save-prompt").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    const text = $("#prompt-text").value;
    button.disabled = true;
    button.textContent = "保存中...";
    try {
      await putFile(PATHS.prompt, text, "chore(prompt): update from admin UI");
      state.prompt = text;
      toast("保存してGitHubへコミットしました", "success");
    } catch (error) {
      toast(`保存に失敗しました: ${error.message}`, "error");
    } finally {
      button.disabled = false;
      button.textContent = "保存してコミット";
    }
  });
}

// ======================================================================
// 起動
// ======================================================================
function setupChrome() {
  $$("#tabs .tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      state.view = tab.dataset.view;
      render();
    });
  });

  $("#reload-btn").addEventListener("click", reloadAll);
  $("#logout-btn").addEventListener("click", () => {
    if (!confirm("保存されたトークンを削除して切断します。よろしいですか？")) return;
    clearAuth();
    location.reload();
  });
}

async function main() {
  // Tailwind CDN が読み込めなかった場合はフォールバックCSSへ切り替える
  if (!window.tailwind) {
    document.documentElement.classList.add("no-tailwind");
  }
  // 暗号化ライブラリはリポジトリに同梱しているが、念のため状態を見ておく
  if (typeof window.sodium === "undefined") {
    $("#sodium-error")?.classList.remove("hidden");
  }

  setupLogin();
  setupChrome();
  setupAccountModal();

  const saved = loadAuth();
  if (saved?.token && saved.owner && saved.repo) {
    try {
      await verifyAndStart({ branch: "main", ...saved });
      return;
    } catch {
      clearAuth();
      toast("保存されていたトークンが使えませんでした。再度接続してください。", "error");
    }
  }
  $("#login-screen").classList.remove("hidden");
}

document.addEventListener("DOMContentLoaded", main);
