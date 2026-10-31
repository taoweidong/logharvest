/**
 * U12 系统管理（用户/角色/菜单）/ U13 操作日志 / U15 登出与收尾
 * —— 页面全流程 E2E 的阶段模块（由 ui_flow.mjs 的 lazyPhase 动态加载）。
 *
 * 契约：`export const phases = { system, audit, logout }`；每个阶段第一句 `ledger.phase(PHASE_LABEL.xxx)`。
 * DEPS=['login']，进来时 st.api 已登录、st.roleIds/st.nasSource/st.canonical 已就绪、
 * st.adminPage / st.operatorPage 由 U2 建好可直接复用。
 *
 * 铁律（与 ui_flow_ops / ui_flow_rules 一致）：
 * - 只断言页面事实：可见文案、el-tag 的 type class、DOM 有无、disabled、真实请求 URL+payload、
 *   cookie/localStorage。API 只用于造前置、自愈清理与残留对账，不参与断言。
 * - 文案逐字来源（改文案必须先改这里）：
 *   web/src/views/system/users/index.vue（列头/脚注/三个弹窗的 label、placeholder、按钮、消息）
 *   web/src/views/system/roles/index.vue + ../filters.ts（客户端筛选、面板头部、内置只读提示）
 *   web/src/views/system/menus/index.vue（树表列头、灰条提示、类型 radio、上级下拉口径）
 *   web/src/views/system/audit-logs/index.vue（七个筛选控件、灰条、el-result、详情抽屉）
 *   web/src/layout/components/lay-navbar/index.vue + store/modules/user.ts（退出系统 / removeToken）
 *   service/src/api/v1/routes_users|roles|menus|operation_logs.py + application/services/rbac.py
 *   （守卫原文 username already exists / cannot disable yourself / built-in menu cannot be deleted …）
 * - 写操作全在本模块自建的行上：用户 ui-sys-*、角色 ui-r-*、菜单 UiE2e* 与 ui:e2e:*。
 *   （块注释里不能写「UiE2e* 后紧跟斜杠」：星号紧跟斜杠会被当成注释结束符。）
 *   内置 admin/operator/viewer 与 35 个内置菜单永不改删。
 * - 不可达分支（403 降级、detail 空、账号已删除行）优先用库里真实历史行；库里没有才 page.route
 *   注入，且每个 route 都在 try/finally 里配对 unroute。
 * - 图标一律留空：菜单图标必须在前端离线注册表里（见项目约束），自建模有不指定 icon 最稳。
 * - 收尾挂 st.teardownHook（main 只在跑了 logout 阶段时才调它）；只跑 --phase system 时
 *   留下的行由下一次运行的 preClean 自愈，所以两个都需要。
 *
 * 运行：
 *   bash e2e/ui_run.sh --phase system,audit,logout
 *   node e2e/ui_probe.mjs /system/users --click 新建用户   # 先照妖镜，别靠猜
 */
import {
  ACCOUNTS,
  RUN_TS,
  WEB,
  Api,
  bodyButtons,
  clearMessages,
  clearSelect,
  closeDrawer,
  dialog,
  esc,
  formErrors,
  go,
  menuTexts,
  newPage,
  openSelect,
  pickOption,
  readMessageBox,
  clickMessageBoxButton,
  recentRequests,
  selectOptionsOf,
  shot,
  tableHeaderTexts,
  tableRow,
  useSession,
  waitFor,
  waitMessage,
  messagesOf,
} from './ui_lib.mjs';

// ---------------------------------------------------------------- 自建数据名

const S_USER = `ui-sys-${RUN_TS}`;
const S_NICK = 'UI 系统页自建';
const S_PASS_OLD = 'E2eSys#Pass1';
const S_PASS_NEW = 'E2eSys#Pass2';

const R_NAME = `UI自建角色${RUN_TS}`;
const R_CODE = `ui-r-${RUN_TS}`;

const M_DIR_TITLE = `UI E2E 目录${RUN_TS}`;
const M_DIR_NAME = `UiE2eDir${RUN_TS}`;
const M_DIR_PATH = `/ui-e2e-dir-${RUN_TS}`;
const M_PAGE_TITLE = `UI E2E 页面${RUN_TS}`;
const M_PAGE_NAME = `UiE2ePage${RUN_TS}`;
const M_PAGE_PATH = `/ui-e2e-page-${RUN_TS}`;
const M_PAGE_COMP = 'logharvest/jobs/index'; // 真实存在的视图，顺带验动态路由的组件解析
const M_PERM_TITLE = `UI E2E 权限${RUN_TS}`;
const M_PERM_NAME = `ui:e2e:${RUN_TS}`;

// ---------------------------------------------------------------- 页面小工具

const norm = (s) => (s ?? '').replace(/\s+/g, ' ').trim();
const bodyText = (page) => page.locator('body').innerText().then(norm).catch(() => '');
const cellsOf = async (row) => (await row.locator('td').allInnerTexts()).map(norm);

/** el-form-item 按 label 锚定（label 精确匹配，弹窗里同名 label 取最后一个）。 */
const item = (scope, label, page) =>
  scope.locator('.el-form-item').filter({ has: (page ?? scope).locator(`label:text-is("${label}")`) }).last();

const inputOf = (scope, label, page) => item(scope, label, page).locator('input, textarea').first();

const textOf = (scope, label, page) => item(scope, label, page).locator('input').first().inputValue();

const switchOf = (scope, label, page) => item(scope, label, page).locator('.el-switch').first();

async function switchState(scope, label, page) {
  const cls = await switchOf(scope, label, page).getAttribute('class').catch(() => '');
  return /is-checked/.test(cls ?? '');
}

/** 弹窗/面板底部的具名按钮（取消 / 创建 / 保存 / 重置 / 删除）。 */
const pushBtn = (scope, name) => scope.getByRole('button', { name: new RegExp(`^${esc(name)}$`) }).last();

/** 表格行的「更多」下拉（el-dropdown 触发按钮无文案，只有图标），返回下拉项按钮。 */
async function openRowDropdown(page, rowText) {
  const row = tableRow(page, rowText);
  await row.waitFor({ state: 'visible', timeout: 8000 });
  await row.locator('.el-dropdown .el-button').first().click();
  const menu = page.locator('.el-dropdown-menu:visible').last();
  await menu.locator('.el-dropdown-menu__item').first().waitFor({ state: 'visible', timeout: 8000 });
  return {
    menu,
    items: (await menu.locator('.el-dropdown-menu__item').allInnerTexts()).map(norm),
    item: (name) => menu.getByRole('button', { name: new RegExp(`^${esc(name)}$`) }).last(),
  };
}

/** el-tree-v2 是虚拟列表：先用面板搜索框把目标节点滚出可视区，再按文案取节点。 */
async function treeNodes(page, keyword) {
  const search = page.getByPlaceholder('请输入菜单名称、权限码或路径进行搜索');
  await search.waitFor({ state: 'visible', timeout: 8000 });
  await search.fill(keyword);
  await page.waitForTimeout(700);
  const nodes = page.locator('.el-tree-node');
  const out = [];
  const n = await nodes.count();
  for (let i = 0; i < n; i += 1) {
    const node = nodes.nth(i);
    if (!(await node.isVisible().catch(() => false))) continue;
    const nodeCls = (await node.getAttribute('class').catch(() => '')) ?? '';
    const boxCls = (await node.locator('.el-checkbox').first().getAttribute('class').catch(() => '')) ?? '';
    out.push({
      node,
      key: await node.getAttribute('data-key').catch(() => null),
      text: norm(await node.innerText().catch(() => '')),
      checked: /\bis-checked\b/.test(nodeCls) || /\bis-checked\b/.test(boxCls),
    });
  }
  return out;
}

const nodeByTitle = (nodes, title) => nodes.find((x) => x.text.includes(title)) ?? null;

/**
 * 捕获真实请求（含 POST/PUT 体）—— ui_lib 的 __requests 只存 method/url/headers，
 * 这里要验的是「页面真的发出了这个 payload」。停止函数必须成对调用。
 */
function captureRequests(page, re) {
  const got = [];
  const handler = (req) => {
    if (!re.test(req.url())) return;
    let postData = null;
    try {
      postData = req.postData();
    } catch {
      postData = null;
    }
    got.push({ method: req.method(), url: req.url(), postData });
  };
  page.on('request', handler);
  return {
    got,
    last: async (label = '请求', timeout = 15000) => waitFor(() => (got.length ? got[got.length - 1] : null),
      { label, timeout }),
    stop: () => page.off('request', handler),
  };
}

const clearReq = (page) => {
  if (Array.isArray(page.__requests)) page.__requests.length = 0;
};

const jsonOf = (req) => {
  try {
    return JSON.parse(req.postData ?? '{}');
  } catch {
    return null;
  }
};

/** 一步一收：任何子步骤抛错都只记一条 FAIL，后面的步骤照跑（整阶段不能被一个定位器拖死）。 */
async function step(ledger, name, fn) {
  try {
    await fn();
  } catch (err) {
    ledger.fail(`${name}（步骤异常）`, err);
  }
}

/** 点掉页面上所有可见弹窗的取消/关闭，回到干净列表。 */
async function closeAllDialogs(page) {
  for (let i = 0; i < 4; i += 1) {
    const dlg = page.locator('.el-dialog:visible').last();
    if ((await dlg.count()) === 0 || !(await dlg.isVisible().catch(() => false))) break;
    const cancel = dlg.getByRole('button', { name: '取消' });
    if ((await cancel.count()) > 0) await cancel.last().click().catch(() => {});
    else await dlg.locator('.el-dialog__headerbtn').first().click().catch(() => {});
    await page.waitForTimeout(400);
  }
  await page.locator('.el-drawer:visible').count().then(async (n) => {
    if (n > 0) await closeDrawer(page);
  });
}

// ---------------------------------------------------------------- 收尾与自愈

/** 深收集菜单树（后端 /menus/tree 是 children 嵌套）。 */
function flattenMenus(tree, out = []) {
  for (const m of tree ?? []) {
    out.push(m);
    flattenMenus(m.children, out);
  }
  return out;
}

const isMineMenu = (m) => /^UiE2e/.test(m.name ?? '') || /^ui:e2e:/.test(m.name ?? '');
const isMineRole = (r) => /^ui-r-/.test(r.code ?? '');
const isMineUser = (u) => /^ui-sys-/.test(u.username ?? '');

