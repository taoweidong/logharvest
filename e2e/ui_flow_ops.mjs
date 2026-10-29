// U7 数据源 / U8 Worker / U9 任务统计 / U14 概览 —— 只断言"页面事实"。
//
// 口径（与 ui_flow.mjs 头部一致）：
// - 文案逐字来自 web/src/views/logharvest/{datasources,workers,stats,overview}/index.vue
//   与 web/src/views/logharvest/format.ts（源码 HEAD=ff23e94，摘要见 e2e/refs/ui-map.md §3/§8/§9/§10）；
//   全站无 data-testid，文案即选择器。
// - 期望值一律现场取 /api（st.api）当基线，不把某一轮探测的常数写死；Api 只负责取基线与
//   清理前置，断言永远读 DOM。
// - 不可达分支（unknown 判定、5xx 降级、负心跳、概览合成载荷…）用 page.route 注入，只改
//   传输与响应体，改不了业务逻辑；每个 route 都在 try/finally 里配对 unroute，阶段异常也
//   不会把注入留在页面上。
// - 写操作共四处，全部当场复原：
//     ① 新建数据源 ui-ds-<RUN_TS>（后端只有 create/test/list，没有删除接口）→ 阶段末尾 psql 删行；
//     ② 单台排空 → UI 点「恢复」+ finally 用 API 兜底 resume，绝不留 draining；
//     ③ 按版本排空 → 打一个库里不存在的版本号（routes_workers.py:110 匹配不到行，只落一条审计）。
//     ④ 概览「执行维护」→ 幂等清扫；本轮栈内无 pending/running、无失联 Worker，预期 0/0/0，
//        并额外核对维护前后 /system/overview 逐字段一致（动了数据就是队列非静止，会另记观察项）。
//   「用它建任务」会直接弹出建任务向导：只看预填、不提交，点「取消」关窗。
//
// 运行：
//   bash e2e/ui_run.sh --phase datasources,workers,stats,overview
//   node e2e/ui_probe.mjs /logharvest/datasources --click 新建数据源   # 先照妖镜，别靠猜

import { execFileSync } from 'node:child_process';
import {
  go, waitFor, shot, tableHeaderTexts, tableRow, bodyButtons, formErrors,
  openSelect, pickOption, selectOptionsOf, clearSelect, dialog,
  readMessageBox, clickMessageBoxButton, waitMessage, clearMessages, messagesOf,
  recentRequests, useSession, esc, RUN_TS, Api,
} from './ui_lib.mjs';

// 与 e2e/up.sh 同一套容器/库名（只用于清理 UI 造出的行，不参与任何断言）。
const PG = {
  container: process.env.PG_CONTAINER || 'lh-e2e-pg',
  user: process.env.PG_USER || 'logharvest',
  db: process.env.E2E_PG_DB || 'logharvest_e2e',
};

function psql(sql) {
  return execFileSync('wslc', [
    'exec', PG.container, 'psql', '-U', PG.user, '-d', PG.db,
    '-v', 'ON_ERROR_STOP=1', '-t', '-c', sql,
  ], { encoding: 'utf8' }).trim();
}

const norm = (t) => (t || '').replace(/\s+/g, ' ').trim();
const bodyText = (page) => page.locator('body').innerText().then(norm).catch(() => '');
const cellsOf = async (row) => (await row.locator('td').allInnerTexts()).map(norm);
const clearRequests = (page) => { if (Array.isArray(page.__requests)) page.__requests.length = 0; };

/** el-tag 的配色（EP 把 type 写进 .el-tag--success 这类类名，比读计算样式稳）。 */
async function tagClassOf(scope, text) {
  const el = scope.locator(`.el-tag:has-text("${text}")`).first();
  return norm(await el.getAttribute('class').catch(() => ''));
}

const hasTag = (cls, type) => cls.includes(`el-tag--${type}`);

/** 卡片定位：数据源页是卡片栅格（不是表格），按名称文本选卡。 */
const dsCard = (page, name) => page.locator('.el-card').filter({ hasText: name }).first();

/**
 * format.ts 的镜像，逐行对照源码抄（format.ts:47 formatGB、:141 formatDuration）。
 * 之所以要镜像：断言"页面显示的字符串"必须先知道"正确字符串应该是什么"，
 * 而正确值只存在于格式化函数里；镜像写错会立刻表现为失败，不会伪装成通过。
 */
const fmtGB = (bytes) => (bytes === null || bytes === undefined ? '-' : `${(bytes / 1024 ** 3).toFixed(2)} GB`);
const fmtCpu = (v) => (typeof v !== 'number' || !Number.isFinite(v) ? '—' : `${v.toFixed(1)}%`);
const fmtMemMB = (v) => (typeof v !== 'number' || !Number.isFinite(v) ? '—' : `${(v / 1024 / 1024).toFixed(1)} MB`);
const fmtDiskGB = (v) => (typeof v !== 'number' || !Number.isFinite(v) ? '—' : `${(v / 1024 / 1024 / 1024).toFixed(2)} GB`);
const fmtDuration = (s) => {
  if (s === null || s === undefined) return '-';
  if (s < 60) return `${Math.round(s)} 秒`;
  if (s < 3600) return `${Math.floor(s / 60)} 分`;
  return `${Math.floor(s / 3600)} 小时 ${Math.floor(((s % 3600) / 60))} 分`;
};
const sumOf = (counts) => Object.values(counts || {}).reduce((t, v) => t + (v ?? 0), 0);

/** overview 的 distribution()（index.vue:78-93）：非 0 键按 JOB_STATUS_OPTIONS 定序，尾部接表外键。 */
const JOB_STATUS_OPTIONS = ['pending', 'running', 'succeeded', 'partial', 'failed', 'cancelled'];
function distribution(counts, total) {
  const keys = Object.keys(counts || {}).filter((k) => counts[k]);
  const ordered = [...JOB_STATUS_OPTIONS.filter((k) => keys.includes(k)), ...keys.filter((k) => !JOB_STATUS_OPTIONS.includes(k))];
  return ordered.map((status) => ({
    status,
    count: counts[status] ?? 0,
    percent: total ? Math.round(((counts[status] ?? 0) / total) * 100) : 0,
  }));
}

/** workers 页的分组视图（index.vue:68-84）：过滤后按版本串 localeCompare 倒序。 */
function workerGroups(rows, statusFilter) {
  const src = statusFilter ? rows.filter((r) => r.status === statusFilter) : rows;
  const byVersion = new Map();
  src.forEach((r) => {
    if (byVersion.has(r.version)) byVersion.get(r.version).push(r);
    else byVersion.set(r.version, [r]);
  });
  return [...byVersion.entries()]
    .sort((a, b) => b[0].localeCompare(a[0]))
    .map(([version, items]) => ({
      version,
      items,
      alive: items.filter((i) => ['online', 'busy', 'draining'].includes(i.status)).length,
    }));
}

const aliveCount = (rows) => rows.filter((r) => ['online', 'busy', 'draining'].includes(r.status)).length;
const metricReportedCount = (rows) => rows.filter((r) => typeof r.cpu_percent === 'number'
  || typeof r.memory_bytes === 'number' || typeof r.temp_disk_free_bytes === 'number').length;

/** 抓某个接口的**响应体**（用于「页面显示的数 = 后端真返回的数」这类对账）；返回停止函数。 */
async function captureResponses(page, re) {
  const got = [];
  const handler = (res) => {
    if (!re.test(res.url())) return;
    got.push(res.json().then((body) => ({ status: res.status(), body })).catch(() => ({ status: res.status(), body: null })));
  };
  page.on('response', handler);
  return {
    stop: async () => { page.off('response', handler); return Promise.all(got); },
  };
}

/** 注入一个固定 JSON 响应；返回反注册函数。5xx 用 fulfill（abort 会撞 axios 10s 超时）。 */
async function inject(page, url, respond) {
  await page.route(url, async (route) => {
    const body = typeof respond === 'function' ? respond(route.request()) : respond;
    if (body && body.__status && body.__body === undefined) {
      await route.fulfill({ status: body.__status, contentType: 'application/json', body: '{}' });
      return;
    }
    await route.fulfill({
      status: body?.__status ?? 200,
      contentType: 'application/json',
      body: JSON.stringify(body?.__body ?? body),
    });
  });
  return () => page.unroute(url);
}

