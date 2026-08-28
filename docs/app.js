/*
 * SNS自動運用 管理画面（GitHub Pages / Vanilla JS）
 *
 * GitHub Pages は静的ホスティングのため Python を実行できない。
 * そこで GitHub REST API (Contents API) を fetch で直接叩き、
 * リポジトリ内の JSON / テキストファイルを読み書きする。
 *
 * 認証は利用者が入力した Personal Access Token を localStorage に保存して使う。
 * トークンをこのソースへ直書きしてはならない（公開リポジトリのため）。
 */

"use strict";

// ======================================================================
// 定数
// ======================================================================
const STORAGE_KEY = "sns-admin-auth";
const API_ROOT = "https://api.github.com";

const PATHS = {
  accounts: "config/accounts.json",
  settings: "config/settings.json",
  queue: "data/queue.json",
  history: "data/post_history.json",
  used: "data/used_items.json",
  runLog: "data/run_log.json",
  prompt: "prompts/Claude×アフィリエイト投稿作成プロンプト.txt",
};

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

const ACCOUNT_DEFAULTS = {
  id: "", name: "", enabled: true,
  genre: "", worldview: "", strength: "", tone: "", target: "",
  search_keywords: [],
  threads_user_id: "", threads_access_token: "", threads_token_env: "",
  posts_per_day: 7, rakuten_affiliate_id: "", note: "",
};

const STATUS_LABEL = { pending: "予約中", sent: "送信済み", failed: "失敗", expired: "期限切れ" };

// ======================================================================
// 状態
// ======================================================================
const state = {
  auth: null,          // { token, owner, repo, branch }
  accounts: [],
  settings: {},
  queue: {},
  history: { posts: [] },
  used: { accounts: {} },
  runLog: {},
  prompt: "",
  shas: {},            // パスごとの blob SHA（上書きコミットに必要）
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

/** アカウントIDから既定のシークレット名を求める（Account.default_token_env と同じ）。 */
function defaultTokenEnv(accountId) {
  return `THREADS_TOKEN_${slugify(accountId).toUpperCase()}`;
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
  }, type === "error" ? 6000 : 3000);
}

