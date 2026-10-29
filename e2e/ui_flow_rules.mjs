/**
 * U10 规则集 + U11 规则维护 —— 页面全流程 E2E 的阶段模块（由 ui_flow.mjs 的 lazyPhase 动态加载）。
 *
 * 契约：`export const phases = { rulesets, rules }`；每个阶段第一句 `ledger.phase(PHASE_LABEL.xxx)`。
 * DEPS=['login']，所以进来时 st.api 已登录、st.oracle/st.canonical/st.nasSource 已就绪。
 *
 * 铁律：只断言页面事实（可见文案 / tag 的 type class / DOM 有无 / disabled / 真实请求 URL+payload）。
 * API 只用于造前置数据；写操作只作用于自建的 `ui-ruleset-*` 规则集，绝不碰 `e2e-canonical`。
 *
 * 文案逐字来源（改文案必须先改这里）：
 * - web/src/views/logharvest/rulesets/index.vue（列头、脚注、分页、详情/编辑规则入口）
 * - web/src/views/logharvest/rulesets/edit-drawer.vue（抽屉标题/描述项/规则摘要/页脚）
 * - web/src/views/rules/maintenance/index.vue（Tab、两个下拉、表单、另存为新版本、跨版本检索）
 * - web/src/views/rules/maintenance/rule-table.vue + rule-form.ts（列顺序、摘要、红字、确认框）
 * - web/src/views/logharvest/format.ts（severity → tag type、formatDateTime）
 * - service/src/api/v1/routes_rule_sets.py + application/services/rule_sets.py（只有 GET/POST；同名 max+1）
 * - service/src/domain/rules/evaluator.py（422 原文：invalid regex pattern / not a named group）
 * - service/src/application/services/analytics.py（跨版本索引的排序、摘要、过滤语义）
 */
import {
  RUN_TS,
  useSession,
  go,
  esc,
  shot,
  waitFor,
  clearMessages,
  waitMessage,
  recentRequests,
  messagesOf,
  clearSelect,
  bodyButtons,
  readMessageBox,
  clickMessageBoxButton,
  closeDrawer,
} from './ui_lib.mjs';

// ---------------------------------------------------------------- 自建数据名

/** U10 用：v1 两条规则、v2 三条（含一条停用），验证「同名版本共存 + 抽屉只读展示」。 */
const RS_NAME = `ui-ruleset-${RUN_TS}`;
/** U11 用： pristine 模板（页面永不改它），只作为「载入既有版本再改名另存」的起点。 */
const M_NAME = `ui-ruleset-m-${RUN_TS}`;
/** U11 用：页面上真正新建出来的版本链 v1/v2/v3（改名另存 → 重排+增规则 → 删规则）。 */
const N_NAME = `ui-ruleset-n-${RUN_TS}`;
/** U11 空白态缺陷检查用的名字：只填不存，不留残留行。 */
const BLANK_NAME = `ui-ruleset-blank-${RUN_TS}`;

const RE_MS = 'cost=(?P<ms>\\d+)ms'; // 实际文本：cost=(?P<ms>\d+)ms
const RE_BAD = 'cost=(?P<ms>\\d+'; // 括号不闭合 → 后端 regex.error → 422

const KW_RULE = {
  id: 'ui-kw', name: 'ui 关键字规则', enabled: true, severity: 'warning', tags: ['ui'],
  match: { type: 'keyword', any: ['OOM', 'kill'], all: [], case_sensitive: false },
};
const RE_RULE = {
  id: 'ui-re', name: 'ui 正则规则', enabled: true, severity: 'error', tags: [],
  match: { type: 'regex', pattern: RE_MS, flags: 'i', timeout_ms: 50 },
  threshold: { field: 'ms', op: 'gt', value: '1000' },
};
const OFF_RULE = {
  id: 'ui-off', name: 'ui 停用规则', enabled: false, severity: 'info', tags: ['off'],
  match: { type: 'keyword', any: ['noop'], all: [], case_sensitive: false },
};
const M_A = {
  id: 'ui-m-a', name: 'm 关键字规则', enabled: true, severity: 'warning', tags: ['ui'],
  match: { type: 'keyword', any: ['OMEGA', 'KAPPA'], all: [], case_sensitive: false },
};
const M_B = {
  id: 'ui-m-b', name: 'm 正则规则', enabled: false, severity: 'error', tags: ['thr'],
  match: { type: 'regex', pattern: RE_MS, flags: 'i', timeout_ms: 50 },
  threshold: { field: 'ms', op: 'gt', value: '1000' },
};
/** all 侧关键词 + 区分大小写 + 多标签：专门用来验 fromRule 的反向摊平（全部命中 3 词 / 「ui, thr」）。 */
const M_C = {
  id: 'ui-m-c', name: 'm 全部命中规则', enabled: true, severity: 'info', tags: ['ui', 'thr'],
  match: { type: 'keyword', any: [], all: ['ALPHA', 'BETA', 'GAMMA'], case_sensitive: true },
};

/** 与 createRuleSet 的请求体同形（name + config.name 都取表单的 trim 值）。 */
const ruleSetPayload = (name, rules) => ({
  name,
  config: { name, file_glob: '**/*', encoding: 'utf-8', fallback_encoding: 'gb18030', rules },
});

// ---------------------------------------------------------------- 页面小工具

const norm = (s) => (s ?? '').replace(/\s+/g, ' ').trim();

/** td 顺序 = 源码列顺序；返回归一化后的文本数组。 */
async function cellsOf(row) {
  return (await row.locator('td').allInnerTexts()).map(norm);
}

/** 当前可见的 el-table（el-tab-pane 未激活的 pane 是 display:none，必须 :visible）。 */
const visTable = (page) => page.locator('.el-table:visible').first();
const rowsOf = (table) => table.locator('.el-table__body .el-table__row');

async function headersOf(table) {
  const cells = await table.locator('.el-table__header th').allInnerTexts();
  return cells.map(norm).filter(Boolean);
}

/** 规则数逐条从详情回填（源码 load() 里每行一个 getRuleSet），回填完成前单元格是「加载中…」。 */
async function settleCounts(page) {
  await waitFor(
    async () => ((await page.locator('.el-table__body').first()
      .locator('.el-table__row').filter({ hasText: '加载中…' }).count()) === 0 ? 'done' : null),
    { label: '规则数回填完成', timeout: 20000 },
  ).catch(() => {});
}

/** 回到第 1 页，保证 findRow 起点确定（共享栈不断言全局行数，只能按 name+version 找我的行）。 */
async function gotoFirstPage(page) {
  const prev = page.getByRole('button', { name: '上一页' });
  for (let i = 0; i < 6; i++) {
    if ((await prev.count()) === 0) break;
    if (await prev.isDisabled().catch(() => true)) break;
    await prev.click();
    await page.waitForTimeout(600);
  }
  await settleCounts(page);
}

/** 在第 1..max 页里找 name+version 精确匹配的行。 */
async function findRow(page, name, version, { max = 6 } = {}) {
  await gotoFirstPage(page);
  const rows = page.locator('.el-table__body .el-table__row');
  for (let p = 1; p <= max; p++) {
    await settleCounts(page);
    const n = await rows.count();
    for (let k = 0; k < n; k++) {
      const cells = await cellsOf(rows.nth(k));
      if (cells[1] === name && cells[2] === String(version)) {
        return { row: rows.nth(k), cells, pageIndex: p };
      }
    }
    const next = page.getByRole('button', { name: '下一页' });
    if ((await next.count()) === 0 || (await next.isDisabled().catch(() => true))) return null;
    await next.click();
    await page.waitForTimeout(700);
  }
  return null;
}

/** el-descriptions 的 label→content 映射（抽屉概览用）。 */
async function descMap(scope) {
  const labels = await scope.locator('.el-descriptions__label').allInnerTexts();
  const values = await scope.locator('.el-descriptions__content').allInnerTexts();
  const out = {};
  labels.forEach((l, i) => { out[norm(l)] = norm(values[i] ?? ''); });
  return out;
}

const tagClassOf = async (cell) => (await cell.locator('.el-tag').first().getAttribute('class').catch(() => '')) ?? '';
const switchClassOf = async (cell) => (await cell.locator('.el-switch').first().getAttribute('class').catch(() => '')) ?? '';

/**
 * el-form-item 按 label 锚定（展开面板里的字段没有 placeholder 时用它）。
 * 注意：带 role="group" 的 el-form-item（标志位、数值阈值）渲染的是
 * <div class="el-form-item__label">，不是 <label> ⇒ 用 label: 锚定永远匹配 0 个元素，
 * 后续每次读值都要等满超时。统一锚 .el-form-item__label 才能同时覆盖两种形态。
 */
function fieldItem(page, label, scope = null) {
  const root = scope ?? page;
  return root
    .locator('.el-form-item')
    .filter({ has: page.locator(`.el-form-item__label:text-is("${label}")`) })
    .last();
}

async function fieldValue(page, label, scope = null) {
  return (await fieldItem(page, label, scope).locator('input, textarea').first().inputValue().catch(() => '')) ?? '';
}

/** 规则维护页的两个下拉：宽度类只出现在包裹 div 上，选择器不会误命中 el-input。 */
const nameBox = (page) => page.locator('.w-56 .el-select').first();
const versionBox = (page) => page.locator('.w-44 .el-select').first();

async function boxText(page, box) {
  return norm(await box.innerText().catch(() => ''));
}

/** 打开 el-select 读全部选项后关闭（选中后 placeholder 被值替换，锚定一律用 box 本身）。 */
async function openBoxOptions(page, box) {
  await box.scrollIntoViewIfNeeded().catch(() => {});
  await box.click();
  const dd = page.locator('.el-select-dropdown:visible').last();
  const items = dd.locator('.el-select-dropdown__item');
  await items.first().waitFor({ state: 'visible', timeout: 8000 });
  const texts = (await items.allInnerTexts()).map(norm);
  await page.keyboard.press('Escape');
  await dd.waitFor({ state: 'hidden', timeout: 5000 }).catch(() => {});
  return texts;
}

/** 点开 el-select 并选中匹配项，返回 { picked, texts }。 */
async function pickInBox(page, box, match, { exact = false } = {}) {
  await box.scrollIntoViewIfNeeded().catch(() => {});
  await box.click();
  const dd = page.locator('.el-select-dropdown:visible').last();
  const items = dd.locator('.el-select-dropdown__item');
  await items.first().waitFor({ state: 'visible', timeout: 8000 });
  const texts = (await items.allInnerTexts()).map(norm);
  const re = match instanceof RegExp ? match : new RegExp(exact ? `^${esc(match)}$` : esc(match));
  const idx = texts.findIndex((t) => re.test(t));
  if (idx < 0) {
    await page.keyboard.press('Escape');
    throw new Error(`下拉没有匹配 ${re} 的选项，可见项=${JSON.stringify(texts.slice(0, 30))}`);
  }
  await items.nth(idx).click();
  await dd.waitFor({ state: 'hidden', timeout: 5000 }).catch(() => {});
  await page.waitForTimeout(350);
  return { picked: texts[idx], texts };
}

/** rule-table 的展开行：同一时刻只留一个面板，避免 multiple expanded-cell 抢 first()。 */
async function collapseAll(page, table) {
  for (let guard = 0; guard < 8; guard++) {
    const opened = table.locator('.el-table__row .el-table__expand-icon--expanded');
    if ((await opened.count()) === 0) break;
    await opened.first().click().catch(() => {});
    await page.waitForTimeout(200);
  }
}

async function expandRow(page, table, index) {
  await collapseAll(page, table);
  const row = rowsOf(table).nth(index);
  await row.locator('.el-table__expand-icon').first().click();
  await waitFor(async () => (await table.locator('.el-table__expanded-cell').count()) > 0,
    { label: '展开面板出现', timeout: 8000 }).catch(() => {});
  return { row, panel: table.locator('.el-table__expanded-cell').last() };
}