// ---------------------------------------------------------------------------
// U7 数据源 `#/logharvest/datasources`
// ---------------------------------------------------------------------------
async function phase_datasources(ledger, st, { PHASE_LABEL }) {
  ledger.phase(PHASE_LABEL.datasources);
  const page = st.adminPage ?? await useSession(ledger, st, 'admin');
  const uiName = `ui-ds-${RUN_TS}`;
  const uiPath = `/nas/${uiName}`;
  const LIST_RE = /\/api\/data-sources(\?|$)/;
  const TEST_RE = /\/api\/data-sources\/\d+\/test$/;
  const created = [];
  try {
    const list = await st.api.get('/data-sources?limit=50');
    const byName = Object.fromEntries(list.map((d) => [d.name, d]));
    ledger.check('基线：库里至少有 e2e-nas / e2e-writable', byName['e2e-nas'] && byName['e2e-writable'],
      list.map((d) => `${d.id}:${d.name}`).join(','));

    await go(page, '/logharvest/datasources', { reload: true, waitText: '企业磁盘挂载点登记' });
    const body = await bodyText(page);
    ledger.check('页头灰字逐字（声明后端强制只读）',
      body.includes('企业磁盘挂载点登记；后端强制只读，处理时不会回写源目录'), body.slice(0, 140));
    const btns = await bodyButtons(page);
    ledger.check('工具条有「刷新」「新建数据源」（admin 有 datasources:write）',
      btns.includes('刷新') && btns.includes('新建数据源'), [...new Set(btns)].join(','));
    ledger.equal('本页是卡片栅格而非表格（无 el-table）', await page.locator('.el-table').count(), 0);
    ledger.equal('卡片数与 API 列表一致', await page.locator('.el-card').count(), list.length);

    // ---- e2e-nas 卡的静态事实 ----
    const nas = dsCard(page, 'e2e-nas');
    const nasText = norm(await nas.innerText());
    ledger.check('卡头「名称 #id」', nasText.includes(`e2e-nas #${byName['e2e-nas'].id}`), nasText.slice(0, 120));
    ledger.check('root_path 用等宽字体显示', /font-mono/.test(norm(await nas.locator('.font-mono').first().getAttribute('class').catch(() => '')))
      && nasText.includes('/nas'), await nas.locator('.font-mono').first().innerText().catch(() => ''));
    ledger.check('tag「启用」success + 「登记只读：是」', hasTag(await tagClassOf(nas, '启用'), 'success') && nasText.includes('登记只读：是'),
      `${await tagClassOf(nas, '启用')} | ${nasText.slice(0, 200)}`);
    ledger.check('描述逐字渲染', nasText.includes(byName['e2e-nas'].description), byName['e2e-nas'].description);
    ledger.check('创建时间是 YYYY-MM-DD HH:mm:ss', /\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}/.test(nasText), nasText.match(/\d{4}-\d{2}-\d{2}[^|]{0,20}/)?.[0] ?? '');
    ledger.equal('刚进页每张卡都写「路径未测试」（判定结果不进库、不缓存）',
      await page.locator('text=路径未测试').count(), list.length);

    // ---- ① 测试路径：e2e-nas → 三个 ✓ + 只读已确认 ----
    clearRequests(page);
    await nas.getByRole('button', { name: '测试路径' }).click();
    await waitFor(async () => (norm(await nas.innerText()).includes('只读已确认') ? true : null), { label: 'e2e-nas 判定徽章出现' });
    const t1 = norm(await nas.innerText());
    ledger.check('e2e-nas 徽章「存在: ✓ / 目录: ✓ / 可读: ✓」',
      t1.includes('存在: ✓') && t1.includes('目录: ✓') && t1.includes('可读: ✓'), t1.slice(0, 220));
    ledger.check('「只读已确认」是 success（真拿到 ro 证据才给绿勾）',
      hasTag(await tagClassOf(nas, '只读已确认'), 'success'), await tagClassOf(nas, '只读已确认'));
    ledger.equal('测试后灰字「路径未测试」从该卡消失', await nas.locator('text=路径未测试').count(), 0);
    const testReqs = await recentRequests(page, TEST_RE);
    const getReqs = (await recentRequests(page, LIST_RE)).filter((r) => r.method === 'GET');
    ledger.check('「测试路径」打的是 POST /data-sources/{id}/test',
      testReqs.length === 1 && testReqs[0].method === 'POST', JSON.stringify(testReqs));
    ledger.check('测试只更新该卡徽章，不重取列表', getReqs.length === 0, `GET 列表 ${getReqs.length} 次`);

    // ---- ② e2e-writable：实测可写，与「登记只读：是」并列 ----
    const wr = dsCard(page, 'e2e-writable');
    await wr.getByRole('button', { name: '测试路径' }).click();
    await waitFor(async () => (norm(await wr.innerText()).includes('可写：禁止处理') ? true : null), { label: 'e2e-writable 判定徽章出现' });
    const t2 = norm(await wr.innerText());
    ledger.check('e2e-writable 判定「可写：禁止处理」danger',
      hasTag(await tagClassOf(wr, '可写：禁止处理'), 'danger') && !t2.includes('只读已确认'), await tagClassOf(wr, '可写：禁止处理'));
    ledger.skip('观察项：「登记只读：是」与实测「可写：禁止处理」并列',
      `${t2.slice(0, 160)}｜schemas.py:21 read_only 是 Literal[True]、routes_data_sources.py 无更新接口 ⇒ 该 tag 恒为「是」，只声明不校验`);

    // ---- ③ unknown 判定：前端必须说「未确认」，不给假绿勾（规格 §7.2）----
    const offUnknown = await inject(page, TEST_RE, {
      exists: true, is_dir: true, readable: true, writability: 'unknown', read_only_confirmed: false,
    });
    try {
      await nas.getByRole('button', { name: '测试路径' }).click();
      await waitFor(async () => (norm(await nas.innerText()).includes('只读未确认') ? true : null), { label: '「只读未确认」出现' });
      ledger.check('writability=unknown → 灰字「只读未确认」warning（不冒充已确认）',
        hasTag(await tagClassOf(nas, '只读未确认'), 'warning'), await tagClassOf(nas, '只读未确认'));
    } finally {
      await offUnknown();
    }

    // ---- ④ 测试失败必须清掉旧徽章 + detail 透传 ----
    clearMessages(page);
    const offFail = await inject(page, TEST_RE, { __status: 500, __body: { detail: 'e2e 注入：NAS 挂载暂不可达' } });
    try {
      await nas.getByRole('button', { name: '测试路径' }).click();
      await waitFor(async () => (await nas.locator('text=路径未测试').count() ? true : null), { label: '失败后回到「路径未测试」' });
      ledger.check('测试失败清掉旧徽章（旧绿勾不会被读成"这次也通过了"）',
        !norm(await nas.innerText()).includes('只读已确认'), norm(await nas.innerText()).slice(0, 160));
      const msgs = await waitMessage(page, /NAS 挂载暂不可达/).then(() => messagesOf(page));
      ledger.check('页面消息透传后端 detail 原文', msgs.some((m) => m.includes('e2e 注入：NAS 挂载暂不可达')), JSON.stringify(msgs));
      ledger.check('5xx 另触发全局兜底消息（两条并存，非互斥）',
        msgs.some((m) => m.includes('服务暂时不可用，请稍后重试')), JSON.stringify(msgs));
    } finally {
      await offFail();
      await nas.getByRole('button', { name: '测试路径' }).click();
      await waitFor(async () => (norm(await nas.innerText()).includes('只读已确认') ? true : null), { label: '恢复真实判定' }).catch(() => {});
    }
    await shot(page, ledger, 'ds_tested_cards');

    // ---- ⑤ 新建数据源：表单结构 → 必填 → 创建 → 重名 409 ----
    await page.getByRole('button', { name: '新建数据源' }).click();
    const dlg = await dialog(page, '新建数据源');
    const labels = (await dlg.locator('.el-form-item__label').allInnerTexts()).map(norm);
    ledger.equal('对话框字段顺序', labels.join('|'), '名称|根路径|只读|描述');
    const ph = (await dlg.locator('input').evaluateAll((els) => els.map((e) => e.placeholder).filter(Boolean)));
    ledger.check('两个 placeholder 逐字（重名 409 写在提示里）',
      ph.includes('唯一名称，重名返回 409') && ph.includes('企业磁盘挂载点的服务器绝对路径'), JSON.stringify(ph));
    const roInput = dlg.locator('.el-form-item').filter({ hasText: '只读' }).locator('input').first();
    ledger.equal('「只读」是 disabled 固定值，界面不给开关', await roInput.inputValue(), 'true（后端强制只读，不提供开关）');
    ledger.check('「只读」输入框确实 disabled', await roInput.isDisabled(), 'enabled');
    clearRequests(page);
    await dlg.getByRole('button', { name: '创建' }).click();
    await page.waitForTimeout(500);
    const errs = await formErrors(page);
    ledger.check('空提交的两条必填红字', errs.some((t) => t.includes('请填写数据源名称')) && errs.some((t) => t.includes('请填写挂载点绝对路径')), JSON.stringify(errs));
    ledger.check('必填拦住时没有发出 POST',
      (await recentRequests(page, LIST_RE)).filter((r) => r.method === 'POST').length === 0,
      JSON.stringify(await recentRequests(page, LIST_RE)));

    await dlg.getByPlaceholder('唯一名称，重名返回 409').fill(uiName);
    await dlg.getByPlaceholder('企业磁盘挂载点的服务器绝对路径').fill(uiPath);
    await dlg.locator('textarea').fill('UI 流程建的挂载点，跑完就删');
    await dlg.getByRole('button', { name: '创建' }).click();
    await waitMessage(page, new RegExp(`数据源“${esc(uiName)}”已创建`));
    await waitFor(async () => ((await dsCard(page, uiName).count()) ? true : null), { label: '新卡片出现在列表里' });
    const afterCreate = await st.api.get('/data-sources?limit=50');
    const row = afterCreate.find((d) => d.name === uiName);
    created.push(uiName);
    ledger.check('创建成功并回列表（卡片带新 id）', Boolean(row) && norm(await dsCard(page, uiName).innerText()).includes(`#${row.id}`),
      JSON.stringify(row));
    ledger.check('新卡片 read_only 恒真 + 未测试徽章', /登记只读：是/.test(norm(await dsCard(page, uiName).innerText()))
      && (await dsCard(page, uiName).locator('text=路径未测试').count()) === 1, norm(await dsCard(page, uiName).innerText()));
    const newCard = dsCard(page, uiName);
    await newCard.getByRole('button', { name: '测试路径' }).click();
    await waitFor(async () => (norm(await newCard.innerText()).includes('存在: ✗') ? true : null), { label: '不存在路径的 ✗ 徽章' });
    const t3 = norm(await newCard.innerText());
    ledger.check('不存在的路径给出三个 ✗（danger，不粉饰）',
      t3.includes('存在: ✗') && t3.includes('目录: ✗') && t3.includes('可读: ✗') && !t3.includes('✓'), t3.slice(0, 200));
    ledger.skip('观察项：路径不存在仍显示「只读已确认」',
      `${t3.slice(0, 200)}｜source_guard.py:116 判的是挂载点而不是该路径本身 ⇒ /nas 挂载 ro 就返回 read_only，绿勾与「存在: ✗」并排容易被读成矛盾`);

    await page.getByRole('button', { name: '新建数据源' }).click();
    const dup = await dialog(page, '新建数据源');
    await dup.getByPlaceholder('唯一名称，重名返回 409').fill(uiName);
    await dup.getByPlaceholder('企业磁盘挂载点的服务器绝对路径').fill('/nas/other');
    clearMessages(page);
    await dup.getByRole('button', { name: '创建' }).click();
    await waitMessage(page, /data source name already exists/);
    ledger.check('重名返回 409 且后端 detail 原样上屏',
      (await messagesOf(page)).some((m) => m.includes('data source name already exists')), JSON.stringify(await messagesOf(page)));
    ledger.check('409 后对话框不关（用户能改名字重试）', await page.locator('.el-dialog:visible').count() > 0,
      `visible dialog=${await page.locator('.el-dialog:visible').count()}`);
    await dup.getByRole('button', { name: '取消' }).click();
    await page.waitForTimeout(400);
    ledger.equal('列表里没有多出不该有的行（仍 3 张卡）', await page.locator('.el-card').count(), list.length + 1);

    // ---- ⑥ 窗口式翻页脚注 ----
    const foot = (await page.locator('.text-gray-500').allInnerTexts()).map(norm).find((t) => t.includes('后端列表接口不返回总数'));
    ledger.check('脚注说明无总数 + 页码/本页条数', foot.includes(`第 1 页，本页 ${list.length + 1} 条`), foot);
    const pager = await page.getByRole('button', { name: '上一页' }).first().isDisabled();
    const nextDisabled = await page.getByRole('button', { name: '下一页' }).first().isDisabled();
    ledger.check('不足 limit：上一页禁用、下一页也禁用', pager && nextDisabled, `上一页 disabled=${pager} 下一页 disabled=${nextDisabled}`);

    // ---- ⑦ 「用它建任务」带 data_source_id 直达作业页并弹出预填向导 ----
    await dsCard(page, 'e2e-nas').getByRole('button', { name: '用它建任务' }).click();
    await page.waitForURL(/#\/logharvest\/jobs\?data_source_id=/, { timeout: 15000 }).catch(() => null);
    ledger.check('跳转 URL 带上 data_source_id', /data_source_id=1\b/.test(page.url()), page.url());
    const wiz = await dialog(page, '新建分析任务');
    // 向导打开时数据源列表还在异步拉取，el-select 的选中项 label 要等列表回来才渲染；
    // 读一次就断言会偶发拿到空 label（condition 与 detail 是两次读取，差一帧就会自相矛盾）。
    let wizText = '';
    let prefilled = false;
    const prefillDeadline = Date.now() + 10000;
    while (Date.now() < prefillDeadline && !prefilled) {
      wizText = norm(await wiz.innerText());
      prefilled = wizText.includes('e2e-nas（/nas）');
      if (!prefilled) await page.waitForTimeout(300);
    }
    ledger.check('向导第①步数据源已预填（显示 name（root_path））', prefilled, wizText.slice(0, 200));
    await wiz.getByRole('button', { name: '取消' }).click();
    await page.waitForTimeout(500);
    ledger.equal('向导取消后没有留下弹窗', await page.locator('.el-dialog:visible').count(), 0);
    await go(page, '/logharvest/datasources', { reload: true });

    // ---- ⑧ 空态（后端没有任何数据源时页面说什么）----
    const offEmpty = await inject(page, LIST_RE, []);
    try {
      await go(page, '/logharvest/datasources', { reload: true });
      await waitFor(async () => ((await page.locator('.el-empty').count()) ? true : null), { label: '空态出现' });
      ledger.check('空态文案逐字', norm(await page.locator('.el-empty__description').first().innerText())
        === '还没有登记任何数据源：先新建一个挂载点', norm(await page.locator('.el-empty__description').first().innerText()));
      ledger.equal('空列表下卡片为 0', await page.locator('.el-card').count(), 0);
      ledger.check('空列表脚注「本页 0 条」', (await bodyText(page)).includes('第 1 页，本页 0 条'),
        (await page.locator('.text-gray-500').allInnerTexts()).map(norm).join(' | '));
    } finally {
      await offEmpty();
    }
    await go(page, '/logharvest/datasources', { reload: true, waitText: 'e2e-nas' });
    ledger.equal('撤掉注入后真实列表回来自愈', await page.locator('.el-card').count(), list.length + 1);
    await shot(page, ledger, 'ds_final_list');
  } finally {
    if (created.length) {
      const out = psql(`DELETE FROM data_sources WHERE name LIKE 'ui-ds-%' RETURNING name`);
      ledger.check('清理 UI 造出的数据源行（后端没有删除接口，只能出库）', /ui-ds-/.test(out) || out === '', out.slice(0, 200));
    }
  }
}

// ---------------------------------------------------------------------------
// U8 Worker 管理 `#/logharvest/workers`
// ---------------------------------------------------------------------------
const STATUS_TAG = { online: 'success', busy: 'primary', draining: 'warning', offline: 'info' };

/** `fromNow`（format.ts:68-77）的镜像；注意它没有负数钳位，未来时间会输出「-N …前」。 */
function fromNowMirror(value) {
  if (!value) return '-';
  const seconds = Math.floor((Date.now() - new Date(/Z|[+-]\d{2}:?\d{2}$/.test(value) ? value : `${value}Z`).getTime()) / 1000);
  if (seconds < 60) return `${seconds} 秒前`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return `${Math.floor(seconds / 86400)} 天前`;
}

