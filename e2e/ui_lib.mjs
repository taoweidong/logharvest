// LogHarvest 前端 E2E 的公共层：配置、HTTP 客户端、断言台账、DOM 操作原语。
//
// 定位口径全部来自 e2e/refs/ui-map.md（只读源码整理，行号对应 HEAD=ff23e94）：全站没有
// data-testid，所以"文案即选择器"；生产是 hash 路由；列表接口不返回总数，所以断言的是
// 「第 N 页，本页 M 条」这类窗口式文案；v-perms 是 removeChild，所以权限负断言一律用
// toHaveCount(0)。
//
// ElMessage 只活 2000ms（web/src/utils/message.ts:60），蹲守瞬时 DOM 的断言一定漏。
// RECORDER 给每个 context 注入 MutationObserver，把消息文本永久记进 window.__messages，
// 测试读的是历史而不是 2 秒窗口 —— 它不改应用行为，只在测试页面里加记录器。

import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
export const PW_PATH = (
  process.env.PLAYWRIGHT_PATH
  || 'D:/nodejs/node_global/node_modules/@playwright/cli/node_modules/playwright'
).replace(/\\/g, '/');
export const { chromium } = require(PW_PATH);

const THIS_DIR = path.dirname(new URL(import.meta.url).pathname.replace(/^\/(\w:)/, '$1'));
export const E2E_DIR = THIS_DIR;
export const ARTIFACTS = path.join(THIS_DIR, 'artifacts');
export const UI_DIR = path.join(ARTIFACTS, 'ui');
export const CTX_DIR = path.join(ARTIFACTS, 'ctx');

export const WEB = (process.env.E2E_WEB_BASE || 'http://127.0.0.1:18080/').replace(/\/?$/, '/');
export const API_BASE = process.env.E2E_API_BASE || 'http://127.0.0.1:18080/api';

// 本地 e2e 占位口令，绝非生产凭据（与 e2e/env.sh、e2e/rbac_flow.py 同一套）。
export const ACCOUNTS = {
  admin: { username: process.env.ADMIN_USERNAME || 'admin', password: process.env.ADMIN_PASSWORD || 'E2eAdmin#2026' },
  operator: { username: 'ui-operator', password: 'E2eUi#Pass1' },
  viewer: { username: 'ui-viewer', password: 'E2eUi#Pass1' },
};

export const RUN_TS = Date.now();
export const jobName = (prefix) => `${prefix}-ui-${RUN_TS}`;

export const cleanOracle = () => {
  const fixtures = JSON.parse(fs.readFileSync(path.join(ARTIFACTS, 'fixtures.json'), 'utf-8'));
  return fixtures.cases.clean;
};

export class ApiError extends Error {
  constructor(status, body, method, url) {
    super(`${method} ${url} -> HTTP ${status}: ${JSON.stringify(body).slice(0, 400)}`);
    this.status = status;
    this.body = body;
  }
}

/** 只用于造前置数据与收尾，不参与页面断言。 */
export class Api {
  constructor(base = API_BASE) {
    this.base = base;
    this.token = null;
    this.calls = 0;
  }

  async call(method, p, payload) {
    const headers = { 'Content-Type': 'application/json' };
    if (this.token) headers.Authorization = `Bearer ${this.token}`;
    this.calls += 1;
    const response = await fetch(`${this.base}${p}`, {
      method,
      headers,
      body: payload === undefined ? undefined : JSON.stringify(payload),
    });
    const text = await response.text();
    let body = null;
    if (text) {
      try {
        body = JSON.parse(text);
      } catch {
        body = text;
      }
    }
    return { status: response.status, body };
  }

  async get(p) {
    const r = await this.call('GET', p);
    if (r.status >= 300) throw new ApiError(r.status, r.body, 'GET', p);
    return r.body;
  }

  async send(method, p, payload) {
    const r = await this.call(method, p, payload);
    if (r.status >= 300) throw new ApiError(r.status, r.body, method, p);
    return r.body;
  }

  async login(username, password) {
    const r = await this.send('POST', '/login', { username, password });
    this.token = r.data.accessToken;
    return this.token;
  }
}

// ---------------------------------------------------------------------------
// 断言台账：与 e2e/client.py 的 Ledger 同一形状，报告阶段直接合并多份 JSON。
// ---------------------------------------------------------------------------
export class Ledger {
  constructor(title) {
    this.title = title;
    this.checks = [];
    this.phaseName = '';
  }

  phase(name) {
    this.phaseName = name;
    console.log(`\n---- ${name} ${'-'.repeat(Math.max(0, 56 - name.length))}`);
  }