/**
 * 面板可能因 rowClassName 重算被卸载（el-table 无 row-key ⇒ 整行重建，展开态丢失）。
 * 任何「改字段之后再读/写面板」的动作都必须重新解析面板，必要时按 index 重展开，
 * 否则拿着旧 locator 会一直等到 30s 超时（实测 run3 的「红字=(无)」+ fill 超时就是这么来的）。
 */
async function livePanel(page, index, need = 'textarea:visible') {
  const t = visTable(page);
  const cur = t.locator('.el-table__expanded-cell:visible').last();
  if ((await cur.locator(need).count()) > 0) return cur;
  const { panel } = await expandRow(page, t, index);
  await panel.locator(need).first().waitFor({ timeout: 8000 }).catch(() => {});
  return panel;
}

/** recentRequests 不带 body，这里单独记 POST /rule-sets 的真实请求体（断言“前端拦下时根本没发请求”）。 */
async function trackRuleSetPosts(st, page) {
  st.rulesetPosts = st.rulesetPosts ?? [];
  st.__trackedPages = st.__trackedPages ?? new Set();
  if (st.__trackedPages.has(page)) return;
  st.__trackedPages.add(page);
  page.on('request', (req) => {
    if (req.method() !== 'POST' || !/\/api\/rule-sets$/.test(req.url())) return;
    let body = null;
    try { body = JSON.parse(req.postData() ?? 'null'); } catch { body = req.postData(); }
    const entry = { url: req.url(), body, status: 0, id: 0, version: 0 };
    st.rulesetPosts.push(entry);
    // 响应里的 id/version 才是「刚由页面创建的那一条」。UI 的下拉在 loadSets() 完成前
    // 仍显示上一条选中项（index.vue 先弹「已创建版本 N」再 await loadSets），
    // 只按 vN（#任意） 匹配会把模板行的 #50 当成自建的 v1（实测 run4 的四连错）。
    req.response()
      .then(async (res) => {
        entry.status = res.status();
        try {
          const json = await res.json();
          entry.id = Number(json?.id ?? 0);
          entry.version = Number(json?.version ?? 0);
        } catch { /* 422 的 detail 不是 {id,version}，留 0 由调用方判定 */ }
      })
      .catch(() => {});
  });
}

/** 等 body 文本出现/消失（Element Plus 的重渲染是异步的，读太早会把「还没卸载」读成「同时在」）。 */
async function bodyHas(page, text, { timeout = 12000 } = {}) {
  return waitFor(async () => ((await page.locator('body').innerText()).replace(/\s+/g, ' ').includes(text) ? text : null),
    { label: `文案「${text}」出现`, timeout }).catch(() => null);
}

async function bodyText(page) {
  return norm(await page.locator('body').innerText());
}

// ---------------------------------------------------------------- U10 规则集