async function phase_workers(ledger, st, { PHASE_LABEL }) {
  ledger.phase(PHASE_LABEL.workers);
  const page = st.adminPage ?? await useSession(ledger, st, 'admin');
  const LIST_RE = /\/api\/workers\?limit=/;
  const DRAIN_VER_RE = /\/api\/workers\/drain-version/;
  const FAKE_VERSION = '0.0.0-e2e-none';
  const cardHeaders = async () => {
    const out = [];
    for (const card of await page.locator('.el-card').all()) {
      out.push(norm(await card.locator('.el-card__header').innerText().catch(() => '')));
    }
    return out;
  };
  const headerFor = (list, version) => list.find((t) => t.startsWith(`版本 ${version} `)) ?? '';
  const footerText = async () => (await page.locator('.text-gray-500').allInnerTexts())
    .map(norm).find((t) => t.includes('后端列表接口不返回总数')) ?? '';
  const opsCell = (id, name) => tableRow(page, id).getByRole('button', { name, exact: true });
  let rows = [];
  let groups = [];
  try {
    rows = await st.api.get('/workers?limit=100&offset=0');
    // 离线行是本阶段多处断言的前置（离线 tooltip、「只剩 offline 分组」筛选、恢复按钮
    // disabled）。栈里只有两台默认 Worker，全在线——没有离线行就种一行夹具：与
    // limits_flow 收尾"离线行仍保留在 /workers 里"是同一条约定，api_flow P7 已改为
    // 对离线行按合法状态对待，所以这里留下的行不影响其它流程。
    if (!rows.some((r) => r.status === 'offline')) {
      try {
        psql(`INSERT INTO workers (id, hostname, version, status, drain_requested, max_concurrent_copies,
              last_heartbeat, started_at)
              SELECT 'lh-e2e-worker-3', 'lh-e2e-worker-3', '0.1.0', 'offline', false, 4,
                     now() - interval '2 hours', now() - interval '3 hours'
              WHERE NOT EXISTS (SELECT 1 FROM workers WHERE status = 'offline')
              ON CONFLICT (id) DO NOTHING;`);
        rows = await st.api.get('/workers?limit=100&offset=0');
        ledger.check('前置：库里没有离线 Worker 行 → 已种一行离线夹具（心跳冻结 2 小时前）',
          rows.some((r) => r.status === 'offline'),
          rows.map((r) => `${r.id}@${r.version}:${r.status}`).join(','));
      } catch (err) {
        ledger.fail('前置：种离线 Worker 夹具行', err);
      }
    } else {
      ledger.check('前置：库里已有离线 Worker 行，直接用作夹具', true,
        rows.filter((r) => r.status === 'offline').map((r) => r.id).join(','));
    }
    ledger.check('基线：Worker 池至少 3 台（两台默认在线 + 一行离线夹具），列表接口全部返回', rows.length >= 3,
      rows.map((r) => `${r.id}@${r.version}:${r.status}`).join(','));
    groups = workerGroups(rows, '');

    await go(page, '/logharvest/workers', { reload: true, waitText: 'Worker（按版本分组）' });
    const body = await bodyText(page);
    ledger.check('提示块 1 标题「这里不执行任何部署」（部署边界写死在页面上）', body.includes('这里不执行任何部署'), body.slice(0, 120));
    ledger.check('提示块 1 声明离线标记重启后生效', body.includes('离线机器上的标记会在其重启后生效'), body.slice(200, 460));
    ledger.check('提示块 2 区分「—」与 0（逐字含反引号）',
      body.includes('CPU / 内存 / 临时盘空闲 / 最近错误：`—` 是「没有上报过」，不是 0'), body.slice(460, 640));
    ledger.check('提示块 2 把误判写进说明（读成 CPU 空闲就是本界面要避免的）',
      body.includes('把它读成「CPU 空闲」或「临时盘已满」就是本界面刻意避免的误判'), '');

    const heads = await cardHeaders(page);
    ledger.equal('分组顺序＝版本串 localeCompare 倒序（无 semver 语义）',
      heads.map((t) => (t.match(/^版本 (\S+)/) || [])[1]).join(','), groups.map((g) => g.version).join(','));
    for (const g of groups) {
      const head = headerFor(heads, g.version);
      ledger.check(`分组头「版本 ${g.version}」的台数/存活口径一致`,
        head.includes(`${g.items.length} 台`)
          && head.includes(`存活（online/busy/draining）${g.alive} 台 · 离线 ${g.items.length - g.alive} 台`)
          && head.includes('排空该版本'), head);
    }

    const foot = await footerText(page);
    ledger.equal('脚注逐字（含「不返回总数」的诚实声明）', foot,
      `后端列表接口不返回总数：状态筛选只作用于本页；第 1 页，本页 ${rows.length} 条，共 ${groups.length} 个版本，`
      + `存活（online/busy/draining）${aliveCount(rows)} 台；至少上报过一项资源的 ${metricReportedCount(rows)} 台`
      + '（其余显示「—」= 该 Worker 尚未上报，需升级到 M2c Task 4 的 Worker 并重启后才有数据）');
    ledger.check('分页按钮在无总数语义下只能靠「本页满页」判断', (await page.getByRole('button', { name: '上一页' }).isDisabled())
      && (await page.getByRole('button', { name: '下一页' }).isDisabled()), foot.slice(0, 60));

    // 逐行核对真实值与 format.ts 镜像（列序：id/主机名/状态/当前任务/并发/CPU/内存/临时盘/心跳/排空标记/最近错误/操作）
    const negatives = [];
    for (const r of rows) {
      const cells = await cellsOf(tableRow(page, r.id));
      ledger.equal(`${r.id} 桌面列数`, cells.length, 12);
      ledger.equal(`${r.id} 状态 tag 配色`, hasTag(await tagClassOf(tableRow(page, r.id), r.status), STATUS_TAG[r.status] ?? 'info'), true);
      ledger.equal(`${r.id} 当前文件任务（null→"-"，不是破折号）`, cells[3], r.current_task_id ?? '-');
      ledger.equal(`${r.id} CPU 格式`, cells[5], fmtCpu(r.cpu_percent));
      ledger.equal(`${r.id} 内存格式`, cells[6], fmtMemMB(r.memory_bytes));
      ledger.equal(`${r.id} 临时盘格式`, cells[7], fmtDiskGB(r.temp_disk_free_bytes));
      ledger.equal(`${r.id} 最近错误（null→「—」）`, cells[10], r.last_error ?? '—');
      ledger.equal(`${r.id} 排空标记文案`, cells[9], r.drain_requested ? '已标记' : '无');
      const hb = /^(-?\d+) (秒|分钟|小时|天)前$/.exec(cells[8]);
      ledger.check(`${r.id} 心跳相对时间可解析`, Boolean(hb), `cell=${cells[8]} 镜像=${fromNowMirror(r.last_heartbeat)}`);
      if (hb && Number(hb[1]) < 0) negatives.push(`${r.id}=${cells[8]}`);
      ledger.equal(`${r.id} 排空按钮 disabled ⇔ 已请求排空`, await opsCell(r.id, '排空').isDisabled(), r.drain_requested);
      ledger.equal(`${r.id} 恢复按钮 disabled ⇔ 已离线（后端 409）`, await opsCell(r.id, '恢复').isDisabled(), r.status === 'offline');
    }
    if (negatives.length) {
      ledger.check('[产品缺陷] 容器/浏览器时钟偏差下心跳显示「-N 秒前」', true,
        `${negatives.join(', ')}；证据 format.ts:68-77 fromNow 无负数钳位（diff<0 直接落进 <60 分支）`);
    } else {
      ledger.skip('观察项：本次浏览器与容器时钟一致，未自然出现负心跳',
        `样例=${rows.map((r) => `${r.id}:${fromNowMirror(r.last_heartbeat)}`).join(',').slice(0, 160)}`);
    }
    const offlineRow = rows.find((r) => r.status === 'offline');
    if (offlineRow) {
      await opsCell(offlineRow.id, '恢复').hover().catch(() => {});
      await page.waitForTimeout(700);
      const tips = (await page.locator('.el-popper').allInnerTexts()).map(norm);
      const tip = tips.find((t) => t.includes('Worker 已离线')) ?? '';
      if (tip) ledger.equal('离线行的 tooltip 解释为什么不能恢复', tip, 'Worker 已离线，后端对离线机器恢复领取返回 409');
      else ledger.skip('观察项：离线行的 tooltip 文案未渲染', `popper=${tips.join(' | ').slice(0, 160)}`);
    }

    // 状态筛选：只作用于本页（脚注自己声明过），但脚注的数字不跟着变
    await pickOption(page, '状态筛选', 'offline', { exact: true });
    await waitFor(async () => (await page.locator('.el-card').count()) === 1, { label: '只剩 offline 分组' });
    const visibleRows = await page.locator('.el-table__body .el-table__row').count();
    const filteredFoot = await footerText(page);
    const claimed = Number((filteredFoot.match(/本页 (\d+) 条/) || [])[1] ?? -1);
    if (claimed > 0 && claimed !== visibleRows) {
      ledger.check('[产品缺陷] 状态筛选后脚注仍报全量行数（「本页 N 条」与可见表格矛盾）', true,
        `脚注=${filteredFoot}；可见表格行=${visibleRows}；证据 workers/index.vue:459-463（rows.length/alive 用全量，groups.length/metricReported 用筛选后）`);
    } else {
      ledger.skip('观察项：本次筛选后可见行数恰好等于脚注行数，未复现口径矛盾',
        `脚注=${filteredFoot}；可见行=${visibleRows}`);
    }
    ledger.check('同一脚注里的「共 N 个版本」又跟着筛选走（两种口径混在一句话里）',
      filteredFoot.includes(`共 ${workerGroups(rows, 'offline').length} 个版本`), filteredFoot);
    await clearSelect(page, 'offline');
    await waitFor(async () => (await page.locator('.el-card').count()) === groups.length, { label: '清除筛选后分组回来' });

    ledger.equal('版本号下拉＝本页行的去重升序版本',
      (await selectOptionsOf(page, '版本号')).join(','),
      Array.from(new Set(rows.map((r) => r.version))).sort().join(','));

    // 按版本排空：先验证按钮的 disabled 门槛，再用不存在的版本走完整 POST（只写审计，不动 Worker）
    const drainVerBtn = page.getByRole('button', { name: '按版本排空' });
    ledger.equal('版本号为空时「按版本排空」不可点', await drainVerBtn.isDisabled(), true);
    await openSelect(page, '版本号');
    await page.keyboard.type(FAKE_VERSION);
    await page.waitForTimeout(500);
    await page.keyboard.press('Enter');
    await waitFor(async () => (await drainVerBtn.isDisabled()) === false, { label: 'allow-create 后按钮可用' });
    ledger.equal('allow-create 的自定义版本进入输入框',
      norm(await page.locator('.el-select__placeholder').nth(1).innerText()), FAKE_VERSION);
    clearMessages(page);
    clearRequests(page);
    await drainVerBtn.click();
    const verMb = await readMessageBox(page);
    ledger.equal('按版本排空确认文案逐字', verMb.body, `确认排空版本 ${FAKE_VERSION} 的全部 Worker？离线机器也会被记下标记，重启后生效。`);
    ledger.equal('按版本排空标题/按钮', `${verMb.title}|${verMb.buttons.join('|')}`, '按版本排空|取消|排空');
    await clickMessageBoxButton(page, '排空');
    // 注意：clearMessages 只能在触发动作之前调——toast 往往在 clickMessageBoxButton 内部的
    // 400ms 等待里就已经落地，事后清一次就把要断言的证据擦掉了。
    const verMsg = await waitMessage(page, new RegExp(esc(`版本 ${FAKE_VERSION}：`)), 12000);
    ledger.equal('不存在的版本回执为 0 台（drained/flagged 都是 0）', verMsg,
      `版本 ${FAKE_VERSION}：已排空 0 台，标记 0 台离线（重启后生效）`);
    const afterFake = await st.api.get('/workers?limit=100&offset=0');
    ledger.equal('按版本排空（不存在版本）没有改动任何 Worker',
      afterFake.map((r) => `${r.id}:${r.status}:${r.drain_requested}`).join(','),
      rows.map((r) => `${r.id}:${r.status}:${r.drain_requested}`).join(','));
    ledger.equal('页面卡片数不变', await page.locator('.el-card').count(), groups.length);

    // 「排空该版本」链接：会预填版本并弹确认框；这里只取消，不发 POST。
    // 顺序很关键——必须跑在下面「清除版本号」的缺陷复现之前：那个缺陷会把整块表格摘掉。
    // 版本号此时仍是 FAKE_VERSION，链接直接把分组头的版本盖上去，不需要先清下拉。
    const linkVersion = groups[0].version;
    clearRequests(page);
    const linkBtn = page.locator('.el-card').first().getByRole('button', { name: '排空该版本' });
    await waitFor(async () => (await linkBtn.count()) > 0, { label: '分组头的「排空该版本」出现' });
    await linkBtn.click();
    const linkMb = await readMessageBox(page);
    ledger.equal('链接式排空复用同一确认框（版本来自分组头，不是输入框里的假版本）', linkMb.body,
      `确认排空版本 ${linkVersion} 的全部 Worker？离线机器也会被记下标记，重启后生效。`);
    await clickMessageBoxButton(page, '取消');
    await waitFor(async () => (await page.locator('.el-message-box:visible').count()) === 0, { label: '确认框关闭' });
    ledger.equal('取消后没有发出 drain-version 请求',
      (await recentRequests(page, DRAIN_VER_RE)).filter((r) => r.method === 'POST').length, 0);
    ledger.equal('取消后版本号已被链接预填（按钮可用，需要用户自己清）',
      norm(await page.locator('.el-select__placeholder').nth(1).innerText()), linkVersion);
    ledger.equal('预填后「按版本排空」变为可点（这里不再点第二次）', await drainVerBtn.isDisabled(), false);

    // [产品缺陷] 清除版本号下拉会把整页表格抹掉：clearable 的 el-select 在清除时把 v-model 置成
    // undefined（EP 的 valueOnClear 默认回退到 emptyValue=undefined），而模板写的是
    // `!versionInput.trim()`（workers/index.vue:322），渲染期抛 TypeError → Vue 把
    // PureTableBar 整棵子树从 DOM 摘掉，页面只剩两块提示块，只有刷新才恢复。
    // 同一时刻 statusFilter 也被置成 undefined，但 filteredRows 只做真值判断，所以没事。
    const pageerr = [];
    const onConsole = (m) => { if (m.type() === 'error') pageerr.push(m.text()); };
    const onError = (e) => pageerr.push(String(e && e.message));
    page.on('console', onConsole);
    page.on('pageerror', onError);
    await clearSelect(page, linkVersion);
    await page.waitForTimeout(900);
    const afterClear = await page.evaluate(() => ({
      cards: document.querySelectorAll('.el-card').length,
      tables: document.querySelectorAll('.el-table').length,
      selects: document.querySelectorAll('.el-select').length,
      alerts: document.querySelectorAll('.el-alert').length,
    }));
    page.off('console', onConsole);
    page.off('pageerror', onError);
    const crashMsg = (pageerr.find((t) => /trim/.test(t)) || pageerr[0] || '').replace(/\s+/g, ' ').slice(0, 200);
    if (afterClear.cards === 0 && afterClear.tables === 0 && afterClear.alerts > 0) {
      await shot(page, ledger, 'workers_clear_crash');
      ledger.check('[产品缺陷] 点版本号下拉的清除图标后，Worker 分组表格整块消失（只剩两块提示）', true,
        `清除前 ${groups.length} 个分组 → 清除后 ${JSON.stringify(afterClear)}；控制台：${crashMsg || '(无报错，仅有白屏)'}`
        + '；证据 workers/index.vue:322 `:disabled="!versionInput.trim()"` + workers/index.vue:145 `ref("")`');
    } else {
      ledger.equal('清除版本号后页面保持完整（缺陷未复现，修复后的回归断言）',
        `${afterClear.cards}|${afterClear.tables}`, `${groups.length}|${groups.length}`);
    }
    ledger.check('白屏范围＝只有表格子树被摘（两块提示与页头都在，不是路由卸载或被踢回登录）',
      (await page.locator('.el-alert').count()) === 2 && !/login|access-denied/.test(page.url()),
      `alerts=${afterClear.alerts} url=${page.url()}`);
    await go(page, '/logharvest/workers', { reload: true, waitText: 'Worker（按版本分组）' });
    ledger.equal('刷新后分组恢复（缺陷只影响当前会话的渲染，不落数据）',
      await page.locator('.el-card').count(), groups.length);
    ledger.equal('刷新后版本号回到空值，「按版本排空」重新禁用', await drainVerBtn.isDisabled(), true);
    await shot(page, ledger, 'workers_after_clear_reload');
  } catch (err) {
    ledger.fail('U8 Worker 页只读核对与筛选', err);
  }
  try {
    // 单台排空→恢复的闭环：会真实改动 worker 状态，收尾必须恢复（finally 里兜底）
    const live = (await st.api.get('/workers?limit=100&offset=0')).filter((r) => !r.drain_requested && r.status !== 'offline');
    if (!live.length) ledger.skip('观察项：没有可排空的存活 Worker，跳过单台闭环', '环境里的 Worker 全部带排空标记或离线');
    else {
      const target = live[0];
      await go(page, '/logharvest/workers', { reload: true, waitText: 'Worker（按版本分组）' });
      clearMessages(page);
      await opsCell(target.id, '排空').click();
      const mb = await readMessageBox(page);
      ledger.equal('单台排空确认文案逐字', mb.body, `确认排空 Worker ${target.id}？它跑完当前文件任务后不再领取新任务。`);
      ledger.equal('单台排空标题/按钮', `${mb.title}|${mb.buttons.join('|')}`, '排空 Worker|取消|排空');
      await clickMessageBoxButton(page, '排空');
      const drainMsg = await waitMessage(page, new RegExp(esc(`${target.id}：drain_requested=`)), 12000);
      ledger.equal('排空回执逐字（状态被后端改成 draining）', drainMsg, `${target.id}：drain_requested=true，状态 draining`);
      await waitFor(async () => (await cellsOf(tableRow(page, target.id)))[2] === 'draining', { label: '表格刷新为 draining', timeout: 15000 });
      const drained = await cellsOf(tableRow(page, target.id));
      ledger.equal('排空后 tag 配色变 warning', hasTag(await tagClassOf(tableRow(page, target.id), 'draining'), 'warning'), true);
      ledger.equal('排空后排空标记变「已标记」', drained[9], '已标记');
      ledger.equal('排空后「排空」按钮自我禁用（幂等保护）', await opsCell(target.id, '排空').isDisabled(), true);
      ledger.equal('排空后「恢复」按钮可点（非离线）', await opsCell(target.id, '恢复').isDisabled(), false);
      clearMessages(page);
      await opsCell(target.id, '恢复').click();
      const resumeMsg = await waitMessage(page, /已恢复领取/, 12000);
      ledger.equal('恢复回执逐字', resumeMsg, `${target.id} 已恢复领取（状态 online）`);
      await waitFor(async () => (await cellsOf(tableRow(page, target.id)))[9] === '无', { label: '排空标记回到「无」', timeout: 15000 });
      const restored = (await st.api.get('/workers?limit=100&offset=0')).find((r) => r.id === target.id);
      ledger.check('收尾：环境恢复原状（drain_requested=false 且不是 draining）',
        restored.drain_requested === false && restored.status !== 'draining',
        `${restored.id}:${restored.status}:flag=${restored.drain_requested}`);
      ledger.equal('恢复后「排空」按钮重新可用', await opsCell(target.id, '排空').isDisabled(), false);
    }
  } catch (err) {
    ledger.fail('U8 单台排空/恢复闭环', err);
    try {
      const list = await st.api.get('/workers?limit=100&offset=0');
      for (const r of list.filter((x) => x.status === 'draining')) {
        await st.api.send('POST', `/workers/${r.id}/resume`);
        ledger.check(`异常收尾：恢复被排空的 ${r.id}`, true, '兜底 resume');
      }
    } catch (cleanupErr) {
      ledger.check('异常收尾失败：Worker 可能留在 draining 状态', false, String(cleanupErr).slice(0, 200));
    }
  }
  try {
    // 注入：接口不可用 / 空列表 / 未上报资源 / 未来心跳
    let off = await inject(page, LIST_RE, { __status: 500, __body: {} });
    try {
      clearMessages(page);
      await go(page, '/logharvest/workers', { reload: true });
      await waitFor(async () => (await messagesOf(page)).some((t) => t.includes('Worker 列表加载失败')), { label: '页面兜底文案出现' });
      const msgs = (await messagesOf(page)).map(norm);
      ledger.check('列表 500 且无 detail 时用页面兜底文案', msgs.some((t) => t === 'Worker 列表加载失败'), msgs.join(' | '));
      ledger.check('同一次失败还叠加全局 5xx 文案（两条 toast）', msgs.some((t) => t.includes('服务暂时不可用，请稍后重试')), msgs.join(' | '));
      ledger.skip('观察项：加载失败与「真的没有 Worker」共用同一个空态', '只有 toast 能区分；见 workers/index.vue:330-334 与 92-96 的 rows=[]');
    } finally {
      await off();
    }
    off = await inject(page, LIST_RE, { __status: 500, __body: { detail: 'e2e 注入：Worker 注册表暂时不可读' } });
    try {
      clearMessages(page);
      await go(page, '/logharvest/workers', { reload: true });
      const msg = await waitMessage(page, /e2e 注入：Worker 注册表暂时不可读/, 15000);
      ledger.equal('后端 detail 原样透出（不被兜底文案吞掉）', msg, 'e2e 注入：Worker 注册表暂时不可读');
    } finally {
      await off();
    }
    off = await inject(page, LIST_RE, []);
    try {
      clearMessages(page);
      await go(page, '/logharvest/workers', { reload: true, waitText: '没有可见的 Worker' });
      ledger.equal('空列表文案逐字', norm(await page.locator('.el-empty__description').first().innerText()),
        '没有可见的 Worker：请先启动 Worker 进程（python -m src.worker.main）');
      ledger.equal('空列表时分组为 0', await page.locator('.el-card').count(), 0);
      ledger.check('空列表脚注仍然成句（0 条/0 个版本/存活 0 台）', (await footerText(page)).includes('本页 0 条'), await footerText(page));
    } finally {
      await off();
    }
    // 一台「什么都没上报过 + 心跳在未来」的 Worker：真实环境造不出「0 台上报」，只能注入
    const future = new Date(Date.now() + 3600_000).toISOString().slice(0, 19);
    const synth = {
      id: 'e2e-synth-worker', hostname: 'e2e-synth', version: '9.9.9-synth', status: 'busy',
      drain_requested: false, current_task_id: null, max_concurrent_copies: 2,
      cpu_percent: null, memory_bytes: null, temp_disk_free_bytes: null,
      last_heartbeat: future, started_at: future, last_error: null,
    };
    off = await inject(page, LIST_RE, [synth]);
    try {
      clearMessages(page);
      await go(page, '/logharvest/workers', { reload: true, waitText: 'e2e-synth-worker' });
      const cells = await cellsOf(tableRow(page, 'e2e-synth-worker'));
      ledger.equal('未上报 CPU 显示「—」而不是 0.0%', cells[5], '—');
      ledger.equal('未上报内存显示「—」而不是 0.0 MB', cells[6], '—');
      ledger.equal('未上报临时盘显示「—」而不是 0.00 GB', cells[7], '—');
      ledger.equal('未上报最近错误显示「—」', cells[10], '—');
      ledger.check('脚注出现「至少上报过一项资源的 0 台」这一真实环境拿不到的分支',
        (await footerText(page)).includes('至少上报过一项资源的 0 台'), await footerText(page));
      ledger.check('[产品缺陷] 未来心跳（+1 小时）落进「秒前」分支，显示 -36xx 秒前而不是 -1 小时前',
        /^-\d{4} 秒前$/.test(cells[8]),
        `cell=${cells[8]} 注入 last_heartbeat=${future}（浏览器时钟之后 1 小时）；证据 format.ts:71-74 先判 seconds<60，负数无 Math.max(0,…) 钳位`);
      ledger.equal('busy 状态 tag 为 primary', hasTag(await tagClassOf(tableRow(page, 'e2e-synth-worker'), 'busy'), 'primary'), true);
    } finally {
      await off();
    }
    const expectGroups = workerGroups(await st.api.get('/workers?limit=100&offset=0'), '').length;
    await go(page, '/logharvest/workers', { reload: true, waitText: 'Worker（按版本分组）' });
    await waitFor(async () => (await page.locator('.el-card').count()) === expectGroups, { label: '撤掉注入后真实分组回来自愈' });
    ledger.equal('撤掉注入后分组数回到真实值', await page.locator('.el-card').count(), expectGroups);
    await shot(page, ledger, 'workers_final');
  } catch (err) {
    ledger.fail('U8 Worker 页异常分支（注入）', err);
  }
}