  check(name, ok, detail = '') {
    const text = typeof detail === 'string' ? detail : JSON.stringify(detail);
    this.checks.push({ name, ok: Boolean(ok), detail: text.slice(0, 500), phase: this.phaseName });
    console.log(`  [${ok ? 'PASS' : 'FAIL'}] ${name}${!ok && text ? `  <- ${text.slice(0, 220)}` : ''}`);
    return Boolean(ok);
  }

  equal(name, actual, expected) {
    return this.check(name, actual === expected, `actual=${JSON.stringify(actual)} expected=${JSON.stringify(expected)}`);
  }

  skip(name, reason) {
    this.checks.push({ name, ok: true, detail: `SKIPPED: ${reason}`, phase: this.phaseName });
    console.log(`  [SKIP] ${name}  (${reason})`);
  }

  fail(name, err) {
    this.checks.push({ name, ok: false, detail: `${err?.name ?? 'Error'}: ${err?.message ?? err}`.slice(0, 500), phase: this.phaseName });
    console.log(`  [FAIL] ${name}  <- ${err?.message ?? err}`);
  }

  get failed() {
    return this.checks.filter((c) => !c.ok);
  }

  summary() {
    const passed = this.checks.filter((c) => c.ok && !c.detail.startsWith('SKIPPED')).length;
    const skipped = this.checks.filter((c) => c.ok && c.detail.startsWith('SKIPPED')).length;
    const failed = this.failed.length;
    const result = {
      title: this.title,
      generated_at: new Date().toISOString().slice(0, 19),
      total: this.checks.length,
      passed,
      skipped,
      failed,
      checks: this.checks,
    };
    fs.mkdirSync(ARTIFACTS, { recursive: true });
    const out = path.join(ARTIFACTS, `ledger_${this.title.toLowerCase().replace(/\s+/g, '_')}.json`);
    fs.writeFileSync(out, JSON.stringify(result, null, 2), 'utf-8');
    console.log(`\n==== ${this.title}: ${passed} passed / ${skipped} skipped / ${failed} failed（共 ${this.checks.length} 条）-> ${out}`);
    for (const c of this.failed.slice(0, 20)) console.log(`  !! [${c.phase}] ${c.name}: ${c.detail}`);
    return result;
  }
}

// ---------------------------------------------------------------------------
// 页面原语
// ---------------------------------------------------------------------------
export const RECORDER = () => {
  window.__messages = [];
  const scan = (node) => {
    if (!node || node.nodeType !== 1) return;
    const el = node;
    if (el.classList && el.classList.contains('el-message')) window.__messages.push((el.textContent || '').trim());
    el.querySelectorAll?.('.el-message').forEach((x) => window.__messages.push((x.textContent || '').trim()));
  };
  const attach = () => {
    new MutationObserver((records) => records.forEach((r) => r.addedNodes.forEach(scan)))
      .observe(document.documentElement, { childList: true, subtree: true });
  };
  if (document.documentElement) attach();
  else document.addEventListener('DOMContentLoaded', attach);
};

export async function newPage(browser, { storageState } = {}) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, storageState });
  await context.addInitScript(RECORDER);
  const page = await context.newPage();
  page.__requests = [];
  page.on('request', (req) => {
    if (req.url().includes('/api/')) page.__requests.push({ method: req.method(), url: req.url(), headers: req.headers() });
  });
  return { context, page };
}

export const messagesOf = (page) => page.evaluate(() => window.__messages || []);
export const clearMessages = (page) => page.evaluate(() => { window.__messages = []; });

/** 读记录器历史，返回第一条匹配 re 的消息文本。 */
export async function waitMessage(page, re, timeout = 8000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const list = await messagesOf(page);
    const hit = list.find((t) => re.test(t));
    if (hit) return hit;
    await page.waitForTimeout(100);
  }
  throw new Error(`未在 ${timeout}ms 内捕获消息 ${re}，历史=${JSON.stringify(await messagesOf(page))}`);
}

export async function recentRequests(page, re) {
  return page.__requests.filter((r) => re.test(r.url));
}

/** hash 路由：同文档换 hash 不会重载，需要重拉 /auth/routes 时显式 reload。 */
export async function go(page, hash, { reload = false, wait = null, waitText = null } = {}) {
  const target = `${WEB}#${hash}`;
  const sameHash = page.url().split('#')[1] === hash;
  if (sameHash || reload) {
    // 同一文档换 hash 不会重载；同 hash 再 goto 更是 no-op，想重拉 /auth/routes 与
    // 组件 onMounted 必须显式 reload（Playwright 的 goto 在 URL 完全相同时不保证重载）。
    await page.goto(target, { waitUntil: 'domcontentloaded' });
    await page.reload({ waitUntil: 'domcontentloaded' });
  } else {
    await page.goto(target, { waitUntil: 'domcontentloaded' });
  }
  if (waitText) await page.getByText(waitText, { exact: false }).first().waitFor({ state: 'visible', timeout: 15000 });
  if (wait) await wait.waitFor({ state: 'visible', timeout: 15000 });
  await page.waitForTimeout(350);
  return page;
}