async function phase_rulesets(ledger, st, { PHASE_LABEL }) {
  ledger.phase(PHASE_LABEL.rulesets);
  const page = st.adminPage ?? await useSession(ledger, st, 'admin');
  const { api } = st;

  // 前置数据走 API（页面断言绝不引用接口返回）：同名两条版本行，v2 多一条停用规则。
  const v1 = await api.send('POST', '/rule-sets', ruleSetPayload(RS_NAME, [KW_RULE, RE_RULE]));
  const v2 = await api.send('POST', '/rule-sets', ruleSetPayload(RS_NAME, [KW_RULE, RE_RULE, OFF_RULE]));
  st.createdRuleSets.push(v1.id, v2.id);
  await trackRuleSetPosts(st, page);

  await go(page, '/logharvest/rulesets', { reload: true, waitText: '新建规则集' });
  await settleCounts(page);

  const reqs = await recentRequests(page, /\/api\/rule-sets/);
  ledger.check(
    '列表发的是窗口式请求 GET /api/rule-sets?limit=20&offset=0（无 total 语义）',
    reqs.some((r) => r.method === 'GET' && /limit=20&offset=0/.test(r.url)),
    reqs.slice(-3).map((r) => `${r.method} ${r.url}`).join(' | '),
  );

  const table = page.locator('.el-table').first();
  ledger.equal(
    '桌面列头照源码：ID/名称/版本/规则数/创建时间/操作',
    (await headersOf(table)).join('|'),
    'ID|名称|版本|规则数|创建时间|操作',
  );

  const hit2 = await findRow(page, RS_NAME, 2);
  ledger.check(`列表出现自建行 ${RS_NAME} v2`, Boolean(hit2), `cells=${JSON.stringify(hit2?.cells ?? [])}`);
  if (hit2) {
    ledger.check(
      'v2 行的 ID/名称/版本/规则数/创建时间 逐项正确（规则数由详情逐条回填）',
      hit2.cells[0] === String(v2.id)
        && hit2.cells[1] === RS_NAME
        && hit2.cells[2] === '2'
        && hit2.cells[3] === '3'
        && /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(hit2.cells[4]),
      JSON.stringify(hit2.cells),
    );
    ledger.check(
      '列表行不含规则内容（列表接口不返回 config，规则数走单独详情请求）',
      !/ui-kw|ui-re|ui-off|cost=|OOM/.test(hit2.cells.join(' ')),
      hit2.cells.join(' '),
    );
  }
  const hit1 = await findRow(page, RS_NAME, 1);
  ledger.check(
    '同名 v1 行与 v2 行共存且规则数各是 2/3（版本递增不覆盖）',
    Boolean(hit1) && hit1.cells[2] === '1' && hit1.cells[3] === '2' && hit2?.cells[3] === '3',
    `v1=${JSON.stringify(hit1?.cells ?? [])} v2=${JSON.stringify(hit2?.cells ?? [])}`,
  );

  // findRow 是「翻页找行」的：命中在第 2 页就会把表格留在第 2 页。下面两项断言的都是
  // 第 1 页的窗口语义，所以必须先回到第 1 页，否则读到的脚注是「第 2 页」。
  await gotoFirstPage(page);
  const foot = await bodyText(page);
  ledger.check(
    '脚注原文：裸数组窗口 + 第 1 页 + 引导到规则维护整页',
    foot.includes('后端列表接口不返回总数与规则内容；同名规则集以版本递增共存，第')
      && /共存，第\s*1\s*页。规则的增删改与「另存新版本」在/.test(foot)
      && foot.includes('「规则管理 → 规则维护」整页完成。'),
    (foot.match(/后端列表接口[^。]*。[^。]*。/) ?? [''])[0],
  );

  const prev = page.getByRole('button', { name: '上一页' });
  const next = page.getByRole('button', { name: '下一页' });
  const rowCount = await rowsOf(table).count();
  ledger.check(
    '分页按钮：第 1 页「上一页」disabled，「下一页」disabled 与否等于本页行数是否满 20',
    (await prev.isDisabled()) && (await next.isDisabled()) === (rowCount < 20),
    `本页 ${rowCount} 行，上一页 disabled=${await prev.isDisabled()}，下一页 disabled=${await next.isDisabled()}`,
  );

  if (!(await next.isDisabled())) {
    await next.click();
    await page.waitForTimeout(800);
    await settleCounts(page);
    const foot2 = await bodyText(page);
    const off20 = (await recentRequests(page, /\/api\/rule-sets\?.*offset=20/)).length > 0;
    ledger.check('翻到下一页发 offset=20 且脚注变成「第 2 页」', off20 && /共存，第\s*2\s*页/.test(foot2), `请求=${off20} 脚注片段=${(foot2.match(/共存，第\s*\d+\s*页/) ?? [''])[0]}`);
    await prev.click();
    await page.waitForTimeout(800);
    await settleCounts(page);
  } else {
    ledger.skip('翻到下一页发 offset=20', '本页行数 < 20，下一页按窗口语义 disabled（共享栈数据量，非缺陷）');
  }

  const btns = [...new Set(await bodyButtons(page))];
  ledger.check(
    '列表页没有任何删除/停用/下线入口（后端也无 PUT/DELETE，只靠新版本共存）',
    !btns.some((t) => /^(删除|停用|启用|下线|编辑|更新|新建版本)$/.test(t)),
    btns.join(','),
  );

  await page.getByRole('button', { name: '新建规则集' }).click();
  await page.waitForTimeout(1000);
  ledger.check(
    '「新建规则集」跳维护页且不带任何 query（源码 goMaintenance() 无 row ⇒ query=undefined）',
    page.url().includes('#/rules/maintenance') && !/rule_set_id|version=/.test(page.url()),
    page.url(),
  );

  await go(page, '/logharvest/rulesets', { reload: true, waitText: '新建规则集' });
  await settleCounts(page);
  const row2 = await findRow(page, RS_NAME, 2);
  if (row2) {
    await row2.row.getByRole('button', { name: '详情' }).click();
    const dw = page.locator('.el-drawer:visible').last();
    await dw.waitFor({ state: 'visible', timeout: 8000 }).catch(() => {});
    // 抽屉标题是三元表达式：detail 还没回来时是回退态「查看规则集 #id」。等到概览渲染出来
    // 再读，断言的才是「detail 已加载」那一支，否则就是在测加载中的标题。
    const head = await waitFor(async () => {
      const h = norm(await dw.locator('.el-drawer__header').innerText().catch(() => ''));
      return h.startsWith(`规则集 ${RS_NAME} v2（#${v2.id}）`) ? h : null;
    }, { label: '抽屉标题回填', timeout: 12000 }).catch(async () =>
      norm(await dw.locator('.el-drawer__header').innerText().catch(() => '')));
    ledger.check(
      `抽屉标题「规则集 ${RS_NAME} v2（#${v2.id}）」`,
      head.includes(`规则集 ${RS_NAME} v2（#${v2.id}）`),
      head,
    );

    const desc = await descMap(dw);
    ledger.check(
      '抽屉概览 8 项描述与后端 detail 一致（含 v 前缀、# id、编码回退、创建人）',
      Object.keys(desc).join('|') === 'ID|名称|版本|创建时间|创建人|规则条数|文件通配|编码'
        && desc['ID'] === `#${v2.id}`
        && desc['名称'] === RS_NAME
        && desc['版本'] === 'v2'
        && desc['规则条数'] === '3'
        && desc['文件通配'] === '**/*'
        && desc['编码'] === 'utf-8 / 回退 gb18030'
        && desc['创建人'] === 'admin',
      JSON.stringify(desc),
    );
    ledger.check(
      '抽屉里的创建时间是后端原始串（带 T），与列表的 formatDateTime 不同形',
      /T\d{2}:\d{2}/.test(desc['创建时间'] ?? '') && !/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(desc['创建时间'] ?? ''),
      desc['创建时间'] ?? '(缺失)',
    );

    const dTable = dw.locator('.el-table').first();
    const dRows = rowsOf(dTable);
    ledger.check(
      '抽屉规则表列头与行数（7 列 / config.rules 3 条）',
      (await headersOf(dTable)).join('|') === '规则 id|展示名|严重级|启用|匹配|阈值|标签'
        && (await dRows.count()) === 3,
      `headers=${(await headersOf(dTable)).join('|')} rows=${await dRows.count()}`,
    );

    const c0 = await cellsOf(dRows.nth(0));
    ledger.check(
      '关键字行：严重级列是纯文本 warning（源码没给 tag 插槽）+「启用」走 success plain tag + 任一命中忽略大小写摘要 + 无阈值「-」',
      c0[0] === 'ui-kw' && c0[2] === 'warning' && c0[3] === '启用'
        && c0[4] === '关键字（任一命中，忽略大小写）：OOM、kill' && c0[5] === '-' && c0[6] === 'ui'
        && /el-tag--success/.test(await tagClassOf(dRows.nth(0).locator('td').nth(3)))
        && /el-tag--plain/.test(await tagClassOf(dRows.nth(0).locator('td').nth(3)))
        && (await dRows.nth(0).locator('td').nth(2).locator('.el-tag').count()) === 0,
      JSON.stringify(c0),
    );

    const c1 = await cellsOf(dRows.nth(1));
    ledger.check(
      '正则行：error 纯文本 + pattern/标志/超时摘要 + 阈值「ms gt 1000」+ 空标签走「-」',
      c1[0] === 'ui-re' && c1[2] === 'error' && c1[4] === `正则 /${RE_MS}/（标志 i，超时 50ms）`
        && c1[5] === 'ms gt 1000' && c1[6] === '-'
        && c1[3] === '启用',
      JSON.stringify(c1),
    );

    const c2 = await cellsOf(dRows.nth(2));
    ledger.check(
      'enabled=false 的规则在抽屉里是「停用」info tag（不是红字也不是缺行）',
      c2[0] === 'ui-off' && c2[2] === 'info' && c2[3] === '停用'
        && /el-tag--info/.test(await tagClassOf(dRows.nth(2).locator('td').nth(3))),
      JSON.stringify(c2),
    );

    const footer = norm(await dw.locator('.el-drawer__footer').innerText().catch(() => ''));
    const goBtn = dw.getByRole('button', { name: '去维护页编辑' });
    ledger.check(
      '写权限页脚文案「规则编辑已迁到「规则维护」整页…」+「去维护页编辑」可点',
      footer.includes('规则编辑已迁到「规则维护」整页：改完另存为新版本（同名版本 max+1）')
        && (await goBtn.count()) === 1 && !(await goBtn.isDisabled().catch(() => true)),
      footer,
    );
    await shot(page, ledger, 'rulesets_drawer_v2');

    await goBtn.click();
    await page.waitForTimeout(1200);
    ledger.check(
      '「去维护页编辑」带 rule_set_id+version 深链，且维护页直接加载该版本（3 条）',
      page.url().includes(`rule_set_id=${v2.id}`) && page.url().includes(`version=2`)
        && (await bodyHas(page, '规则（3 条）')) !== null,
      page.url(),
    );
  }

  await go(page, '/logharvest/rulesets', { reload: true, waitText: '新建规则集' });
  await settleCounts(page);
  const row1 = await findRow(page, RS_NAME, 1);
  if (row1) {
    await row1.row.getByRole('button', { name: '编辑规则' }).click();
    await page.waitForTimeout(1200);
    ledger.check(
      '行内「编辑规则」深链到 v1，且 v2 已存在时 v1 仍是 2 条（版本行不可变，引用安全）',
      page.url().includes(`rule_set_id=${v1.id}`) && page.url().includes('version=1')
        && (await bodyHas(page, '规则（2 条）')) !== null,
      `${page.url()} body=${(await bodyText(page)).match(/规则（\d+ 条）/)?.[0] ?? ''}`,
    );
  }

  await go(page, '/logharvest/rulesets', { reload: true, waitText: '新建规则集' });
  await settleCounts(page);
  await page.setViewportSize({ width: 390, height: 844 });
  const mobileHeaders = await waitFor(async () => {
    const h = await headersOf(page.locator('.el-table').first());
    return h.join('|') === 'ID|名称|版本|规则数|操作' ? h.join('|') : null;
  }, { label: '窄屏列头去掉创建时间', timeout: 12000 }).catch(() => null);
  await page.setViewportSize({ width: 1440, height: 900 });
  const backHeaders = await waitFor(async () => {
    const h = await headersOf(page.locator('.el-table').first());
    return h.join('|') === 'ID|名称|版本|规则数|创建时间|操作' ? h.join('|') : null;
  }, { label: '恢复桌面列头', timeout: 12000 }).catch(() => null);
  ledger.check(
    '390 窄屏裁掉「创建时间」列，回 1440 后该列恢复',
    Boolean(mobileHeaders) && Boolean(backHeaders),
    `390=${mobileHeaders ?? '(未生效)'} 1440=${backHeaders ?? '(未恢复)'}`,
  );
  await settleCounts(page);

  // ---- 「加载中…」是被 UI 承认的中间态：把详情请求钉住就能看到灰色原文 ----
  const detailRe = /\/api\/rule-sets\/\d+$/;
  // reload 会把在途请求 abort 掉，Playwright 侧这些 route 可能已被处理；不 catch 的话
  // route.continue 抛「Route is already handled!」，未捕获的 rejection 直接干掉整个进程。
  await page.route(detailRe, async (route) => {
    await new Promise((r) => setTimeout(r, 1500));
    await route.continue().catch(() => {});
  });
  await go(page, '/logharvest/rulesets', { reload: true, waitText: '新建规则集' });
  const pending = await waitFor(
    async () => {
      const cell = page
        .locator('.el-table__body .el-table__row td:nth-child(4) span.text-gray-400')
        .first();
      const txt = norm(await cell.innerText().catch(() => ''));
      return txt === '加载中…' ? (await cell.count()) : null;
    },
    { label: '钉住详情请求后规则数列停在「加载中…」', timeout: 8000 },
  ).catch(() => null);
  await page.unroute(detailRe);
  await settleCounts(page);
  const greyLeft = await page
    .locator('.el-table__body .el-table__row td:nth-child(4) span.text-gray-400')
    .count();
  ledger.check(
    '规则数是异步逐行回填：详情请求未回来时该列是灰色「加载中…」span（非 tag），回填后全部变成 plain tag',
    pending !== null && greyLeft === 0
      && (await page.locator('.el-table__body .el-table__row td:nth-child(4) .el-tag.el-tag--plain').count()) > 0,
    `钉住期间灰色单元格=${pending ?? '(未捕获)'} 回填后灰色残留=${greyLeft}`,
  );

  // ---- 列表接口挂掉：走 apiErrorDetail 的兜底文案 + 空表 ----
  const listRe = /\/api\/rule-sets\?.*/;
  await page.route(listRe, (route) => route.abort());
  clearMessages(page).catch(() => {});
  await go(page, '/logharvest/rulesets', { reload: true, waitText: '新建规则集' });
  const failMsg = await waitMessage(page, /规则集列表加载失败/, 12000).catch(() => null);
  const emptyText = norm(
    await page.locator('.el-table__empty-text').first().innerText().catch(() => ''),
  );
  ledger.check(
    '列表请求失败：ElMessage 原文「规则集列表加载失败」（axios 无 response ⇒ apiErrorDetail 取兜底串）+ 表格空态',
    failMsg === '规则集列表加载失败' && emptyText.includes('暂无数据')
      && (await rowsOf(page.locator('.el-table').first()).count()) === 0,
    `消息=${failMsg ?? '(未捕获)'} 空态=${emptyText || '(无)'}`,
  );
  await page.unroute(listRe);
  await go(page, '/logharvest/rulesets', { reload: true, waitText: '新建规则集' });
  await settleCounts(page);
  const recovered = await rowsOf(page.locator('.el-table').first()).count();
  ledger.check(
    '解除拦截后重新加载即恢复（rows.value=[] 只在 catch 里赋值，没有卡死的错误态）',
    recovered > 0,
    `恢复后本页 ${recovered} 行`,
  );

  // ---- 只读账号：同一抽屉必须换文案、且没有任何写入口 ----
  const op = st.operatorPage ?? await useSession(ledger, st, 'operator');
  await go(op, '/logharvest/rulesets', { reload: true, waitText: '新建规则集' }).catch(() => {});
  await waitFor(async () => (await op.locator('.el-table__body .el-table__row').count()) > 0,
    { label: 'operator 列表出行', timeout: 15000 }).catch(() => {});
  await settleCounts(op);
  const opRow = await findRow(op, RS_NAME, 2);
  ledger.check(
    'operator 行内只有「详情」：无 rulesets:write ⇒「编辑规则」被 v-perms 移出 DOM',
    Boolean(opRow)
      && (await opRow.row.getByRole('button', { name: '编辑规则' }).count()) === 0
      && (await opRow.row.getByRole('button', { name: '详情' }).count()) === 1,
    `找到第 ${opRow?.pageIndex ?? 0} 页 buttons=${JSON.stringify(opRow ? await opRow.row.locator('button').allInnerTexts() : [])}`,
  );
  if (opRow) {
    await opRow.row.getByRole('button', { name: '详情' }).click();
    const odw = op.locator('.el-drawer:visible').last();
    await odw.waitFor({ state: 'visible', timeout: 8000 }).catch(() => {});
    const ofoot = norm(await odw.locator('.el-drawer__footer').innerText().catch(() => ''));
    ledger.check(
      '只读抽屉页脚改成「当前账号只有只读权限，界面不提供任何写入口」',
      ofoot.includes('当前账号只有只读权限，界面不提供任何写入口'),
      ofoot,
    );
    ledger.check(
      '只读抽屉里「去维护页编辑」与「新建规则集」都不在 DOM（v-perms 是 removeChild，不是 disabled）',
      (await odw.getByRole('button', { name: '去维护页编辑' }).count()) === 0
        && (await op.getByRole('button', { name: '新建规则集' }).count()) === 0,
      `按钮=${[...new Set(await bodyButtons(op))].join(',')}`,
    );
    await shot(op, ledger, 'rulesets_drawer_readonly');
    await closeDrawer(op);
  }

  // ---- 窄屏抽屉：size="min(860px, 94vw)" 必须跟着视口收缩 ----
  await page.setViewportSize({ width: 390, height: 844 });
  await go(page, '/logharvest/rulesets', { reload: true, waitText: '新建规则集' });
  await settleCounts(page);
  const row390 = await findRow(page, RS_NAME, 1);
  if (row390) {
    await row390.row.getByRole('button', { name: '详情' }).click();
    const d390 = page.locator('.el-drawer:visible').last();
    await d390.waitFor({ state: 'visible', timeout: 8000 }).catch(() => {});
    const w390 = Math.round((await d390.boundingBox().catch(() => null))?.width ?? 0);
    ledger.check(
      '390 视口下详情抽屉按 94vw 收缩（约 367px），不再按 860px 顶出屏幕',
      w390 >= 340 && w390 <= 400,
      `抽屉宽 ${w390}px`,
    );
    await closeDrawer(page);
  } else {
    ledger.skip('390 视口下详情抽屉宽度', '窄屏翻页后没找到自建行（共享栈数据量大）');
  }
  await page.setViewportSize({ width: 1440, height: 900 });
  await settleCounts(page);

  ledger.skip(
    '观察项：抽屉「创建时间」直接渲染后端原始串（带 T 与微秒），与列表的 formatDateTime 不同形',
    '建议 web/src/views/logharvest/rulesets/edit-drawer.vue 的 created_at 也走 formatDateTime，两处口径一致',
  );
  ledger.skip(
    '观察项：规则集版本只增不减——列表页与维护页都没有删除/停用入口（后端也确实没有 PUT/DELETE），本阶段自建的 ui-ruleset-* 版本行无法清理',
    '建议后端提供 DELETE /api/rule-sets/{id}（或归档标记），列表行给 rulesets:write 账号一个入口',
  );
  await shot(page, ledger, 'rulesets_final');
}

// ---------------------------------------------------------------- U11 规则维护

/**
 * 点「另存为新版本」并等页面自己承认成功。
 * 三条缺一不可：ElMessage「已创建版本 N」+ POST 响应带真实 id + 版本下拉刷成 vN（#该 id）。
 * index.vue 的顺序是 createRuleSet → message → await loadSets() → 选中项回填，
 * 所以只看消息或只看「vN（#数字）」都会读到保存前的旧选中态（实测把模板 #50 认成自建 v1）。
 * 返回 { msg, ver, id, payload, posted }；payload 是监听到的真实 POST 体。
 */