// ---------------------------------------------------------------------------
// U9 任务统计 `#/logharvest/stats`
// ---------------------------------------------------------------------------

/** statusTagType 的镜像（format.ts:81-92）：作业与文件任务共用同一套状态词表。 */
const JOB_TAG = { pending: 'info', running: 'primary', succeeded: 'success', partial: 'warning', failed: 'danger', cancelled: 'warning' };

/** 卡内 5 个快捷跳转（stats/index.vue:94-103 的字面顺序 + 字面文案）。 */
const QUICK_JUMP = [['running', '运行中'], ['succeeded', '成功'], ['partial', '部分成功'], ['failed', '失败'], ['cancelled', '已取消']];

const cardByLabel = (page, label) => page.locator('.el-card')
  .filter({ has: page.locator(`div.text-sm.text-gray-500:text-is("${label}")`) }).first();
const cardValue = (page, label) => cardByLabel(page, label).locator('.text-2xl').first();

/**
 * 指标卡的"数字本身"。有些卡片的 .text-2xl 里还挂着子元素（概览页积压时会多出
 * <el-tag>积压</el-tag>），innerText 会把角标一起读进来变成「7 分 积压」，
 * 于是"数字等于 formatDuration"的断言注定对不上。这里只取直接文本节点。
 */
const cardNumber = async (page, label) => norm(await cardValue(page, label).evaluate((n) =>
  Array.from(n.childNodes).filter((c) => c.nodeType === Node.TEXT_NODE).map((c) => c.textContent).join(' ')));
const cardByHeader = (page, header) => page.locator('.el-card')
  .filter({ has: page.locator(`.el-card__header:text-is("${header}")`) }).first();
const trendHeader = (d) => `每日趋势（${d} 天，条形长度按命中行数归一）`;

/** barPercent 的镜像（stats/index.vue:47-55）：分母取窗口内最大命中行数；有命中至少 2% 宽。 */
const barPercent = (matchedRows, daily) => {
  const max = daily.reduce((m, r) => Math.max(m, r.matched_rows), 1);
  const p = Math.round((matchedRows / max) * 100);
  return matchedRows > 0 ? Math.max(p, 2) : p;
};

/** days>0 且失败率为空时页面显示「未知」（index.vue:38-44：分母 ≤0 → null，不冒充 0%）。 */
const rateMirror = (t) => {
  const done = t.file_succeeded + t.file_failed;
  if (done <= 0) return null;
  return (t.file_failed / done) * 100;
};

/** 每日一行的真实 DOM（探针 e2e/artifacts/probe_snap.mjs 核对过类名，不靠猜）。 */
async function dailyFacts(row) {
  return {
    date: norm(await row.locator('span.w-24').innerText()),
    meta: norm(await row.locator('div.text-sm > span:nth-child(2)').innerText()),
    metaVisible: await row.locator('div.text-sm > span:nth-child(2)').isVisible().catch(() => false),
    tags: (await row.locator('.el-tag').allInnerTexts()).map(norm),
    lines: norm(await row.locator('span.w-20').innerText()),
    progressCls: norm(await row.locator('.el-progress').getAttribute('class').catch(() => '')),
    width: norm(await row.locator('.el-progress-bar__inner').getAttribute('style').catch(() => '')),
  };
}

/** 概览页两张分布卡的行（el-tag + 右侧计数）；必须逐元素读，整行 innerText 会把状态词和数字粘成「succeeded97（82%）」。 */
async function distFacts(card) {
  const out = [];
  for (const row of await card.locator('.mb-2').all()) {
    out.push({
      status: norm(await row.locator('.el-tag').innerText()),
      tagCls: norm(await row.locator('.el-tag').getAttribute('class').catch(() => '')),
      value: norm(await row.locator('div.text-sm > span:last-child').innerText()),
      width: norm(await row.locator('.el-progress-bar__inner').getAttribute('style').catch(() => '')),
    });
  }
  return out;
}

const lastRequest = async (page, re) => norm((await recentRequests(page, re)).at(-1)?.url ?? '');

/**
 * 「等请求真正落地」的守门：统计页的下拉与单选都挂了 `:disabled="loading"`，而 EP 的
 * el-select 在 disabled 时把点击整个吞掉（useSelect.mjs:409 `toggleMenu` 见到 `selectDisabled`
 * 直接 return），所以这时 `openSelect` 的 click 是空操作，表现为 8s 超时。
 * 更阴的是卡片值可能恰好等于上一档的旧值（本栈所有作业都落在同一天，7 天与 14 天的
 * totals 完全相同），"卡片已恢复"的断言会假通过，然后下一步一头撞在 disabled 上。
 */
const waitNoLoading = (page) => waitFor(async () => (
  (await page.locator('.el-loading-mask:visible').count()) === 0
  && (await page.locator('.el-select__wrapper.is-disabled').count()) === 0
  && (await page.locator('.el-radio-button.is-disabled').count()) === 0
    ? true : null), { label: '加载态退场（遮罩消失 + 控件解除 disabled）', timeout: 15000 });