/** ISO文字列を「MM/DD HH:MM」形式へ。JSTのオフセット付き文字列はそのまま解釈される。 */
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
// GitHub REST API
// ======================================================================
function contentsUrl(path) {
  const { owner, repo } = state.auth;
  const encoded = path.split("/").map(encodeURIComponent).join("/");
  return `${API_ROOT}/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/contents/${encoded}`;
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
    const detail = payload?.message || `HTTP ${response.status}`;
    const error = new Error(detail);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

/** ファイルを取得する。存在しなければ null を返す。 */
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
  const body = {
    message,
    content: encodeBase64(text),
    branch: state.auth.branch,
  };
  if (state.shas[path]) body.sha = state.shas[path];

  try {
    const payload = await ghFetch(contentsUrl(path), { method: "PUT", body: JSON.stringify(body) });
    state.shas[path] = payload.content.sha;
    return payload;
  } catch (error) {
    // 409/422 は他の場所（GitHub Actions など）から更新されSHAがずれた場合
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
// ログイン
// ======================================================================
function guessRepoFromUrl() {
  // https://<owner>.github.io/<repo>/ の形式からリポジトリを推測する
  const host = location.hostname;
  const match = host.match(/^([^.]+)\.github\.io$/i);
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
    ]);

    const list = Array.isArray(accountsRaw) ? accountsRaw : (accountsRaw.accounts || []);
    state.accounts = list.map((account) => ({ ...ACCOUNT_DEFAULTS, ...account }));
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

// ======================================================================
// 保存
// ======================================================================
async function saveAccounts(message) {
  await putJson(PATHS.accounts, { accounts: state.accounts }, message);
  toast("保存してGitHubへコミットしました", "success");
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

  if (state.view === "dashboard") renderDashboard(target);
  else if (state.view === "accounts") renderAccounts(target);
  else if (state.view === "settings") renderSettings(target);
  else if (state.view === "prompt") renderPrompt(target);
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
  const accounts = state.queue?.accounts || {};
  const entries = Object.entries(accounts);
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
// アカウント管理
// ======================================================================
function renderAccounts(root) {
  const cards = state.accounts.map((account, index) => {
    const secretName = account.threads_token_env || defaultTokenEnv(account.id);
    const tokenWarning = account.threads_access_token
      ? '<span class="badge badge-expired">平文トークン</span>'
      : "";

    return `<div class="card p-4 space-y-3">
      <div class="flex items-start gap-3">
        <div class="min-w-0 flex-1">
          <div class="flex flex-wrap items-center gap-2">
            <h3 class="font-bold truncate">${escapeHtml(account.name || account.id)}</h3>
            <span class="badge ${account.enabled ? "badge-on" : "badge-off"}">${account.enabled ? "有効" : "無効"}</span>
            ${tokenWarning}
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

      <div class="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
        <span>1日 ${escapeHtml(account.posts_per_day)} 投稿</span>
        <span>キーワード: ${escapeHtml((account.search_keywords || []).join(" / ") || account.genre || "-")}</span>
        <span>Secret: <code class="code">${escapeHtml(secretName)}</code></span>
      </div>
    </div>`;
  }).join("");

  const secretList = state.accounts
    .map((a) => `${a.threads_token_env || defaultTokenEnv(a.id)}  # ${a.name || a.id}`)
    .join("\n");

  root.innerHTML = `
    <div class="flex items-center justify-between gap-3">
      <div>
        <h2 class="font-bold">アカウント管理</h2>
        <p class="text-xs text-muted">テーマ（発信ジャンル＋世界観＋強み）とThreadsトークンを登録します。</p>
      </div>
      <button id="add-account" class="btn-primary shrink-0">＋ 追加</button>
    </div>

    ${cards || '<div class="card p-8 text-center text-muted text-sm">アカウントがまだ登録されていません。「＋ 追加」から登録してください。</div>'}

    <div class="card p-4">
      <h3 class="font-bold text-sm mb-1">GitHub Secretsに登録するトークン名</h3>
      <p class="text-xs text-muted mb-2">
        公開リポジトリのため、Threadsトークンは <code class="code">Settings → Secrets and variables → Actions</code> に
        以下の名前で登録してください。
      </p>
      <pre class="post-body text-xs">${escapeHtml(secretList || "（アカウント未登録）")}</pre>
    </div>
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
  if (!confirm(`アカウント「${account.name || account.id}」を削除します。よろしいですか？`)) return;

  const backup = [...state.accounts];
  state.accounts.splice(index, 1);
  try {
    await saveAccounts(`chore(accounts): remove ${account.id}`);
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
  $("#acc-token-env").value = account.threads_token_env || "";
  $("#acc-token").value = account.threads_access_token || "";
  $("#acc-user-id").value = account.threads_user_id || "";
  $("#acc-note").value = account.note || "";
  $("#acc-enabled").checked = account.enabled !== false;

  updateSecretPreview();
  $("#account-modal").classList.remove("hidden");
  document.body.style.overflow = "hidden";
}

function closeAccountModal() {
  $("#account-modal").classList.add("hidden");
  document.body.style.overflow = "";
}

function updateSecretPreview() {
  const explicit = $("#acc-token-env").value.trim();
  const id = $("#acc-id").value.trim();
  $("#acc-secret-name").textContent = explicit || (id ? defaultTokenEnv(id) : "アカウントIDを入力してください");
}

function setupAccountModal() {
  $("#account-modal-close").addEventListener("click", closeAccountModal);
  $("#account-cancel").addEventListener("click", closeAccountModal);
  $("#acc-id").addEventListener("input", updateSecretPreview);
  $("#acc-token-env").addEventListener("input", updateSecretPreview);
  $("#account-modal").addEventListener("click", (event) => {
    if (event.target === $("#account-modal")) closeAccountModal();
  });

  $("#account-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const index = Number($("#acc-index").value);
    const isNew = index < 0;

    const id = slugify($("#acc-id").value);
    if (!id) {
      toast("アカウントIDには半角英数字を含めてください", "error");
      return;
    }
    const duplicated = state.accounts.some((a, i) => a.id === id && i !== index);
    if (duplicated) {
      toast(`アカウントIDが重複しています: ${id}`, "error");
      return;
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
      threads_access_token: $("#acc-token").value.trim(),
      threads_token_env: $("#acc-token-env").value.trim(),
      posts_per_day: Number($("#acc-posts").value) || 7,
      rakuten_affiliate_id: $("#acc-affiliate").value.trim(),
      note: $("#acc-note").value.trim(),
    };

    const backup = [...state.accounts];
    if (isNew) state.accounts.push(account);
    else state.accounts[index] = account;

    const button = $("#account-form button[type=submit]");
    button.disabled = true;
    button.textContent = "保存中...";
    try {
      await saveAccounts(`chore(accounts): ${isNew ? "add" : "update"} ${id}`);
      closeAccountModal();
      render();
    } catch (error) {
      state.accounts = backup;
      toast(`保存に失敗しました: ${error.message}`, "error");
    } finally {
      button.disabled = false;
      button.textContent = "保存してコミット";
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
        <p class="text-xs text-muted">
          <code class="code">${escapeHtml(PATHS.prompt)}</code> を編集します。
        </p>
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