/**
 * 清掉 UI 造出的用户/角色/菜单（先子后父，先用户后角色，否则守卫会拒绝）。
 * 只做清理与残留对账，不替页面断言说话。
 */
async function cleanupMine(ledger, st) {
  const api = st.api;
  const users = await api.get('/users?limit=500').catch(() => []);
  for (const u of users.filter(isMineUser)) {
    const r = await api.call('DELETE', `/users/${u.id}`);
    ledger.check(`清理自建用户 ${u.username}`, r.status < 300 || r.status === 404, JSON.stringify(r.body).slice(0, 160));
  }
  const roles = await api.get('/roles?limit=500').catch(() => []);
  for (const role of roles.filter(isMineRole)) {
    const r = await api.call('DELETE', `/roles/${role.id}`);
    ledger.check(`清理自建角色 ${role.code}`, r.status < 300 || r.status === 404, JSON.stringify(r.body).slice(0, 160));
  }
  const tree = await api.get('/menus/tree').catch(() => []);
  const mine = flattenMenus(tree).filter(isMineMenu).sort((a, b) => b.menu_type - a.menu_type);
  for (const m of mine) {
    const r = await api.call('DELETE', `/menus/${m.id}`);
    ledger.check(`清理自建菜单 ${m.name}（type=${m.menu_type}）`, r.status < 300 || r.status === 404,
      JSON.stringify(r.body).slice(0, 160));
  }

  const leftUsers = (await api.get('/users?limit=500').catch(() => [])).filter(isMineUser);
  const leftRoles = (await api.get('/roles?limit=500').catch(() => [])).filter(isMineRole);
  const leftMenus = flattenMenus(await api.get('/menus/tree').catch(() => [])).filter(isMineMenu);
  ledger.equal('残留对账：自建用户已清零', leftUsers.length, 0);
  ledger.equal('残留对账：自建角色已清零', leftRoles.length, 0);
  ledger.equal('残留对账：自建菜单已清零', leftMenus.length, 0);

  const after = flattenMenus(await api.get('/menus/tree').catch(() => []));
  ledger.equal('残留对账：自定义菜单（is_system=false）归零', after.filter((m) => !m.is_system).length, 0);
  const codes = (await api.get('/roles?limit=500').catch(() => [])).map((r) => r.code).sort();
  ledger.check('内置角色 admin/operator/viewer 仍在', ['admin', 'operator', 'viewer'].every((c) => codes.includes(c)),
    JSON.stringify(codes));
  const builtins = ['用户管理', '角色管理', '菜单管理', '操作日志'];
  ledger.check('系统管理四个内置页面仍在菜单树里',
    builtins.every((t) => after.some((m) => m.title === t)),
    JSON.stringify(after.filter((m) => !m.is_system).map((m) => [m.id, m.name, m.title])).slice(0, 200));
}

/**
 * 挂收尾钩子（幂等）。main() 只在 wanted 含 logout 时调 phase_teardown，
 * 所以 system 与 logout 都调一次 ensureTeardown；已有别人的钩子要串起来，不能顶掉。
 */
function ensureTeardown(st) {
  if (st.sysTeardownInstalled) return;
  st.sysTeardownInstalled = true;
  const outer = typeof st.teardownHook === 'function' ? st.teardownHook : null;
  st.teardownHook = async (ledger, ctx) => {
    if (outer) await outer(ledger, ctx).catch((err) => ledger.fail('前置模块收尾钩子异常', err));
    // 收尾用全新 admin 会话：U15 已经退出过，旧 token 的 cookie 没了，API 侧要重新登录。
    const api = new Api();
    await api.login(ACCOUNTS.admin.username, ACCOUNTS.admin.password);
    const prev = ctx.api;
    ctx.api = api;
    try {
      await cleanupMine(ledger, ctx);
    } finally {
      ctx.api = prev;
    }
    ledger.skip('观察项：数据源与规则集/作业没有删除接口',
      'U7 数据源用 psql 自清；规则集与作业只有 POST/GET，UI 造出的行按仓库约定留在库里。');
  };
}

/** 上一轮异常留下的行先清掉，本轮才有干净的列表可数。 */
async function preClean(ledger, st) {
  if (!st.api.token) await st.api.login(ACCOUNTS.admin.username, ACCOUNTS.admin.password);
  const before = {
    users: (await st.api.get('/users?limit=500').catch(() => [])).filter(isMineUser).length,
    roles: (await st.api.get('/roles?limit=500').catch(() => [])).filter(isMineRole).length,
    menus: flattenMenus(await st.api.get('/menus/tree').catch(() => [])).filter(isMineMenu).length,
  };
  const residue = before.users + before.roles + before.menus;
  if (residue === 0) {
    ledger.check('前置自愈：库里没有上一轮残留', true, JSON.stringify(before));
    return;
  }
  ledger.check('前置自愈：本轮开局即发现上一轮残留', false, `残留=${JSON.stringify(before)}`);
  await cleanupMine(ledger, st);
}

// ---------------------------------------------------------------------------
// U12-A 用户管理：列表口径 → 新建（三段校验）→ 搜索下推 → 修改 → 分配角色
// → 重置密码（并用全新会话真登录一次）→ 服务端守卫 → 删除。
// ---------------------------------------------------------------------------

/** 多选下拉在已选中后没有 placeholder，只能按容器取；字符串锚点会失效。 */
async function pickIn(page, select, match) {
  await select.waitFor({ state: 'visible', timeout: 8000 });
  await select.click();
  const dd = page.locator('.el-select-dropdown:visible').last();
  const items = dd.locator('.el-select-dropdown__item');
  await items.first().waitFor({ state: 'visible', timeout: 8000 });
  const texts = (await items.allInnerTexts()).map(norm);
  const re = match instanceof RegExp ? match : new RegExp(esc(match));
  const idx = texts.findIndex((t) => re.test(t));
  if (idx < 0) {
    await page.keyboard.press('Escape');
    throw new Error(`下拉没有匹配 ${re} 的选项，可见项=${JSON.stringify(texts.slice(0, 25))}`);
  }
  await items.nth(idx).click();
  await page.waitForTimeout(350);
  // 多选下拉选中后面板不会自动关（单选才会）：面板还开着就会挡住后续的按钮点击
  //（实测创建按钮被 .el-select-dropdown__item 拦截 30s 超时），所以关一下再返回。
  if (await dd.isVisible().catch(() => false)) {
    await page.keyboard.press('Escape');
    await dd.waitFor({ state: 'hidden', timeout: 5000 }).catch(() => {});
    await page.waitForTimeout(250);
  }
  return texts[idx];
}

/** EP 当前版本把多选标签渲染在 .el-select__selected-item 里（内层仍是 .el-tag），
 *  旧口径 .el-select__tags .el-tag 永远是空集——直接取 select 作用域内的 .el-tag。 */
const tagsIn = async (select) => (await select.locator('.el-tag').allInnerTexts()).map(norm);

async function clearIn(page, select) {
  await select.hover();
  const icon = select.locator('.el-select__clear').first();
  await icon.waitFor({ state: 'visible', timeout: 5000 });
  await icon.click();
  await page.waitForTimeout(300);
}