export const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/** 锚点写法：
 *  - 字符串：el-select 的 placeholder（仅未选中时存在）或已选中文本；
 *  - 对象 { formLabel: '作业', placeholder?: '选择要查看的作业' }：按 el-form-item 的 label 锚定。
 *    选中之后 placeholder 会被值替换掉，字符串锚点会失效——重复选择同一个下拉时**必须**用 formLabel。
 */
const anchorText = (anchor) => (typeof anchor === 'string' ? anchor : `${anchor.formLabel ?? ''}(${anchor.placeholder ?? ''})`);

export async function openSelect(page, anchor, { scope = page, nth = 0 } = {}) {
  let box;
  if (typeof anchor === 'object' && anchor !== null) {
    if (!anchor.formLabel) throw new Error(`对象锚点需要 formLabel，收到 ${JSON.stringify(anchor)}`);
    const fields = scope.locator('.el-form-item').filter({ has: page.locator(`label:text-is("${anchor.formLabel}")`) });
    const n = await fields.count();
    if (n === 0) throw new Error(`页面上没有 label 为「${anchor.formLabel}」的 el-form-item（先确认文案来自源码）`);
    box = fields.last().locator('.el-select').first();
  } else {
    box = scope.locator('.el-select')
      .filter({ has: page.locator(`.el-select__placeholder:text-is("${anchor}")`) })
      .nth(nth);
    if ((await box.count()) === 0) {
      box = scope.locator('.el-select').filter({ hasText: anchor }).nth(nth);
    }
  }
  await box.waitFor({ state: 'visible', timeout: 8000 });
  await box.click();
  const dropdown = page.locator('.el-select-dropdown:visible').last();
  await dropdown.locator('.el-select-dropdown__item').first().waitFor({ state: 'visible', timeout: 8000 });
  return { box, dropdown, items: dropdown.locator('.el-select-dropdown__item') };
}

/** 点开 el-select 并选中匹配项；返回实际选中的文本。 */
export async function pickOption(page, anchor, match, options = {}) {
  const { dropdown, items } = await openSelect(page, anchor, options);
  const texts = (await items.allInnerTexts()).map((t) => t.trim());
  const { exact = false } = options;
  const re = match instanceof RegExp ? match : new RegExp(exact ? `^${esc(match)}$` : esc(match));
  const idx = texts.findIndex((t) => re.test(t));
  if (idx < 0) {
    await page.keyboard.press('Escape');
    throw new Error(`下拉「${anchorText(anchor)}」没有匹配 ${re} 的选项，可见项=${JSON.stringify(texts.slice(0, 25))}`);
  }
  await items.nth(idx).click();
  await dropdown.waitFor({ state: 'hidden', timeout: 5000 }).catch(() => {});
  return texts[idx];
}

export async function selectOptionsOf(page, anchor, options = {}) {
  const { dropdown, items } = await openSelect(page, anchor, options);
  const texts = (await items.allInnerTexts()).map((t) => t.trim());
  await page.keyboard.press('Escape');
  // Escape 后必须等本面板真的隐藏再返回：立即连开下一个下拉时，旧面板还在关闭
  // 过渡里（items 仍 :visible），下一次 openSelect 的 waitFor 会撞上旧面板的选项。
  await dropdown.waitFor({ state: 'hidden', timeout: 5000 }).catch(() => {});
  return texts;
}

/** el-form 字段红字（.el-form-item__error）。 */
export const formErrors = (page) => page.locator('.el-form-item__error').allInnerTexts().then((a) => a.map((t) => t.trim()));

/** 行内 el-table 行定位（本 EP 版本无克隆固定表，实测 .el-table__body .el-table__row 计数等于真实行数）。 */
export const tableRow = (page, text, { index = -1 } = {}) => {
  const rows = page.locator('.el-table__body .el-table__row');
  if (index >= 0) return rows.nth(index);
  return rows.filter({ hasText: text }).first();
};

/** 表头文案。
 *  EP 有两种表头 DOM：默认（fixed 列）表头是独立表格 `.el-table__header th`；
 *  `table-layout="auto"`（system 四个页面）只渲染一张表，表头退化成
 *  `thead.el-table__body-header th`，此时 `.el-table__header` 计数为 0。
 *  实测（HEAD=ff23e94）：/logharvest/jobs 走前者，/system/users 走后者。
 *  取并集并按 DOM 顺序去重，两种布局都能拿到表头，且不会重复计数。
 */