async function saveNewVersion(page, st, version) {
  const posts = st.rulesetPosts ?? [];
  const before = posts.length;
  await clearMessages(page);
  await page.getByRole('button', { name: '另存为新版本' }).click();
  const msg = await waitMessage(page, new RegExp(`已创建版本 ${version}(?!\\d)`), 25000).catch(() => null);
  const post = await waitFor(
    async () => {
      if (posts.length <= before) return null;
      const p = posts[posts.length - 1];
      return p.id > 0 ? p : null;
    },
    { label: `POST /api/rule-sets 响应带回 id`, timeout: 25000 },
  ).catch(() => null);
  const createdId = Number(post?.id ?? 0);
  const pattern = createdId > 0
    ? new RegExp(`^v${version}（#${createdId}）$`)
    : new RegExp(`^v${version}（#\\d+）$`);
  const ver = await waitFor(
    async () => {
      const txt = await boxText(page, versionBox(page));
      return pattern.test(txt) ? txt : null;
    },
    { label: `版本下拉变成 v${version}${createdId ? `（#${createdId}）` : ''}`, timeout: 25000 },
  ).catch(() => null);
  const id = Number((ver?.match(/#(\d+)/) ?? [])[1] ?? createdId);
  // 后端没有 DELETE，页面上另存的每个版本都得进清理清单，收尾按 id 删行。
  if (id > 0 && !st.createdRuleSets.includes(id)) st.createdRuleSets.push(id);
  return {
    msg,
    ver,
    id,
    status: Number(post?.status ?? 0),
    payload: posts.length > before ? posts[posts.length - 1].body : null,
    posted: posts.length > before,
  };
}

async function phase_rules(ledger, st, { PHASE_LABEL }) {
  ledger.phase(PHASE_LABEL.rules);
  const page = st.adminPage ?? await useSession(ledger, st, 'admin');
  const { api } = st;

  // pristine 模板：页面永不改它，只作为「载入既有版本 → 改名另存」的起点（绕开新建入口缺陷）。
  const m = await api.send('POST', '/rule-sets', ruleSetPayload(M_NAME, [M_A, M_B]));
  st.createdRuleSets.push(m.id);
  await trackRuleSetPosts(st, page);

  const boxClass = async (box) => (await box.getAttribute('class').catch(() => '')) ?? '';
  // EP 2.14：el-select 的 is-disabled 落在 .el-select__wrapper 上，根节点 class 仍是 "el-select w-xx"；
  // el-switch 的 is-disabled 则在根节点。读 disabled 一律 root+wrapper 合并，别猜。
  const wrapperClass = async (box) =>
    (await box.locator('.el-select__wrapper').first().getAttribute('class').catch(() => '')) ?? '';
  const isDisabledBox = async (box) => {
    const cls = `${await wrapperClass(box)} ${await boxClass(box)}`;
    return /is-disabled/.test(cls);
  };
  const idInputOf = (row) => row.locator('td').nth(2).locator('input').first();
  const sevCell = (row) => row.locator('td').nth(3);
  const swCell = (row) => row.locator('td').nth(4);
  const modeOf = async (row) => norm(
    await row.locator('td').nth(5).locator('.el-radio-button.is-active .el-radio-button__inner').innerText().catch(() => ''),
  );
  const idsInOrder = async (table) => {
    const out = [];
    const n = await rowsOf(table).count();
    for (let k = 0; k < n; k++) out.push(await idInputOf(rowsOf(table).nth(k)).inputValue().catch(() => ''));
    return out;
  };
  const dividerOf = async () => ((await bodyText(page)).match(/规则（\d+ 条）/))?.[0] ?? '';

  // ---- 空态：等价于列表页「新建规则集」进来（无 rule_set_id / rule_set_name）----
  await go(page, '/rules/maintenance', { reload: true, waitText: '添加规则' });
  const t = visTable(page);
  const saveBtn = page.getByRole('button', { name: '另存为新版本' });

  const tabs = (await page.locator('.el-tabs__item').allInnerTexts()).map(norm);
  const activeTabTxt = norm(await page.locator('.el-tabs__item.is-active').innerText().catch(() => ''));
  ledger.check(
    'Tab 文案照源码：写权限下是「编辑规则」+「跨版本检索」，默认停在编辑规则',
    tabs.join('|') === '编辑规则|跨版本检索' && activeTabTxt === '编辑规则',
    `tabs=${tabs.join('|')} active=${activeTabTxt}`,
  );

  const blankBody = await bodyText(page);
  const blankVerdict = {
    名称placeholder: (await boxText(page, nameBox(page))) === '选择规则集',
    版本placeholder: (await boxText(page, versionBox(page))) === '选择版本',
    版本disabled: await isDisabledBox(versionBox(page)),
    分隔线0条: blankBody.includes('规则（0 条）'),
    表格空态: (await t.locator('.el-table__empty-text').innerText().catch(() => '')).includes('暂无数据'),
    保存提示: blankBody.includes('保存后留在当前编辑态，可继续调整；正则编译等校验以后端 422 为准。'),
    保存按钮1个: (await saveBtn.count()) === 1,
  };
  ledger.check(
    '空态：两个下拉只剩 placeholder、版本下拉 disabled、分隔线「规则（0 条）」、表格空态「暂无数据」+ 保存行提示原文（422 为准）',
    Object.values(blankVerdict).every(Boolean),
    `逐项=${JSON.stringify(blankVerdict)} 版本wrapper=${await wrapperClass(versionBox(page))} 空态原文=${JSON.stringify(await t.locator('.el-table__empty-text').innerText().catch(() => '(无 .el-table__empty-text)'))} 保存按钮=${await saveBtn.count()}`,
  );

  await fieldItem(page, '规则集名称').locator('input').first().fill(BLANK_NAME);
  await page.getByRole('button', { name: '添加规则' }).click();
  await page.waitForTimeout(500);
  const bRow = rowsOf(t).nth(0);
  const bCls = (await bRow.getAttribute('class').catch(() => '')) ?? '';
  const { panel: bPanel } = await expandRow(page, t, 0);
  const bErr = norm(await bPanel.locator('.text-red-600').first().innerText().catch(() => ''));
  ledger.check(
    '「添加规则」出来的空行立即整行标红 + 展开面板红字「规则 id 必填」（设计 §11 逐行标红）',
    (await rowsOf(t).count()) === 1 && /rule-row-error/.test(bCls) && bErr === '规则 id 必填',
    `行 class=${bCls} 面板红字=${bErr || '(无)'}`,
  );

  const panelHadTextarea = (await bPanel.locator('textarea:visible').count()) > 0;
  await idInputOf(bRow).fill('ui-blank');
  await page.waitForTimeout(400);
  // 改 id 会触发 rowClassName 重算，el-table 无 row-key 时展开行会被卸载（实测面板消失）；
  // 展开态不是持久状态，后续要写字段就得重新展开——这本身就是页面行为的一部分。
  const bPanel2 = await livePanel(page, 0);
  await bPanel2.locator('textarea:visible').first().fill('NOOP');
  ledger.skip(
    '观察项：改「规则 id」后展开面板是否被卸载（el-table 未设 row-key ⇒ 整行重建，展开态丢失）',
    `本次实测：改 id 前面板可见 textarea=${panelHadTextarea ? '有' : '无'}；`
      + '改 id 后由 harness 自动重发展开才能继续填「匹配配置」。'
      + '（建议 web/src/views/rules/maintenance/rule-table.vue 给 el-table 配 row-key="id"，'
      + '否则每次编辑 id 都会把用户刚展开的面板关掉）',
  );
  await page.waitForTimeout(400);
  const bCls2 = (await rowsOf(visTable(page)).nth(0).getAttribute('class').catch(() => '')) ?? '';
  const blankDisabled = await saveBtn.isDisabled().catch(() => true);
  ledger.check(
    '[产品缺陷] 名称已填、唯一一行合法（红字已消失）后「另存为新版本」仍 disabled ⇒ 列表页「新建规则集」入口根本无法落库',
    !/rule-row-error/.test(bCls2) && blankDisabled,
    `行 class=${bCls2 || '(不标红)'} disabled=${blankDisabled} 分隔线=${await dividerOf()}（证据：web/src/views/rules/maintenance/index.vue:397 :disabled="!selectedId || !form.rules.length"，而 web/src/views/logharvest/rulesets/index.vue:103 的「新建规则集」调 goMaintenance() 不带 row ⇒ 无 rule_set_id ⇒ selectedId 恒 undefined）`,
  );

  // ---- 深链载入模板 v1，读回 config ----
  await go(page, `/rules/maintenance?rule_set_id=${m.id}`, { reload: true, waitText: '规则（2 条）' });
  const mt = visTable(page);
  const mrs = rowsOf(mt);
  const c1 = await cellsOf(mrs.nth(0));
  const c2 = await cellsOf(mrs.nth(1));
  ledger.check(
    `深链 ?rule_set_id=${m.id} 即选中：两个下拉的当前值 + 表单四项取 config 原文 + 列头 8 列 + 「规则（2 条）」`,
    (await boxText(page, nameBox(page))) === M_NAME
      && (await boxText(page, versionBox(page))) === `v1（#${m.id}）`
      && (await fieldValue(page, '规则集名称')) === M_NAME
      && (await fieldValue(page, '文件通配')) === '**/*'
      && (await fieldValue(page, '编码')) === 'utf-8'
      && (await fieldValue(page, '回退编码')) === 'gb18030'
      && (await headersOf(mt)).join('|') === '#|规则 id|严重级|启用|匹配方式|匹配配置|阈值|操作'
      && (await mrs.count()) === 2,
    `name=${await boxText(page, nameBox(page))} ver=${await boxText(page, versionBox(page))} headers=${(await headersOf(mt)).join('|')} rows=${await mrs.count()}`,
  );

  ledger.check(
    '第 1 行内联态：id 输入框/严重级纯文本/启用 is-checked/选中「关键字」/摘要「任一命中 2 词」/阈值「—」/操作三键',
    (await idInputOf(mrs.nth(0)).inputValue()) === 'ui-m-a'
      && c1[1] === '1' && c1[3] === 'warning'
      && /is-checked/.test(await switchClassOf(swCell(mrs.nth(0))))
      && (await modeOf(mrs.nth(0))) === '关键字'
      && c1[6] === '任一命中 2 词：OMEGA、KAPPA' && c1[7] === '—' && c1[8] === '上移 下移 删除',
    `cells=${JSON.stringify(c1)}`,
  );
  ledger.check(
    '第 2 行内联态：停用（无 is-checked）+ 选中「正则」+ 摘要「/pattern/i · 50ms」+ 阈值「ms gt 1000」',
    (await idInputOf(mrs.nth(1)).inputValue()) === 'ui-m-b'
      && c2[3] === 'error'
      && !/is-checked/.test(await switchClassOf(swCell(mrs.nth(1))))
      && (await modeOf(mrs.nth(1))) === '正则'
      && c2[6] === `/${RE_MS}/i · 50ms` && c2[7] === 'ms gt 1000',
    `cells=${JSON.stringify(c2)}`,
  );

  // ---- 内联编辑：严重级下拉 + 启用开关（这两处改动会带进 N v1）----
  const sevPicked = await pickInBox(page, sevCell(mrs.nth(0)).locator('.el-select').first(), 'info');
  await swCell(mrs.nth(1)).locator('.el-switch').click();
  await page.waitForTimeout(300);
  ledger.check(
    '内联编辑即时生效：严重级下拉选项＝源码 SEVERITIES 三项、改选后单元格变 info；第 2 行启用开关点开 is-checked',
    sevPicked.texts.join('|') === 'info|warning|error'
      && sevPicked.picked === 'info'
      && norm(await sevCell(mrs.nth(0)).innerText()) === 'info'
      && /is-checked/.test(await switchClassOf(swCell(mrs.nth(1))))
      && (await idInputOf(mrs.nth(1)).inputValue()) === 'ui-m-b',
    `选项=${sevPicked.texts.join('|')} 严重级=${norm(await sevCell(mrs.nth(0)).innerText())} 开关 class=${await boxClass(swCell(mrs.nth(1)).locator('.el-switch'))}`,
  );

  // ---- 展开面板：正则行的字段集合与阈值回显 ----
  const { panel: p2 } = await expandRow(page, mt, 1);
  const pLabels = (await p2.locator('.el-form-item__label').allInnerTexts()).map(norm).filter(Boolean);
  const cbClass = async (text) => (await p2.locator('.el-checkbox').filter({ hasText: text }).first().getAttribute('class').catch(() => '')) ?? '';
  const thrItem = fieldItem(page, '数值阈值', p2);
  const thrHint = norm(await thrItem.locator('.hint').innerText().catch(() => ''));
  ledger.check(
    '正则行展开面板：只有正则专属字段（关键字三项不出现）+ 模式原文 + 只勾 i + 超时 50 + 「后端仅允许 i/m/s 三个标志」',
    pLabels.join('|') === '规则 id|展示名|标签|正则模式|标志位|超时（毫秒）|数值阈值'
      && (await p2.locator('textarea').first().inputValue().catch(() => '')) === RE_MS
      && /is-checked/.test(await cbClass('i（忽略大小写）'))
      && !/is-checked/.test(await cbClass('m（多行）'))
      && !/is-checked/.test(await cbClass('s（点匹配换行）'))
      && (await fieldItem(page, '超时（毫秒）', p2).locator('input').first().inputValue().catch(() => '')) === '50'
      && (await bodyHas(page, '后端仅允许 i/m/s 三个标志')) !== null,
    `字段=${pLabels.join('|')}`,
  );
  ledger.check(
    '阈值区回显 ms / gt / 1000（开关已开）+ 提示原文含「422 原文回显」与「关键字规则不可启用阈值」',
    /is-checked/.test(await switchClassOf(thrItem))
      && (await thrItem.getByPlaceholder('字段（正则命名分组）').inputValue().catch(() => '')) === 'ms'
      && norm(await thrItem.locator('.el-select').first().innerText().catch(() => '')) === 'gt'
      && (await thrItem.getByPlaceholder('比较值（数值）').inputValue().catch(() => '')) === '1000'
      && thrHint.includes('422 原文回显') && thrHint.includes('关键字规则不可启用阈值'),
    `field=${await thrItem.getByPlaceholder('字段（正则命名分组）').inputValue().catch(() => '')} op=${norm(await thrItem.locator('.el-select').first().innerText().catch(() => ''))} value=${await thrItem.getByPlaceholder('比较值（数值）').inputValue().catch(() => '')} 提示=${thrHint}`,
  );

  // ---- 必填即时红字（前端只拦得了必填）----
  const patTa = p2.locator('textarea').first();
  await patTa.fill('');
  await page.waitForTimeout(400);
  const emptyCls = (await rowsOf(visTable(page)).nth(1).getAttribute('class').catch(() => '')) ?? '';
  // 清空后 rowHint 出现 ⇒ rowClassName 变化 ⇒ 行重建，旧 p2 可能已脱离 DOM，重新解析再读红字。
  const panelStillMounted = (await p2.count()) > 0;
  const p2v = await livePanel(page, 1);
  const patErr = norm(await p2v.locator('.text-red-600').first().innerText().catch(() => ''));
  ledger.check(
    '清空正则模式即时整行标红 + 面板红字「正则模式不能为空」（必填由前端拦）',
    /rule-row-error/.test(emptyCls) && patErr === '正则模式不能为空',
    `行 class=${emptyCls || '(无)'} 红字=${patErr || '(无)'} 旧面板是否还在=${panelStillMounted ? '在（无需重展开）' : '已被卸载（el-table 无 row-key ⇒ 整行重建）'}`,
  );
  /** 后端/前端校验的红色告警：`<el-alert type="error"><pre>{{ backendError }}</pre></el-alert>`。 */
  const errAlert = async () =>
    norm(await page.locator('.el-alert:visible pre').first().innerText().catch(() => ''));

  // ---- 填回原正则：行红字消失（前端只拦必填，不去猜正则语法）----
  await p2v.locator('textarea').first().fill(RE_MS);
  await page.waitForTimeout(400);
  const okCls = (await rowsOf(visTable(page)).nth(1).getAttribute('class').catch(() => '')) ?? '';
  ledger.check(
    '填回原正则后整行不再标红（rowError 只管必填/超时/阈值语义，正则语法交给后端）',
    !/rule-row-error/.test(okCls),
    `行 class=${okCls || '(不标红)'}`,
  );

  // ---- 关键字规则打开数值阈值：行级红字 + validateRules 拦下保存（根本不发请求）----
  const postsBase = (st.rulesetPosts ?? []).length;
  const thrFieldsVisible = async (panel) =>
    (await panel.locator('input[placeholder="字段（正则命名分组）"]').count()) > 0;
  const { panel: p1 } = await expandRow(page, mt, 0);
  const kwThrSwitch = fieldItem(page, '数值阈值', p1).locator('.el-switch').first();
  await kwThrSwitch.click();
  await page.waitForTimeout(400);
  // 开关一点 rowError 就出现 ⇒ 行重建 ⇒ 面板/开关可能全部脱旧，重新解析后再读再点。
  const p1b = await livePanel(page, 0, 'input[placeholder="字段（正则命名分组）"]');
  const kwThrSwitch2 = fieldItem(page, '数值阈值', p1b).locator('.el-switch').first();
  const kwErr = norm(await p1b.locator('.text-red-600').first().innerText().catch(() => ''));
  const kwCls = (await rowsOf(visTable(page)).nth(0).getAttribute('class').catch(() => '')) ?? '';
  ledger.check(
    '关键字规则开「数值阈值」：阈值三项出现 + 面板红字原文「阈值依赖正则命名分组，关键字规则没有可提取字段」+ 整行标红',
    kwErr === '阈值依赖正则命名分组，关键字规则没有可提取字段'
      && /rule-row-error/.test(kwCls)
      && (await thrFieldsVisible(p1b)),
    `红字=${kwErr || '(无)'} 行 class=${kwCls || '(无)'}`,
  );
  await clearMessages(page);
  await saveBtn.click();
  await page.waitForTimeout(900);
  const kwAlert = await errAlert();
  ledger.check(
    'validateRules 拦在本地：告警原文「规则 ui-m-a：阈值依赖…」（带行 id 前缀），POST /api/rule-sets 一次也没发出去',
    kwAlert === '规则 ui-m-a：阈值依赖正则命名分组，关键字规则没有可提取字段'
      && (st.rulesetPosts ?? []).length === postsBase
      && !(await messagesOf(page)).some((t) => /已创建版本/.test(t)),
    `告警=${kwAlert || '(无)'} POST 增量=${(st.rulesetPosts ?? []).length - postsBase} 消息=${JSON.stringify(await messagesOf(page))}`,
  );
  await kwThrSwitch2.click();
  await page.waitForTimeout(300);

  // ---- 规则 id 重复：整表校验失败，但 rowError 是逐行的 ⇒ 两行都不标红 ----
  await idInputOf(rowsOf(mt).nth(1)).fill('ui-m-a');
  await page.waitForTimeout(400);
  const dupCls = (await rowsOf(visTable(page)).nth(1).getAttribute('class').catch(() => '')) ?? '';
  await clearMessages(page);
  await saveBtn.click();
  await page.waitForTimeout(900);
  const dupAlert = await errAlert();
  ledger.check(
    '重复规则 id：告警原文「规则 id 不能重复」，行本身不标红（全表唯一性不落行），依旧不发请求',
    dupAlert === '规则 id 不能重复' && !/rule-row-error/.test(dupCls)
      && (st.rulesetPosts ?? []).length === postsBase,
    `告警=${dupAlert || '(无)'} 行 class=${dupCls || '(无)'} POST 增量=${(st.rulesetPosts ?? []).length - postsBase}`,
  );
  await idInputOf(rowsOf(mt).nth(1)).fill('ui-m-b');
  await page.waitForTimeout(300);

  // ---- 后端 422：先用接口拿到逐字原文，再让页面复现同一句（不猜文案）----
  const badProbe = await api.call('POST', '/rule-sets',
    ruleSetPayload(N_NAME, [{ ...M_B, enabled: true, match: { ...M_B.match, pattern: RE_BAD } }]));
  const detail422 = norm(typeof badProbe.body?.detail === 'string' ? badProbe.body.detail : '');
  ledger.check(
    '前置事实：非法正则的 POST /rule-sets 由后端回 422，detail 是纯字符串（编译校验先于 pathspec/codec）',
    badProbe.status === 422 && detail422.includes('invalid regex pattern'),
    `status=${badProbe.status} body=${JSON.stringify(badProbe.body).slice(0, 300)}`,
  );
  const { panel: p2b } = await expandRow(page, mt, 1);
  await p2b.locator('textarea').first().fill(RE_BAD);
  await page.waitForTimeout(400);
  await clearMessages(page);
  await saveBtn.click();
  await page.waitForTimeout(1600);
  const alert422 = await errAlert();
  const postedBad = (st.rulesetPosts ?? []).length > postsBase;
  ledger.check(
    '正则语法前端不拦：请求真的发给后端了，页面 <pre> 告警逐字复现 422 detail 原文',
    postedBad && alert422 === detail422 && detail422.startsWith("rule 'ui-m-b': invalid regex pattern"),
    `页面告警=${alert422 || '(无)'} 接口原文=${detail422} 发出请求=${postedBad}`,
  );
  ledger.check(
    '422 之后不产生半版本：名称/版本下拉仍停在模板 v1，且没有「已创建版本」成功消息',
    (await boxText(page, nameBox(page))) === M_NAME
      && (await boxText(page, versionBox(page))) === `v1（#${m.id}）`
      && !(await messagesOf(page)).some((t) => /已创建版本/.test(t)),
    `name=${await boxText(page, nameBox(page))} ver=${await boxText(page, versionBox(page))} 消息=${JSON.stringify(await messagesOf(page))}`,
  );
  // 保存失败后 el-alert 插到表格上方，p2b 可能已被卸载 ⇒ 填回前重新解析面板
  const p2c = await livePanel(page, 1);
  await p2c.locator('textarea').first().fill(RE_MS);
  await page.waitForTimeout(400);
  await collapseAll(page, mt);
  await shot(page, ledger, 'rules_validate_422');

  // ---- 改名另存为新版本：v1 的 POST 体完全由页面组装 ----
  await fieldItem(page, '规则集名称').locator('input').first().fill(N_NAME);
  const s1 = await saveNewVersion(page, st, 1);
  const pl1 = s1.payload ?? {};
  ledger.check(
    '改名后「另存为新版本」：ElMessage「已创建版本 1」+ 版本下拉变 v1（#新 id）+ 名称下拉切到新名字',
    s1.msg === '已创建版本 1' && /^v1（#\d+）$/.test(s1.ver ?? '')
      && (await boxText(page, nameBox(page))) === N_NAME,
    `消息=${s1.msg ?? '(无)'} 版本框=${s1.ver ?? '(无)'} name=${await boxText(page, nameBox(page))}`,
  );
  ledger.check(
    '页面真实 POST 体：name 与 config.name 同为 trim 后的表单值，rules 带进内联编辑（ui-m-a severity=info、ui-m-b enabled=true、threshold 仍是字符串 "1000"）',
    pl1.name === N_NAME && pl1.config?.name === N_NAME
      && pl1.config?.file_glob === '**/*' && pl1.config?.encoding === 'utf-8'
      && pl1.config?.fallback_encoding === 'gb18030'
      && pl1.config?.rules?.length === 2
      && pl1.config.rules[0].id === 'ui-m-a' && pl1.config.rules[0].severity === 'info'
      && pl1.config.rules[0].threshold === null
      && pl1.config.rules[1].id === 'ui-m-b' && pl1.config.rules[1].enabled === true
      && pl1.config.rules[1].match.pattern === RE_MS && pl1.config.rules[1].match.flags === 'i'
      && pl1.config.rules[1].threshold?.value === '1000',
    JSON.stringify(pl1).slice(0, 480),
  );

  // ---- 新增行：newRule() 的默认值直接可见 ----
  await page.getByRole('button', { name: '添加规则' }).click();
  await page.waitForTimeout(600);
  const nr = rowsOf(mt).nth(2);
  const nCells = await cellsOf(nr);
  ledger.check(
    '「添加规则」追加 newRule() 默认行：id 空（整行标红）、严重级 info、启用 on、匹配方式默认「关键字」、摘要「任一命中 0 词：（空）」、阈值「—」、超时 50',
    (await rowsOf(mt).count()) === 3
      && (await idInputOf(nr).inputValue()) === ''
      && /rule-row-error/.test((await nr.getAttribute('class').catch(() => '')) ?? '')
      && nCells[1] === '3' && nCells[3] === 'info'
      && /is-checked/.test(await switchClassOf(swCell(nr)))
      && (await modeOf(nr)) === '关键字'
      && nCells[6] === '任一命中 0 词：（空）' && nCells[7] === '—',
    `cells=${JSON.stringify(nCells)}`,
  );
  const up0 = rowsOf(mt).nth(0).locator('td').nth(8).getByRole('button', { name: '上移' });
  const down2 = nr.locator('td').nth(8).getByRole('button', { name: '下移' });
  ledger.check(
    '行内排序按钮的边界：首行「上移」disabled、末行「下移」disabled（onMoveRule 越界直接 return）',
    (await up0.isDisabled()) && (await down2.isDisabled()),
    `首行上移 disabled=${await up0.isDisabled()}，末行下移 disabled=${await down2.isDisabled()}`,
  );

  // ---- 填第 3 行：all 侧关键词 + 区分大小写 + 多标签 + 严重级 warning ----
  const { panel: p3a } = await expandRow(page, mt, 2);
  await p3a.locator('input').first().fill('ui-m-c');
  await page.waitForTimeout(500);
  // 实测（探针复现）：改任意字段 el-table 都会重建行 ⇒ 展开面板被关掉（缺 row-key），
  // 所以每个字段写之前都得重新展开；「展示名」这类不影响校验的字段同样会把面板关掉。
  const p3 = await livePanel(page, 2);
  await fieldItem(page, '展示名', p3).locator('input').first().fill('m 全部命中规则');
  await page.waitForTimeout(450);
  const panelAfterHarmlessEdit = (await visTable(page).locator('.el-table__expanded-cell:visible').count()) > 0;
  ledger.check(
    '[产品缺陷] 编辑「展示名」（不影响任何校验）后展开面板同样被关掉 ⇒ 用户无法连续填完一行的多个字段',
    panelAfterHarmlessEdit === false,
    `改展示名后可见展开面板=${panelAfterHarmlessEdit ? '还在' : '已消失（必须重新点展开箭头）'}`,
  );
  await fieldItem(page, '标签', await livePanel(page, 2)).locator('input').first().fill('ui, thr');
  await pickInBox(page, sevCell(nr).locator('.el-select').first(), 'warning');
  await (await livePanel(page, 2)).locator('.el-radio').filter({ hasText: '全部命中（all）' }).click();
  await (await livePanel(page, 2)).locator('textarea').first().fill('ALPHA\nBETA\nGAMMA');
  await page.waitForTimeout(500);
  // newRule() 的 caseSensitive 默认 false，POST 体断言要 case_sensitive=true ⇒ 必须真点开关
  //（点开关同样会关掉面板，所以之后再读面板一律走 livePanel）。
  const kwCaseSwitch = async () =>
    (await livePanel(page, 2, '.el-form-item:has(.el-form-item__label:text-is("区分大小写")) .el-switch'))
      .locator('.el-switch').first();
  const caseSwitch = await kwCaseSwitch();
  await caseSwitch.click();
  await page.waitForTimeout(450);
  const caseClassAfter = await boxClass(await kwCaseSwitch());
  ledger.check(
    '「区分大小写」开关可点且落到 is-checked（关键字行第三个布尔字段，与 any/all 互斥彼此独立）',
    /is-checked/.test(caseClassAfter),
    `class=${caseClassAfter || '(读不到)'}`,
  );
  const nCells2 = await cellsOf(rowsOf(mt).nth(2));
  ledger.check(
    '关键词写进 all 侧后摘要即时改「全部命中 3 词：ALPHA、BETA、GAMMA」，红字消失，标签逗号摊平',
    nCells2[6] === '全部命中 3 词：ALPHA、BETA、GAMMA'
      && nCells2[3] === 'warning'
      && !/rule-row-error/.test((await rowsOf(mt).nth(2).getAttribute('class').catch(() => '')) ?? ''),
    `cells=${JSON.stringify(nCells2)}`,
  );
  const p3kw = await livePanel(page, 2, '.el-form-item:has(.el-form-item__label:text-is("关键词"))');
  const kwLabels = (await p3kw.locator('.el-form-item__label').allInnerTexts()).map(norm).filter(Boolean);
  await rowsOf(mt).nth(2).locator('td').nth(5).locator('.el-radio-button').filter({ hasText: '正则' }).click();
  await page.waitForTimeout(500);
  const p3rx = await livePanel(page, 2, '.el-form-item:has(.el-form-item__label:text-is("正则模式"))');
  const rxLabels = (await p3rx.locator('.el-form-item__label').allInnerTexts()).map(norm).filter(Boolean);
  ledger.check(
    '匹配方式单选切换即换字段集：关键字面板有「关键词/区分大小写」，切到正则后只剩「正则模式/标志位/超时」',
    kwLabels.includes('关键词') && kwLabels.includes('区分大小写') && !kwLabels.includes('正则模式')
      && rxLabels.includes('正则模式') && rxLabels.includes('标志位') && !rxLabels.includes('关键词'),
    `关键字=${kwLabels.join('|')} 正则=${rxLabels.join('|')}`,
  );
  await rowsOf(mt).nth(2).locator('td').nth(5).locator('.el-radio-button').filter({ hasText: '关键字' }).click();
  await page.waitForTimeout(400);

  // ---- 上移新行，再另存 v2 ----
  await collapseAll(page, mt);
  await rowsOf(mt).nth(2).locator('td').nth(8).getByRole('button', { name: '上移' }).click();
  await page.waitForTimeout(500);
  const order2 = await idsInOrder(mt);
  ledger.check(
    '「上移」把第 3 行换到第 2 位，序号列跟着重排（1/2/3），行数据不丢',
    order2.join('|') === 'ui-m-a|ui-m-c|ui-m-b'
      && (await cellsOf(rowsOf(mt).nth(1)))[1] === '2'
      && (await cellsOf(rowsOf(mt).nth(2)))[1] === '3',
    `顺序=${order2.join('|')}`,
  );
  const s2 = await saveNewVersion(page, st, 2);
  const pl2 = s2.payload ?? {};
  ledger.check(
    "另存 v2：「已创建版本 2」+ 版本下拉 v2（#新 id）+ POST 体 3 条规则按页面顺序（all=[ALPHA,BETA,GAMMA]、case_sensitive=true、tags 摊成 [ui,thr]）",
    s2.msg === '已创建版本 2' && /^v2（#\d+）$/.test(s2.ver ?? '')
      && pl2.config?.rules?.length === 3
      && (pl2.config.rules[1]?.id ?? '') === 'ui-m-c'
      && JSON.stringify(pl2.config.rules[1]?.match?.all) === JSON.stringify(['ALPHA', 'BETA', 'GAMMA'])
      && pl2.config.rules[1]?.match?.any?.length === 0
      && pl2.config.rules[1]?.match?.case_sensitive === true
      && JSON.stringify(pl2.config.rules[1]?.tags) === JSON.stringify(['ui', 'thr'])
      && pl2.config.rules[1]?.severity === 'warning',
    `消息=${s2.msg ?? '(无)'} 版本框=${s2.ver ?? '(无)'} rules=${JSON.stringify((pl2.config?.rules ?? []).map((r) => r.id))} r1=${JSON.stringify(pl2.config?.rules?.[1] ?? null).slice(0, 320)}`,
  );

  // ---- 删除规则：ElMessageBox 确认框文案逐字 + 取消不删、确认才删（本地态，未落库）----
  const delBtn = rowsOf(mt).nth(0).locator('td').nth(8).getByRole('button', { name: '删除' });
  await delBtn.click();
  const mb = await readMessageBox(page);
  ledger.check(
    '「删除」弹确认框：标题「删除规则」、正文带全角引号与「保存新版本后才会落库。」、按钮顺序取消/删除',
    mb.title === '删除规则'
      && mb.body === '确认删除规则“ui-m-a”？保存新版本后才会落库。'
      && mb.buttons.join('|') === '取消|删除',
    `title=${mb.title} body=${mb.body} buttons=${mb.buttons.join('|')}`,
  );
  await clickMessageBoxButton(page, '取消');
  ledger.check(
    '取消后行数不变（emit("remove") 只在 confirm resolve 后触发）',
    (await rowsOf(mt).count()) === 3 && (await dividerOf()) === '规则（3 条）',
    `行=${await rowsOf(mt).count()} 分隔线=${await dividerOf()}`,
  );
  await delBtn.click();
  await clickMessageBoxButton(page, '删除');
  await page.waitForTimeout(500);
  const order3 = await idsInOrder(mt);
  ledger.check(
    '确认后首行消失、序号重排为 1/2，分隔线同步「规则（2 条）」',
    order3.join('|') === 'ui-m-c|ui-m-b' && (await dividerOf()) === '规则（2 条）',
    `顺序=${order3.join('|')} 分隔线=${await dividerOf()}`,
  );
  const s3 = await saveNewVersion(page, st, 3);
  ledger.check(
    '另存 v3：「已创建版本 3」+ POST 体只剩删除后的 2 条',
    s3.msg === '已创建版本 3' && /^v3（#\d+）$/.test(s3.ver ?? '')
      && (s3.payload?.config?.rules ?? []).map((r) => r.id).join('|') === 'ui-m-c|ui-m-b',
    `版本框=${s3.ver ?? '(无)'} rules=${JSON.stringify((s3.payload?.config?.rules ?? []).map((r) => r.id))}`,
  );

  // ---- 版本下拉：同名多版本倒序共存，模板规则集一个版本都没多 ----
  const readyVersionBox = async () => {
    await waitFor(
      async () => ((await isDisabledBox(versionBox(page))) ? null : true),
      { label: '版本下拉解除 loadingDetail', timeout: 12000 },
    ).catch(() => {});
  };
  await readyVersionBox();
  const vOptsN = await openBoxOptions(page, versionBox(page));
  ledger.check(
    '当前名称下版本下拉倒序列出 v3/v2/v1（三个独立 id ⇒ 另存是同名 max+1，不覆盖）',
    vOptsN.join('|') === `v3（#${s3.id}）|v2（#${s2.id}）|v1（#${s1.id}）`,
    vOptsN.join('|'),
  );
  await pickInBox(page, nameBox(page), M_NAME, { exact: true });
  await page.waitForTimeout(800);
  await readyVersionBox();
  const vOptsM = await openBoxOptions(page, versionBox(page));
  ledger.check(
    '切回模板规则集：版本下拉仍只有 v1（#原 id）⇒ 页面的另存从不污染来源版本',
    vOptsM.join('|') === `v1（#${m.id}）`,
    vOptsM.join('|'),
  );
  await pickInBox(page, nameBox(page), N_NAME, { exact: true });
  await page.waitForTimeout(800);
  await shot(page, ledger, 'rules_versions');

  // ---- 跨版本检索 Tab：真实 GET /api/rules 的窗口式检索 ----
  await page.locator('.el-tabs__item').filter({ hasText: '跨版本检索' }).first().click();
  await page.waitForTimeout(700);
  const stTable = visTable(page);
  const stRows = rowsOf(stTable);
  const visBtn = (name) => page.locator('button:visible').filter({ hasText: name }).first();
  const sPrev = visBtn('上一页');
  const sNext = visBtn('下一页');
  const sFootnote = async () => norm(
    (await bodyText(page)).match(/后端列表接口不返回总数[^；]*；第[^页]*页，本页[^条]*条/)?.[0]
      ?? (await bodyText(page)).match(/跨版本检索是后端查询期解析[^；]*/)?.[0] ?? '',
  );

  const sHeaders = await headersOf(stTable);
  ledger.check(
    '检索 Tab 激活后：未检索时脚注原文 + 7 列表头 + 空表 + 上一页/下一页都 disabled',
    (await bodyHas(page, '跨版本检索是后端查询期解析 config，未执行检索')) !== null
      && sHeaders.join('|') === '规则集|版本|规则 ID|类型|严重级|摘要|操作'
      && (await stRows.count()) === 0
      && (await sPrev.isDisabled()) && (await sNext.isDisabled()),
    `headers=${sHeaders.join('|')} rows=${await stRows.count()} 上一页 disabled=${await sPrev.isDisabled().catch(() => 'n/a')} 下一页 disabled=${await sNext.isDisabled().catch(() => 'n/a')}`,
  );

  const reqsBeforeSearch = (await recentRequests(page, /\/api\/rules/)).length;
  await page.getByPlaceholder('规则集名称').fill(N_NAME);
  await page.getByRole('button', { name: '检索' }).click();
  await waitFor(async () => (await stRows.count()) === 7, { label: '检索出 7 条索引行', timeout: 20000 }).catch(() => {});
  const rulesReqs = await recentRequests(page, /\/api\/rules/);
  const lastSearch = rulesReqs[rulesReqs.length - 1];
  ledger.check(
    '点「检索」真的发 GET /api/rules?rule_set_name=…&limit=20&offset=0（窗口参数，无 total）',
    rulesReqs.length > reqsBeforeSearch && lastSearch.method === 'GET'
      && /rule_set_name=/.test(lastSearch.url) && /limit=20/.test(lastSearch.url) && /offset=0/.test(lastSearch.url),
    `请求增量=${rulesReqs.length - reqsBeforeSearch} url=${(lastSearch?.url ?? '').slice(0, 260)}`,
  );

  const sCells = [];
  for (let k = 0; k < await stRows.count(); k++) sCells.push(await cellsOf(stRows.nth(k)));
  // 「版本」列渲染的是裸数字（后端 version 整数），不带 v 前缀——实测 7 行首列全是名称、次列 3/2/1。
  ledger.check(
    '结果按「名称升序 + 版本降序 + rule_id 升序」排：v3(ui-m-b,ui-m-c) → v2(a,b,c) → v1(a,b)，共 7 行',
    sCells.map((c) => `${c[1]}/${c[2]}`).join(' ')
      === '3/ui-m-b 3/ui-m-c 2/ui-m-a 2/ui-m-b 2/ui-m-c 1/ui-m-a 1/ui-m-b'
      && sCells.every((c) => c[0] === N_NAME)
      && (await sFootnote()) === '后端列表接口不返回总数；第 1 页，本页 7 条',
    `顺序=${sCells.map((c) => `${c[1]}/${c[2]}`).join(' ')} 脚注=${await sFootnote()}`,
  );
  ledger.check(
    '摘要照后端 _rule_summary：正则行「正则: pattern · 阈值 ms gt 1000」，关键字行「关键字: 前 3 词逗号拼接」',
    sCells[0][5] === `正则: ${RE_MS} · 阈值 ms gt 1000`
      && sCells[1][5] === '关键字: ALPHA, BETA, GAMMA'
      && sCells[2][5] === '关键字: OMEGA, KAPPA',
    `摘要=${JSON.stringify(sCells.map((c) => c[5]))}`,
  );
  const sevTagAt = async (k) => tagClassOf(stRows.nth(k).locator('td').nth(4));
  const typeTagCls0 = await tagClassOf(stRows.nth(0).locator('td').nth(3));
  const sevCls = [];
  for (let k = 0; k < 3; k++) sevCls.push(`${sCells[k][4]}=${await sevTagAt(k)}`);
  ledger.check(
    '类型列是 plain tag、严重级列走 severityTagType（v3/ui-m-b error→danger、v3/ui-m-c warning→warning、v2/ui-m-a 页面改选后的 info→info）',
    /el-tag--plain/.test(typeTagCls0)
      && sCells[0][3] === 'regex' && sCells[1][3] === 'keyword'
      && sCells[0][4] === 'error' && sCells[1][4] === 'warning' && sCells[2][4] === 'info'
      && /el-tag--danger/.test(sevCls[0])
      && /el-tag--warning/.test(sevCls[1])
      && /el-tag--info/.test(sevCls[2]),
    `类型 class=${typeTagCls0} 级=${sevCls.join(' ')}`,
  );
  ledger.check(
    '窗口语义：7 行 < limit=20 ⇒「下一页」disabled，「上一页」offset=0 时 disabled',
    (await sNext.isDisabled()) && (await sPrev.isDisabled()),
    `下一页 disabled=${await sNext.isDisabled()}，上一页 disabled=${await sPrev.isDisabled()}`,
  );
  await shot(page, ledger, 'rules_search_index');

  // ---- 关键字大小写折叠 + 命中范围（后端在 id/名称/标签/关键词或 pattern 原文上做子串）----
  const kwBox = page.getByPlaceholder('规则 ID / 正则 / 关键词');
  const searchBtn = page.getByRole('button', { name: '检索' });
  await kwBox.fill('omega');
  await searchBtn.click();
  await waitFor(async () => (await stRows.count()) === 2, { label: 'keyword=omega 出 2 行', timeout: 20000 }).catch(() => {});
  const lowerUrl = ((await recentRequests(page, /keyword=omega(&|$)/)).slice(-1)[0] ?? {}).url ?? '';
  const lowerCells = await cellsOf(stRows.nth(0));
  await kwBox.fill('OMEGA');
  await searchBtn.click();
  await waitFor(async () => ((await recentRequests(page, /keyword=OMEGA(&|$)/)).length > 0), { label: 'keyword=OMEGA 请求发出', timeout: 15000 }).catch(() => {});
  await waitFor(async () => (await stRows.count()) === 2, { label: 'keyword=OMEGA 出 2 行', timeout: 20000 }).catch(() => {});
  const upperCells = await cellsOf(stRows.nth(0));
  ledger.check(
    'keyword 子串是大小写折叠的：omega 与 OMEGA 都只命中含 OMEGA 关键字的 ui-m-a（v1、v2 两个版本 = 2 行）',
    /keyword=omega/.test(lowerUrl) && lowerCells[2] === 'ui-m-a' && upperCells[2] === 'ui-m-a'
      && (await stRows.count()) === 2,
    `小写请求=${lowerUrl.slice(0, 200)} 首行=${JSON.stringify(lowerCells)} 大写首行=${JSON.stringify(upperCells)}`,
  );

  // ---- 查不到：空表 + 「本页 0 条」+ 下一页 disabled（窗口语义没有 total 也照样成立）----
  await kwBox.fill('zz-绝对查不到的词');
  await searchBtn.click();
  await waitFor(async () => (await stRows.count()) === 0, { label: '无结果清空', timeout: 20000 }).catch(() => {});
  await page.waitForTimeout(500);
  ledger.check(
    '零命中脚注「第 1 页，本页 0 条」+ 空态「暂无数据」+ 下一页 disabled',
    (await sFootnote()) === '后端列表接口不返回总数；第 1 页，本页 0 条'
      && (await sNext.isDisabled())
      && (await visTable(page).locator('.el-table__empty-text').innerText().catch(() => '')).includes('暂无数据'),
    `脚注=${await sFootnote()} 下一页 disabled=${await sNext.isDisabled()}`,
  );

  // ---- type 过滤：命中规则内容而非列 ⇒ 展平后精确匹配 ----
  await kwBox.fill('');
  await pickInBox(page, page.locator('.el-select.w-28:visible').first(), 'regex', { exact: true });
  await searchBtn.click();
  await waitFor(async () => (await stRows.count()) === 3, { label: 'type=regex 出 3 行', timeout: 20000 }).catch(() => {});
  const typeUrl = ((await recentRequests(page, /type=regex/)).slice(-1)[0] ?? {}).url ?? '';
  const regexSeverities = [];
  for (let k = 0; k < await stRows.count(); k++) regexSeverities.push((await cellsOf(stRows.nth(k)))[4]);
  ledger.check(
    '类型下拉只有关键字/正则两项，勾选后请求带 type=regex 且 3 个版本各回一行正则规则（严重级都是 error）',
    /type=regex/.test(typeUrl) && (await stRows.count()) === 3
      && regexSeverities.join('|') === 'error|error|error',
    `url=${typeUrl.slice(0, 200)} 严重级=${regexSeverities.join('|')}`,
  );

  // ---- 窄屏裁列：searchColumns 在 mobile 下丢掉「版本」与「摘要」----
  await page.setViewportSize({ width: 390, height: 844 });
  const sMobile = await waitFor(async () => {
    const h = await headersOf(visTable(page));
    return h.join('|') === '规则集|规则 ID|类型|严重级|操作' ? h.join('|') : null;
  }, { label: '检索表 390 裁列', timeout: 12000 }).catch(() => null);
  await page.setViewportSize({ width: 1440, height: 900 });
  const sDesktop = await waitFor(async () => {
    const h = await headersOf(visTable(page));
    return h.length === 7 ? h.join('|') : null;
  }, { label: '检索表恢复 7 列', timeout: 12000 }).catch(() => null);
  ledger.check(
    '390 窄屏检索表裁掉「版本/摘要」保住定位列，回 1440 后 7 列恢复',
    Boolean(sMobile) && Boolean(sDesktop),
    `390=${sMobile ?? '(未生效)'} 1440=${sDesktop ?? '(未恢复)'}`,
  );

  // ---- 「去编辑」：同页切 Tab 直接加载该行版本，但不改 URL ----
  const goEdit = stRows.nth(0).locator('td').nth(6).getByRole('button', { name: '去编辑' });
  const urlBeforeGoEdit = page.url();
  await goEdit.click();
  await page.waitForTimeout(900);
  ledger.check(
    '「去编辑」切回编辑 Tab 并加载 v3（2 条规则，顺序 ui-m-c|ui-m-b：删除的是首行 ui-m-a）',
    norm(await page.locator('.el-tabs__item.is-active').innerText()) === '编辑规则'
      && (await boxText(page, versionBox(page))) === `v3（#${s3.id}）`
      && (await dividerOf()) === '规则（2 条）'
      && (await idsInOrder(visTable(page))).join('|') === 'ui-m-c|ui-m-b',
    `Tab=${norm(await page.locator('.el-tabs__item.is-active').innerText())} ver=${await boxText(page, versionBox(page))} 分隔线=${await dividerOf()} 顺序=${(await idsInOrder(visTable(page))).join('|')}`,
  );
  const qBefore = new URL(page.url()).hash.split('?')[1] ?? '';
  ledger.check(
    '「去编辑」后地址栏仍是进来的深链（rule_set_id 指向模板）：URL 与编辑态脱节',
    new URL(page.url()).hash.split('?')[1] === qBefore && qBefore.includes(`rule_set_id=${m.id}`)
      && !new RegExp(`rule_set_id=${s3.id}\\b`).test(qBefore),
    `hash query=${qBefore || '(无)'} url=${page.url()}`,
  );
  ledger.skip(
    '观察项：「去编辑」只做 Tab 切换 + loadVersion，不改 URL —— 此时刷新页面会退回深链里的旧版本',
    '建议 goToEdit 里 router.replace({ query: { rule_set_id, version } })，让地址栏与编辑态一致',
  );

  // ---- 无 rulesets:write 的 operator：整页只读（v-perms/`:disabled` 的真实表现）----
  const op = st.operatorPage ?? await useSession(ledger, st, 'operator');
  await go(op, `/rules/maintenance?rule_set_id=${s2.id}`, { reload: true, waitText: '规则（3 条）' });
  const opTable = visTable(op);
  const opBtns = [...new Set(await bodyButtons(op))];
  const opTabs = (await op.locator('.el-tabs__item').allInnerTexts()).map(norm);
  const opName = op.locator('.w-56 .el-select').first();
  const opVer = op.locator('.w-44 .el-select').first();
  const opDivider = async () => ((await bodyText(op)).match(/规则（\d+ 条）/))?.[0] ?? '';
  ledger.check(
    'operator 的 Tab 首项变成「查看规则」，名称下拉 disabled、版本下拉仍可切换（源码 :disabled="!selectedName || loadingDetail" 不含 canWrite ⇒ 只读账号被允许跨版本浏览），规则表没有「操作」列（v-if="!readonly" 直接不渲染）',
    opTabs.join('|') === '查看规则|跨版本检索'
      && (await isDisabledBox(opName))
      && !(await isDisabledBox(opVer))
      && (await headersOf(opTable)).join('|') === '#|规则 id|严重级|启用|匹配方式|匹配配置|阈值',
    `tabs=${opTabs.join('|')} 名称 wrapper=${await wrapperClass(opName)} 版本 wrapper=${await wrapperClass(opVer)} headers=${(await headersOf(opTable)).join('|')}`,
  );
  ledger.check(
    '写入口全部不在 DOM：无「添加规则」「另存为新版本」「上移/下移/删除」（不是 disabled，是 removeChild/v-if）',
    !opBtns.some((t) => /添加规则|另存为新版本|上移|下移|^删除$/.test(t)),
    opBtns.join(','),
  );
  ledger.check(
    'el-form :disabled="!canWrite" 摊到每个控件：规则集名称输入框与行内 id/严重级/启用全部 disabled，但数据照读（3 条、顺序 ui-m-a|ui-m-c|ui-m-b）',
    (await fieldItem(op, '规则集名称').locator('input').first().isDisabled().catch(() => false))
      && (await idInputOf(rowsOf(opTable).nth(0)).isDisabled().catch(() => false))
      && (await isDisabledBox(rowsOf(opTable).nth(0).locator('td').nth(3).locator('.el-select').first()))
      && (await isDisabledBox(rowsOf(opTable).nth(0).locator('td').nth(4).locator('.el-switch').first()))
      && (await idsInOrder(opTable)).join('|') === 'ui-m-a|ui-m-c|ui-m-b',
    `名称 disabled=${await fieldItem(op, '规则集名称').locator('input').first().isDisabled().catch(() => 'n/a')} 顺序=${(await idsInOrder(opTable)).join('|')}`,
  );
  const { panel: opPanel } = await expandRow(op, opTable, 0);
  const opIdDisabled = await opPanel.locator('input').first().isDisabled().catch(() => false);
  const opSwitchCls = await switchClassOf(opPanel);
  const opKwText = await opPanel.locator('textarea').first().inputValue().catch(() => '');
  ledger.check(
    '只读用户仍能展开面板看全部字段，且控件读得到、改不动：id disabled + 「区分大小写」开关带 is-disabled + 关键词原文照读（el-switch 的禁用只体现在根节点 class，isDisabled() 对 div 恒 false ⇒ 按 class 判）',
    opIdDisabled && /is-disabled/.test(opSwitchCls) && opKwText === 'OMEGA\nKAPPA',
    `id disabled=${opIdDisabled} switch class=${opSwitchCls || '(无)'} textarea=${JSON.stringify(opKwText)}`,
  );
  await collapseAll(op, opTable);
  await op.locator('.el-tabs__item').filter({ hasText: '跨版本检索' }).first().click();
  await op.waitForTimeout(700);
  await op.getByPlaceholder('规则集名称').fill(N_NAME);
  await op.getByRole('button', { name: '检索' }).click();
  await waitFor(async () => (await rowsOf(visTable(op)).count()) === 7,
    { label: 'operator 检索出 7 行', timeout: 20000 }).catch(() => {});
  const opRowBtns = [];
  const opSearchRows = rowsOf(visTable(op));
  for (let k = 0; k < (await opSearchRows.count()); k++) {
    opRowBtns.push(norm(await opSearchRows.nth(k).locator('td').nth(6).locator('button').first()
      .innerText().catch(() => '(无按钮)')));
  }
  ledger.check(
    '检索表的行内按钮在无写权限时是「查看」而不是「去编辑」（同一插槽换文案），7 行索引全部只读',
    opRowBtns.length === 7 && opRowBtns.every((t) => t === '查看') && !opBtns.includes('去编辑'),
    `行数=${await rowsOf(visTable(op)).count()} 按钮=${JSON.stringify(opRowBtns)} 页面上是否残留「去编辑」=${opBtns.includes('去编辑')}`,
  );

  // 只读账号点「查看」走的仍是同一个 goToEdit（源码 index.vue:217）：切 Tab + loadVersion，不写库
  await opSearchRows.nth(0).locator('td').nth(6).locator('button').first().click();
  await op.waitForTimeout(900);
  const opViewOrder = await idsInOrder(visTable(op));
  const opViewBtns = [...new Set(await bodyButtons(op))];
  ledger.check(
    'operator 点首行「查看」切到「查看规则」Tab 并加载该行版本（v3 的 2 条：ui-m-c|ui-m-b），写入口依旧不在 DOM',
    norm(await op.locator('.el-tabs__item.is-active').innerText()) === '查看规则'
      && opViewOrder.join('|') === 'ui-m-c|ui-m-b'
      && (await opDivider()) === '规则（2 条）'
      && !opViewBtns.some((t) => /添加规则|另存为新版本/.test(t)),
    `Tab=${norm(await op.locator('.el-tabs__item.is-active').innerText())} 顺序=${opViewOrder.join('|')} 分隔线=${await opDivider()}`,
  );
  await shot(op, ledger, 'rules_readonly');

  // ---- 422 透传族之二：阈值字段不是命名分组（校验在 compile_ruleset，前端只能原样显示）----
  await go(page, `/rules/maintenance?rule_set_id=${s3.id}`, { reload: true, waitText: '规则（2 条）' });
  await collapseAll(page, mt);
  const postsThr = (st.rulesetPosts ?? []).length;
  const NEED_THR_FIELD = 'input[placeholder="字段（正则命名分组）"]';
  const thrFieldOf = async () => (await livePanel(page, 1, NEED_THR_FIELD))
    .locator(NEED_THR_FIELD).first();
  const { panel: pThr } = await expandRow(page, mt, 1);
  const thrField = pThr.locator(NEED_THR_FIELD).first();
  await thrField.fill('nope');
  await clearMessages(page);
  await saveBtn.click();
  await page.waitForTimeout(1600);
  const thrAlert = await errAlert();
  // 422 后 el-alert 插到表格上方 ⇒ 表格重排、展开面板又被卸载（还是缺 row-key）。读回/续填一律
  // 重新解析面板，拿着保存前的旧 locator 会一路等到 30s 超时（实测 run4 的阶段异常就是这么挂的）。
  const thrFieldAfter = await thrFieldOf();
  const thrValue = await thrFieldAfter.inputValue().catch(() => '');
  ledger.check(
    '阈值字段填了不存在的分组名：前端放行、请求发出，告警逐字复现后端「not a named group in pattern (available: [\'ms\'])」',
    thrAlert === "rule 'ui-m-b': threshold field 'nope' is not a named group in pattern (available: ['ms'])"
      && (st.rulesetPosts ?? []).length === postsThr + 1
      && !(await messagesOf(page)).some((t) => /已创建版本/.test(t)),
    `告警=${thrAlert || '(无)'} POST 增量=${(st.rulesetPosts ?? []).length - postsThr} 消息=${JSON.stringify(await messagesOf(page))}`,
  );
  ledger.check(
    '422 后仍在原版本编辑态：版本下拉停在 v3，阈值字段读回的还是刚填的 nope（页面不回滚，交给用户改）',
    (await boxText(page, versionBox(page))) === `v3（#${s3.id}）` && thrValue === 'nope',
    `ver=${await boxText(page, versionBox(page))} field=${JSON.stringify(thrValue)}`,
  );
  await thrFieldAfter.fill('ms');
  await page.waitForTimeout(300);

  // ---- 422 透传族之三：编码不存在（LookupError 也被路由折成 422，顺序在编译与 pathspec 之后）----
  const postsEnc = (st.rulesetPosts ?? []).length;
  const encInput = fieldItem(page, '编码').locator('input').first();
  await encInput.fill('e2e-no-such-codec');
  await collapseAll(page, mt);
  await clearMessages(page);
  await saveBtn.click();
  await page.waitForTimeout(1600);
  const encAlert = await errAlert();
  ledger.check(
    '编码写成不存在的 codec：告警原文「unknown encoding \'e2e-no-such-codec\'」（codecs.lookup 的 LookupError），且请求确实发出去了',
    encAlert === "unknown encoding 'e2e-no-such-codec'"
      && (st.rulesetPosts ?? []).length === postsEnc + 1
      && !(await messagesOf(page)).some((t) => /已创建版本/.test(t)),
    `告警=${encAlert || '(无)'} POST 增量=${(st.rulesetPosts ?? []).length - postsEnc}`,
  );
  await encInput.fill('utf-8');
  await page.waitForTimeout(300);

  // ---- 改回合法值再另存 v4：两次 422 都没留下版本行，下拉恰好 4 项倒序 ----
  const s4 = await saveNewVersion(page, st, 4);
  const pl4 = s4.payload ?? {};
  ledger.check(
    '另存 v4 成功且 POST 体回到合法值（阈值 field=ms、编码 utf-8、删除后的 2 条规则）',
    s4.msg === '已创建版本 4' && /^v4（#\d+）$/.test(s4.ver ?? '')
      && pl4.config?.encoding === 'utf-8' && pl4.config?.fallback_encoding === 'gb18030'
      && (pl4.config?.rules ?? []).map((r) => r.id).join('|') === 'ui-m-c|ui-m-b'
      && pl4.config?.rules?.[1]?.threshold?.field === 'ms'
      && pl4.config.rules[1].match.pattern === RE_MS,
    `消息=${s4.msg ?? '(无)'} 版本框=${s4.ver ?? '(无)'} body=${JSON.stringify(pl4).slice(0, 300)}`,
  );
  const dbNames = await api.get('/rule-sets?limit=500&offset=0');
  const mine = dbNames.filter((r) => r.name === N_NAME).map((r) => r.version).sort((a, b) => a - b);
  ledger.check(
    '数据面对账：库里 N_NAME 只有 v1..v4 四行 ⇒ 前端拦下的 2 次与后端 422 的 3 次失败都没写库',
    mine.join('|') === '1|2|3|4',
    `后端版本=${mine.join('|')} ids=${JSON.stringify(dbNames.filter((r) => r.name === N_NAME).map((r) => [r.id, r.version]))}`,
  );
  await readyVersionBox();
  const vOpts4 = await openBoxOptions(page, versionBox(page));
  ledger.check(
    '版本下拉倒序列出 4 个独立 id（v4/v3/v2/v1）：另存是同名 max+1，失败不占号也不覆盖',
    vOpts4.join('|') === `v4（#${s4.id}）|v3（#${s3.id}）|v2（#${s2.id}）|v1（#${s1.id}）`,
    vOpts4.join('|'),
  );
  await pickInBox(page, versionBox(page), `v1（#${s1.id}）`, { exact: true });
  await page.waitForTimeout(900);
  await readyVersionBox();
  ledger.check(
    '手动切到 v1 即回读 v1 的内容（2 条、顺序 ui-m-a|ui-m-b、阈值仍 ms gt 1000）：版本行不可变，切回去就是当时的快照',
    (await dividerOf()) === '规则（2 条）'
      && (await idsInOrder(mt)).join('|') === 'ui-m-a|ui-m-b'
      && (await cellsOf(rowsOf(mt).nth(1)))[7] === 'ms gt 1000',
    `分隔线=${await dividerOf()} 顺序=${(await idsInOrder(mt)).join('|')} 阈值=${(await cellsOf(rowsOf(mt).nth(1)))[7]}`,
  );
  await shot(page, ledger, 'rules_422_and_versions');

  // ---- 收尾：后端只有 GET/POST，界面也就没有任何删除入口 ----
  await go(op, '/rules/maintenance', { reload: true, waitText: '跨版本检索' }).catch(() => {});
  const cleanBtns = [...new Set(await bodyButtons(op))];
  ledger.check(
    '维护页同样没有任何规则集级删除/停用入口（与后端「只 POST 新版本」一致）',
    !cleanBtns.some((t) => /删除规则集|停用|下线|归档/.test(t)),
    cleanBtns.join(','),
  );
  ledger.skip(
    `观察项：本阶段留下的 ${st.createdRuleSets.length} 个规则集版本无法清理（${RS_NAME} v1/v2、${M_NAME} v1、${N_NAME} v1..v4 = id ${[...new Set(st.createdRuleSets)].join('/')}）；只能由收尾脚本按 id 直连库删行`,
    '后端没有 DELETE/归档接口，只能等库外清理；建议补 DELETE /api/rule-sets/{id} 或加 disabled 标记位',
  );
  await shot(page, ledger, 'rules_final');
}

// __PHASES__
export const phases = { rulesets: phase_rulesets, rules: phase_rules };