async function phase_stats(ledger, st, { PHASE_LABEL }) {
  ledger.phase(PHASE_LABEL.stats);
  const page = st.adminPage ?? await useSession(ledger, st, 'admin');
  const STATS_RE = /\/api\/stats\/jobs(\?|$)/;
  const DSLIST_RE = /\/api\/data-sources(\?|$)/;
  try {
    const base = await st.api.get('/stats/jobs?days=14');
    const t = base.totals;
    await go(page, '/logharvest/stats', { reload: true, waitText: '任务统计' });

    // ---- 工具条 ----
    const body = await bodyText(page);
    ledger.check('页头灰字逐字（UTC 日界与本地时间可能差一天）',
      body.includes('日期为 UTC 日界，与本地时间可能差一天'), body.slice(0, 160));
    const rbTexts = (await page.locator('.el-radio-button').allInnerTexts()).map(norm);
    ledger.equal('天数只有设计约定的三档 7/14/30 天', JSON.stringify(rbTexts), JSON.stringify(['7 天', '14 天', '30 天']));
    ledger.equal('默认窗口是 14 天（is-active + input:checked）',
      norm(await page.locator('.el-radio-button.is-active').innerText()), '14 天');
    ledger.equal('默认窗口对应的单选框真的处于选中态',
      await page.locator('.el-radio-button.is-active input').isChecked(), true);
    ledger.equal('数据源下拉未选中时占位符「全部数据源」（不过滤）',
      norm(await page.locator('.el-select__placeholder').first().innerText()), '全部数据源');
    const btns = await bodyButtons(page);
    ledger.check('工具条有「刷新」（无新建/删除：统计页只读）', btns.includes('刷新'), [...new Set(btns)].join(','));
    ledger.equal('页面 6 张卡片（4 指标 + 每日趋势 + Top 规则）', await page.locator('.el-card').count(), 6);

    // ---- ① 四张指标卡：页面显示的每个数都必须等于 /api/stats/jobs 现场返回的数 ----
    ledger.equal('「作业数」= totals.jobs', norm(await cardValue(page, '作业数').innerText()), String(t.jobs));
    const jumpTags = page.locator('.el-card').filter({ has: page.locator('div.text-sm.text-gray-500:text-is("作业数")') })
      .first().locator('.mt-2 .el-tag');
    const jumpTexts = (await jumpTags.allInnerTexts()).map(norm);
    ledger.equal('快捷跳转 5 个 tag 的顺序与文案逐字（运行中/成功/部分成功/失败/已取消）',
      JSON.stringify(jumpTexts), JSON.stringify(QUICK_JUMP.map(([k, label]) => `${label} ${t[k]}`)));
    for (const [key, label] of QUICK_JUMP) {
      ledger.equal(`「${label}」tag 配色 = statusTagType('${key}')`,
        hasTag(await tagClassOf(cardByLabel(page, '作业数'), `${label} ${t[key]}`), JOB_TAG[key]), true);
    }
    ledger.equal('「处理字节」= formatGB(bytes_total)', norm(await cardValue(page, '处理字节').innerText()), fmtGB(t.bytes_total));
    ledger.check('「处理字节」副行「文件任务共 N 个」',
      (await bodyText(page)).includes(`文件任务共 ${t.files_total} 个`), `files_total=${t.files_total}`);
    ledger.equal('「命中行数」= totals.matched_rows_total', norm(await cardValue(page, '命中行数').innerText()), String(t.matched_rows_total));
    const rate = rateMirror(t);
    const rateText = rate === null ? '未知' : `${rate.toFixed(1)}%`;
    ledger.equal('「文件失败率」= files_failed/(succeeded+failed) 的一位小数', norm(await cardValue(page, '文件失败率').innerText()), rateText);
    ledger.check('失败率 ≤10% 用绿色、>10% 用红色（配色承载"要不要紧张"的判断）',
      rate !== null && (rate > 10 ? /text-red-600/.test(norm(await cardValue(page, '文件失败率').innerHTML())) : /text-green-700/.test(norm(await cardValue(page, '文件失败率').innerHTML()))),
      `rate=${rate} html=${norm(await cardValue(page, '文件失败率').innerHTML())}`);
    ledger.check('「文件失败率」副行「成功 A / 失败 B」',
      (await bodyText(page)).includes(`成功 ${t.file_succeeded} / 失败 ${t.file_failed}`), `s=${t.file_succeeded} f=${t.file_failed}`);

    // ---- ② 每日趋势：逐行对账 + 条形宽度 = barPercent ----
    const trend = cardByHeader(page, trendHeader(14));
    ledger.equal('趋势卡存在（标题含窗口天数）', await trend.count(), 1);
    const rows = trend.locator('.daily-row');
    ledger.equal('每日行数与 API daily 长度一致', await rows.count(), base.daily.length);
    for (let i = 0; i < base.daily.length; i += 1) {
      const d = base.daily[i];
      const f = await dailyFacts(rows.nth(i));
      ledger.equal(`第 ${i + 1} 行日期逐字`, f.date, d.date);
      ledger.equal(`第 ${i + 1} 行副信息「作业 N · 字节 X GB」`, f.meta, `作业 ${d.jobs_created} · 字节 ${fmtGB(d.bytes_total)}`);
      ledger.equal(`第 ${i + 1} 行命中行数「N 行」`, f.lines, `${d.matched_rows} 行`);
      const wantTags = [];
      if (d.files_succeeded > 0) wantTags.push(`成 ${d.files_succeeded}`);
      if (d.files_failed > 0) wantTags.push(`败 ${d.files_failed}`);
      ledger.equal(`第 ${i + 1} 行成/败 tag（0 不渲染，避免"成 0"读成好消息）`, JSON.stringify(f.tags), JSON.stringify(wantTags));
      ledger.check(`第 ${i + 1} 行条形配色随失败数切换（有失败=warning，全成功=success）`,
        f.progressCls.includes(d.files_failed > 0 ? 'is-warning' : 'is-success'), f.progressCls);
      ledger.check(`第 ${i + 1} 行条形宽度 = barPercent(${d.matched_rows})`,
        f.width.includes(`width: ${barPercent(d.matched_rows, base.daily)}%`), `${f.width} 期望 ${barPercent(d.matched_rows, base.daily)}%`);
    }
    ledger.check('趋势脚注逐字（可点击的两个出口）',
      (await bodyText(page)).includes('点击某一行跳「任务创建」列表并带上当日条件；点「成/败」标签额外带上状态条件。'), '');

    // ---- ③ Top 规则 ----
    const top = cardByHeader(page, 'Top 规则（按命中行数）');
    ledger.equal('Top 规则表头（宽屏四列，含规则版本）',
      JSON.stringify(await tableHeaderTexts(top)), JSON.stringify(['规则 ID', '规则版本', '命中行数', '涉及作业']));
    ledger.equal('Top 规则行数不超过后端 TOP_RULES_LIMIT=10', await top.locator('.el-table__body .el-table__row').count(), Math.min(base.top_rules.length, 10));
    for (let i = 0; i < Math.min(3, base.top_rules.length); i += 1) {
      const r = base.top_rules[i];
      ledger.equal(`Top 第 ${i + 1} 行四列逐字（顺序也必须是后端给的命中数倒序）`,
        JSON.stringify(await cellsOf(tableRow(page, '', { index: i }))),
        JSON.stringify([String(r.rule_id), String(r.rule_version), String(r.hits), String(r.jobs)]));
    }
    // ---- ④ 窗口切换：radio → 请求参数 → 卡片与趋势标题同步 ----
    const dsList = await st.api.get('/data-sources?limit=50');
    const d7 = await st.api.get('/stats/jobs?days=7');
    clearRequests(page);
    await page.locator('.el-radio-button', { hasText: '7 天' }).click();
    await waitFor(async () => ((await recentRequests(page, STATS_RE)).length >= 1 ? true : null), { label: '切到 7 天后发出请求' });
    ledger.check('点「7 天」真的把 days=7 发给后端（不是纯前端切片）', /days=7(&|$)/.test(await lastRequest(page, STATS_RE)), await lastRequest(page, STATS_RE));
    await waitFor(async () => ((await cardByHeader(page, trendHeader(7)).count()) === 1 ? true : null), { label: '趋势卡标题改成 7 天' });
    ledger.equal('切换窗口后趋势卡标题跟着变', await cardByHeader(page, trendHeader(7)).count(), 1);
    ledger.equal('切换窗口后旧标题消失（同一时刻只存在一个窗口）', await cardByHeader(page, trendHeader(14)).count(), 0);
    await waitFor(async () => ((await cardValue(page, '作业数').innerText()).trim() === String(d7.totals.jobs) ? true : null), { label: '7 天窗口数据落地' });
    ledger.equal('7 天窗口的「作业数」= /stats/jobs?days=7 的 totals.jobs', norm(await cardValue(page, '作业数').innerText()), String(d7.totals.jobs));
    ledger.equal('一次 radio 切换只发一次统计请求（没有重复请求风暴）', (await recentRequests(page, STATS_RE)).length, 1);
    clearRequests(page);
    await page.locator('.el-radio-button', { hasText: '14 天' }).click();
    await waitFor(async () => ((await recentRequests(page, STATS_RE)).length >= 1 ? true : null), { label: '切回 14 天' });
    await waitFor(async () => ((await cardValue(page, '作业数').innerText()).trim() === String(t.jobs) ? true : null), { label: '14 天基线回来' });
    ledger.equal('切回 14 天后卡片恢复基线值', norm(await cardValue(page, '作业数').innerText()), String(t.jobs));

    // ---- ⑤ 数据源过滤：选中 → 带参数 → 卡片等于该源的统计 ----
    await waitNoLoading(page);
    const dsOptions = await selectOptionsOf(page, '全部数据源');
    ledger.equal('下拉里的数据源与 /api/data-sources 同名同数',
      JSON.stringify(dsOptions.slice().sort()), JSON.stringify(dsList.map((d) => d.name).sort()));
    const perDs = [];
    for (const d of dsList) {
      const s = await st.api.get(`/stats/jobs?days=14&data_source_id=${d.id}`);
      perDs.push({ ds: d, stats: s });
    }
    const withJobs = perDs.filter((x) => x.stats.totals.jobs > 0);
    const zeroDs = perDs.find((x) => x.stats.totals.jobs === 0);
    if (withJobs.length) {
      const pick = withJobs[0];
      clearRequests(page);
      await waitNoLoading(page);
      await pickOption(page, '全部数据源', pick.ds.name, { exact: true });
      await waitFor(async () => (/data_source_id=/.test(await lastRequest(page, STATS_RE)) ? true : null), { label: '带上 data_source_id 的请求发出' });
      ledger.check('选中数据源后请求真的带 data_source_id',
        new RegExp(`data_source_id=${pick.ds.id}(&|$)`).test(await lastRequest(page, STATS_RE)), await lastRequest(page, STATS_RE));
      await waitFor(async () => ((await cardValue(page, '作业数').innerText()).trim() === String(pick.stats.totals.jobs) ? true : null), { label: `${pick.ds.name} 的统计落地` });
      ledger.equal(`过滤到「${pick.ds.name}」后「作业数」= 该源的 totals.jobs`, norm(await cardValue(page, '作业数').innerText()), String(pick.stats.totals.jobs));
      ledger.equal(`过滤到「${pick.ds.name}」后「命中行数」= 该源的 matched_rows_total`, norm(await cardValue(page, '命中行数').innerText()), String(pick.stats.totals.matched_rows_total));
      // 清空筛选：clearable 的 el-select 清空后模型是 undefined，请求不能再带 data_source_id
      clearRequests(page);
      await clearSelect(page, pick.ds.name);
      await waitFor(async () => ((await recentRequests(page, STATS_RE)).length >= 1 ? true : null), { label: '清空后重新请求' });
      const cleared = await lastRequest(page, STATS_RE);
      ledger.check('清空数据源筛选后请求不再携带 data_source_id（回到"不过滤"）', !/data_source_id=/.test(cleared), cleared);
      await waitFor(async () => ((await cardValue(page, '作业数').innerText()).trim() === String(t.jobs) ? true : null), { label: '全量基线回来' });
      ledger.equal('清空筛选后卡片回到全量基线', norm(await cardValue(page, '作业数').innerText()), String(t.jobs));
    } else {
      ledger.skip('数据源过滤（选中/清空）', `没有带作业的可用数据源：${JSON.stringify(perDs.map((x) => [x.ds.name, x.stats.totals.jobs]))}`);
    }

    // ---- ⑥ 零作业窗口：整页切到空态文案（有真源就用真源，没有就注入合成分支） ----
    const zeroExpect = {
      jobs: '0', bytes: fmtGB(0), lines: '0', rate: '未知',
      trendEmpty: '该时间段没有作业', topEmpty: '该时间段没有命中',
    };
    let offZero = null;
    const zeroViaInject = !zeroDs;
    if (zeroViaInject) {
      offZero = await inject(page, STATS_RE, {
        days: 14, data_source_id: null,
        totals: { jobs: 0, running: 0, succeeded: 0, partial: 0, failed: 0, cancelled: 0, files_total: 0, bytes_total: 0, matched_rows_total: 0, file_succeeded: 0, file_failed: 0 },
        daily: [], top_rules: [],
      });
    }
    try {
      if (zeroDs) { await waitNoLoading(page); await pickOption(page, '全部数据源', zeroDs.ds.name, { exact: true }); }
      else await go(page, '/logharvest/stats', { reload: true, waitText: '任务统计' });
      await waitFor(async () => ((await cardValue(page, '作业数').innerText()).trim() === zeroExpect.jobs ? true : null), { label: '零作业统计落地' });
      ledger.equal('零作业时「作业数」显示 0', norm(await cardValue(page, '作业数').innerText()), zeroExpect.jobs);
      ledger.equal('零作业时不给 5 个快捷跳转 tag（jobs>0 才渲染）', await cardByLabel(page, '作业数').locator('.mt-2 .el-tag').count(), 0);
      ledger.equal('零作业时「处理字节」是 0.00 GB（真实零，不是未知的「—」）', norm(await cardValue(page, '处理字节').innerText()), zeroExpect.bytes);
      ledger.equal('零作业时「命中行数」显示 0', norm(await cardValue(page, '命中行数').innerText()), zeroExpect.lines);
      ledger.equal('失败率分母为 0 时显示「未知」而不是 0.0%（不冒充好消息）', norm(await cardValue(page, '文件失败率').innerText()), zeroExpect.rate);
      ledger.check('「未知」用 text-gray-400 弱化（与真实数字区分）',
        /text-gray-400/.test(norm(await cardValue(page, '文件失败率').innerHTML())), norm(await cardValue(page, '文件失败率').innerHTML()));
      ledger.equal('趋势卡空态文案「该时间段没有作业」', await page.locator('.el-empty__description:visible').filter({ hasText: '该时间段没有作业' }).count(), 1);
      ledger.equal('Top 规则空态文案「该时间段没有命中」', await page.locator('.el-empty__description:visible').filter({ hasText: '该时间段没有命中' }).count(), 1);
      ledger.equal('空态下 Top 规则表格整体不渲染（v-if 让位给 el-empty）', await page.locator('.el-table').count(), 0);
    } finally {
      if (offZero) await offZero();
    }
    if (zeroViaInject) {
      await go(page, '/logharvest/stats', { reload: true, waitText: '任务统计' });
      await waitFor(async () => ((await cardValue(page, '作业数').innerText()).trim() === String(t.jobs) ? true : null), { label: '撤掉零作业注入后回真实值' });
    } else if (zeroDs) {
      await clearSelect(page, zeroDs.ds.name);
      await waitFor(async () => ((await cardValue(page, '作业数').innerText()).trim() === String(t.jobs) ? true : null), { label: '撤掉零作业筛选后回真实值' });
    }
    ledger.equal('撤掉零作业分支后回到真实基线（注入没有残留）', norm(await cardValue(page, '作业数').innerText()), String(t.jobs));

    // ---- ⑦ 刷新按钮 = 恰好一次请求 ----
    clearRequests(page);
    await page.getByRole('button', { name: '刷新' }).click();
    await waitFor(async () => ((await recentRequests(page, STATS_RE)).length >= 1 ? true : null), { label: '刷新发出请求' });
    await page.waitForTimeout(600);
    ledger.equal('点一次「刷新」只发一次统计请求', (await recentRequests(page, STATS_RE)).length, 1);
    // ---- ⑧ 统计接口 500：未知用「—」，绝不显示 0；页面与全局拦截器各说各话 ----
    let off500 = null;
    try {
      clearMessages(page);
      off500 = await inject(page, STATS_RE, { __status: 500 });
      await go(page, '/logharvest/stats', { reload: true, waitText: '任务统计' });
      const msgs = await waitMessage(page, /任务统计加载失败/).then(() => messagesOf(page)).catch(() => messagesOf(page));
      ledger.check('500 时 axios 拦截器给全局提示「服务暂时不可用，请稍后重试」',
        msgs.some((m) => m.includes('服务暂时不可用，请稍后重试')), JSON.stringify(msgs));
      ledger.check('页面自己给业务提示「任务统计加载失败」（带接口失败的原因，不只是"没数据"）',
        msgs.some((m) => m.includes('任务统计加载失败')), JSON.stringify(msgs));
      ledger.equal('加载失败后「作业数」显示未知「—」而不是 0', norm(await cardValue(page, '作业数').innerText()), '—');
      ledger.equal('加载失败后「处理字节」显示「—」', norm(await cardValue(page, '处理字节').innerText()), '—');
      ledger.equal('加载失败后「命中行数」显示「—」', norm(await cardValue(page, '命中行数').innerText()), '—');
      ledger.equal('加载失败后失败率显示「未知」', norm(await cardValue(page, '文件失败率').innerText()), '未知');
      ledger.equal('数据缺失时不给快捷跳转 tag（不给可点击的假入口）', await cardByLabel(page, '作业数').locator('.mt-2 .el-tag').count(), 0);
      ledger.equal('数据缺失时副行「文件任务共 N 个」整体消失而不是写 0',
        (await bodyText(page)).includes('文件任务共'), false);
      ledger.equal('趋势与 Top 规则各自落到空态', await page.locator('.el-empty:visible').count(), 2);
    } finally {
      if (off500) await off500();
    }
    await go(page, '/logharvest/stats', { reload: true, waitText: '任务统计' });
    await waitFor(async () => ((await cardValue(page, '作业数').innerText()).trim() === String(t.jobs) ? true : null), { label: '撤掉 500 注入后自愈' });
    ledger.equal('撤掉 500 注入后卡片自愈回真实值', norm(await cardValue(page, '作业数').innerText()), String(t.jobs));

    // ---- ⑨ 403：整页降级为 el-result，卡片一个都不留，也不误报"服务不可用" ----
    let off403 = null;
    try {
      clearMessages(page);
      off403 = await inject(page, STATS_RE, { __status: 403, __body: { detail: '无权访问' } });
      await go(page, '/logharvest/stats', { reload: true, waitText: '无查看权限' });
      ledger.equal('403 时整页只有一块 el-result', await page.locator('.el-result').count(), 1);
      ledger.equal('result 主标题逐字', norm(await page.locator('.el-result__title').innerText()), '无查看权限');
      ledger.equal('result 副标题逐字（说清缺哪个权限、怎么解决）',
        norm(await page.locator('.el-result__subtitle').innerText()),
        '任务统计需要 jobs:read 权限（接口返回 403），请联系管理员分配后重试。');
      ledger.equal('降级后 6 张卡片整体不渲染（不给残缺看板）', await page.locator('.el-card').count(), 0);
      const m403 = await messagesOf(page);
      ledger.check('403 属业务结果：不弹「服务暂时不可用」（拦截器只兜 5xx/断网）',
        !m403.some((m) => m.includes('服务暂时不可用')), JSON.stringify(m403));
      ledger.check('403 也不弹「任务统计加载失败」（避免把权限问题说成故障）',
        !m403.some((m) => m.includes('任务统计加载失败')), JSON.stringify(m403));
    } finally {
      if (off403) await off403();
    }

    // ---- ⑩ 数据源下拉单独失败：统计不受影响（两条加载链路互不绑架） ----
    let offDs = null;
    try {
      clearMessages(page);
      offDs = await inject(page, DSLIST_RE, { __status: 500 });
      await go(page, '/logharvest/stats', { reload: true, waitText: '任务统计' });
      const mds = await waitMessage(page, /数据源下拉加载失败/).then(() => messagesOf(page)).catch(() => messagesOf(page));
      ledger.check('下拉失败时提示「数据源下拉加载失败，统计不受影响」',
        mds.some((m) => m.includes('数据源下拉加载失败，统计不受影响')), JSON.stringify(mds));
      await waitFor(async () => ((await cardValue(page, '作业数').innerText()).trim() === String(t.jobs) ? true : null), { label: '统计不受下拉失败影响' });
      ledger.equal('下拉失败但统计照常显示（作业数仍是真实值）', norm(await cardValue(page, '作业数').innerText()), String(t.jobs));
      ledger.equal('下拉退回占位符「全部数据源」（不过滤）',
        norm(await page.locator('.el-select__placeholder').first().innerText()), '全部数据源');
    } finally {
      if (offDs) await offDs();
    }
    await go(page, '/logharvest/stats', { reload: true, waitText: '任务统计' });
    await waitNoLoading(page);
    await waitFor(async () => ((await selectOptionsOf(page, '全部数据源')).length === dsList.length ? true : null), { label: '下拉重新拿到数据源' });
    ledger.equal('撤掉注入后下拉重新有全部数据源', (await selectOptionsOf(page, '全部数据源')).length, dsList.length);
    // ---- ⑪ 窄屏（≤760 触发 device=mobile）：列与副信息按设计退让 ----
    await page.setViewportSize({ width: 390, height: 812 });
    await waitFor(async () => ((await page.locator('.el-table__header th:visible').count()) === 3 ? true : null), { timeout: 8000, label: '窄屏下 Top 规则列数变化' }).catch(() => null);
    const narrowHeaders = (await tableHeaderTexts(page)).filter(Boolean);
    ledger.equal('窄屏下 Top 规则去掉「规则版本」（只留 3 列，窄屏挤不下也要保住主键与命中数）',
      JSON.stringify(narrowHeaders), JSON.stringify(['规则 ID', '命中行数', '涉及作业']));
    const narrowRow = await dailyFacts(page.locator('.daily-row').first());
    ledger.check('窄屏下每日行的「作业 N · 字节 X」副信息隐藏（hidden sm:inline，行数与成/败仍保留）',
      narrowRow.metaVisible === false && /\d+ 行/.test(narrowRow.lines), `metaVisible=${narrowRow.metaVisible} lines=${narrowRow.lines}`);
    await page.setViewportSize({ width: 1440, height: 900 });
    await waitFor(async () => ((await tableHeaderTexts(page)).length === 4 ? true : null), { label: '回到宽屏恢复四列' });
    ledger.equal('回到宽屏后「规则版本」列回来',
      JSON.stringify(await tableHeaderTexts(page)), JSON.stringify(['规则 ID', '规则版本', '命中行数', '涉及作业']));

    // ---- ⑫ 跳转：点日期行 / 点状态 tag，条件要真传到列表页 ----
    const day = base.daily[0]?.date;
    if (!day) {
      ledger.skip('日期行跳转', '本轮窗口内没有 daily 行可点');
    } else {
      clearRequests(page);
      await page.locator('.daily-row').first().click();
      // hash 路由跳转是异步的（click 只负责派发事件），先等一下再读 URL，否则这里会拿到上一页。
      await page.waitForURL(/#\/logharvest\/jobs/, { timeout: 15000 }).catch(() => null);
      ledger.check('点日期行跳到任务列表并带上 date=<UTC 日>',
        new RegExp(`#/logharvest/jobs\\?date=${esc(day)}`).test(page.url()), page.url());
      await waitFor(async () => ((await page.locator(`.el-tag:has-text("UTC 日：${day}")`).count()) >= 1 ? true : null), { label: '列表页出现 UTC 日提示' });
      ledger.equal('列表页出现可清除的「UTC 日：<date>」提示 tag',
        await page.locator(`.el-tag:has-text("UTC 日：${day}")`).count(), 1);
      const dayReq = await lastRequest(page, /\/api\/jobs(\?|$)/);
      ledger.check('[产品缺陷] 日期条件只在前端过滤，没进后端查询参数（跨页时列表会漏行）',
        !/[?&]date=/.test(dayReq),
        `请求=${dayReq || '(无)'}；证据 jobs/index.vue:59 用 utcDay(row.created_at) 本地过滤、:76 只把 status 拼进 params，date 只落在 URL 提示（:198）。当前窗口只有一天数据所以看不出错，多天时第 2 页起就会漏。`);
      clearRequests(page);
      await page.locator('.el-tag:has-text("UTC 日：") .el-tag__close').first().click();
      await waitFor(async () => ((await page.locator('.el-tag:has-text("UTC 日：")').count()) === 0 ? true : null), { label: '点 × 后提示消失' });
      ledger.check('点提示 tag 的 × 后 tag 消失且 URL 里的 date 同步抹掉（刷新不会又回来）',
        !/[?&]date=/.test(page.url()), page.url());
    }

    await go(page, '/logharvest/stats', { reload: true, waitText: '任务统计' });
    await waitFor(async () => ((await cardValue(page, '作业数').innerText()).trim() === String(t.jobs) ? true : null), { label: '回到统计页' });
    clearRequests(page);
    await cardByLabel(page, '作业数').locator('.mt-2 .el-tag').nth(1).click();
    await page.waitForURL(/#\/logharvest\/jobs\?/, { timeout: 15000 }).catch(() => null);
    ledger.check('点「成功」tag 带上 status=succeeded（用后端状态原词，不做二次翻译）',
      /status=succeeded/.test(page.url()), page.url());
    await waitFor(async () => (/status=succeeded/.test(await lastRequest(page, /\/api\/jobs(\?|$)/)) ? true : null), { label: '状态条件进后端查询' });
    const okReq = await lastRequest(page, /\/api\/jobs(\?|$)/);
    ledger.check('状态条件确实进后端（/api/jobs?status=succeeded，由后端过滤而非翻页后再筛）',
      /[?&]status=succeeded(&|$)/.test(okReq), okReq);
    await waitFor(async () => ((await page.locator('.el-select').filter({ hasText: 'succeeded' }).count()) >= 1 ? true : null), { label: '状态下拉回填 succeeded' });
    ledger.check('列表页状态下拉回填成 succeeded（用户看得见"我现在被筛的是什么"）',
      await page.locator('.el-select').filter({ hasText: 'succeeded' }).count() >= 1, await bodyText(page).then((b) => b.slice(0, 200)));
    const statusCells = (await page.locator('.el-table__body .el-table__row').allInnerTexts()).map(norm);
    ledger.check('筛出的每一行状态都是 succeeded（后端过滤生效，没有混进别的状态）',
      statusCells.length > 0 && statusCells.every((c) => c.includes('succeeded')), `${statusCells.length} 行，首行=${statusCells[0] ?? ''}`);

    await go(page, '/logharvest/stats', { reload: true, waitText: '任务统计' });
    await waitFor(async () => ((await cardValue(page, '作业数').innerText()).trim() === String(t.jobs) ? true : null), { label: '再次回到统计页' });
    clearRequests(page);
    await cardByLabel(page, '作业数').locator('.mt-2 .el-tag').nth(0).click();
    await page.waitForURL(/#\/logharvest\/jobs\?/, { timeout: 15000 }).catch(() => null);
    ledger.check('点「运行中 0」也照样跳转并带上 status=running（0 个状态也是可查的事实）',
      /status=running/.test(page.url()), page.url());
    await waitFor(async () => ((await page.locator('.el-select').filter({ hasText: 'running' }).count()) >= 1 ? true : null), { label: '状态下拉回填 running' });
    await waitFor(async () => ((await page.locator('.el-table__body .el-table__row').count()) === 0 ? true : null), { timeout: 10000, label: 'running 过滤后零行' });
    ledger.equal('running 过滤后表格零行（与统计页「运行中 0」一致，两处不打架）', await page.locator('.el-table__body .el-table__row').count(), 0);
    ledger.check('零行时给出表格空态文案而不是白屏',
      (await page.locator('.el-table__empty-text, .el-empty').count()) >= 1,
      await page.locator('.el-table__empty-text').allInnerTexts().then((a) => a.map(norm).join(',')));
    await shot(page, ledger, 'stats_final');
  } catch (err) {
    ledger.fail('U9 统计页异常分支（注入）', err);
  }
}

// ---------------------------------------------------------------------------
// U14 运行概览 `#/logharvest/overview`
// ---------------------------------------------------------------------------

/** versionRows 的镜像（overview/index.vue:64-74）：按台数倒序，分母是版本分布合计（不是存活数）。 */
function versionRows(byVersion) {
  const total = sumOf(byVersion) || 1;
  return Object.entries(byVersion || {})
    .sort((a, b) => b[1] - a[1])
    .map(([version, count]) => ({ version, count, percent: Math.round((count / total) * 100) }));
}

async function phase_overview(ledger, st, { PHASE_LABEL }) {
  ledger.phase(PHASE_LABEL.overview);
  const page = st.adminPage ?? await useSession(ledger, st, 'admin');
  const OVERVIEW_RE = /\/api\/system\/overview(\?|$)/;
  const STATUS_RE = /\/api\/system\/status(\?|$)/;
  const MAINT_RE = /\/api\/system\/maintenance/;
  const PENDING_WARN_SECONDS = 300;
  const ovTag = (s) => JOB_TAG[s] ?? 'info';
  try {
    const ov = await st.api.get('/system/overview');
    const stt = await st.api.get('/system/status');
    const jobTotal = sumOf(ov.jobs);
    const fileTotal = sumOf(ov.file_tasks);
    await go(page, '/logharvest/overview', { reload: true, waitText: '运行概览' });

    // ---- ① 页头与时间戳：数据是什么时候的，用户必须看得见 ----
    const body = await bodyText(page);
    ledger.check('右上角显示「数据截至 HH:mm:ss」（概览不自动轮询，必须给新鲜度）',
      /数据截至 \d{1,2}:\d{2}:\d{2}/.test(body), body.match(/数据截至[^\s]*\s*\S{0,10}/)?.[0] ?? body.slice(0, 140));
    ledger.check('加载完成后不再显示「加载中…」', !body.includes('加载中…'), body.slice(0, 120));
    ledger.equal('页面共 7 张卡片（4 指标 + 3 分布）', await page.locator('.el-card').count(), 7);

    // ---- ② 四张指标卡：每个数都等于 /api/system/overview 现场返回的数 ----
    ledger.equal('「作业总数」= Σ overview.jobs', norm(await cardValue(page, '作业总数').innerText()), String(jobTotal));
    ledger.equal('「24 小时命中行数」= matches_24h', norm(await cardValue(page, '24 小时命中行数').innerText()), String(ov.matches_24h));
    ledger.equal('「存活 Worker」= workers.online', norm(await cardValue(page, '存活 Worker').innerText()), String(ov.workers.online));
    const pendingWarn = (ov.oldest_pending_seconds ?? 0) > PENDING_WARN_SECONDS;
    const pendingText = await cardNumber(page, '最老 pending 等待');
    ledger.equal('「最老 pending 等待」= formatDuration(oldest_pending_seconds)', pendingText, fmtDuration(ov.oldest_pending_seconds));
    ledger.check('积压告警是严格">300 秒"才亮（当前值不亮就不该亮）',
      (await cardValue(page, '最老 pending 等待').locator('.el-tag').count()) === (pendingWarn ? 1 : 0),
      `oldest_pending_seconds=${ov.oldest_pending_seconds} 文案=${pendingText}`);

    // ---- ③ 两张分布卡：顺序/配色/百分比都由前端算出来，逐行核对 ----
    const jobRows = await distFacts(cardByHeader(page, '作业状态分布'));
    const wantJob = distribution(ov.jobs, jobTotal);
    ledger.equal('作业分布行数 = 非零状态数（0 不给行，避免"pending 0（0%）"的假安心）', jobRows.length, wantJob.length);
    for (let i = 0; i < wantJob.length; i += 1) {
      const w = wantJob[i];
      const g = jobRows[i] ?? {};
      ledger.equal(`作业分布第 ${i + 1} 行状态与顺序（按 JOB_STATUS_OPTIONS 定序）`, g.status, w.status);
      ledger.equal(`作业分布第 ${i + 1} 行「N（P%）」`, g.value, `${w.count}（${w.percent}%）`);
      ledger.equal(`作业分布第 ${i + 1} 行配色 = statusTagType`, hasTag(g.tagCls ?? '', ovTag(w.status)), true);
      ledger.check(`作业分布第 ${i + 1} 行进度条宽度 = ${w.percent}%`, (g.width ?? '').includes(`width: ${w.percent}%`), g.width);
    }
    ledger.check('作业分布脚注「共 N 个作业」= 分母', body.includes(`共 ${jobTotal} 个作业`), `jobTotal=${jobTotal}`);
    ledger.check('各行百分比取整后允许合计 ±2（Math.round 的固有误差，不假装能凑满 100）',
      Math.abs(wantJob.reduce((s, w) => s + w.percent, 0) - 100) <= 2,
      `合计=${wantJob.reduce((s, w) => s + w.percent, 0)}`);

    const fileRows = await distFacts(cardByHeader(page, '文件任务状态分布'));
    const wantFile = distribution(ov.file_tasks, fileTotal);
    ledger.equal('文件任务分布行数 = 非零状态数', fileRows.length, wantFile.length);
    for (let i = 0; i < wantFile.length; i += 1) {
      const w = wantFile[i];
      const g = fileRows[i] ?? {};
      ledger.equal(`文件分布第 ${i + 1} 行状态与顺序`, g.status, w.status);
      ledger.equal(`文件分布第 ${i + 1} 行「N（P%）」`, g.value, `${w.count}（${w.percent}%）`);
      ledger.equal(`文件分布第 ${i + 1} 行配色`, hasTag(g.tagCls ?? '', ovTag(w.status)), true);
    }
    ledger.check('文件分布脚注「共 N 个文件任务」= 分母', body.includes(`共 ${fileTotal} 个文件任务`), `fileTotal=${fileTotal}`);

    // ---- ④ Worker 版本分布：滚动升级时靠它确认旧版本是否清零 ----
    const vRows = versionRows(ov.workers.by_version);
    const vTags = (await page.locator('.el-tag--large').allInnerTexts()).map(norm);
    ledger.equal('版本 tag 数与 by_version 键数一致', vTags.length, vRows.length);
    ledger.equal('版本 tag 逐字（台数倒序 + 占比）',
      JSON.stringify(vTags), JSON.stringify(vRows.map((r) => `${r.version}：${r.count} 台（${r.percent}%）`)));
    ledger.check('口径说明逐字（含"按版本排空"的升级入口指引）',
      (await bodyText(page)).includes('存活口径与后端一致：`online|busy|draining` 都算存活，`offline` 不计。')
        && (await bodyText(page)).includes('滚动升级请到「Worker 管理」用“按版本排空”'), '');
    const vSum = vRows.reduce((s, r) => s + r.count, 0);
    if (vSum !== ov.workers.online) {
      ledger.check('[产品缺陷] 版本分布合计（含 offline 行）与「存活 Worker」卡片不等，脚注却写着「offline 不计」',
        true, `版本分布合计=${vSum} 存活卡片=${ov.workers.online}；证据 queries.py:172-177 用 _worker_online_count（online/busy/draining）算 online，by_version 含 offline（schemas.py:226-230 注释），前端 overview/index.vue:64-74 直接把 by_version 全量画出来却在 :252 声称"offline 不计"。要么在版本分布里剔掉 offline，要么把脚注改成"含 offline"。`);
    } else {
      ledger.equal('本轮 by_version 合计恰等于存活数（offline 行为 0，缺陷未显现）', vSum, ov.workers.online);
    }

    // ---- ⑤ admin 才有「执行维护」（system:operate） ----
    const adminBtns = await bodyButtons(page);
    ledger.check('admin 看到「刷新」+「执行维护」', adminBtns.includes('刷新') && adminBtns.includes('执行维护'), [...new Set(adminBtns)].join(','));
    const vpage = st.viewerPage ?? await useSession(ledger, st, 'viewer');
    await go(vpage, '/logharvest/overview', { reload: true, waitText: '运行概览' });
    const viewerBtns = await bodyButtons(vpage);
    ledger.check('viewer 只有 SYSTEM_READ：整页可读但「执行维护」被 v-perms 摘掉（不是禁用）',
      !viewerBtns.includes('执行维护') && viewerBtns.includes('刷新'), [...new Set(viewerBtns)].join(','));
    ledger.equal('viewer 页不出现 el-result 降级（读权限够用）', await vpage.locator('.el-result').count(), 0);
    ledger.equal('viewer 与 admin 读到同一个「作业总数」（只读口径不分叉）',
      norm(await cardValue(vpage, '作业总数').innerText()), String(jobTotal));
    // ---- ⑥ 执行维护：取消必须先于执行，三个数逐字对着后端响应体 ----
    await clearMessages(page);
    clearRequests(page);
    await page.locator('button:has-text("执行维护")').first().click();
    const mBox = await readMessageBox(page);
    ledger.equal('维护确认框标题', mBox.title, '执行维护');
    ledger.equal('确认框把这次会动哪三件事说全（回收/降级/收尾），不是含糊的「确定吗？」', mBox.body,
      '执行一次维护：回收心跳超时的文件任务、把失联 Worker 记为离线、收尾停滞的作业。确认继续？');
    ledger.equal('按钮逐字 = 取消 + 执行', JSON.stringify(mBox.buttons), JSON.stringify(['取消', '执行']));
    await clickMessageBoxButton(page, '取消');
    await page.waitForTimeout(400);
    ledger.equal('点「取消」绝不发 POST（confirm 被 reject 就 return）',
      (await recentRequests(page, MAINT_RE)).filter((r) => r.method === 'POST').length, 0);
    ledger.equal('取消后数据没被动过（作业总数仍是基线）',
      norm(await cardValue(page, '作业总数').innerText()), String(jobTotal));

    const maintCap = await captureResponses(page, MAINT_RE);
    const ovGetsBefore = (await recentRequests(page, OVERVIEW_RE)).filter((r) => r.method === 'GET').length;
    await page.locator('button:has-text("执行维护")').first().click();
    await readMessageBox(page);
    await clickMessageBoxButton(page, '执行');
    const maintMsg = await waitMessage(page, /维护完成/);
    const isToastSuccess = (await page.locator('.el-message--success').count()) >= 1;
    const maintResp = (await maintCap.stop()).filter((r) => r.body).pop() ?? { status: 0, body: {} };
    const mnum = maintResp.body ?? {};
    ledger.equal('维护只发一次 POST（点两下不会重复清扫）',
      (await recentRequests(page, MAINT_RE)).filter((r) => r.method === 'POST').length, 1);
    ledger.equal('提示里的三个数逐字等于后端响应体（前端不自己编数）', norm(maintMsg),
      `维护完成：回收 ${mnum.recovered} 个文件任务，降级 ${mnum.workers_demoted} 台 Worker，收尾 ${mnum.jobs_finalized} 个作业`);
    ledger.check('成功用 success 型提示（维护完成不该弹成红色错误）', isToastSuccess, JSON.stringify(await messagesOf(page)));
    let ovGetsAfter = ovGetsBefore;
    await waitFor(async () => {
      ovGetsAfter = (await recentRequests(page, OVERVIEW_RE)).filter((r) => r.method === 'GET').length;
      return ovGetsAfter > ovGetsBefore ? true : null;
    }, { label: '维护后自动重拉概览' });
    ledger.equal('维护完自动重拉概览，且只重拉一次（旧快照不会继续挂着，也不连环刷）',
      ovGetsAfter, ovGetsBefore + 1);

    const successAlert = page.locator('.el-alert--success');
    await waitFor(async () => ((await successAlert.count()) === 1 ? true : null), { label: '「上次维护」绿条出现' });
    ledger.equal('「上次维护」标题同样逐字复述响应体', norm(await successAlert.locator('.el-alert__title').innerText()),
      `上次维护：回收 ${mnum.recovered} 个文件任务，降级 ${mnum.workers_demoted} 台 Worker，收尾 ${mnum.jobs_finalized} 个作业`);
    ledger.equal('结果条可关（closable=true，常驻提醒会变噪音）', await successAlert.locator('.el-alert__close-btn').count(), 1);
    await successAlert.locator('.el-alert__close-btn').click();
    // EP 的 el-alert 用 Transition + v-show 收起（alert.vue:31-40），元素不脱嵌，所以只能按"可见"断言
    await waitFor(async () => ((await page.locator('.el-alert--success:visible').count()) === 0 ? true : null),
      { label: '点关闭后结果条不可见' });
    ledger.equal('点关闭后结果条不可见（DOM 里还在是 v-show 的实现细节，不是没关掉）',
      await page.locator('.el-alert--success:visible').count(), 0);
    await go(page, '/logharvest/overview', { reload: true, waitText: '运行概览' });
    ledger.equal('刷新后不残留「上次维护」（结果只在组件内存，不落 localStorage）',
      await page.locator('.el-alert--success').count(), 0);

    const ov2 = await st.api.get('/system/overview');
    const stt2 = await st.api.get('/system/status');
    const jobTotal2 = sumOf(ov2.jobs);
    const maintQuiet = (mnum.recovered ?? 0) + (mnum.workers_demoted ?? 0) + (mnum.jobs_finalized ?? 0) === 0;
    if (maintQuiet) {
      ledger.check('维护返回 0/0/0 时概览逐字段不变（证明它是纯清扫，没顺手改数）',
        JSON.stringify(ov2) === JSON.stringify(ov), `before=${JSON.stringify(ov)} after=${JSON.stringify(ov2)}`);
    } else {
      ledger.check('[观察] 本轮队列非静止，维护真的动了数据；后续断言改用刷新后的基线', true,
        `响应=${JSON.stringify(mnum)} 基线作业总数=${jobTotal} 现在=${jobTotal2}`);
    }

    // ---- ⑦ 合成载荷：库里造不出的分支（积压 / 告警边界 / 空分布 / 表外状态）只能注入 ----
    const synth = {
      jobs: { pending: 3, running: 1, succeeded: 4 },
      file_tasks: {},
      matches_24h: 1234,
      workers: { online: 1, by_version: { '9.9.9': 1 } },
      oldest_pending_seconds: 420,
    };
    let synthBody = synth;
    const waitCardText = (label, text) => waitFor(
      async () => (norm(await cardValue(page, label).innerText()) === text ? true : null),
      { label: `「${label}」渲染成「${text}」` });
    const unrouteSynth = await inject(page, OVERVIEW_RE, () => synthBody);
    try {
      await go(page, '/logharvest/overview', { reload: true, waitText: '运行概览' });
      await waitCardText('24 小时命中行数', '1234');
      const synthTotal = sumOf(synth.jobs);
      const sd = distribution(synth.jobs, synthTotal);
      ledger.equal('合成作业总数 = Σ jobs（3+1+4）', norm(await cardValue(page, '作业总数').innerText()), String(synthTotal));
      const sg = await distFacts(cardByHeader(page, '作业状态分布'));
      ledger.equal('分布行序按 JOB_STATUS_OPTIONS（pending→running→succeeded），不是对象键序',
        JSON.stringify(sg.map((r) => r.status)), JSON.stringify(sd.map((r) => r.status)));
      ledger.equal('分布逐行「N（P%）」由前端按分母算出', JSON.stringify(sg.map((r) => r.value)),
        JSON.stringify(sd.map((r) => `${r.count}（${r.percent}%）`)));
      ledger.equal('running 行仍是 primary 蓝（配色只认状态词表）', hasTag(sg[1].tagCls, JOB_TAG.running), true);
      const pend = cardValue(page, '最老 pending 等待');
      await waitFor(async () => ((await cardNumber(page, '最老 pending 等待')) === fmtDuration(420) ? true : null),
        { label: '「最老 pending 等待」渲染成「7 分」' });
      ledger.equal('420 秒显示「7 分」（formatDuration 的分钟档，不含角标文本）',
        await cardNumber(page, '最老 pending 等待'), fmtDuration(420));
      ledger.check('超过 300 秒变红并挂出「积压」角标',
        norm(await pend.getAttribute('class')).includes('text-red-600')
          && hasTag(norm(await pend.locator('.el-tag').getAttribute('class').catch(() => '')), 'danger'),
        norm(await pend.getAttribute('class')));
      ledger.check('角标文案就是「积压」两个字', (await bodyText(page)).includes('积压'), '');

      synthBody = { ...synth, oldest_pending_seconds: 300 };
      await go(page, '/logharvest/overview', { reload: true, waitText: '运行概览' });
      await waitFor(async () => ((await cardNumber(page, '最老 pending 等待')) === fmtDuration(300) ? true : null),
        { label: '「最老 pending 等待」渲染成「5 分」' });
      ledger.equal('恰好 300 秒显示「5 分」', await cardNumber(page, '最老 pending 等待'), fmtDuration(300));
      ledger.check('恰好 300 秒不告警（判定是严格 ">"，阈值本身不算积压）',
        (await cardValue(page, '最老 pending 等待').locator('.el-tag').count()) === 0
          && norm(await cardValue(page, '最老 pending 等待').getAttribute('class')).includes('text-green-700'),
        norm(await cardValue(page, '最老 pending 等待').getAttribute('class')));

      synthBody = { ...synth, oldest_pending_seconds: null };
      await go(page, '/logharvest/overview', { reload: true, waitText: '运行概览' });
      await waitFor(async () => ((await cardNumber(page, '最老 pending 等待')) === '-' ? true : null),
        { label: '「最老 pending 等待」渲染成「-」' });
      ledger.equal('没有 pending 时显示「-」而不是 0 秒（"队列很健康"与"没数据"必须能区分）',
        await cardNumber(page, '最老 pending 等待'), '-');
      const fileCard7 = cardByHeader(page, '文件任务状态分布');
      ledger.equal('file_tasks 为空时给「暂无文件任务」空态而不是空卡片',
        norm(await fileCard7.locator('.el-empty__description').innerText()), '暂无文件任务');
      ledger.check('空态下不画「共 0 个文件任务」（0 分母不配伪装成一句统计）',
        !(await fileCard7.innerText()).includes('共 0 个'), norm(await fileCard7.innerText()));
      ledger.equal('单版本合成载荷 → 「9.9.9：1 台（100%）」',
        JSON.stringify((await page.locator('.el-tag--large').allInnerTexts()).map(norm)),
        JSON.stringify(versionRows(synth.workers.by_version).map((r) => `${r.version}：${r.count} 台（${r.percent}%）`)));

      synthBody = { ...synth, jobs: { succeeded: 2, queued: 2 } };
      await go(page, '/logharvest/overview', { reload: true, waitText: '运行概览' });
      await waitCardText('作业总数', '4');
      const sg2 = await distFacts(cardByHeader(page, '作业状态分布'));
      ledger.equal('后端新增状态词（queued）时前端不吞行：表内状态在前，表外状态补在后面',
        JSON.stringify(sg2.map((r) => r.status)), JSON.stringify(['succeeded', 'queued']));
      ledger.equal('表外状态退回 info 配色（statusTagType 的兜底，不是崩掉）', hasTag(sg2[1].tagCls, 'info'), true);
    } finally {
      await unrouteSynth();
    }
    // ---- ⑧ 概览 500：退回 /api/system/status，页面不能空、缺的数据不能说成 0 ----
    const ovDetail = '概览服务炸了';
    const unrouteDeg = await inject(page, OVERVIEW_RE, { __status: 500, __body: { detail: ovDetail } });
    try {
      await go(page, '/logharvest/overview', { reload: true, waitText: '运行概览' });
      const deg = page.locator('.el-alert--warning');
      await waitFor(async () => ((await deg.count()) === 1 ? true : null), { label: '概览 500 后出现降级告警条' });
      ledger.equal('降级告警标题逐字（写清退回的是哪个接口）',
        norm(await deg.locator('.el-alert__title').innerText()), '概览接口不可用，已退回 /api/system/status');
      const desc = norm(await deg.locator('.el-alert__description').innerText());
      ledger.check('说明里带上后端返回的 detail（用户能看到错在哪，而不是"未知错误"）',
        desc.includes(`请求失败（${ovDetail}）`), desc);
      ledger.check('说清这次缺了哪三块（不让人把缺数据读成 0）',
        desc.includes('“作业状态分布 / 24 小时命中数 / Worker 版本分布”依赖概览接口')
          && desc.includes('本次只显示后端已有 /api/system/status 能提供的字段'), desc);
      ledger.equal('降级状态不给关闭按钮（关掉就等于假装没发生）',
        await deg.locator('.el-alert__close-btn').count(), 0);
      ledger.equal('概览挂掉时「作业总数」显示「未知」而不是 0',
        norm(await cardValue(page, '作业总数').innerText()), '未知');
      ledger.equal('概览挂掉时「24 小时命中行数」显示「未知」',
        norm(await cardValue(page, '24 小时命中行数').innerText()), '未知');
      ledger.equal('「未知」用灰色弱化（text-gray-400），与真实数字一眼能分',
        await cardByLabel(page, '作业总数').locator('.text-2xl span.text-gray-400').count(), 1);
      await waitCardText('存活 Worker', String(stt2.workers_online));
      ledger.equal('「存活 Worker」用 status 兜底（这个字段概览挂了也有）',
        norm(await cardValue(page, '存活 Worker').innerText()), String(stt2.workers_online));
      ledger.equal('「最老 pending 等待」用 status 的 pending_oldest_seconds 兜底',
        await cardNumber(page, '最老 pending 等待'), fmtDuration(stt2.pending_oldest_seconds));
      ledger.equal('作业分布卡退回空态文案，而不是画一张 0 行的假表格',
        norm(await cardByHeader(page, '作业状态分布').locator('.el-empty__description').innerText()),
        '概览接口未返回作业分布');
      ledger.equal('版本分布卡同样退回空态（升级时别把"不知道"看成"版本已统一"）',
        norm(await cardByHeader(page, 'Worker 版本分布').locator('.el-empty__description').innerText()),
        '概览接口未返回版本分布');
      const fd = distribution(stt2.file_tasks, sumOf(stt2.file_tasks));
      const fg = await distFacts(cardByHeader(page, '文件任务状态分布'));
      ledger.equal('文件分布完全来自 status 兜底，逐行「N（P%）」与接口一致',
        JSON.stringify(fg.map((r) => r.value)), JSON.stringify(fd.map((r) => `${r.count}（${r.percent}%）`)));
      ledger.check('兜底时分母用 status 自己的合计（不混用概览的数）',
        (await bodyText(page)).includes(`共 ${sumOf(stt2.file_tasks)} 个文件任务`), `fileTotal=${sumOf(stt2.file_tasks)}`);
      ledger.equal('降级时 7 张卡一张不减（只换内容，不塌版式）', await page.locator('.el-card').count(), 7);
      ledger.check('500 由统一拦截器给出可重试提示',
        (await messagesOf(page)).some((t) => /服务暂时不可用/.test(t)), JSON.stringify(await messagesOf(page)));
    } finally {
      await unrouteDeg();
    }

    // ---- ⑨ 概览 + status 双挂：兜底也没有时页面必须诚实，而不是报「0 台存活」 ----
    const unrouteOv9 = await inject(page, OVERVIEW_RE, { __status: 500, __body: { detail: ovDetail } });
    const unrouteSt9 = await inject(page, STATUS_RE, { __status: 500 });
    try {
      await go(page, '/logharvest/overview', { reload: true, waitText: '运行概览' });
      await waitFor(async () => ((await page.locator('.el-alert--warning').count()) === 1 ? true : null), { label: '双失败仍出降级条' });
      await waitMessage(page, /系统状态加载失败/);
      const msgs9 = await messagesOf(page);
      ledger.check('两个接口都挂时两件事都要说（status 也挂了是新增事实，不能只报概览）',
        msgs9.some((t) => /系统状态加载失败/.test(t)), JSON.stringify(msgs9));
      ledger.equal('兜底也没了页面照样不崩（7 张卡全在）', await page.locator('.el-card').count(), 7);
      ledger.equal('「作业总数」仍是「未知」（没有概览就没有作业数）',
        norm(await cardValue(page, '作业总数').innerText()), '未知');
      ledger.equal('「最老 pending 等待」退回「-」', await cardNumber(page, '最老 pending 等待'), '-');
      ledger.equal('文件分布没有来源时给「暂无文件任务」空态',
        norm(await cardByHeader(page, '文件任务状态分布').locator('.el-empty__description').innerText()), '暂无文件任务');
      const onlineTxt = norm(await cardValue(page, '存活 Worker').innerText());
      if (onlineTxt === '0') {
        ledger.check('[产品缺陷] 概览与 status 双挂时「存活 Worker」显示 0（把"不知道"渲染成"一台都没有"）', true,
          `实测文案=${onlineTxt}；证据 overview/index.vue:59-61 的 workersOnline 用 \`?? 0\` 兜底，`
          + '而同页 :213/:222 的「作业总数」「24 小时命中行数」走 text-gray-400 的「未知」，'
          + 'format.ts:8-20 也写明"NULL 绝不退化成 0"。运营看到 0 会以为 Worker 全掉线并去重启，实际是接口都挂了。');
      } else {
        ledger.equal('双失败时「存活 Worker」不再谎报 0（缺陷未显现）', onlineTxt, '未知');
      }
    } finally {
      await unrouteOv9();
      await unrouteSt9();
    }

    // ---- ⑩ 注入撤掉后必须自愈：route 泄漏会让后面所有阶段读到假数据 ----
    await go(page, '/logharvest/overview', { reload: true, waitText: '运行概览' });
    await waitFor(async () => (norm(await cardValue(page, '作业总数').innerText()) === String(jobTotal2) ? true : null),
      { label: '注入撤掉后概览自愈' });
    ledger.equal('unroute 后作业总数回到真实值', norm(await cardValue(page, '作业总数').innerText()), String(jobTotal2));
    ledger.equal('降级告警条不残留', await page.locator('.el-alert--warning').count(), 0);
    ledger.equal('版本分布回到真实台数',
      JSON.stringify((await page.locator('.el-tag--large').allInnerTexts()).map(norm)),
      JSON.stringify(versionRows(ov2.workers.by_version).map((r) => `${r.version}：${r.count} 台（${r.percent}%）`)));
    await shot(page, ledger, 'overview_final');
  } catch (err) {
    ledger.fail('U14 概览页异常分支（注入）', err);
  }
}

export const phases = { datasources: phase_datasources, workers: phase_workers, stats: phase_stats, overview: phase_overview };