export const tableHeaderTexts = (page) => page.evaluate(() => {
  const seen = new Set();
  const out = [];
  document.querySelectorAll('.el-table__header th, thead.el-table__body-header th').forEach((th) => {
    if (seen.has(th)) return;
    seen.add(th);
    const t = (th.textContent || '').replace(/\s+/g, ' ').trim();
    if (t) out.push(t);
  });
  return out;
});

export const bodyButtons = (page) => page.locator('button').allInnerTexts()
  .then((a) => a.map((t) => t.trim()).filter(Boolean));

export async function readMessageBox(page) {
  const box = page.locator('.el-message-box:visible').last();
  await box.waitFor({ state: 'visible', timeout: 8000 });
  return {
    box,
    title: (await box.locator('.el-message-box__title').innerText()).trim(),
    body: (await box.locator('.el-message-box__message').innerText()).replace(/\s+/g, ' ').trim(),
    buttons: (await box.locator('.el-button').allInnerTexts()).map((t) => t.trim()).filter(Boolean),
  };
}

export async function clickMessageBoxButton(page, name) {
  const box = page.locator('.el-message-box:visible').last();
  await box.getByRole('button', { name }).click();
  await page.waitForTimeout(400);
}

export async function dialog(page, title) {
  const d = page.getByRole('dialog').filter({ hasText: title }).last();
  await d.waitFor({ state: 'visible', timeout: 8000 });
  return d;
}

export async function stepButton(page, name) {
  await page.getByRole('button', { name }).click();
  await page.waitForTimeout(500);
}

export async function shot(page, ledger, label) {
  fs.mkdirSync(UI_DIR, { recursive: true });
  const slug = `${ledger.phaseName.slice(0, 10).replace(/[^0-9A-Za-z]+/g, '') || 'phase'}_${label.replace(/[^0-9A-Za-z_-]+/g, '-')}`;
  const file = path.join(UI_DIR, `${slug}.png`);
  await page.screenshot({ path: file, fullPage: false }).catch(() => {});
  const txt = path.join(UI_DIR, `${slug}.txt`);
  await page.locator('body').innerText({ timeout: 4000 })
    .then((t) => fs.writeFileSync(txt, t, 'utf-8')).catch(() => {});
  return file;
}

export const waitFor = async (fn, { timeout = 20000, interval = 700, label = 'condition' } = {}) => {
  const deadline = Date.now() + timeout;
  let last;
  while (Date.now() < deadline) {
    try {
      last = await fn();
      if (last) return last;
    } catch (err) {
      last = `err:${err.message}`;
    }
    await new Promise((r) => setTimeout(r, interval));
  }
  throw new Error(`${label} 未在 ${timeout}ms 内满足，最后值=${String(last)}`);
};

// ---------------------------------------------------------------------------
// 会话与页面复用原语（从 ui_flow.mjs 上移，供 ui_flow*.mjs 各阶段模块共用）。
// ---------------------------------------------------------------------------

/** 会话表：同一账号只登录一次，context 常驻到收尾。 */
export const sessions = new Map();