async function usersLoop(ledger, st, page) {
  const baseline = await st.api.get('/users?limit=20&offset=0').catch(() => []);
  const baseUrl = page.url();

  await step(ledger, 'U12-用户 列表口径', async () => {
    ledger.equal('用户表头（含固定操作列）', (await tableHeaderTexts(page)).join('|'),
      'ID|用户名|昵称|角色|状态|创建时间|操作');
    // waitText 只保证骨架渲染；首屏数据在飞时脚注会短暂显示「本页 0 条」，
    // 断言前必须等行数真的追上接口基线。
    await waitFor(async () =>
      ((await page.locator('.el-table__body .el-table__row').count()) === baseline.length ? true : null),
      { label: '用户首屏行渲染到位', timeout: 15000 });
    const foot = norm(await page.locator('.page-foot span').first().innerText());
    ledger.check('脚注是窗口式口径（无总数）+ 本页条数等于接口返回条数',
      /^后端列表接口不返回总数；第 1 页，本页 \d+ 条$/.test(foot) && foot.endsWith(`${baseline.length} 条`),
      `脚注=「${foot}」 接口条数=${baseline.length}`);
    ledger.equal('只有 3 行时上一页禁用', await page.getByRole('button', { name: '上一页' }).isDisabled(), true);
    ledger.equal('不足一页时下一页禁用', await page.getByRole('button', { name: '下一页' }).isDisabled(), true);
    ledger.check('搜索框占位来自源码', await page.getByPlaceholder('搜索用户名 / 昵称').isVisible(), baseUrl);
    const counts = await Promise.all(['admin', 'ui-operator', 'ui-viewer'].map((u) => tableRow(page, u).count()));
    ledger.check('内置三行都在列表里', counts.every((c) => c === 1), JSON.stringify(counts));
  });

  await step(ledger, 'U12-用户 新建校验与创建', async () => {
    await page.getByRole('button', { name: '新建用户' }).click();
    const dlg = await dialog(page, '新建用户');
    ledger.equal('新建弹窗字段顺序', (await dlg.locator('.el-form-item label').allInnerTexts()).map(norm).join('|'),
      '用户名|昵称|初始密码|角色|启用');
    ledger.equal('新建弹窗占位文案', (await Promise.all(
      ['用户名', '昵称', '初始密码'].map((l) => inputOf(dlg, l, page).getAttribute('placeholder')),
    )).map((x) => x ?? '').join('|'), '登录名，唯一|显示名称（可选）|至少 8 位');

    await pushBtn(dlg, '创建').click();
    await page.waitForTimeout(600);
    ledger.check('空提交给两条必填红字',
      (await formErrors(page)).sort().join('|') === '请填写初始密码|请填写用户名', JSON.stringify(await formErrors(page)));

    await inputOf(dlg, '用户名', page).fill('-bad-ui-name');
    await pushBtn(dlg, '创建').click();
    await page.waitForTimeout(600);
    ledger.check('非法用户名给正则红字',
      (await formErrors(page)).some((t) => t === '以字母或数字开头，仅含字母、数字、. _ @ -，长度 2-50'),
      JSON.stringify(await formErrors(page)));

    await inputOf(dlg, '用户名', page).fill(S_USER);
    await inputOf(dlg, '初始密码', page).fill('abc');
    await pushBtn(dlg, '创建').click();
    await page.waitForTimeout(600);
    ledger.check('短密码给长度红字', (await formErrors(page)).includes('密码至少 8 位'),
      JSON.stringify(await formErrors(page)));

    await inputOf(dlg, '昵称', page).fill(S_NICK);
    await inputOf(dlg, '初始密码', page).fill(S_PASS_OLD);
    const picked = await pickIn(page, item(dlg, '角色', page).locator('.el-select').first(), /只读用户/);
    ledger.check('角色选项带出角色代码', /\（viewer）$/.test(picked), picked);
    ledger.equal('启用开关默认开', await switchState(dlg, '启用', page), true);
    clearMessages(page);
    const cap = captureRequests(page, /\/api\/users(\?|$)/);
    await pushBtn(dlg, '创建').click();
    const msg = await waitMessage(page, new RegExp(`用户“${esc(S_USER)}”已创建`));
    cap.stop();
    ledger.check('创建成功提示逐字命中', msg.includes(S_USER), msg);
    // (\?|$) 会把创建成功后的列表刷新 GET /users?limit=20 一并捕获，取最后一个 POST 才是创建请求。
    const created = cap.got.filter((r) => r.method === 'POST').at(-1);
    const payload = jsonOf(created ?? { postData: '' });
    ledger.check('POST /users 的 body 是页面表单值',
      payload?.username === S_USER && payload?.password === S_PASS_OLD && payload?.nickname === S_NICK
        && Array.isArray(payload?.role_ids) && payload.role_ids.length === 1
        && payload.role_ids[0] === st.roleIds.viewer,
      JSON.stringify(payload));
    await dlg.waitFor({ state: 'hidden', timeout: 8000 }).catch(() => {});
  });

  await step(ledger, 'U12-用户 新行的单元格事实', async () => {
    const row = tableRow(page, S_USER);
    await row.waitFor({ state: 'visible', timeout: 10000 });
    const cells = await cellsOf(row);
    ledger.equal('新行的 用户名|昵称|角色|状态', cells.slice(1, 5).join('|'), `${S_USER}|${S_NICK}|只读用户|启用`);
    ledger.check('状态标签是 success 配色',
      hasTagCls(await row.locator('.el-tag').last().getAttribute('class'), 'success'),
      await row.locator('.el-tag').last().getAttribute('class'));
    ledger.check('操作列有 修改 / 分配角色 + 更多下拉',
      norm(await row.locator('td').last().innerText()).startsWith('修改 分配角色'),
      norm(await row.locator('td').last().innerText()));
  });

  await step(ledger, 'U12-用户 关键字搜索下推后端', async () => {
    clearReq(page);
    await page.getByPlaceholder('搜索用户名 / 昵称').fill(S_USER);
    await page.getByRole('button', { name: '搜索', exact: true }).click();
    await waitFor(async () => ((await page.locator('.el-table__body .el-table__row').count()) === 1 ? true : null),
      { label: '搜索收敛到 1 行' });
    const hits = await recentRequests(page, /\/api\/users/);
    const params = new URL(hits.at(-1)?.url ?? baseUrl).searchParams;
    ledger.equal('搜索把 keyword 交给后端', params.get('keyword'), S_USER);
    ledger.check('命中后只有自建那一行', (await tableRow(page, S_USER).count()) === 1
      && (await page.locator('.el-table__body .el-table__row').count()) === 1,
      `行数=${await page.locator('.el-table__body .el-table__row').count()}`);

    clearReq(page);
    await page.getByRole('button', { name: '重置' }).click();
    await waitFor(async () => ((await page.locator('.el-table__body .el-table__row').count()) >= 3 ? true : null),
      { label: '重置后回到全量' });
    const after = await recentRequests(page, /\/api\/users/);
    const p2 = new URL(after.at(-1)?.url ?? baseUrl).searchParams;
    ledger.check('重置清空 keyword 并重新拉全量', !p2.get('keyword') && (await page.getByPlaceholder('搜索用户名 / 昵称').inputValue()) === '',
      `URL=${after.at(-1)?.url ?? '无'}`);
  });

  await step(ledger, 'U12-用户 修改弹窗（只改昵称）', async () => {
    const newNick = `${S_NICK}改`;
    await tableRow(page, S_USER).getByRole('button', { name: '修改' }).click();
    const dlg = await dialog(page, '编辑用户');
    ledger.equal('编辑时用户名禁用', await inputOf(dlg, '用户名', page).isEditable(), false);
    ledger.check('编辑时没有初始密码与角色字段',
      (await dlg.locator('.el-form-item').filter({ has: page.locator('label:text-is("初始密码")') }).count()) === 0
        && (await dlg.locator('.el-form-item').filter({ has: page.locator('label:text-is("角色")') }).count()) === 0,
      (await dlg.locator('.el-form-item label').allInnerTexts()).map(norm).join('|'));
    await inputOf(dlg, '昵称', page).fill(newNick);
    clearMessages(page);
    const cap = captureRequests(page, /\/api\/users\/\d+$/);
    await pushBtn(dlg, '保存').click();
    const msg = await waitMessage(page, /用户信息已更新/);
    const req = await cap.last('PUT /users/{id}');
    cap.stop();
    ledger.check('修改成功提示逐字命中', msg === '用户信息已更新', msg);
    ledger.equal('PUT body 只带昵称与状态', JSON.stringify(jsonOf(req)?.nickname) + `/${jsonOf(req)?.is_active}`,
      `"${newNick}"/true`);
    await waitFor(async () => {
      const cells = await cellsOf(tableRow(page, S_USER));
      return cells[2] === newNick ? true : null;
    }, { label: '昵称列回填' });
    ledger.equal('行昵称已更新', (await cellsOf(tableRow(page, S_USER)))[2], newNick);
  });

  await step(ledger, 'U12-用户 分配角色（整集合覆盖）', async () => {
    const [mine] = await st.api.get(`/users?keyword=${S_USER}`).catch(() => []);
    const userId = mine?.id;
    await tableRow(page, S_USER).getByRole('button', { name: '分配角色' }).click();
    const dlg = await dialog(page, `分配角色 - ${S_USER}`);
    ledger.check('灰字写明覆盖语义', (await dlg.innerText()).includes('提交后会以当前选择覆盖该用户的角色集合。'),
      norm(await dlg.innerText()).slice(0, 160));
    const select = dlg.locator('.el-select').first();
    // 标签随 dialog 挂载异步渲染，打开瞬间读会拿到空集。
    await waitFor(async () => ((await tagsIn(select)).length > 0 ? true : null), { label: '预填角色标签渲染', timeout: 8000 });
    // 标签渲染的是下拉选项的完整 label：{name}（{code}）——用户页源码 :529。
    ledger.equal('打开时预填当前角色集合', (await tagsIn(select)).join('|'), '只读用户（viewer）');
    await clearIn(page, select);
    ledger.equal('清空后没有已选标签', (await tagsIn(select)).join('|'), '');
    await pickIn(page, select, /运维人员/);
    clearMessages(page);
    const cap = captureRequests(page, /\/api\/users\/\d+\/roles$/);
    await pushBtn(dlg, '保存').click();
    const msg = await waitMessage(page, new RegExp(`用户“${esc(S_USER)}”的角色已更新`));
    const req = await cap.last('PUT /users/{id}/roles');
    cap.stop();
    ledger.check('保存提示逐字命中', msg.includes('角色已更新'), msg);
    ledger.equal('请求打在自建用户上且方法是 PUT',
      `${req.method} ${new URL(req.url).pathname}`, `PUT /api/users/${userId}/roles`);
    ledger.equal('请求体就是页面当前选择', JSON.stringify(jsonOf(req)?.role_ids), JSON.stringify([st.roleIds.operator]));
    await waitFor(async () => ((await cellsOf(tableRow(page, S_USER)))[3] === '运维人员' ? true : null),
      { label: '角色列回填' });
    ledger.equal('行的角色标签换成运维人员', (await cellsOf(tableRow(page, S_USER)))[3], '运维人员');
  });

  await step(ledger, 'U12-用户 重置密码 + 全新会话验新口令', async () => {
    const dd = await openRowDropdown(page, S_USER);
    const ddText = dd.items.join('|');
    ledger.check('「更多」下拉里有 重置密码 / 删除', ddText.includes('重置密码') && ddText.includes('删除'), ddText);
    await dd.item('重置密码').click();
    const dlg = await dialog(page, `重置密码 - ${S_USER}`);
    await pushBtn(dlg, '重置').click();
    await page.waitForTimeout(600);
    ledger.check('空提交给两条必填红字', (await formErrors(page)).sort().join('|') === '请再次输入新密码|请填写新密码',
      JSON.stringify(await formErrors(page)));
    await inputOf(dlg, '新密码', page).fill('short');
    await inputOf(dlg, '确认密码', page).fill('different');
    await pushBtn(dlg, '重置').click();
    await page.waitForTimeout(600);
    const errs = await formErrors(page);
    ledger.check('长度与一致性红字', errs.includes('密码至少 8 位') && errs.includes('两次输入的密码不一致'),
      JSON.stringify(errs));
    await inputOf(dlg, '新密码', page).fill(S_PASS_NEW);
    await inputOf(dlg, '确认密码', page).fill(S_PASS_NEW);
    clearMessages(page);
    const cap = captureRequests(page, /\/api\/users\/\d+\/reset-password$/);
    await pushBtn(dlg, '重置').click();
    const msg = await waitMessage(page, new RegExp(`用户“${esc(S_USER)}”的密码已重置`));
    const req = await cap.last('POST reset-password');
    cap.stop();
    ledger.check('重置提示逐字命中', msg.includes('密码已重置'), msg);
    ledger.equal('走的是 POST /users/{id}/reset-password', req.method, 'POST');
    ledger.check('请求体带上新口令', (req.postData ?? '').includes(S_PASS_NEW), req.postData ?? 'null');

    const { context, page: fresh } = await newPage(st.browser);
    try {
      await fresh.goto(`${WEB}#/login`, { waitUntil: 'domcontentloaded' });
      await fresh.locator('form').first().waitFor({ state: 'visible', timeout: 20000 });
      await fresh.getByPlaceholder('账号').fill(S_USER);
      await fresh.getByPlaceholder('密码').fill(S_PASS_OLD);
      clearMessages(fresh);
      await fresh.getByRole('button', { name: '登录' }).click();
      const bad = await waitMessage(fresh, /invalid credentials/, 15000);
      ledger.check('旧口令立即失效（后端 detail 原样透出）', bad.includes('invalid credentials'), bad);
      ledger.check('失败后仍停在登录页', /#\/login/.test(fresh.url()), fresh.url());
      await fresh.getByPlaceholder('密码').fill(S_PASS_NEW);
      clearMessages(fresh);
      const landing = fresh.waitForURL(/#\/(home|access-denied)/, { timeout: 30000 }).catch(() => null);
      await fresh.getByRole('button', { name: '登录' }).click();
      await landing;
      const good = await waitMessage(fresh, /登录成功/, 15000);
      ledger.check('新口令能登录（重置真的落库）',
        good.includes('登录成功') && !/#\/login/.test(fresh.url()), `${good} @ ${fresh.url()}`);
    } finally {
      await context.close().catch(() => {});
    }
  });

  await step(ledger, 'U12-用户 后端守卫：不能自禁 / 自删 admin', async () => {
    await tableRow(page, 'admin').getByRole('button', { name: '修改' }).click();
    const dlg = await dialog(page, '编辑用户');
    ledger.equal('编辑 admin 时启用开关初始为开', await switchState(dlg, '启用', page), true);
    await switchOf(dlg, '启用', page).click();
    clearMessages(page);
    await pushBtn(dlg, '保存').click();
    const msg = await waitMessage(page, /cannot disable yourself/);
    ledger.check('禁用自己被后端拒绝并原样透出', msg.includes('cannot disable yourself'), msg);
    ledger.check('失败时弹窗不关（前端只在成功路径关）', await dlg.isVisible(), norm(await dlg.innerText()).slice(0, 120));
    await switchOf(dlg, '启用', page).click();
    await pushBtn(dlg, '取消').click();
    await page.waitForTimeout(500);
    ledger.equal('admin 行仍是启用', (await cellsOf(tableRow(page, 'admin')))[4], '启用');

    const dd = await openRowDropdown(page, 'admin');
    await dd.item('删除').click();
    const box = await readMessageBox(page);
    ledger.equal('删除 admin 的确认框正文', box.body, '确认删除用户“admin”？删除后无法恢复。');
    clearMessages(page);
    await clickMessageBoxButton(page, '删除');
    const msg2 = await waitMessage(page, /cannot delete yourself/);
    ledger.check('删除自己被后端拒绝', msg2.includes('cannot delete yourself'), msg2);
    ledger.equal('admin 行还在', await tableRow(page, 'admin').count(), 1);
    await closeAllDialogs(page);
  });

  await step(ledger, 'U12-用户 删除自建用户', async () => {
    const dd = await openRowDropdown(page, S_USER);
    await dd.item('删除').click();
    const box = await readMessageBox(page);
    ledger.equal('删除确认框标题', box.title, '删除用户');
    ledger.equal('确认框正文带自建用户名', box.body, `确认删除用户“${S_USER}”？删除后无法恢复。`);
    ledger.check('按钮是 取消 / 删除', box.buttons.join('|').includes('删除') && box.buttons.join('|').includes('取消'),
      box.buttons.join('|'));
    clearReq(page);
    await clickMessageBoxButton(page, '取消');
    const canceled = await recentRequests(page, /\/api\/users\/\d+$/);
    ledger.equal('取消不发 DELETE 请求', canceled.length, 0);
    ledger.equal('取消后行还在', await tableRow(page, S_USER).count(), 1);

    const dd2 = await openRowDropdown(page, S_USER);
    await dd2.item('删除').click();
    await readMessageBox(page);
    clearMessages(page);
    await clickMessageBoxButton(page, '删除');
    const msg = await waitMessage(page, new RegExp(`用户“${esc(S_USER)}”已删除`));
    ledger.check('删除提示逐字命中', msg.includes('已删除'), msg);
    await waitFor(async () => ((await tableRow(page, S_USER).count()) === 0 ? true : null), { label: '行从列表消失' });
    ledger.equal('列表回到内置三行', await page.locator('.el-table__body .el-table__row').count(), 3);
  });
}

const hasTagCls = (cls, type) => (cls ?? '').includes(`el-tag--${type}`);

/** 只读下拉的选项清单（读完按 Escape，不改选择）。 */
async function optionsIn(page, select) {
  await select.waitFor({ state: 'visible', timeout: 8000 });
  await select.click();
  const dd = page.locator('.el-select-dropdown:visible').last();
  const items = dd.locator('.el-select-dropdown__item');
  await items.first().waitFor({ state: 'visible', timeout: 8000 });
  const texts = (await items.allInnerTexts()).map(norm);
  await page.keyboard.press('Escape');
  await page.waitForTimeout(250);
  return texts;
}

/** 打开新建菜单弹窗（类型 radio 与上级下拉的口径全在这里验）。 */
async function openMenuDialog(page, ledger) {
  await page.getByRole('button', { name: '新建菜单' }).click();
  return dialog(page, '新建菜单');
}

async function createMenu(page, ledger, { type, parent, title, name, path, component, rank }) {
  const dlg = await openMenuDialog(page, ledger);
  await item(dlg, '类型', page).locator('.el-radio').filter({ hasText: type }).first().click();
  await page.waitForTimeout(400);
  if (parent !== null) await pickIn(page, item(dlg, '上级', page).locator('.el-select').first(), parent);
  await inputOf(dlg, '标题', page).fill(title);
  await inputOf(dlg, '名称/权限码', page).fill(name);
  // 防御性清场：payload 构建只对 component/icon/show_link 按 menu_type 守卫，path 没守卫
  // （menus/index.vue buildPayload）。本轮第 3 轮全量曾出现一次权限节点带着上一个页面
  // 菜单的 path 落库（audit id 1346），两次复现均未再触发——这里把不填且可见的字段显式
  // 清空，与"用户会核对输入"的真实操作一致；buildPayload 的 path 守卫缺失记为观察项。
  // 注意权限类型下 路径/组件 被 v-if 摘除，清场前先判存在。
  if (path === undefined && (await item(dlg, '路径', page).count()) > 0) await inputOf(dlg, '路径', page).fill('');
  if (component === undefined && (await item(dlg, '组件', page).count()) > 0) await inputOf(dlg, '组件', page).fill('');
  if (rank !== undefined) await item(dlg, '排序', page).locator('input').first().fill(String(rank));
  // 提交前核对回填：弹窗收放/类型切换的竞态下 fill 偶发没落进 v-model——第 3 轮全量
  // 出现过权限节点带上一菜单的 path 落库（audit id 1346）、第 5 轮出现过目录带空路径
  // 提交被「请填写路径」拦下（POST 根本没发出）。与真实用户一致：提交前把填过的字段
  // 读一遍，发现值不对就重填一次。
  for (const [label, value] of [
    ['标题', title],
    ['名称/权限码', name],
    ...(path !== undefined ? [['路径', path]] : []),
    ...(component !== undefined ? [['组件', component]] : []),
  ]) {
    const input = inputOf(dlg, label, page);
    await waitFor(async () => ((await input.inputValue()) === value ? true : null), { label: `${label} 回填核对`, timeout: 4000 })
      .catch(async () => {
        await input.fill(value);
        await waitFor(async () => ((await input.inputValue()) === value ? true : null), { label: `${label} 重填核对`, timeout: 4000 });
      });
  }
  clearMessages(page);
  let cap = captureRequests(page, /\/api\/menus$/);
  await pushBtn(dlg, '创建').click();
  // 静默未提交兜底：弹窗收放动画期的点击可能落空（实测一次：无 POST、无红字、无消息）。
  // 8s 内没有 POST 时按真实用户行为处理——有校验红字就大声失败，否则再点一次「创建」。
  try {
    await cap.last('POST /menus', 8000);
  } catch {
    const errs = await formErrors(page);
    cap.stop();
    if (errs.length > 0) throw new Error(`创建被表单校验拦截：${JSON.stringify(errs)}`);
    clearMessages(page);
    cap = captureRequests(page, /\/api\/menus$/);
    await pushBtn(dlg, '创建').click();
  }
  const msg = await waitMessage(page, new RegExp(`菜单“${esc(title || name)}”已创建`));
  cap.stop();
  await waitFor(async () => ((await dlg.isVisible().catch(() => false)) ? null : true), { label: '创建弹窗关闭' });
  return msg;
}

// ---------------------------------------------------------------------------
// U12-B 菜单管理：树表口径 → 内置只读 → 三段校验 → 目录/页面/权限 三级链
// → 动态路由真的进侧边栏并可打开 → 重名守卫 → 排序可改。
// ---------------------------------------------------------------------------
async function menusLoop(ledger, st, page) {
  const treeBefore = flattenMenus(await st.api.get('/menus/tree').catch(() => []));

  await step(ledger, 'U12-菜单 树表口径', async () => {
    ledger.equal('菜单表头九列', (await tableHeaderTexts(page)).join('|'),
      '标题|类型|名称/权限码|路径|组件|排序|显示|状态|操作');
    const note = norm(await page.locator('.page-foot span').last().innerText());
    ledger.equal('灰条说明逐字命中', note,
      '内置菜单由系统维护，不能删除；名称、类型、上级、路径、组件不可修改，仅可调整标题、图标、排序与显示状态。');
    const row = tableRow(page, 'Users');
    const cells = await cellsOf(row);
    ledger.equal('内置页面行的九列取值', cells.slice(0, 8).join('|'),
      '用户管理|页面|Users|/system/users|system/users/index|1|是|启用');
    ledger.equal('内置行操作列只有 修改 + 内置标签', cells[8], '修改 内置');
    ledger.check('内置行没有删除按钮', await row.getByRole('button', { name: '删除' }).count() === 0,
      String(await row.getByRole('button', { name: '删除' }).count()));
    ledger.check('类型标签按目录/页面/权限配色',
      hasTagCls(await row.locator('.el-tag').first().getAttribute('class'), 'primary'),
      await row.locator('.el-tag').first().getAttribute('class'));
    ledger.equal('目录行的类型标签是 info 配色',
      hasTagCls(await tableRow(page, 'LogHarvest').locator('.el-tag').first().getAttribute('class'), 'info'), true);
    ledger.equal('权限行的类型标签是 warning 配色',
      hasTagCls(await tableRow(page, 'jobs:read').locator('.el-tag').first().getAttribute('class'), 'warning'), true);
    ledger.equal('权限行的组件列是破折号', (await cellsOf(tableRow(page, 'jobs:read')))[4], '—');
  });

  await step(ledger, 'U12-菜单 搜索是纯客户端树过滤', async () => {
    const total = await page.locator('.el-table__body .el-table__row').count();
    clearReq(page);
    await page.getByPlaceholder('搜索标题 / 权限码 / 路径').fill('操作日志');
    await page.getByRole('button', { name: '搜索', exact: true }).click();
    await page.waitForTimeout(700);
    const hits = await recentRequests(page, /\/api\/menus/);
    ledger.equal('点搜索不发任何菜单请求', hits.length, 0);
    const fewer = await page.locator('.el-table__body .el-table__row').count();
    ledger.check('过滤后行数收缩但保留祖先链', fewer > 0 && fewer < total, `过滤前=${total} 过滤后=${fewer}`);
    ledger.equal('命中节点在树里', await tableRow(page, 'AuditLogs').count(), 1);
    await page.getByRole('button', { name: '重置' }).click();
    await page.waitForTimeout(700);
    ledger.equal('重置恢复整棵树', await page.locator('.el-table__body .el-table__row').count(), total);
  });

  await step(ledger, 'U12-菜单 内置行的编辑弹窗是半锁定', async () => {
    await tableRow(page, 'Users').getByRole('button', { name: '修改' }).click();
    const dlg = await dialog(page, '编辑菜单');
    ledger.check('内置提示条逐字命中',
      (await dlg.locator('.el-alert__title').innerText()).includes(
        '这是内置菜单：名称、类型、上级、路径与组件由系统固定，只能修改展示类字段。'),
      norm(await dlg.locator('.el-alert__title').innerText()));
    for (const label of ['名称/权限码', '路径', '组件']) {
      ledger.equal(`编辑内置时「${label}」禁用`, await inputOf(dlg, label, page).isEditable(), false);
    }
    ledger.check('编辑内置时上级下拉禁用（取样式类，根节点自身不报 disabled）',
      (await item(dlg, '上级', page).locator('.el-select__wrapper').first()
        .getAttribute('class')).includes('is-disabled'),
      await item(dlg, '上级', page).locator('.el-select__wrapper').first().getAttribute('class'));
    // isTypeLocked 落在 el-radio 上（is-disabled 类 + input disabled），radio-group 本身
    // 没有 aria-disabled 属性——按实际渲染断言（menus/index.vue:486 :disabled="isTypeLocked"）。
    const checkedRadio = item(dlg, '类型', page).locator('.el-radio.is-checked').first();
    ledger.check('编辑态类型单选被锁死',
      (await checkedRadio.getAttribute('class')).includes('is-disabled') && (await checkedRadio.locator('input').isDisabled()),
      `class=${await checkedRadio.getAttribute('class')}`);
    ledger.equal('标题仍可改', await inputOf(dlg, '标题', page).isEditable(), true);
    await shot(page, ledger, 'menu_builtin_locked');
    await pushBtn(dlg, '取消').click();
    await page.waitForTimeout(500);
    ledger.skip('后端守卫 built-in menu cannot change name',
      '前端把名称/路径/组件/上级全禁用并锁类型，UI 无从触发；该守卫只在 API 层验证（rbac_flow 已覆盖）。');
  });

  await step(ledger, 'U12-菜单 新建的必填集与类型切换口径', async () => {
    const dlg = await openMenuDialog(page, ledger);
    ledger.equal('默认类型是页面', norm(await item(dlg, '类型', page).locator('.el-radio.is-checked').innerText()), '页面');
    // 新建时 parent_id 初始化为数值 0（ROOT_VALUE，menus/index.vue:22/126），而页面/权限
    // 类型的上级选项里没有 value=0 的项（「根目录」只在目录类型注入）——el-select 找不到
    // 匹配选项就把原始值渲染出来，placeholder 文案实际不可见。按真实渲染断言，并记录观察项。
    ledger.equal('页面态上级显示原始值 0（placeholder 被数值模型顶掉）',
      await item(dlg, '上级', page).locator('.el-select__placeholder').first().innerText(), '0');
    await pushBtn(dlg, '创建').click();
    await page.waitForTimeout(700);
    const errs = (await formErrors(page)).sort();
    ledger.equal('页面类型的必填集五条', errs.join('|'),
      ['请填写标题', '请填写名称/权限码', '请填写路径', '请填写组件路径', '页面/权限必须选择上级节点'].sort().join('|'));

    await item(dlg, '类型', page).locator('.el-radio').filter({ hasText: '权限' }).first().click();
    await page.waitForTimeout(500);
    ledger.equal('权限态上级显示原始值 0（同上）', await item(dlg, '上级', page).locator('.el-select__placeholder').first().innerText(), '0');
    ledger.equal('权限态名称占位', await inputOf(dlg, '名称/权限码', page).getAttribute('placeholder'), '如 jobs:read');
    ledger.skip('观察项：新建页面/权限时「上级」显示原始值 0 而不是 placeholder 文案',
      'parent_id 初始化为数值 0（menus/index.vue:22 ROOT_VALUE），页面/权限类型的选项里没有 value=0 的项'
      + '（「根目录」只在目录类型注入），el-select 找不到匹配项就渲染原始值；校验会拦（页面/权限必须选择上级节点），'
      + '仅观感问题——建议新建页面/权限时 parent_id 初始为 null 让 placeholder 露出来');
    // 权限类型渲染 6 个表单项（类型/上级/标题/名称/权限码/排序/启用），路径/组件/图标/
    // 显示在菜单/页面缓存都被 v-if 摘掉（menus/index.vue:530-585）。用 __label 选择器：
    // 裸 `label` 会把类型 radio 的 目录/页面/权限 三个 <label> 也算进来。
    ledger.equal('权限态没有路径/组件/图标/显示在菜单字段',
      (await dlg.locator('.el-form-item__label').allInnerTexts()).map(norm).join('|'),
      '类型|上级|标题|名称/权限码|排序|启用');
    await inputOf(dlg, '标题', page).fill('临时权限');
    await inputOf(dlg, '名称/权限码', page).fill('Bad-Code');
    await pushBtn(dlg, '创建').click();
    await page.waitForTimeout(700);
    ledger.check('权限码格式红字逐字命中',
      (await formErrors(page)).includes('权限码需以小写字母开头，仅含小写字母、数字、: _ -'),
      JSON.stringify(await formErrors(page)));
    await pushBtn(dlg, '取消').click();
    await page.waitForTimeout(500);
  });

  await step(ledger, 'U12-菜单 上级下拉按类型过滤', async () => {
    const dlg = await openMenuDialog(page, ledger);
    const dirOpts = await optionsIn(page, item(dlg, '上级', page).locator('.el-select').first());
    ledger.check('页面类型的上级只有目录（无根目录项）',
      dirOpts.length > 0 && !dirOpts.some((t) => t === '根目录') && dirOpts.some((t) => t.includes('（目录）')),
      JSON.stringify(dirOpts.slice(0, 12)));
    await item(dlg, '类型', page).locator('.el-radio').filter({ hasText: '目录' }).first().click();
    await page.waitForTimeout(500);
    const rootOpts = await optionsIn(page, item(dlg, '上级', page).locator('.el-select').first());
    ledger.check('目录类型多一个「根目录」', rootOpts.includes('根目录')
      && rootOpts.some((t) => t.includes('（目录）')), JSON.stringify(rootOpts.slice(0, 12)));
    await pushBtn(dlg, '取消').click();
    await page.waitForTimeout(500);
  });

  await step(ledger, 'U12-菜单 建出目录→页面→权限三级链', async () => {
    const m1 = await createMenu(page, ledger, {
      type: '目录', parent: '根目录', title: M_DIR_TITLE, name: M_DIR_NAME, path: M_DIR_PATH, rank: 909,
    });
    ledger.check('目录创建提示', m1.includes(M_DIR_TITLE), m1);
    const dirRow = tableRow(page, M_DIR_NAME);
    await dirRow.waitFor({ state: 'visible', timeout: 10000 });
    const dirCells = await cellsOf(dirRow);
    ledger.equal('目录行的列取值', [dirCells[0], dirCells[1], dirCells[2], dirCells[3], dirCells[4], dirCells[5]].join('|'),
      `${M_DIR_TITLE}|目录|${M_DIR_NAME}|${M_DIR_PATH}|—|909`);

    const m2 = await createMenu(page, ledger, {
      type: '页面', parent: M_DIR_TITLE, title: M_PAGE_TITLE, name: M_PAGE_NAME,
      path: M_PAGE_PATH, component: M_PAGE_COMP, rank: 1,
    });
    ledger.check('页面创建提示', m2.includes(M_PAGE_TITLE), m2);
    const pageCells = await cellsOf(tableRow(page, M_PAGE_NAME));
    ledger.equal('页面行的组件列写实值', pageCells[4], M_PAGE_COMP);

    const m3 = await createMenu(page, ledger, {
      type: '权限', parent: M_PAGE_TITLE, title: M_PERM_TITLE, name: M_PERM_NAME,
    });
    ledger.check('权限创建提示', m3.includes(M_PERM_TITLE), m3);
    const permCells = await cellsOf(tableRow(page, M_PERM_NAME));
    // 权限节点不填 path 时由后端继承父页面的 path（rbac.py:463-465 create_menu 的刻意设计），
    // 行上显示的就是父页面的路径，组件列才是「—」。
    ledger.equal('权限行的 类型|路径|组件|显示（path 继承父页面）', [permCells[1], permCells[3], permCells[4], permCells[6]].join('|'),
      `权限|${M_PAGE_PATH}|—|否`);
    ledger.skip('观察项：权限节点的 path 由后端继承父页面（不是前端漏清）',
      'create_menu 在 payload.path 为空时取 parent.path（rbac.py:463-465）；权限行因此总是显示父页面的路径，属既定行为');
    const ids = await st.api.get('/menus/tree').then((t) => flattenMenus(t));
    st.myMenuIds = {
      dir: ids.find((m) => m.name === M_DIR_NAME)?.id,
      page: ids.find((m) => m.name === M_PAGE_NAME)?.id,
      perm: ids.find((m) => m.name === M_PERM_NAME)?.id,
    };
    ledger.check('三个自建模都能从树里取到 id',
      Object.values(st.myMenuIds).every((v) => typeof v === 'number' && v > 0), JSON.stringify(st.myMenuIds));
    await shot(page, ledger, 'menu_three_level_tree');
  });

  await step(ledger, 'U12-菜单 新菜单进侧边栏并真的能打开', async () => {
    const fresh = await go(page, '/system/menus', { reload: true, waitText: '名称/权限码' });
    const menu = await menuTexts(fresh);
    // pure-admin 的 SidebarItem 对"只有一个可见子页"的目录折叠成叶子项（不渲染分组标题），
    // 自建目录只有一个页面，所以侧边栏出现的是页面项而不是目录分组——这是框架行为不是缺陷。
    if (menu.groups.includes(M_DIR_TITLE)) {
      ledger.check('侧边栏出现自建目录分组', true, menu.groups.join(','));
      ledger.check('侧边栏出现自建页面', menu.items.includes(M_PAGE_TITLE), menu.items.join(','));
    } else {
      ledger.check('侧边栏出现自建页面（单子目录被折叠成叶子项）', menu.items.includes(M_PAGE_TITLE),
        `groups=${JSON.stringify(menu.groups)} items=${JSON.stringify(menu.items).slice(0, 200)}`);
      ledger.skip('观察项：单子页目录在侧边栏折叠成叶子项（pure-admin SidebarItem 常规行为）',
        `目录 ${M_DIR_TITLE} 只有 ${M_PAGE_TITLE} 一个子页，分组标题不渲染；想要分组可在目录下再建一个页面`);
    }
    ledger.check('权限节点不进菜单', !menu.items.includes(M_PERM_TITLE) && !menu.groups.includes(M_PERM_TITLE),
      JSON.stringify([...menu.groups, ...menu.items]).slice(0, 300));
    await go(page, M_PAGE_PATH, { waitText: '分析作业' });
    ledger.check('动态路由指向的组件真渲染（复用作业列表视图）',
      (await bodyText(page)).includes('分析作业'), page.url());
    await go(page, '/system/menus', { waitText: '菜单管理' });
  });

  await step(ledger, 'U12-菜单 重名守卫与排序可改', async () => {
    const dlg = await openMenuDialog(page, ledger);
    await item(dlg, '类型', page).locator('.el-radio').filter({ hasText: '目录' }).first().click();
    await page.waitForTimeout(400);
    await pickIn(page, item(dlg, '上级', page).locator('.el-select').first(), '根目录');
    await inputOf(dlg, '标题', page).fill('重名试验');
    await inputOf(dlg, '名称/权限码', page).fill(M_DIR_NAME);
    await inputOf(dlg, '路径', page).fill(`/ui-e2e-dup-${RUN_TS}`);
    clearMessages(page);
    await pushBtn(dlg, '创建').click();
    const msg = await waitMessage(page, /menu name already exists/);
    ledger.check('重名由后端拒绝并原样透出', msg.includes('menu name already exists'), msg);
    ledger.check('失败时弹窗保持打开', await dlg.isVisible(), norm(await dlg.innerText()).slice(0, 120));
    await pushBtn(dlg, '取消').click();
    await page.waitForTimeout(500);

    await tableRow(page, M_PAGE_NAME).getByRole('button', { name: '修改' }).click();
    const edit = await dialog(page, '编辑菜单');
    const editChecked = item(edit, '类型', page).locator('.el-radio.is-checked').first();
    ledger.check('编辑态类型仍锁死',
      (await editChecked.getAttribute('class')).includes('is-disabled') && (await editChecked.locator('input').isDisabled()),
      `class=${await editChecked.getAttribute('class')}`);
    await item(edit, '排序', page).locator('input').first().fill('7');
    clearMessages(page);
    const cap = captureRequests(page, /\/api\/menus\/\d+$/);
    await pushBtn(edit, '保存').click();
    const msg2 = await waitMessage(page, new RegExp(`菜单“${esc(M_PAGE_TITLE)}”已更新`));
    const req = await cap.last('PUT /menus/{id}');
    cap.stop();
    ledger.check('更新提示逐字命中', msg2.includes('已更新'), msg2);
    ledger.equal('PUT 打在自建页面 id 上', new URL(req.url).pathname, `/api/menus/${st.myMenuIds.page}`);
    ledger.equal('请求体里的排序是新值', jsonOf(req)?.rank, 7);
    await waitFor(async () => ((await cellsOf(tableRow(page, M_PAGE_NAME)))[5] === '7' ? true : null),
      { label: '排序列回填' });
  });

  await step(ledger, 'U12-菜单 有子的目录删不掉', async () => {
    await tableRow(page, M_DIR_TITLE).getByRole('button', { name: '删除' }).click();
    const box = await readMessageBox(page);
    ledger.equal('删除菜单确认框标题', box.title, '删除菜单');
    ledger.equal('确认框正文写明连带清授权', box.body,
      `确认删除菜单“${M_DIR_TITLE}”？其权限授权会被一并清理，且无法恢复。`);
    clearMessages(page);
    await clickMessageBoxButton(page, '删除');
    const msg = await waitMessage(page, /menu still has children/);
    ledger.check('后端拒绝删除有子目录', msg.includes('menu still has children'), msg);
    ledger.equal('目录行仍在', await tableRow(page, M_DIR_NAME).count(), 1);
  });
}

// ---------------------------------------------------------------------------
// U12-C 角色管理：列表口径 → 客户端筛选（三段）→ 新建校验 → 创建 + 重名守卫
// → 修改 → 内置角色半锁定 → 权限面板（只读 / 可写保存）→ 授权占用守卫 → 删除。
// ---------------------------------------------------------------------------

const roleFoot = (page) => page.locator('.page-foot').last();
const footSpans = async (page) => (await roleFoot(page).locator('span').allInnerTexts()).map(norm);
const totalOf = (t) => Number((String(t).match(/共 (\d+) 条/) ?? [])[1] ?? -1);
const roleRows = (page) => page.locator('.el-table__body .el-table__row');
const rowTags = async (row) => (await row.locator('.el-tag').allInnerTexts()).map(norm).join('|');
const searchSelect = (page) => page.locator('.search-form .el-select').first();

async function rolesLoop(ledger, st, page) {
  const apiRoles = () => st.api.get('/roles?limit=500').catch(() => []);

  await step(ledger, 'U12-角色 列表口径', async () => {
    ledger.equal('角色表头五列', (await tableHeaderTexts(page)).join('|'), 'ID|角色名|角色标识|状态|操作');
    const baseline = await apiRoles();
    // 同 usersLoop：先等行数追上接口基线，再读页脚，避免拍到「共 0 条」的过渡帧。
    await waitFor(async () =>
      ((await roleRows(page).count()) === baseline.length ? true : null),
      { label: '角色首屏行渲染到位', timeout: 15000 });
    const foot = await footSpans(page);
    ledger.equal('脚注写明内置口径', foot[0], '内置角色由系统维护：不可删除，角色代码、启用状态与菜单权限均只读。');
    ledger.equal('「共 N 条」等于后端行数', totalOf(foot[1] ?? ''), baseline.length);
    ledger.equal('可见行数等于后端行数', await roleRows(page).count(), baseline.length);

    const adminRow = tableRow(page, '管理员');
    const cells = await cellsOf(adminRow);
    ledger.equal('管理员行的名称列', cells[1], '管理员 内置 系统管理员，拥有全部权限');
    ledger.equal('管理员行的标识列', cells[2], 'admin');
    ledger.equal('管理员行的状态列', cells[3], '启用');
    ledger.equal('管理员行的操作列（无删除）', cells[4], '修改 权限');
    ledger.equal('内置行的标签对', await rowTags(adminRow), '内置|启用');
    ledger.check('内置标签是 warning 配色',
      hasTagCls(await adminRow.locator('.el-tag').first().getAttribute('class'), 'warning'),
      await adminRow.locator('.el-tag').first().getAttribute('class'));
    // 只有 admin 是 is_system；operator/viewer 是库里普通行，页面上标「自定义」。
    ledger.equal('运维人员行的标签对（非内置）', await rowTags(tableRow(page, '运维人员')), '自定义|启用');
    ledger.equal('运维人员行的操作列（有删除，但本轮绝不点）', (await cellsOf(tableRow(page, '运维人员')))[4], '修改 权限 删除');
  });

  await step(ledger, 'U12-角色 筛选是纯客户端且点搜索才生效', async () => {
    const cap = captureRequests(page, /\/api\/roles/);
    try {
      await page.getByPlaceholder('请输入角色名称').fill('只读');
      ledger.equal('只填不点搜索不生效', totalOf((await footSpans(page))[1] ?? ''), 3);
      await page.getByRole('button', { name: '搜索' }).click();
      ledger.equal('点搜索后只剩 1 条', totalOf((await footSpans(page))[1] ?? ''), 1);
      ledger.equal('筛选后可见行数', await roleRows(page).count(), 1);
      // 行文本以 ID 列开头（ID 列在桌面端渲染），断言用包含而不是前缀。
      const onlyRow = norm(await roleRows(page).first().innerText());
      ledger.check('剩下的就是只读用户', onlyRow.includes('只读用户') && !onlyRow.includes('运维人员') && !onlyRow.includes('管理员'),
        onlyRow);

      await page.getByRole('button', { name: '重置' }).click();
      ledger.equal('重置回到 3 条', totalOf((await footSpans(page))[1] ?? ''), 3);
      ledger.equal('重置清空名称输入框', await page.getByPlaceholder('请输入角色名称').inputValue(), '');

      await page.getByPlaceholder('请输入角色标识').fill('admin');
      await page.getByRole('button', { name: '搜索' }).click();
      ledger.equal('按标识 admin 只剩 1 条', totalOf((await footSpans(page))[1] ?? ''), 1);
      await page.getByRole('button', { name: '重置' }).click();

      await pickIn(page, searchSelect(page), '已停用');
      await page.getByRole('button', { name: '搜索' }).click();
      ledger.equal('已停用：0 条', totalOf((await footSpans(page))[1] ?? ''), 0);
      ledger.equal('已停用：表格没有数据行', await roleRows(page).count(), 0);
      const emptyText = norm(await page.locator('.el-table__empty-text').innerText().catch(() => ''));
      ledger.check('空结果走 el-table 空态文案', emptyText.length > 0, emptyText);

      await pickIn(page, searchSelect(page), '已启用');
      await page.getByRole('button', { name: '搜索' }).click();
      ledger.equal('已启用：回到 3 条', totalOf((await footSpans(page))[1] ?? ''), 3);
      ledger.check('筛选全程零请求（纯客户端过滤）', cap.got.length === 0, JSON.stringify(cap.got.map((r) => r.url)));
    } finally {
      cap.stop();
      await page.getByRole('button', { name: '重置' }).click();
      await page.waitForTimeout(400);
    }
  });

  await step(ledger, 'U12-角色 新建必填与代码格式', async () => {
    await page.getByRole('button', { name: '新建角色' }).click();
    const dlg = await dialog(page, '新建角色');
    ledger.equal('表单项顺序', (await dlg.locator('.el-form-item__label').allInnerTexts()).map(norm).join('|'),
      '角色名|角色代码|描述|启用');
    ledger.equal('两个输入框的占位符',
      (await dlg.locator('input').evaluateAll((els) => els.map((e) => e.placeholder).filter(Boolean))).join('|'),
      '面向管理员的显示名称|如 ops_admin，创建后不可修改');
    clearMessages(page);
    await pushBtn(dlg, '创建').click();
    // validate() 的红字是 nextTick 渲染：点了立刻读会拿到 []（U12 首轮实测踩坑）。
    await waitFor(async () => ((await formErrors(page)).length >= 2 ? true : null), { label: '必填红字渲染', timeout: 6000 }).catch(() => {});
    const errs = await formErrors(page);
    ledger.check('空表单拦住两个必填',
      errs.includes('请填写角色名称') && errs.includes('请填写角色代码'), JSON.stringify(errs));
    await inputOf(dlg, '角色代码', page).fill('Bad_Code');
    await inputOf(dlg, '角色名', page).click();
    await page.waitForTimeout(500);
    const fmt = await formErrors(page);
    ledger.check('代码格式规则逐字命中',
      fmt.includes('以小写字母开头，仅含小写字母、数字、_ 或 -，长度 2-50'), JSON.stringify(fmt));
    await pushBtn(dlg, '取消').click();
    await page.waitForTimeout(500);
    ledger.equal('取消后弹窗消失', await page.locator('.el-dialog:visible').count(), 0);
  });

  await step(ledger, 'U12-角色 创建自建角色并被重名守卫拦住', async () => {
    const cap = captureRequests(page, /\/api\/roles$/);
    try {
      await page.getByRole('button', { name: '新建角色' }).click();
      const dlg = await dialog(page, '新建角色');
      await inputOf(dlg, '角色名', page).fill(R_NAME);
      await inputOf(dlg, '角色代码', page).fill(R_CODE);
      await item(dlg, '描述', page).locator('textarea').first().fill('UI 自建，用于权限面板');
      clearMessages(page);
      await pushBtn(dlg, '创建').click();
      const msg = await waitMessage(page, new RegExp(`角色“${esc(R_NAME)}”已创建`));
      ledger.check('创建提示逐字命中', msg.includes(R_NAME) && msg.includes('已创建'), msg);
      const req = await cap.last('POST /roles');
      ledger.equal('POST 打在 /api/roles', new URL(req.url).pathname, '/api/roles');
      const body = jsonOf(req) ?? {};
      ledger.equal('请求体字段集合', Object.keys(body).sort().join(','), 'code,description,is_active,name');
      ledger.equal('请求体 code', body.code, R_CODE);
      ledger.equal('请求体 name', body.name, R_NAME);
      ledger.equal('请求体 description', body.description, 'UI 自建，用于权限面板');
      ledger.equal('请求体 is_active', body.is_active, true);
      await waitFor(async () => (totalOf((await footSpans(page))[1] ?? '') === 4 ? true : null), { label: '列表多出第 4 行' });
      const myRow = tableRow(page, R_NAME);
      ledger.equal('新行的标识列', (await cellsOf(myRow))[2], R_CODE);
      ledger.equal('新行的标签对', await rowTags(myRow), '自定义|启用');
      ledger.check('自定义标签是 info 配色',
        hasTagCls(await myRow.locator('.el-tag').first().getAttribute('class'), 'info'),
        await myRow.locator('.el-tag').first().getAttribute('class'));
      ledger.check('新行有删除按钮', await myRow.getByRole('button', { name: '删除' }).count() === 1, 'count');
      st.myRoleId = (await apiRoles()).find((r) => r.code === R_CODE)?.id ?? null;
      ledger.check('取到自建角色 id（后续请求路径要用）', typeof st.myRoleId === 'number' && st.myRoleId > 0, String(st.myRoleId));
    } finally {
      cap.stop();
    }

    await page.getByRole('button', { name: '新建角色' }).click();
    const dlg2 = await dialog(page, '新建角色');
    await inputOf(dlg2, '角色名', page).fill('重名试验');
    await inputOf(dlg2, '角色代码', page).fill(R_CODE);
    clearMessages(page);
    await pushBtn(dlg2, '创建').click();
    const dup = await waitMessage(page, /role code already exists/);
    ledger.check('同代码由后端拒绝并原样透出', dup.includes('role code already exists'), dup);
    ledger.check('被拒时弹窗保持打开', await dlg2.isVisible(), norm(await dlg2.innerText()).slice(0, 120));
    await pushBtn(dlg2, '取消').click();
    await page.waitForTimeout(500);
    ledger.equal('重名试验没有多出一行', totalOf((await footSpans(page))[1] ?? ''), 4);
  });
}

// ==== 阶段实现 ====
// 上半部分的 usersLoop / menusLoop / rolesLoop 只跑"页面事实"，进入哪个子页、
// 按什么顺序跑由这里的三个 phase 函数编排；收尾统一走 ensureTeardown。

async function phase_system(ledger, st, { PHASE_LABEL }) {
  ledger.phase(PHASE_LABEL.system);
  const page = st.adminPage ?? await useSession(ledger, st, 'admin');
  await preClean(ledger, st);
  ensureTeardown(st);

  // 顺序 = 视图清单顺序（§13 用户 → §14 角色 → §15 菜单）。
  // 三个循环彼此独立：users 删掉自建用户、roles/menus 的自建行由收尾清理。
  await go(page, '/system/users', { waitText: '昵称' });
  await usersLoop(ledger, st, page);

  await go(page, '/system/roles', { waitText: '角色标识' });
  await rolesLoop(ledger, st, page);

  await go(page, '/system/menus', { waitText: '名称/权限码' });
  await menusLoop(ledger, st, page);
  await shot(page, ledger, 'system_final');
}

// ---------------------------------------------------------------------------
// U13 操作日志：页面口径 → 动作族/对象类型/操作人/时间范围/对象 ID 五路下推
// → 操作人快链（就地 7 天）→ 窗口翻页 → 详情抽屉 → viewer 无菜单直达 404。
// ---------------------------------------------------------------------------

const auditRows = (page) => page.locator('.el-table__body .el-table__row');
const auditFoot = async (page) => norm(await page.locator('.page-foot span').first().innerText());
const lastQuery = async (page, re) => {
  const reqs = await recentRequests(page, re);
  const req = reqs[reqs.length - 1];
  return req ? Object.fromEntries(new URL(req.url).searchParams) : {};
};

/** 首个"操作人可点链接"所在行（actor_id=null 的行渲染成灰字「账号已删除」，不可点）。 */
async function firstActorButton(page) {
  const rows = auditRows(page);
  const n = await rows.count();
  for (let i = 0; i < Math.min(n, 5); i += 1) {
    const btn = rows.nth(i).locator('td').nth(1).locator('button');
    if ((await btn.count()) > 0) return btn.first();
  }
  return null;
}

async function phase_audit(ledger, st, { PHASE_LABEL }) {
  ledger.phase(PHASE_LABEL.audit);
  const page = st.adminPage ?? await useSession(ledger, st, 'admin');
  const LIST_RE = /\/api\/operation-logs(\?|$)/;

  await go(page, '/system/audit-logs', { waitText: '时间范围' });

  await step(ledger, 'U13 页面口径与首屏请求', async () => {
    const body = await bodyText(page);
    ledger.check('灰字说明逐字（本地时区显示 / UTC 存储 / 无总数倒序）',
      body.includes('时间为浏览器本地时区显示；服务端按 UTC 存储，列表不含总数、按时间倒序'),
      body.slice(0, 240));
    ledger.equal('表头七列', (await tableHeaderTexts(page)).join('|'), '时间|操作人|动作|对象|摘要|来源 IP|操作');
    const btns = await bodyButtons(page);
    ledger.check('筛选区有 查询 / 重置', btns.includes('查询') && btns.includes('重置'), [...new Set(btns)].join(','));
    const q = await lastQuery(page, LIST_RE);
    ledger.equal('进页自动拉 limit=50&offset=0', `${q.limit}|${q.offset}`, '50|0');
    // go 的 waitText 命中的是页头标题（数据还在飞），行数断言前必须等首屏行真的渲染。
    await waitFor(async () => ((await auditRows(page).count()) > 0 ? true : null), { label: '首屏日志行渲染', timeout: 15000 });
    const n = await auditRows(page).count();
    ledger.check('首屏有日志行（前序阶段已产生写操作）', n > 0 && n <= 50, `rows=${n}`);
    const foot = await auditFoot(page);
    ledger.check('脚注窗口式口径且条数真实', foot === `第 1 页，本页 ${n} 条`, foot);
    ledger.equal('第 1 页上一页禁用', await page.getByRole('button', { name: '上一页' }).isDisabled(), true);
    ledger.equal('不足 50 条时下一页禁用', await page.getByRole('button', { name: '下一页' }).isDisabled(), n < 50);

    const fams = await selectOptionsOf(page, '动作族');
    ledger.equal('动作族九个预置项与源码一致', fams.join('|'),
      'auth.|job.|user.|role.|menu.|rule_set.|data_source.|worker.|system.');
    const rts = await selectOptionsOf(page, '对象类型');
    ledger.equal('对象类型八个预置项与源码一致', rts.join('|'),
      'job|rule_set|data_source|user|role|menu|worker|system');
    ledger.skip('观察项：时间范围不成对的 warning 拦截不可经 UI 触发',
      'datetimerange 的 model 只会是 null 或成对数组（audit-logs/index.vue:62-64 的 rangeOk 守卫没有真实入口）');
  });

  await step(ledger, 'U13 动作族筛选下推 action= 前缀', async () => {
    await pickOption(page, '动作族', 'job.');
    clearReq(page);
    await page.getByRole('button', { name: '查询' }).click();
    await page.waitForTimeout(700);
    const q = await lastQuery(page, LIST_RE);
    ledger.equal('查询请求带 action=job.', q.action ?? '', 'job.');
    const rowTexts = await auditRows(page).allInnerTexts();
    ledger.check('可见行动作列都是 job. 开头',
      rowTexts.length > 0 && rowTexts.every((t) => /job\./.test(t)), `${rowTexts.length} 行`);
    await page.getByRole('button', { name: '重置' }).click();
    await page.waitForTimeout(700);
  });

  await step(ledger, 'U13 对象类型筛选下推 resource_type=', async () => {
    await pickOption(page, '对象类型', 'user');
    clearReq(page);
    await page.getByRole('button', { name: '查询' }).click();
    await page.waitForTimeout(700);
    const q = await lastQuery(page, LIST_RE);
    ledger.equal('查询请求带 resource_type=user', q.resource_type ?? '', 'user');
    const rowTexts = await auditRows(page).allInnerTexts();
    ledger.check('可见行对象列都是 user（#id 或无 id）',
      rowTexts.length > 0 && rowTexts.every((t) => /user(#\d+)?(\s|$)/.test(t)), `${rowTexts.length} 行`);
    await page.getByRole('button', { name: '重置' }).click();
    await page.waitForTimeout(700);
  });

  await step(ledger, 'U13 操作人筛选下推 actor_id=', async () => {
    await pickOption(page, '操作人', 'admin');
    clearReq(page);
    await page.getByRole('button', { name: '查询' }).click();
    await page.waitForTimeout(700);
    const q = await lastQuery(page, LIST_RE);
    const adminId = (await st.api.get('/users?keyword=admin')).find((u) => u.username === 'admin')?.id;
    ledger.equal('查询请求带 actor_id=<admin id>', Number(q.actor_id ?? NaN), adminId);
    const rowTexts = await auditRows(page).allInnerTexts();
    ledger.check('可见行操作人都是 admin', rowTexts.length > 0 && rowTexts.every((t) => /admin/.test(t)),
      `${rowTexts.length} 行`);
    await page.getByRole('button', { name: '重置' }).click();
    await page.waitForTimeout(700);
  });

  await step(ledger, 'U13 时间范围成对下推且为 UTC Z 串', async () => {
    // datetimerange 的 popper 面板里还有一套同 placeholder 的输入框，必须锁在筛选区的
    // 编辑器里填，否则 getByPlaceholder 命中 2 个元素（strict mode violation）。
    const editor = page.locator('.search-form .el-date-editor').first();
    await editor.getByPlaceholder('开始时间').fill('2026-10-29 00:00:00');
    await editor.getByPlaceholder('结束时间').fill('2026-10-30 23:59:59');
    await page.keyboard.press('Enter');
    await page.waitForTimeout(400);
    clearReq(page);
    await page.getByRole('button', { name: '查询' }).click();
    await page.waitForTimeout(800);
    const q = await lastQuery(page, LIST_RE);
    // toApiDateTime = Date.toISOString()（format.ts:125-132），恒带毫秒。
    const zShape = /\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$/;
    ledger.check('请求带 created_from/created_to 且以 Z 结尾',
      zShape.test(decodeURIComponent(q.created_from ?? '')) && zShape.test(decodeURIComponent(q.created_to ?? '')),
      JSON.stringify({ from: q.created_from, to: q.created_to }));
    ledger.equal('时间范围模式下不带对象 ID', q.resource_id ?? '', '');
    await page.getByRole('button', { name: '重置' }).click();
    await page.waitForTimeout(700);
  });

  await step(ledger, 'U13 对象 ID 查空走 el-empty 空态', async () => {
    await page.getByPlaceholder('对象 ID').fill('no-such-object-id-e2e');
    clearReq(page);
    await page.getByRole('button', { name: '查询' }).click();
    await page.waitForTimeout(800);
    const q = await lastQuery(page, LIST_RE);
    ledger.equal('查询请求带 resource_id=', q.resource_id ?? '', 'no-such-object-id-e2e');
    ledger.equal('结果为空', await auditRows(page).count(), 0);
    const empty = await page.locator('.el-empty__description').innerText().catch(() => '');
    ledger.equal('空态文案是「尚未产生写操作」', empty.trim(), '尚未产生写操作');
    await page.getByRole('button', { name: '重置' }).click();
    await page.waitForTimeout(800);
    ledger.check('重置后行数恢复', (await auditRows(page).count()) > 0, '');
  });

  await step(ledger, 'U13 操作人快链=就地按该人并带近 7 天范围', async () => {
    const btn = await firstActorButton(page);
    if (!btn) {
      ledger.skip('操作人快链', '首屏前 5 行都是「账号已删除」灰字，没有可点链接');
      return;
    }
    const name = (await btn.innerText()).trim();
    clearReq(page);
    await btn.click();
    await page.waitForTimeout(800);
    const q = await lastQuery(page, LIST_RE);
    const uid = (await st.api.get(`/users?keyword=${encodeURIComponent(name)}`)).find((u) => u.username === name)?.id;
    ledger.equal(`快链请求带 actor_id=<${name} 的 id>`, Number(q.actor_id ?? NaN), uid);
    ledger.check('快链自动带最近 7 天时间范围', Boolean(q.created_from && q.created_to),
      JSON.stringify({ from: q.created_from ?? null, to: q.created_to ?? null }));
    const rowTexts = await auditRows(page).allInnerTexts();
    ledger.check('筛选后操作人列都是该用户', rowTexts.length > 0 && rowTexts.every((t) => t.includes(name)),
      `${rowTexts.length} 行`);
    await page.getByRole('button', { name: '重置' }).click();
    await page.waitForTimeout(700);
  });

  await step(ledger, 'U13 窗口翻页', async () => {
    const n = await auditRows(page).count();
    if (n < 50) {
      ledger.skip('翻页', `第 1 页仅 ${n} 条（< limit=50），下一页天然禁用`);
      return;
    }
    ledger.equal('满页时下一页可用', await page.getByRole('button', { name: '下一页' }).isDisabled(), false);
    clearReq(page);
    await page.getByRole('button', { name: '下一页' }).click();
    await page.waitForTimeout(900);
    const q = await lastQuery(page, LIST_RE);
    ledger.equal('第 2 页请求 offset=50', q.offset, '50');
    const foot2 = await auditFoot(page);
    ledger.check('脚注进第 2 页', foot2.startsWith('第 2 页'), foot2);
    await page.getByRole('button', { name: '上一页' }).click();
    await page.waitForTimeout(900);
    const foot1 = await auditFoot(page);
    ledger.check('上一页回到第 1 页', foot1.startsWith('第 1 页'), foot1);
  });

  await step(ledger, 'U13 详情抽屉', async () => {
    await auditRows(page).first().getByRole('button', { name: '详情' }).click();
    const drawer = page.locator('.el-drawer:visible').last();
    await drawer.waitFor({ state: 'visible', timeout: 8000 });
    ledger.equal('抽屉标题', (await drawer.locator('.el-drawer__header').innerText()).trim(), '操作详情');
    const labels = (await drawer.locator('.el-descriptions__label').allInnerTexts()).map(norm);
    ledger.equal('描述清单七个字段', labels.join('|'), 'ID|时间|操作人|动作|对象|摘要|来源 IP');
    const hasPre = (await drawer.locator('pre').count()) > 0;
    const noInfo = (await drawer.getByText('该记录没有附加信息').count()) > 0;
    ledger.check('detail 区是 JSON pre 或「该记录没有附加信息」（二选一）', hasPre !== noInfo,
      `pre=${hasPre} noInfo=${noInfo}`);
    if (hasPre) {
      const pre = (await drawer.locator('pre').first().innerText()).trim();
      let parsed = true;
      try {
        JSON.parse(pre);
      } catch {
        parsed = false;
      }
      ledger.check('detail JSON 可解析', parsed, pre.slice(0, 120));
    }
    await closeDrawer(page);
  });

  await step(ledger, 'U13 viewer 无菜单直达 = 404 页（不是 403）', async () => {
    const vpage = st.viewerPage ?? await useSession(ledger, st, 'viewer');
    await go(vpage, '/system/audit-logs');
    const body = await bodyText(vpage);
    ledger.check('viewer 直达得到 404 文案', body.includes('抱歉，你访问的页面不存在'), body.slice(0, 160));
  });

  await shot(page, ledger, 'audit_final');
}

// ---------------------------------------------------------------------------
// U15 登出：头像下拉「退出系统」→ POST /logout → cookie/localStorage 清空
// → 受保护页被守卫送回登录页。收尾钩子在这里最终确认已挂。
// ---------------------------------------------------------------------------

async function phase_logout(ledger, st, { PHASE_LABEL }) {
  ledger.phase(PHASE_LABEL.logout);
  ensureTeardown(st);
  const page = st.adminPage ?? await useSession(ledger, st, 'admin');

  await go(page, '/home', { waitText: '概览' });

  await step(ledger, 'U15 头像下拉与退出系统', async () => {
    const trigger = page.locator('.el-dropdown-link').first();
    await trigger.waitFor({ state: 'visible', timeout: 8000 });
    await trigger.click();
    const item = page.locator('.el-dropdown-menu:visible').getByText('退出系统');
    await item.waitFor({ state: 'visible', timeout: 8000 });
    clearReq(page);
    await item.click();
    await page.waitForURL(/#\/login/, { timeout: 15000 });
    const reqs = await recentRequests(page, /\/api\/logout/);
    ledger.check('退出打了 POST /api/logout', reqs.some((r) => r.method === 'POST'),
      JSON.stringify(reqs.map((r) => `${r.method} ${r.url}`)));
    const cookieNames = (await page.context().cookies()).map((c) => c.name);
    ledger.check('authorized-token 与 multiple-tabs cookie 已清',
      !cookieNames.includes('authorized-token') && !cookieNames.includes('multiple-tabs'),
      JSON.stringify(cookieNames));
    const ls = await page.evaluate(() => localStorage.getItem('user-info'));
    ledger.check('localStorage user-info 已清', ls === null, String(ls).slice(0, 120));
    ledger.check('落地在登录页', /#\/login/.test(page.url()), page.url());
  });

  await step(ledger, 'U15 退出后受保护页跳回登录', async () => {
    await go(page, '/system/users');
    const form = page.locator('form').first();
    await form.waitFor({ state: 'visible', timeout: 15000 }).catch(() => {});
    ledger.check('直达 /system/users 被守卫送回登录页（表单可见）',
      /#\/login/.test(page.url()) && (await form.isVisible().catch(() => false)), page.url());
    ledger.check('登录页账号/密码输入框回来',
      await page.getByPlaceholder('账号').isVisible().catch(() => false)
      && await page.getByPlaceholder('密码').isVisible().catch(() => false), page.url());
  });

  await shot(page, ledger, 'logout_final');
}

// __PHASES__
export const phases = { system: phase_system, audit: phase_audit, logout: phase_logout };