export async function openSession(st, key, { ledger = null, name = null } = {}) {
  if (sessions.has(key)) return sessions.get(key);
  const account = ACCOUNTS[key];
  const { context, page } = await newPage(st.browser);
  const form = page.locator('form').first();
  await page.goto(`${WEB}#/login`, { waitUntil: 'domcontentloaded' });
  await form.waitFor({ state: 'visible', timeout: 20000 }).catch(() => {});
  await page.getByPlaceholder('账号').fill(account.username);
  await page.getByPlaceholder('密码').fill(account.password);
  const landing = page.waitForURL(/#\/(home|access-denied)/, { timeout: 30000 }).catch(() => null);
  await page.getByRole('button', { name: '登录' }).click();
  await landing;
  await page.waitForTimeout(800);
  if (ledger) await shot(page, ledger, `session_${name ?? key}_landing`);
  const entry = { context, page, cookie: await context.cookies() };
  sessions.set(key, entry);
  st[key === 'admin' ? 'adminPage' : `${key}Page`] = page;
  return entry;
}

/** 取会话并做最低限度自证：登录 cookie 存在 + 落地 URL 记入台账。 */
export async function useSession(ledger, st, key, { name = null } = {}) {
  const { context, page } = await openSession(st, key, { ledger, name });
  const has = await context.cookies().then((c) => c.some((x) => x.name === 'authorized-token'));
  ledger.check(`${key} 会话已建立（cookie authorized-token）`, has, `cookies=${JSON.stringify((await context.cookies()).map((c) => c.name))}`);
  ledger.check(`${key} 落地页`, /#\/(home|access-denied)/.test(page.url()), page.url());
  return page;
}

/** 用 API 取消作业，仅用于造终态/清理；409（已终态）按预期吞掉。 */
export async function cancelViaApi(ledger, api, jobId, label) {
  const r = await api.call('POST', `/jobs/${jobId}/cancel`, {});
  if (r.status === 409) {
    ledger.check(`${label}：API 取消返回 409（已是终态，符合预期）`, true, JSON.stringify(r.body).slice(0, 160));
    return 'terminal';
  }
  if (r.status >= 300) throw new ApiError(r.status, r.body, 'POST', `/jobs/${jobId}/cancel`);
  return r.body?.status ?? 'ok';
}

export async function waitJobTerminal(api, jobId, { timeout = 600000, interval = 3000 } = {}) {
  const deadline = Date.now() + timeout;
  let job;
  while (Date.now() < deadline) {
    job = await api.get(`/jobs/${jobId}`);
    if (['succeeded', 'partial', 'failed', 'cancelled'].includes(job.status)) return job;
    await new Promise((r) => setTimeout(r, interval));
  }
  throw new Error(`作业 ${jobId} 未在 ${timeout}ms 内进入终态，最后状态=${job?.status}`);
}

/** 侧边菜单文本（分组 = el-sub-menu__title，叶子 = el-menu-item）。 */
export async function menuTexts(page) {
  return page.evaluate(() => {
    const txt = (el) => (el.textContent || '').replace(/\s+/g, ' ').trim();
    const pick = (sel) => [...document.querySelectorAll(sel)].map(txt).filter(Boolean);
    return { groups: pick('.el-sub-menu__title'), items: pick('.el-menu-item'), url: location.hash };
  });
}

/** el-form-item 里按 label 定位下拉（步骤②的选择器没有 placeholder，只能靠 label）。 */
export async function pickFormSelect(page, label, match, { type = null } = {}) {
  const item = page.locator('.el-form-item').filter({ has: page.locator(`label:text-is("${label}")`) }).last();
  await item.locator('.el-select').first().waitFor({ state: 'visible', timeout: 8000 });
  await item.locator('.el-select').first().click();
  const dropdown = page.locator('.el-select-dropdown:visible').last();
  const items = dropdown.locator('.el-select-dropdown__item');
  await items.first().waitFor({ state: 'visible', timeout: 8000 });
  if (type !== null) await item.locator('input').last().fill(type);
  const texts = (await items.allInnerTexts()).map((t) => t.trim());
  const re = match instanceof RegExp ? match : new RegExp(esc(match));
  const idx = texts.findIndex((t) => re.test(t));
  if (idx < 0) {
    await page.keyboard.press('Escape');
    throw new Error(`字段「${label}」没有匹配 ${re} 的选项，可见项=${JSON.stringify(texts.slice(0, 20))}`);
  }
  await items.nth(idx).click();
  await page.waitForTimeout(400);
  return texts[idx];
}

/** 清空 clearable 的 el-select：图标 .el-select__clear 只在 hover 时出现（源码 nsSelect.e("clear")），
 *  所以必须先 hover 再点；点它会同时触发 change → 页面重新拉列表（不带该筛选参数）。
 *  已选中项没有 placeholder 可锚定，故按当前显示的 value 文本定位。 */
export async function clearSelect(page, value) {
  const box = page.locator('.el-select').filter({ hasText: value }).first();
  await box.waitFor({ state: 'visible', timeout: 8000 });
  await box.hover();
  const icon = box.locator('.el-select__clear').first();
  await icon.waitFor({ state: 'visible', timeout: 5000 });
  await icon.click();
  await page.waitForTimeout(300);
}

/** 关闭 el-drawer：Escape 在焦点不在抽屉时不一定生效，兜底点头部的 close 按钮。 */
export async function closeDrawer(page) {
  const d = page.locator('.el-drawer');
  if ((await d.count()) === 0) return true;
  await page.keyboard.press('Escape');
  await page.waitForTimeout(500);
  if (await d.first().isVisible().catch(() => false)) {
    await d.first().locator('.el-drawer__close-btn, .el-drawer__header button').first().click().catch(() => {});
    await page.waitForTimeout(500);
  }
  await d.first().waitFor({ state: 'hidden', timeout: 6000 }).catch(() => {});
  return !(await d.first().isVisible().catch(() => false));
}
