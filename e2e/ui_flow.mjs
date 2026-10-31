// LogHarvest 页面全流程 E2E（Playwright + 真实 Chrome 视口 1440x900）。
//
// 口径：
// - 这个文件只断言"页面事实"——可见文案、tag 配色、DOM 是否存在、真实发出的请求。
//   Api 仅用于造前置数据（U0 建 ui-operator/ui-viewer、造大作业）与收尾清理，
//   接口结果永远不作为页面断言的替代。唯一例外是 U5b 的 SSE：那是用 page.route
//   中断"传输"，验证前端降级文案，不碰业务逻辑。
// - 文案全部逐字来自 e2e/refs/ui-map.md §17（源码 HEAD=ff23e94 抄录）；全站无
//   data-testid，所以文案即选择器。
// - 阶段可单跑：node e2e/ui_flow.mjs --phase wizard,jobs（依赖自动补齐，见 DEPS）。
// - 台账落 e2e/artifacts/ledger_ui_flow.json，形状与 python 侧 client.py::Ledger 一致。
//
// 运行前置：e2e/up.sh 已起栈（web=18080、api=18000/经 nginx、PG=15544），
// e2e/make_nas.py 已生成 e2e/artifacts/fixtures.json。

import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import {
  chromium, PW_PATH, WEB, API_BASE, ARTIFACTS, CTX_DIR, ACCOUNTS, RUN_TS, jobName,
  Api, ApiError, Ledger, newPage, messagesOf, clearMessages, waitMessage, recentRequests,
  go, esc, openSelect, pickOption, selectOptionsOf, formErrors, tableRow, tableHeaderTexts,
  bodyButtons, readMessageBox, clickMessageBoxButton, dialog, stepButton, shot, waitFor,
  sessions, openSession, useSession, cancelViaApi, waitJobTerminal, menuTexts,
  pickFormSelect, clearSelect, closeDrawer,
} from './ui_lib.mjs';

const PHASE_LABEL = {
  prep: 'U0 前置', login: 'U1 登录页', roles: 'U2 角色可见性', wizard: 'U3 建任务向导',
  jobs: 'U4 作业列表', detail: 'U5 作业详情', matches: 'U6 命中结果', datasources: 'U7 数据源',
  workers: 'U8 Worker', stats: 'U9 任务统计', rulesets: 'U10 规则集', rules: 'U11 规则维护',
  system: 'U12 系统管理', audit: 'U13 操作日志', overview: 'U14 概览维护', logout: 'U15 登出',
};

// 依赖：任何需要登录态的阶段都隐含 login；写操作阶段隐含 wizard（它产出真实作业与规则集引用）。
const DEPS = {
  login: ['prep'],
  roles: ['login'],
  wizard: ['login'],
  jobs: ['wizard'],
  detail: ['wizard'],
  matches: ['wizard'],
  datasources: ['login'],
  workers: ['login'],
  stats: ['login'],
  rulesets: ['login'],
  rules: ['login'],
  system: ['login'],
  audit: ['login'],
  overview: ['login'],
  logout: ['login'],
};

// ---------------------------------------------------------------------------
// U0 前置：造 UI 专用账号 + 解析数据源/规则集 + 探活。
//
// rbac_flow 会删掉自己建的用户，库里只剩 admin；所以 ui_flow 自建自删 ui-operator /
// ui-viewer，名字带 ui- 前缀避免和其它流程抢同一行。
// ---------------------------------------------------------------------------
async function phase_prep(ledger, st) {
  ledger.phase(PHASE_LABEL.prep);
  const { api } = st;
  await api.login(ACCOUNTS.admin.username, ACCOUNTS.admin.password);
  ledger.check('API 管理员登录（仅用于造数据）', Boolean(api.token), `token=${api.token.slice(0, 16)}…`);

  const health = await fetch(`${API_BASE}/health`).then(async (r) => ({ status: r.status, body: await r.json().catch(() => null) }));
  ledger.equal('GET /api/health 经 nginx', health.status, 200);
  ledger.check('health 报服务名', /logharvest/.test(JSON.stringify(health.body)), JSON.stringify(health.body));
  const web = await fetch(WEB);
  ledger.equal('GET 前端首页', web.status, 200);
  const html = await web.text();
  ledger.check('首页是 SPA 文档（含挂载点）', /<div id="app"|<title>/.test(html), html.slice(0, 120));

  const roles = await api.get('/roles?limit=100');
  const by = Object.fromEntries(roles.map((r) => [r.code, r.id]));
  ledger.check('角色 operator/viewer 存在', by.operator > 0 && by.viewer > 0, JSON.stringify(by));
  st.roleIds = by;

  for (const [username, roleId, code] of [[ACCOUNTS.operator.username, by.operator, 'operator'], [ACCOUNTS.viewer.username, by.viewer, 'viewer']]) {
    const exists = await api.get(`/users?keyword=${username}`).catch(() => [])
      .then((l) => l.some?.((u) => u.username === username));
    if (exists) {
      st.createdUsers.push(username);
      continue;
    }
    const r = await api.call('POST', '/users', { username, password: ACCOUNTS.operator.password, display_name: `UI ${code}`, role_ids: [roleId] });
    ledger.check(`API 建用户 ${username}`, r.status < 300, JSON.stringify(r.body).slice(0, 200));
    st.createdUsers.push(username);
  }

  const ds = await api.get('/data-sources');
  const nas = ds.find((d) => d.name === 'e2e-nas');
  ledger.check('数据源 e2e-nas 存在', Boolean(nas), JSON.stringify(ds.map((d) => [d.id, d.name, d.root_path])));
  st.nasSource = nas;

  const sets = await api.get('/rule-sets?limit=100');
  const canonical = sets.filter((s) => s.name === 'e2e-canonical').sort((a, b) => b.version - a.version)[0]
    ?? sets.sort((a, b) => b.version - a.version)[0];
  ledger.check('规则集 e2e-canonical 存在', Boolean(canonical), JSON.stringify(sets.map((s) => [s.id, s.name, s.version])).slice(0, 300));
  const detail = await api.get(`/rule-sets/${canonical.id}`);
  const rules = detail.config?.rules ?? detail.rules ?? [];
  st.canonical = { id: canonical.id, name: canonical.name, version: canonical.version, ruleCount: rules.length };
  ledger.check('canonical 规则集含规则', st.canonical.ruleCount > 0, JSON.stringify(st.canonical));

  const oracle = st.oracle;
  ledger.equal('fixtures clean 预期命中总数', oracle.expect_total_matches, 14);
  ledger.equal('fixtures clean 预期文件数', oracle.expect_total_files, 2);
  ledger.check('NAS 数据源登记为只读', nas?.read_only === true && nas?.enabled === true, JSON.stringify(nas).slice(0, 200));

  const stats = await api.get('/stats/jobs');
  ledger.check('统计接口可用（U9 的数据面）', typeof stats === 'object' && stats !== null, JSON.stringify(stats).slice(0, 240));
  const overview = await api.get('/system/overview');
  ledger.check('概览接口可用（U14 的数据面）', typeof overview === 'object' && overview !== null, JSON.stringify(overview).slice(0, 240));
}

// ---------------------------------------------------------------------------
// U1 登录页：未登录重定向、必填红字、错口令透传后端 detail、记住我原生 select、
// Enter 提交、成功后 cookie 与落地页。
// ---------------------------------------------------------------------------
async function phase_login(ledger, st) {
  ledger.phase(PHASE_LABEL.login);
  const { context, page } = await newPage(st.browser);
  st.loginPage = page;

  await page.goto(`${WEB}#/logharvest/jobs`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(2000);
  ledger.check('未登录访问业务路由被重定向到 /login', /#\/login/.test(page.url()), page.url());
  // 实测 web/src/router/index.ts:231-235：无 token 访问业务路由 → return {path:"/login"}，
  // 不带 redirect 查询参数（ui-map §1 描述的"落地读 route.query.redirect"是登录页那侧的
  // 能力，拦截这侧并没有把它塞进 URL）。所以这里断言"裸 /#/login"，并显式记下这个差异。
  ledger.equal('未登录时被重定向到裸 #/login（router 不写 redirect 参数）', page.url(), `${WEB}#/login`);

  await page.getByRole('button', { name: '登录' }).click();
  await page.waitForTimeout(600);
  const errs = await formErrors(page);
  ledger.check('空表单校验「请输入账号」', errs.some((t) => t.includes('请输入账号')), JSON.stringify(errs));
  ledger.check('空表单校验「请输入密码」', errs.some((t) => t.includes('请输入密码')), JSON.stringify(errs));
  await shot(page, ledger, 'login_form_errors');

  await page.getByPlaceholder('账号').fill(ACCOUNTS.admin.username);
  await page.getByPlaceholder('密码').fill('Wrong-Pass#1');
  await clearMessages(page);
  await page.getByRole('button', { name: '登录' }).click();
  const failMsg = await waitMessage(page, /invalid credentials|登录失败/);
  ledger.check('错口令提示后端 detail 原文', /invalid credentials/i.test(failMsg), failMsg);
  await shot(page, ledger, 'login_bad_password');

  const days = page.locator('select').first();
  ledger.check('「天内免登录」是原生 select（非 el-select）', (await days.count()) > 0, `select 数量=${await page.locator('select').count()}`);
  const dayOptions = await days.locator('option').allInnerTexts().then((a) => a.map((t) => t.trim()));
  ledger.check('免登录天数选项含 1/7/30', ['1', '7', '30'].every((v) => dayOptions.some((t) => t.includes(v))), JSON.stringify(dayOptions));
  const remember = page.locator('.el-checkbox');
  ledger.check('存在"记住登录"复选框', (await remember.count()) > 0, `checkbox=${await remember.count()}`);
  await days.selectOption('7');

  await page.getByPlaceholder('密码').fill(ACCOUNTS.admin.password);
  await clearMessages(page);
  // Enter 提交（login/index.vue:90-103 的防抖 submit）
  await page.getByPlaceholder('密码').press('Enter');
  const okMsg = await waitMessage(page, /登录成功/, 20000);
  ledger.check('Enter 提交并提示「登录成功」', okMsg.includes('登录成功'), okMsg);
  await page.waitForURL(/#\/home/, { timeout: 20000 }).catch(() => {});
  ledger.check('admin 落地 #/home', /#\/home/.test(page.url()), page.url());
  const cookies = await context.cookies();
  const names = cookies.map((c) => c.name);
  ledger.check('登录态 cookie authorized-token', names.includes('authorized-token'), JSON.stringify(names));
  ledger.check('登录态 cookie multiple-tabs', names.includes('multiple-tabs'), JSON.stringify(names));
  const userInfo = await page.evaluate(() => JSON.parse(localStorage.getItem('user-info') || '{}'));
  ledger.check('localStorage user-info 写入 roles/permissions', Array.isArray(userInfo.roles) && Array.isArray(userInfo.permissions), JSON.stringify({ u: userInfo.username, roles: userInfo.roles }));
  await shot(page, ledger, 'login_admin_home');
  fs.mkdirSync(CTX_DIR, { recursive: true });
  await context.storageState({ path: path.join(CTX_DIR, 'ui_admin_probe.json') });

  // 登出后回到登录页且 cookie 失效（U15 还会从页面按钮再验一次）
  await context.clearCookies();
  await page.goto(`${WEB}#/logharvest/jobs`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1800);
  ledger.check('清 cookie 后再访问业务路由重新被拦到 /login', /#\/login/.test(page.url()), page.url());

  // 登录页一侧确实支持 ?redirect=（login/index.vue 落地优先取 route.query.redirect）
  await page.goto(`${WEB}#/login?redirect=/logharvest/jobs`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1200);
  await page.getByPlaceholder('账号').fill(ACCOUNTS.admin.username);
  await page.getByPlaceholder('密码').fill(ACCOUNTS.admin.password);
  await clearMessages(page);
  await page.getByRole('button', { name: '登录' }).click();
  await page.waitForURL(/#\/logharvest\/jobs/, { timeout: 25000 }).catch(() => {});
  ledger.check('?redirect 深链登录后直达目标页', /#\/logharvest\/jobs/.test(page.url()), page.url());
  await context.storageState({ path: path.join(CTX_DIR, 'ui_admin.json') });
  await shot(page, ledger, 'login_redirect_jobs');
}

// ---------------------------------------------------------------------------
// U2 角色可见性：菜单树按后端 /auth/routes 下发，按钮级由 v-perms 决定。
//
// 负断言一律 count()===0：v-perms 是 removeChild（无权限时元素不在 DOM），
// 用 toBeVisible({visible:false}) 会把"不存在"和"隐藏"混为一谈。
// 直达越权动态页期望是全屏 404 文案（动态路由根本没注册），不是 403。
// ---------------------------------------------------------------------------
const ADMIN_ITEMS = ['概览', '任务创建', '任务统计', '日志分析', '数据源管理', 'Worker 管理',
  '规则维护', '规则集维护', '用户管理', '角色管理', '菜单管理', '操作日志'];

async function phase_roles(ledger, st) {
  ledger.phase(PHASE_LABEL.roles);
  const admin = await useSession(ledger, st, 'admin');
  await go(admin, '/home', { reload: true, waitText: '运行概览' });

  const m = await menuTexts(admin);
  ledger.equal('admin 侧边栏分组', m.groups.join('|'), '任务管理|规则管理|系统管理');
  ledger.check('admin 可见全部 12 个菜单项', ADMIN_ITEMS.every((t) => m.items.includes(t)), JSON.stringify(m.items));
  ledger.check('show_link=false 的「作业详情」不进菜单', !m.items.includes('作业详情'), JSON.stringify(m.items));
  ledger.check('show_link=false 的「运行概览」菜单项不存在（首页项叫「概览」）', !m.items.includes('运行概览'), JSON.stringify(m.items));

  // 直达每个业务页都应渲染，不出现 404。
  // 锚点选页面独有文案（不选菜单同名文本）：侧边栏始终可见，用「任务统计」这类
  // 与菜单重名的词做正断言等于什么都没测。
  const PAGES = [
    ['/logharvest/jobs', '任务创建', '分析作业'],
    ['/logharvest/stats', '任务统计', '每日趋势'],
    ['/logharvest/matches', '日志分析', '命中结果'],
    ['/logharvest/datasources', '数据源管理', 'e2e-nas'],
    ['/logharvest/workers', 'Worker 管理', 'lh-e2e-worker-1'],
    ['/logharvest/rulesets', '规则集维护', '新建规则集'],
    ['/rules/maintenance', '规则维护', '跨版本检索'],
    ['/system/users', '用户管理', '新建用户'],
    ['/system/roles', '角色管理', '新建角色'],
    ['/system/menus', '菜单管理', '新建菜单'],
    ['/system/audit-logs', '操作日志', '动作族'],
  ];
  for (const [hash, title, anchor] of PAGES) {
    await go(admin, hash, { waitText: anchor });
    const body = await admin.locator('body').innerText();
    const docTitle = await admin.title();
    ledger.check(`admin 直达 ${hash} 渲染「${anchor}」`, body.includes(anchor), docTitle);
    ledger.check(`${hash} 浏览器标题为「${title}」`, docTitle.startsWith(title), docTitle);
    ledger.check(`${hash} 不是 404/403 兜底页`, !body.includes('抱歉，你访问的页面不存在') && !body.includes('无权访问'), docTitle);
  }

  // ---- operator：有任务管理/规则管理两页 + 操作日志，无 /system 三页，无 write 权限 ----
  const op = await useSession(ledger, st, 'operator');
  const mo = await menuTexts(op);
  ledger.equal('operator 侧边栏分组（无系统管理）', mo.groups.join('|'), '任务管理|规则管理');
  ledger.check('operator 看不到用户/角色/菜单管理', ['用户管理', '角色管理', '菜单管理'].every((t) => !mo.items.includes(t)), JSON.stringify(mo.items));
  ledger.check('operator 能看到操作日志（audit:read）', mo.items.includes('操作日志'), JSON.stringify(mo.items));
  await go(op, '/system/users');
  const opBody = await op.locator('body').innerText();
  ledger.check('operator 直达 /system/users 得到全屏 404（非 403）', opBody.includes('抱歉，你访问的页面不存在') && !opBody.includes('无权访问'), op.url());
  await go(op, '/logharvest/jobs', { waitText: '分析作业' });
  ledger.check('operator 有 jobs:write →「新建任务」在 DOM', (await op.getByRole('button', { name: '新建任务' }).count()) > 0, await bodyButtons(op).then((b) => [...new Set(b)].slice(0, 12).join(',')));
  await go(op, '/logharvest/rulesets');
  ledger.check('operator 无 rulesets:write →「新建规则集」不在 DOM', (await op.getByRole('button', { name: /新建规则集/ }).count()) === 0, await bodyButtons(op).then((b) => [...new Set(b)].slice(0, 15).join(',')));
  await go(op, '/logharvest/datasources');
  ledger.check('operator 无 datasources:write →「新建数据源」不在 DOM', (await op.getByRole('button', { name: /新建数据源/ }).count()) === 0, await bodyButtons(op).then((b) => [...new Set(b)].slice(0, 15).join(',')));
  await go(op, '/logharvest/workers', { waitText: /lh-e2e-worker-1/ });
  ledger.check('operator 有 workers:operate → 排空按钮在 DOM', (await op.getByRole('button', { name: /^排空$/ }).count()) > 0, await bodyButtons(op).then((b) => [...new Set(b)].slice(0, 15).join(',')));
  await shot(op, ledger, 'roles_operator_workers');

  // ---- viewer：只读页面可见，零 write/operate，且无操作日志菜单 ----
  const vw = await useSession(ledger, st, 'viewer');
  const mv = await menuTexts(vw);
  ledger.check('viewer 无系统管理分组', !mv.groups.includes('系统管理'), JSON.stringify(mv.groups));
  ledger.check('viewer 无操作日志菜单', !mv.items.includes('操作日志'), JSON.stringify(mv.items));
  await go(vw, '/system/audit-logs');
  const vwBody = await vw.locator('body').innerText();
  ledger.check('viewer 直达 /system/audit-logs 得到 404', vwBody.includes('抱歉，你访问的页面不存在'), vw.url());
  await go(vw, '/logharvest/jobs', { waitText: '分析作业' });
  ledger.check('viewer 无 jobs:write →「新建任务」不在 DOM', (await vw.getByRole('button', { name: '新建任务' }).count()) === 0, await bodyButtons(vw).then((b) => [...new Set(b)].slice(0, 12).join(',')));
  ledger.check('viewer 无 jobs:operate → 行内「取消」不在 DOM', (await vw.getByRole('button', { name: '取消' }).count()) === 0, await bodyButtons(vw).then((b) => [...new Set(b)].slice(0, 12).join(',')));
  ledger.check('viewer 可读列表（作业表格仍渲染 50 行窗口）', (await vw.locator('.el-table__body .el-table__row').count()) > 0, `rows=${await vw.locator('.el-table__body .el-table__row').count()}`);
  await go(vw, '/logharvest/workers', { waitText: /lh-e2e-worker-1/ });
  ledger.check('viewer 无 workers:operate → 排空/恢复按钮不在 DOM', (await vw.getByRole('button', { name: /^排空$/ }).count()) === 0 && (await vw.getByRole('button', { name: /^恢复$/ }).count()) === 0, await bodyButtons(vw).then((b) => [...new Set(b)].slice(0, 15).join(',')));
  await go(vw, '/home', { waitText: '运行概览' });
  ledger.check('viewer 无 system:operate →「执行维护」不在 DOM', (await vw.getByRole('button', { name: '执行维护' }).count()) === 0, await bodyButtons(vw).then((b) => [...new Set(b)].slice(0, 12).join(',')));
  await shot(vw, ledger, 'roles_viewer_overview');

  // viewer 的规则维护页应是只读 Tab 名（ui-map §11：Tab 名随角色变）
  await go(vw, '/rules/maintenance', { waitText: /查看规则|规则/ });
  const tabs = await vw.locator('.el-tabs__item').allInnerTexts().then((a) => a.map((t) => t.trim()));
  ledger.check('viewer 规则维护 Tab 是「查看规则」（只读态）', tabs.some((t) => t.includes('查看规则')), JSON.stringify(tabs));
  await go(op, '/rules/maintenance', { waitText: /查看规则|编辑规则/ });
  const opTabs = await op.locator('.el-tabs__item').allInnerTexts().then((a) => a.map((t) => t.trim()));
  ledger.check('operator 规则维护 Tab 同为只读（无 rulesets:write）', opTabs.some((t) => t.includes('查看规则')) && !opTabs.some((t) => t.includes('编辑规则')), JSON.stringify(opTabs));
  await go(admin, '/rules/maintenance', { waitText: /编辑规则/ });
  const adTabs = await admin.locator('.el-tabs__item').allInnerTexts().then((a) => a.map((t) => t.trim()));
  ledger.check('admin 规则维护 Tab 是「编辑规则」', adTabs.some((t) => t.includes('编辑规则')), JSON.stringify(adTabs));
}

// ---------------------------------------------------------------------------
// U3 建任务向导（3 步 el-steps）：空表单红字 → 填第①步 + 路径测试 → 选规则集版本
// 取规则条数 → 第③步默认值与提交摘要 → Idempotency-Key 头 → 成功文案 + 自动跳详情。
// ---------------------------------------------------------------------------

async function phase_wizard(ledger, st) {
  ledger.phase(PHASE_LABEL.wizard);
  const page = st.adminPage ?? await useSession(ledger, st, 'admin');
  await go(page, '/logharvest/jobs', { reload: true, waitText: '分析作业' });
  await page.getByRole('button', { name: '新建任务' }).click();
  const dlg = await dialog(page, '新建分析任务');
  const stepTitles = await dlg.locator('.el-step__title').allInnerTexts().then((a) => a.map((t) => t.trim()));
  ledger.equal('向导步骤条三段', stepTitles.join('|'), '数据源与目录|规则集|过滤与确认');
  const foot = (await dlg.innerText()).replace(/\s+/g, ' ');
  ledger.check('底部进度文案「1 / 3 · …实时进度」', /1\s*\/\s*3.*实时进度/.test(foot), foot.slice(-160));

  // 第①步空提交 → 三条必填红字
  await dlg.getByRole('button', { name: '下一步' }).click();
  await page.waitForTimeout(500);
  const errs = await formErrors(page);
  for (const want of ['请填写作业名称', '请选择数据源', '请填写要扫描的目录']) {
    ledger.check(`第①步必填「${want}」`, errs.some((t) => t.includes(want)), JSON.stringify(errs));
  }
  ledger.equal('第①步仍停留（未误提交）', (await dlg.locator('.el-step__title.is-process').innerText()).trim(), '数据源与目录');
  await shot(page, ledger, 'wizard_step1_required');

  // 「测试数据源根路径」在未选数据源时应 disabled
  const testBtn = dlg.getByRole('button', { name: '测试数据源根路径' });
  ledger.check('未选数据源时「测试数据源根路径」disabled', await testBtn.isDisabled(), 'disabled=false');

  const name = jobName('e2e-clean');
  await dlg.getByPlaceholder('例如：web01 一周错误日志').fill(name);
  const dsText = await pickOption(page, '选择挂载点', 'e2e-nas');
  ledger.check('第①步选中数据源 e2e-nas', dsText.includes('e2e-nas') && dsText.includes('/nas'), dsText);
  await dlg.getByPlaceholder('数据源 root_path 之内的绝对路径').fill('/nas/clean');
  await page.waitForTimeout(300);
  ledger.check('选完数据源后「测试数据源根路径」可点', !(await testBtn.isDisabled()), 'still disabled');
  await testBtn.click();
  await page.waitForTimeout(1500);
  const testText = (await dlg.innerText()).replace(/\s+/g, ' ');
  ledger.check('路径测试结果 ✓（存在/目录/可读）', /存在.*✓|可读.*✓/.test(testText), testText.match(/路径判定[^]{0,180}/)?.[0] ?? testText.slice(0, 200));
  ledger.check('只读挂载给出「只读已确认」', testText.includes('只读已确认'), (testText.match(/可写[^\n]{0,60}|只读[^\n]{0,60}/g) ?? []).join(' | '));
  await shot(page, ledger, 'wizard_step1_tested');

  // 越界路径应被前端必填之外的后端 400 拦下（这里只做前端可见性校验：灰字提示 root_path）
  const hint = (await dlg.innerText()).replace(/\s+/g, ' ');
  ledger.check('灰字说明扫描目录必须在 /nas 之内', hint.includes('必须位于 /nas 之内'), hint.match(/必须位于[^。]{0,80}/)?.[0] ?? '');

  await dlg.getByRole('button', { name: '下一步' }).click();
  await page.waitForTimeout(600);
  ledger.equal('进入第②步', (await dlg.locator('.el-step__title.is-process').innerText()).trim(), '规则集');
  const { id: rsId, version: rsVer, ruleCount } = st.canonical;
  const rsText = await pickFormSelect(page, '规则集版本', `v${rsVer}（#${rsId}）`, { type: `v${rsVer}` });
  ledger.check(`第②步选中 ${st.canonical.name} v${rsVer}（#${rsId}）`, rsText.includes(`v${rsVer}`) && rsText.includes(`#${rsId}`), rsText);
  const countTag = await waitFor(async () => {
    const t = (await dlg.innerText()).replace(/\s+/g, ' ');
    return /规则条数\s*\d+ 条/.test(t) ? t.match(/规则条数\s*(\d+) 条/)[1] : null;
  }, { label: '规则条数标签', timeout: 12000 });
  ledger.equal('第②步「规则条数」取详情后显示 N 条', Number(countTag), ruleCount);

  await dlg.getByRole('button', { name: '下一步' }).click();
  await page.waitForTimeout(600);
  ledger.equal('进入第③步', (await dlg.locator('.el-step__title.is-process').innerText()).trim(), '过滤与确认');
  const sw = dlg.locator('.el-form-item').filter({ hasText: '递归扫描' }).locator('.el-switch');
  ledger.check('第③步「递归扫描」默认开', await sw.first().evaluate((el) => el.classList.contains('is-checked')), await sw.first().getAttribute('class'));
  const depth = await dlg.locator('.el-form-item').filter({ hasText: '最大深度' }).locator('input').first().inputValue();
  ledger.equal('第③步「最大深度」默认 5', depth, '5');
  const maxFiles = await dlg.locator('.el-form-item').filter({ hasText: '最大文件数' }).locator('input').first().inputValue();
  ledger.equal('第③步「最大文件数」默认 20000', maxFiles, '20000');
  const summary = (await dlg.innerText()).replace(/\s+/g, ' ');
  ledger.check('提交摘要含数据源/扫描目录/规则集/文件过滤四行', ['数据源：', '扫描目录：/nas/clean', '规则集：', '文件过滤：递归=是'].every((t) => summary.includes(t)), summary.match(/提交摘要[^]{0,260}/)?.[0]?.slice(0, 300) ?? summary.slice(0, 260));
  await shot(page, ledger, 'wizard_step3_summary');

  // 抓 POST /api/jobs 的请求头与请求体：Idempotency-Key 必须是前端生成的 UUID
  const sent = [];
  const onReq = (req) => {
    if (req.method() === 'POST' && /\/api\/jobs$/.test(req.url())) sent.push({ headers: req.headers(), body: req.postData() });
  };
  page.on('request', onReq);
  await clearMessages(page);
  await dlg.getByRole('button', { name: '提交' }).click();
  const okMsg = await waitMessage(page, /作业 #\d+ 已创建/, 30000);
  page.off('request', onReq);
  const key = sent[0]?.headers['idempotency-key'];
  ledger.check('提交带前端生成的 Idempotency-Key（UUIDv4）', /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(key ?? ''), `key=${key}`);
  const body = JSON.parse(sent[0]?.body ?? '{}');
  ledger.check('请求体字段与向导输入一致', body.name === name && body.source_path === '/nas/clean' && body.data_source_id === st.nasSource.id && body.rule_set_id === rsId, JSON.stringify(body));
  ledger.check('file_filter 带递归/深度/上限', body.file_filter?.recursive === true && body.file_filter?.max_depth === 5 && body.file_filter?.max_files === 20000, JSON.stringify(body.file_filter));
  ledger.check('成功文案「作业 #N 已创建，共 M 个压缩文件」', /作业 #\d+ 已创建，共 2 个压缩文件/.test(okMsg), okMsg);
  const jobId = Number(okMsg.match(/作业 #(\d+)/)[1]);
  st.jobClean = { id: jobId, name };
  st.createdJobs.push(jobId);
  await page.waitForURL(new RegExp(`#\/logharvest\/jobs\/${jobId}`), { timeout: 20000 }).catch(() => {});
  ledger.check('提交成功后自动跳转作业详情', new RegExp(`#\/logharvest\/jobs\/${jobId}$`).test(page.url()), page.url());
  ledger.check('对话框已关闭', (await page.getByRole('dialog').count()) === 0, `dialog=${await page.getByRole('dialog').count()}`);

  // 等作业进终态（数据面用 API 轮询，页面断言留给 U5）
  const job = await waitJobTerminal(st.api, jobId, { timeout: 420000 });
  ledger.equal('clean 作业最终状态', job.status, 'succeeded');
  ledger.equal('clean 作业文件数与 oracle 一致', job.total_files, st.oracle.expect_total_files);
  st.jobClean.job = job;
  await shot(page, ledger, 'wizard_created_detail');
}

// ---------------------------------------------------------------------------
// U4 作业列表：列集合、窗口翻页文案、关键字只过滤本页、状态下推后端、UTC 日标签、
// 取消确认框（再想想=不发请求 / 确认取消=回显后端状态）、终态行 disabled + tooltip。
//
// 取消用例的确定性：先用 API 把在线 Worker 排空。提交作业是同步扫描 + 直接置 running
// （所以靶作业不会是 pending），排空保证的是"文件任务无人认领、作业不会自己跑完"，
// 这样页面点取消前后状态都稳定，不会出现"点到时已经跑完"的竞态；断言完立即恢复领取。
// 这是造前置数据，不是改被测行为——取消动作本身仍由页面按钮发出。
// ---------------------------------------------------------------------------
async function phase_jobs(ledger, st) {
  ledger.phase(PHASE_LABEL.jobs);
  const page = st.adminPage ?? await useSession(ledger, st, 'admin');
  await go(page, '/logharvest/jobs', { reload: true, waitText: '分析作业' });

  const heads = await tableHeaderTexts(page);
  ledger.check('列集合=ID/名称/状态/文件数/总字节/创建时间/操作', ['ID', '名称', '状态', '文件数', '总字节', '创建时间', '操作'].every((t) => heads.includes(t)), JSON.stringify(heads));
  const rows0 = await page.locator('.el-table__body .el-table__row').count();
  ledger.equal('默认窗口取满 limit=50', rows0, 50);
  const foot0 = (await page.locator('body').innerText()).replace(/\s+/g, ' ');
  ledger.check('底部"列表接口不返回总数：第 N 页，本页 M 条"', /列表接口不返回总数[：:].*第 1 页，本页 50 条/.test(foot0), foot0.match(/列表接口不返回总数[^，]{0,90}/)?.[0] ?? foot0.slice(0, 200));
  const prev = page.getByRole('button', { name: '上一页' });
  const next = page.getByRole('button', { name: '下一页' });
  ledger.check('第 1 页「上一页」disabled', await prev.isDisabled(), 'enabled');
  ledger.check('本页取满 50 → 「下一页」enabled', !(await next.isDisabled()), 'disabled');

  // 关键字只过滤本页，不发请求
  const before = (await recentRequests(page, /\/api\/jobs/)).length;
  await page.getByPlaceholder('关键字（本页）').fill(st.jobClean.name);
  await page.waitForTimeout(700);
  const rowsKw = await page.locator('.el-table__body .el-table__row').count();
  const after = (await recentRequests(page, /\/api\/jobs/)).length;
  ledger.equal('关键字过滤后只剩 1 行（前端过滤）', rowsKw, 1);
  ledger.equal('关键字过滤不发请求', after, before);
  const kwRow = page.locator('.el-table__body .el-table__row').first();
  ledger.check('过滤结果就是 clean 作业', (await kwRow.innerText()).includes(st.jobClean.name), (await kwRow.innerText()).replace(/\s+/g, ' ').slice(0, 120));
  await page.getByPlaceholder('关键字（本页）').fill('不存在的关键字zzz');
  await page.waitForTimeout(500);
  ledger.check('无匹配时表格空态「暂无数据」', (await page.locator('body').innerText()).includes('暂无数据'), 'no empty text');
  await page.getByPlaceholder('关键字（本页）').fill('');
  await page.waitForTimeout(500);

  // 状态筛选下推后端
  const reqBefore = (await recentRequests(page, /\/api\/jobs\?/)).length;
  await pickOption(page, '状态筛选', /^succeeded$/);
  await waitFor(async () => (await recentRequests(page, /\/api\/jobs\?/)).length > reqBefore, { label: '状态筛选触发 GET /jobs?status=', timeout: 10000 });
  const statusReq = (await recentRequests(page, /\/api\/jobs\?/)).at(-1);
  ledger.check('状态选择后请求带 status=succeeded', /status=succeeded/.test(statusReq.url), statusReq.url);
  const tags = await page.locator('.el-table__body .el-table__row .el-tag').allInnerTexts();
  ledger.check('筛选后所有行状态都是 succeeded', tags.length > 0 && tags.every((t) => t.trim() === 'succeeded'), JSON.stringify([...new Set(tags.map((t) => t.trim()))]));
  ledger.check('succeeded 用 success 配色（绿）', await page.locator('.el-table__body .el-table__row .el-tag').first().evaluate((el) => el.className.includes('success')), await page.locator('.el-table__body .el-table__row .el-tag').first().getAttribute('class'));
  await clearSelect(page, 'succeeded');
  await waitFor(async () => (await recentRequests(page, /\/api\/jobs\?/)).length > reqBefore + 1, { label: '清空筛选触发新请求', timeout: 10000 });
  const clearedReq = (await recentRequests(page, /\/api\/jobs\?/)).at(-1);
  ledger.check('清空筛选后请求不再带 status', !/status=/.test(clearedReq.url), clearedReq.url);

  // UTC 日标签（统计页条形点击带来的 ?date=）：源码 index.vue:43 明说后端 /jobs 没有
  // date 参数，它只在本页 displayRows 里过滤，界面文案也照此标注。
  // 断言不写死 50 行：套件在 UTC 零点后跑时，第一页属于"今天"的行可能只有几条
  // （库里的作业多建在前一天）。先取无过滤基线，再断言过滤后与基线一致。
  const today = new Date().toISOString().slice(0, 10);
  await go(page, '/logharvest/jobs', { reload: true, waitText: '分析作业' });
  const todayBase = await page.locator('.el-table__body .el-table__row', { hasText: today }).count();
  await go(page, `/logharvest/jobs?date=${today}`, { reload: true, waitText: '分析作业' });
  const tagText = `UTC 日：${today}`;
  ledger.check('URL 带 ?date= 时出现可关闭标签', (await page.getByText(tagText).count()) > 0, await page.locator('body').innerText().then((t) => t.replace(/\s+/g, ' ').slice(0, 200)));
  const dayReq = (await recentRequests(page, /\/api\/jobs\?/)).at(-1);
  ledger.check('?date= 不下推后端（只做本页提示）', !/[?&]date=/.test(dayReq.url), dayReq.url);
  const rowsToday = await page.locator('.el-table__body .el-table__row').count();
  await go(page, '/logharvest/jobs?date=2000-01-01', { reload: true, waitText: 'UTC 日：2000-01-01' });
  await page.waitForTimeout(600);
  const rowsOtherDay = await page.locator('.el-table__body .el-table__row').count();
  ledger.check('日期提示在本页内过滤（无匹配则空态）', rowsToday === todayBase && rowsOtherDay === 0, JSON.stringify({ todayBase, rowsToday, rowsOtherDay }));
  ledger.check('空态文案「暂无数据」随日期提示出现', (await page.locator('body').innerText()).includes('暂无数据'), 'no empty text');

  // 标签的关闭按钮：el-tag(closable) 渲染 .el-tag__close，点它 → clearDayHint() 同时 router.replace 抹掉 URL 里的 date
  await page.locator('.el-tag__close').first().click();
  await page.waitForTimeout(900);
  ledger.check('关闭标签后 URL 同步清掉 date', !/date=/.test(page.url()), page.url());
  ledger.check('标签消失后本页行重新出现（不再被日期提示过滤）', (await page.locator('.el-table__body .el-table__row').count()) > 0, String(await page.locator('.el-table__body .el-table__row').count()));

  // 翻页
  await go(page, '/logharvest/jobs', { reload: true, waitText: '分析作业' });
  const p1 = (await recentRequests(page, /\/api\/jobs\?/)).length;
  await next.click();
  await waitFor(async () => (await recentRequests(page, /\/api\/jobs\?/)).length > p1, { label: '下一页触发 GET /jobs?offset=50', timeout: 10000 });
  const p2req = (await recentRequests(page, /\/api\/jobs\?/)).at(-1);
  ledger.check('下一页请求 offset=50', /offset=50/.test(p2req.url), p2req.url);
  // goNext 同步改 offset：页脚「第 2 页」在响应回来之前就会渲染，那一刻 loading=true、
  // 上一页仍禁用（index.vue:340 disabled= offset===0 || loading）。断言前必须等稳定态
  // ——文案出现且上一页可用——否则是在给过渡帧拍快照。
  await waitFor(async () => {
    const body = (await page.locator('body').innerText()).replace(/\s+/g, ' ');
    return body.includes('第 2 页，本页') && !(await prev.isDisabled()) ? true : null;
  }, { label: '第 2 页稳定态（文案出现且上一页可用）', timeout: 8000 });
  const foot2 = (await page.locator('body').innerText()).replace(/\s+/g, ' ');
  ledger.check('第 2 页文案且上一页可用', /第 2 页，本页 \d+ 条/.test(foot2) && !(await prev.isDisabled()), foot2.match(/第 \d+ 页，本页 \d+ 条[^)]{0,40}/)?.[0] ?? '');
  await prev.click();
  await page.waitForTimeout(1200);
  ledger.check('上一页回到第 1 页', /第 1 页，本页 50 条/.test((await page.locator('body').innerText()).replace(/\s+/g, ' ')), page.url());

  // 详情链接
  await page.getByPlaceholder('关键字（本页）').fill(st.jobClean.name);
  await page.waitForTimeout(600);
  const cleanRow = page.locator('.el-table__body .el-table__row').first();
  await cleanRow.getByRole('button', { name: '详情' }).click();
  await page.waitForURL(new RegExp(`#\/logharvest\/jobs\/${st.jobClean.id}`), { timeout: 15000 }).catch(() => {});
  ledger.check('行内「详情」跳作业详情', new RegExp(`#\/logharvest\/jobs\/${st.jobClean.id}`).test(page.url()), page.url());
  await go(page, '/logharvest/jobs', { waitText: '分析作业' });

  // 终态行的取消：disabled + tooltip
  await page.getByPlaceholder('关键字（本页）').fill(st.jobClean.name);
  await page.waitForTimeout(600);
  const termCancel = page.locator('.el-table__body .el-table__row').first().getByRole('button', { name: '取消' });
  ledger.check('终态行「取消」disabled', await termCancel.isDisabled(), 'enabled');
  await termCancel.hover().catch(() => {});
  const tip = await waitFor(async () => {
    const vis = await page.locator('.el-popper:visible').allInnerTexts();
    return vis.find((t) => t.includes('作业已进入终态')) ?? null;
  }, { label: '终态 tooltip', timeout: 6000 }).catch(() => null);
  ledger.check('终态 tooltip「作业已进入终态，后端不会改写」', (tip ?? '').includes('作业已进入终态，后端不会改写'), tip ?? 'no tooltip');
  await shot(page, ledger, 'jobs_terminal_cancel');

  // 非终态作业的取消（先排空 Worker）：提交接口会同步扫描目录并把作业置为 running，
  // 所以"排空"保证的不是 pending，而是文件任务无人认领 → 作业不会自己跑完，取消结果确定。
  const workers = await st.api.get('/workers');
  const online = workers.filter((w) => w.status === 'online');
  for (const w of online) {
    await st.api.send('POST', `/workers/${w.id}/drain`, {});
    st.drained.push(w.id);
  }
  await waitFor(async () => (await st.api.get('/workers')).filter((w) => st.drained.includes(w.id)).every((w) => w.status === 'draining'), { label: '两台 Worker 进入 draining', timeout: 30000 });
  const cancelName = jobName('e2e-cancel');
  const created = await st.api.send('POST', '/jobs', {
    name: cancelName,
    data_source_id: st.nasSource.id,
    source_path: '/nas/cancel',
    rule_set_id: st.canonical.id,
    file_filter: { recursive: true, max_depth: 5, max_files: 20000 },
  }, undefined);
  const cancelJobId = created.id;
  st.createdJobs.push(cancelJobId);
  ledger.check('API 造非终态取消靶作业', ['pending', 'running'].includes(created.status), JSON.stringify({ id: cancelJobId, status: created.status }));

  await go(page, '/logharvest/jobs', { waitText: '分析作业' });
  await page.getByPlaceholder('关键字（本页）').fill(cancelName);
  await page.waitForTimeout(600);
  const cRow = page.locator('.el-table__body .el-table__row').first();
  const cRowText = (await cRow.innerText()).replace(/\s+/g, ' ');
  ledger.check('非终态作业行显示 pending/running 且「取消」可用', /pending|running/.test(cRowText) && !(await cRow.getByRole('button', { name: '取消' }).isDisabled()), cRowText.slice(0, 120));
  const cancelReqs = () => page.__requests.filter((r) => r.method === 'POST' && /\/api\/jobs\/\d+\/cancel$/.test(r.url.split('?')[0])).length;
  const cancelBtns = cancelReqs();
  await cRow.getByRole('button', { name: '取消' }).click();
  const box = await readMessageBox(page);
  ledger.equal('确认框标题', box.title, '取消作业');
  ledger.check('确认框正文含作业号与名称', box.body.includes(`确认请求取消作业 #${cancelJobId}（${cancelName}）`) && box.body.includes('已进入运行中的文件任务会跑完后收尾'), box.body);
  ledger.check('按钮文案「确认取消」「再想想」', box.buttons.includes('确认取消') && box.buttons.includes('再想想'), JSON.stringify(box.buttons));
  await shot(page, ledger, 'jobs_cancel_confirm');
  await clickMessageBoxButton(page, '再想想');
  await page.waitForTimeout(900);
  const afterBack = cancelReqs();
  ledger.equal('「再想想」不发出取消请求', afterBack, cancelBtns);
  const stillJob = await st.api.get(`/jobs/${cancelJobId}`);
  ledger.check('点「再想想」后作业未被动过（仍非终态）', ['pending', 'running'].includes(stillJob.status) && stillJob.cancel_requested === false, JSON.stringify({ status: stillJob.status, cancel_requested: stillJob.cancel_requested }));

  await cRow.getByRole('button', { name: '取消' }).click();
  await readMessageBox(page);
  await clearMessages(page);
  await clickMessageBoxButton(page, '确认取消');
  const cancelMsg = await waitMessage(page, new RegExp(`作业 #${cancelJobId} 当前状态：`), 15000);
  ledger.check('列表取消回显后端状态「作业 #N 当前状态：cancelled」', cancelMsg.includes('cancelled'), cancelMsg);
  await waitFor(async () => (await st.api.get(`/jobs/${cancelJobId}`)).status === 'cancelled', { label: '后端落 cancelled', timeout: 15000 });
  const refreshed = (await page.locator('body').innerText()).replace(/\s+/g, ' ');
  ledger.check('取消后列表自动刷新（load()）出现 cancelled', refreshed.includes('cancelled'), refreshed.slice(0, 220));
  await shot(page, ledger, 'jobs_cancelled_row');

  // 收尾：只校验本次被排空的 Worker 回到 online（历史遗留的 offline 行不参与判定）。
  const resumedIds = st.drained.splice(0);
  for (const id of resumedIds) await st.api.send('POST', `/workers/${id}/resume`, {});
  await waitFor(async () => (await st.api.get('/workers')).filter((w) => resumedIds.includes(w.id)).every((w) => w.status === 'online'), { label: '恢复后 Worker 回到 online', timeout: 60000 });
  const back = await st.api.get('/workers');
  ledger.check('恢复领取后被排空的 Worker 回到 online', resumedIds.length > 0 && back.filter((w) => resumedIds.includes(w.id)).every((w) => w.status === 'online'), JSON.stringify(back.filter((w) => resumedIds.includes(w.id)).map((w) => `${w.id}:${w.status}`)));
}

// ---------------------------------------------------------------------------
// U5 作业详情：终态作业也开 SSE 取快照（源码 onMounted → startStream）、进度行三数、
// 描述表逐项、非终态才给「取消作业」、文件任务窗口 + 状态下推、尝试历史抽屉
// （行内「历史」与 ?file_task= 深链两条路）、查看命中结果带 job_id；
// 再用切断 SSE 请求验证「轮询降级」，用排空 Worker 验证详情页取消，
// 最后用真实的耗尽作业（e2e-truncated，5 次失败尝试）验证「重试失败文件」闭环。
// ---------------------------------------------------------------------------
async function phase_detail(ledger, st) {
  ledger.phase(PHASE_LABEL.detail);
  const page = st.adminPage ?? await useSession(ledger, st, 'admin');
  const job = st.jobClean;
  const oracle = st.oracle;
  const filesViaApi = await st.api.get(`/jobs/${job.id}/files?limit=50`);

  await go(page, `/logharvest/jobs/${job.id}`, { reload: true, waitText: `作业 #${job.id}` });
  const head = (await page.locator('.el-card__header').first().innerText()).replace(/\s+/g, ' ').trim();
  ledger.check('头部「作业 #id · 名称」', head.includes(`作业 #${job.id}`) && head.includes(job.name), head.slice(0, 160));
  ledger.check('头部状态 tag 为 succeeded', /succeeded/.test(head), head.slice(0, 160));

  const sseReq = await waitFor(async () => (await recentRequests(page, /\/api\/jobs\/\d+\/events$/)).at(-1) ?? null, { label: 'SSE 请求 /jobs/{id}/events', timeout: 12000 }).catch(() => null);
  ledger.check('打开详情即订阅 SSE 进度流', Boolean(sseReq), sseReq?.url ?? 'no /events request');
  ledger.check('SSE 用 fetch 带 Bearer 头（EventSource 设不了头）', /^Bearer .+/.test(sseReq?.headers?.authorization ?? ''), JSON.stringify(sseReq?.headers ?? {}).slice(0, 160));

  const progLine = await waitFor(async () => {
    const t = (await page.locator('body').innerText()).replace(/\s+/g, ' ');
    const m = t.match(/进度 \d+ \/ \d+ · 已处理行数 \d+ · 命中行数 \d+/);
    return m && m[0] !== `进度 0 / ${job.job.total_files} · 已处理行数 0 · 命中行数 0` ? m[0] : null;
  }, { label: 'SSE 快照回填进度行', timeout: 20000 }).catch(() => null);
  ledger.check('终态作业进度行也有数字（快照回填，不停在 0）', Boolean(progLine), progLine ?? (await page.locator('body').innerText()).replace(/\s+/g, ' ').slice(0, 200));
  ledger.check('进度=文件数且命中行数=oracle', (progLine ?? '').includes(`进度 ${oracle.expect_total_files} / ${oracle.expect_total_files}`) && (progLine ?? '').includes(`命中行数 ${oracle.expect_total_matches}`), `${progLine} oracle=${oracle.expect_total_files}/${oracle.expect_total_matches}`);
  const bodyText1 = (await page.locator('body').innerText()).replace(/\s+/g, ' ');
  ledger.check('by_status 汇总 tag（succeeded: N）来自快照', /succeeded: \d+/.test(bodyText1), bodyText1.match(/(pending|running|succeeded|failed|partial|cancelled): \d+( (pending|running|succeeded|failed|partial|cancelled): \d+)*/)?.[0] ?? '');

  const desc = await page.evaluate(() => {
    const out = {};
    document.querySelectorAll('.el-descriptions__body table tr').forEach((tr) => {
      const cells = [...tr.querySelectorAll('.el-descriptions__label, .el-descriptions__content')];
      for (let i = 0; i + 1 < cells.length; i += 2) out[cells[i].innerText.trim()] = cells[i + 1].innerText.trim();
    });
    return out;
  });
  ledger.check('描述表十项齐全', ['数据源', '规则集', '来源目录', '文件数', '总字节', '请求取消', '开始时间', '结束时间', '创建人', '错误'].every((k) => k in desc), JSON.stringify(Object.keys(desc)));
  ledger.check('描述值与作业一致（数据源/规则集/目录/文件数/取消/创建人）', desc['数据源'] === `#${st.nasSource.id}` && desc['规则集'] === `#${st.canonical.id}` && desc['来源目录'] === '/nas/clean' && desc['文件数'] === String(oracle.expect_total_files) && desc['请求取消'] === '否' && desc['创建人'] === 'admin', JSON.stringify(desc));
  ledger.check('成功作业的错误位是「-」而不是空白', desc['错误'] === '-', JSON.stringify(desc['错误']));
  ledger.check('终态详情不给「取消作业」按钮', (await page.getByRole('button', { name: '取消作业' }).count()) === 0, await bodyButtons(page).then((b) => [...new Set(b)].slice(0, 14).join(',')));
  ledger.check('无失败文件时不给「重试失败文件」按钮', (await page.getByRole('button', { name: /重试失败文件/ }).count()) === 0, await bodyButtons(page).then((b) => [...new Set(b)].slice(0, 14).join(',')));

  const dHeads = await tableHeaderTexts(page);
  ledger.check('文件任务列集合齐全', ['ID', '文件', '状态', '大小', '尝试', '历史', 'Worker', '已处理行', '命中行', '心跳', '错误'].every((t) => dHeads.includes(t)), JSON.stringify(dHeads));
  const dRows = page.locator('.el-table__body').first().locator('.el-table__row');
  ledger.equal('文件任务行数=oracle 文件数', await dRows.count(), oracle.expect_total_files);
  const row0 = (await dRows.first().innerText()).replace(/\s+/g, ' ');
  ledger.check('尝试列写成「attempts / max_attempts」', /\b1 \/ 5\b/.test(row0), row0.slice(0, 160));
  ledger.check('成功行的错误列是「-」（文件任务列用短横，抽屉才用 —）', row0.trim().endsWith('-'), row0.slice(-120));
  const matchedSum = filesViaApi.reduce((s, f) => s + (f.matched_rows ?? 0), 0);
  ledger.equal('后端逐文件命中行数之和=oracle', matchedSum, oracle.expect_total_matches);

  await page.getByRole('button', { name: '查看命中结果' }).click();
  await page.waitForTimeout(900);
  ledger.check('「查看命中结果」带 job_id 跳日志分析', new RegExp(`#/logharvest/matches\\?job_id=${job.id}`).test(page.url()), page.url());

  await go(page, `/logharvest/jobs/${job.id}`, { reload: true, waitText: `作业 #${job.id}` });
  const taskId = filesViaApi[0].id;
  await dRows.first().getByRole('button', { name: '历史' }).click();
  const drawer = page.locator('.el-drawer').filter({ hasText: '尝试历史' }).first();
  await drawer.waitFor({ state: 'visible', timeout: 8000 });
  const dTitle = (await drawer.locator('.el-drawer__title').innerText()).replace(/\s+/g, ' ').trim();
  ledger.check('抽屉标题含任务号与文件名', dTitle === `尝试历史 · 文件任务 #${taskId}（${filesViaApi[0].file_name}）`, dTitle);
  // footer 的「已加载 N 次尝试」在 fetch 落地前读到的永远是 0（detail.vue 打开抽屉才拉
  // 历史列表）——先等尝试行渲染完，再读 footer，否则断言的是加载中间态。
  const aRows = drawer.locator('.el-table__body .el-table__row');
  await waitFor(async () => ((await aRows.count()) >= 1 ? true : null), { label: '抽屉尝试行渲染', timeout: 15000 }).catch(() => {});
  const dFoot = (await drawer.locator('.el-drawer__footer').innerText()).replace(/\s+/g, ' ').trim();
  ledger.check('抽屉 footer 如实报条数与排序', /已加载 1 次尝试，后端按 attempt_no 升序返回/.test(dFoot), dFoot);
  ledger.equal('成功任务的历史只有 1 次尝试', await aRows.count(), 1);
  const aRow = (await aRows.first().innerText()).replace(/\s+/g, ' ');
  ledger.check('历史行含次序/Worker/版本/状态', aRow.includes('0.1.0') && /lh-e2e-worker-\d/.test(aRow) && aRow.includes('succeeded'), aRow.slice(0, 180));
  await shot(page, ledger, 'detail_attempts_drawer');
  ledger.check('行内「历史」抽屉可关闭', await closeDrawer(page), 'drawer still visible');

  // 命中结果页的「所属文件」反向链接：?file_task= 深链要在进页面时自动开抽屉
  await go(page, `/logharvest/jobs/${job.id}?file_task=${taskId}`, { reload: true, waitText: '尝试历史' });
  const drawer2 = page.locator('.el-drawer').filter({ hasText: '尝试历史' }).first();
  await drawer2.waitFor({ state: 'visible', timeout: 8000 });
  const t2 = (await drawer2.locator('.el-drawer__title').innerText()).replace(/\s+/g, ' ').trim();
  ledger.check('?file_task= 深链自动打开同一任务的尝试历史', t2.includes(`#${taskId}（`), t2);
  ledger.check('深链打开时文件名可解析（任务在当前窗口内）', t2.includes(filesViaApi[0].file_name), t2);
  ledger.check('深链抽屉关闭后文件列表不再被抽屉表格串台', await closeDrawer(page), 'drawer still visible');

  // 状态筛选下推到后端（?status=），与列表页的"只过滤本页"不同
  await pickOption(page, '状态筛选', /^succeeded$/);
  await waitFor(async () => (await recentRequests(page, new RegExp(`/api/jobs/${job.id}/files\\?`))).at(-1)?.url.includes('status=succeeded'), { label: '详情页状态筛选下推 ?status=', timeout: 10000 });
  const fReq = (await recentRequests(page, new RegExp(`/api/jobs/${job.id}/files`))).at(-1);
  ledger.check('详情页状态筛选请求带 status=succeeded', /status=succeeded/.test(fReq.url), fReq.url);
  ledger.equal('筛选 succeeded 仍是全部行', await dRows.count(), oracle.expect_total_files);
  await clearSelect(page, 'succeeded');
  await pickOption(page, '状态筛选', /^failed$/);
  await waitFor(async () => (await recentRequests(page, new RegExp(`/api/jobs/${job.id}/files\\?`))).at(-1)?.url.includes('status=failed'), { label: '详情页状态筛选下推 ?status=failed', timeout: 10000 });
  await page.waitForTimeout(600);
  ledger.equal('筛选 failed 时本页为空（接口按状态返回）', await dRows.count(), 0);
  ledger.check('空表时仍给出窗口式说明文案', (await page.locator('body').innerText()).replace(/\s+/g, ' ').includes('状态筛选已下推到后端'), 'footer text');
  await clearSelect(page, 'failed');
  await waitFor(async () => (await dRows.count()) === oracle.expect_total_files, { label: '清空状态筛选后文件行回到全量', timeout: 12000 });
  const fClear = (await recentRequests(page, new RegExp(`/api/jobs/${job.id}/files`))).at(-1);
  ledger.check('清空后请求不再带 status 且行回到全量', !/status=/.test(fClear.url) && (await dRows.count()) === oracle.expect_total_files, fClear.url);

  // —— SSE 通道被切断时如实降级。/events 是页面唯一订阅的流，用 page.route abort 它，
  // 等价于"代理切断/后端不可达"，被测的降级分支仍是页面自己的代码。
  const degradeName = jobName('e2e-degrade');
  const degradeJob = await st.api.send('POST', '/jobs', {
    name: degradeName,
    data_source_id: st.nasSource.id,
    source_path: '/nas/cancel',
    rule_set_id: st.canonical.id,
    file_filter: { recursive: true, max_depth: 5, max_files: 20000 },
  }, undefined);
  st.createdJobs.push(degradeJob.id);
  await page.route('**/api/jobs/*/events', (r) => r.abort());
  await clearMessages(page);
  await go(page, `/logharvest/jobs/${degradeJob.id}`, { reload: true, waitText: `作业 #${degradeJob.id}` });
  const degradeMsg = await waitMessage(page, /实时进度中断|实时进度连接已结束/, 20000);
  ledger.check('SSE 不可用时给出降级提示而不是静默', /轮询/.test(degradeMsg), degradeMsg);
  const degradedTag = await waitFor(async () => ((await page.locator('.el-card__header').first().innerText()).includes('轮询降级') ? '轮询降级' : null), { label: '「轮询降级」tag', timeout: 15000 }).catch(() => null);
  ledger.check('头部出现「轮询降级」tag', Boolean(degradedTag), (await page.locator('.el-card__header').first().innerText()).replace(/\s+/g, ' ').slice(0, 160));
  const degradeProgress = await page.locator('body').innerText().then((t) => t.replace(/\s+/g, ' ').match(/进度 \d+ \/ \d+ · 已处理行数 \d+ · 命中行数 \d+/)?.[0] ?? '');
  const pollReqs = await waitFor(async () => ((await recentRequests(page, new RegExp(`/api/jobs/${degradeJob.id}(\\?|$)`))).length >= 2 ? 'polling' : null), { label: '轮询重复拉取作业详情', timeout: 20000 }).catch(() => null);
  ledger.check('降级后每 5 秒轮询作业详情（同一请求重复出现）', pollReqs === 'polling', `${(await recentRequests(page, new RegExp(`/api/jobs/${degradeJob.id}(\\?|$)`))).length} 次 GET /jobs/${degradeJob.id}`);
  ledger.skip('降级期间进度行的数字来源', `观察项：进度行只读 SSE 快照（by_status），轮询分支不回填，实测文案="${degradeProgress}"`);
  const degradedTerminal = await waitJobTerminal(st.api, degradeJob.id, { timeout: 300000 });
  ledger.equal('轮询期间作业照常跑完（降级不影响后端执行）', degradedTerminal.status, 'succeeded');
  await waitFor(async () => !(await page.locator('.el-card__header').first().innerText()).includes('轮询降级'), { label: '终态后停止轮询并摘掉降级 tag', timeout: 20000 });
  const headTerminal = (await page.locator('.el-card__header').first().innerText()).replace(/\s+/g, ' ').trim();
  ledger.check('终态后头部只剩状态 tag（实时进度/轮询降级都撤销）', /succeeded/.test(headTerminal) && !/轮询降级|实时进度/.test(headTerminal), headTerminal.slice(0, 160));
  await page.unroute('**/api/jobs/*/events').catch(() => {});
  await shot(page, ledger, 'detail_after_degrade');

  // —— 详情页取消：排空 Worker 让作业停在非终态，取消按钮与文案才有确定的观测窗口
  const onlineWorkers = (await st.api.get('/workers')).filter((w) => w.status === 'online');
  for (const w of onlineWorkers) {
    await st.api.send('POST', `/workers/${w.id}/drain`, {}, undefined);
    st.drained.push(w.id);
  }
  await waitFor(async () => (await st.api.get('/workers')).filter((w) => st.drained.includes(w.id)).every((w) => w.status === 'draining'), { label: '在线 Worker 全部 draining', timeout: 40000 });
  const dName = jobName('e2e-detail-cancel');
  const dJob = await st.api.send('POST', '/jobs', {
    name: dName,
    data_source_id: st.nasSource.id,
    source_path: '/nas/cancel',
    rule_set_id: st.canonical.id,
    file_filter: { recursive: true, max_depth: 5, max_files: 20000 },
  }, undefined);
  st.createdJobs.push(dJob.id);
  await go(page, `/logharvest/jobs/${dJob.id}`, { reload: true, waitText: `作业 #${dJob.id}` });
  const cancelBtn = page.getByRole('button', { name: '取消作业' });
  ledger.check('非终态详情给「取消作业」按钮', await cancelBtn.isVisible().catch(() => false), await bodyButtons(page).then((b) => [...new Set(b)].slice(0, 14).join(',')));
  await clearMessages(page);
  await cancelBtn.click();
  const dBox = await readMessageBox(page);
  ledger.equal('详情页确认框正文', dBox.body, '确认请求取消该作业？');
  ledger.equal('详情页确认框标题', dBox.title, '取消作业');
  ledger.check('默认按钮是 locale 的 确定/取消', dBox.buttons.includes('确定') && dBox.buttons.includes('取消'), JSON.stringify(dBox.buttons));
  await clickMessageBoxButton(page, '取消');
  await page.waitForTimeout(700);
  ledger.check('点「取消」不发请求也不改状态', (await st.api.get(`/jobs/${dJob.id}`)).status !== 'cancelled', JSON.stringify({ status: (await st.api.get(`/jobs/${dJob.id}`)).status }));
  await clearMessages(page);
  await cancelBtn.click();
  await readMessageBox(page);
  await clickMessageBoxButton(page, '确定');
  const dMsg = await waitMessage(page, /已请求取消，作业状态：/, 15000);
  ledger.check('取消回显「已请求取消，作业状态：cancelled」', dMsg.includes('cancelled'), dMsg);
  await waitFor(async () => (await st.api.get(`/jobs/${dJob.id}`)).status === 'cancelled', { label: '后端落 cancelled', timeout: 20000 });
  await page.waitForTimeout(1200);
  const dHead = (await page.locator('.el-card__header').first().innerText()).replace(/\s+/g, ' ').trim();
  ledger.check('取消后头部状态转 cancelled 且按钮消失', dHead.includes('cancelled') && (await page.getByRole('button', { name: '取消作业' }).count()) === 0, dHead.slice(0, 160));
  const dDesc = await page.evaluate(() => {
    const out = {};
    document.querySelectorAll('.el-descriptions__body table tr').forEach((tr) => {
      const cells = [...tr.querySelectorAll('.el-descriptions__label, .el-descriptions__content')];
      for (let i = 0; i + 1 < cells.length; i += 2) out[cells[i].innerText.trim()] = cells[i + 1].innerText.trim();
    });
    return out;
  });
  ledger.check('取消后描述「请求取消」转为「是」', dDesc['请求取消'] === '是', JSON.stringify(dDesc['请求取消']));
  await shot(page, ledger, 'detail_cancelled');
  const resumed = st.drained.splice(0);
  for (const id of resumed) await st.api.send('POST', `/workers/${id}/resume`, {}, undefined);
  await waitFor(async () => (await st.api.get('/workers')).filter((w) => resumed.includes(w.id)).every((w) => w.status === 'online'), { label: '恢复领取', timeout: 60000 });

  // —— 「重试失败文件」闭环 + 耗尽任务的尝试历史。用真实的失败作业 e2e-truncated（1 个
  // failed 任务、5 次失败尝试）。手动重试不清 attempts（jobs.py:233 注释"手动重试是一次新
  // 尝试"），所以点一次正好多跑一次、结果必然又失败——一次同时验掉"失败历史可读"与
  // "重试真的重新入队并被 Worker 领走"。这是被测功能本身，不是造数据。
  const failCandidates = (await st.api.get('/jobs?limit=100&offset=0')).filter((j) => j.name === 'e2e-truncated' && j.status === 'failed').sort((a, b) => b.id - a.id);
  const failJob = failCandidates[0];
  if (!failJob) {
    ledger.skip('重试失败文件闭环', '环境里没有 e2e-truncated 失败作业可重试（先跑 e2e/api_flow 的文件提取用例）');
    return;
  }
  const failFiles = await st.api.get(`/jobs/${failJob.id}/files?limit=50`);
  const failTask = failFiles.find((f) => f.status === 'failed');
  const beforeAttempts = (await st.api.get(`/jobs/${failJob.id}/files/${failTask.id}/attempts`)).length;
  ledger.check('前置：e2e-truncated 有耗尽的失败任务', beforeAttempts >= 5 && failTask.attempts >= failTask.max_attempts, JSON.stringify({ id: failJob.id, attempts: failTask.attempts, max_attempts: failTask.max_attempts, history: beforeAttempts }));

  await go(page, `/logharvest/jobs/${failJob.id}`, { reload: true, waitText: `作业 #${failJob.id}` });
  const retryBtn = page.getByRole('button', { name: /重试失败文件/ });
  const retryBtnText = (await retryBtn.innerText()).replace(/\s+/g, ' ').trim();
  ledger.check('失败作业给「重试失败文件（N）」，N=失败文件数', /重试失败文件（1）/.test(retryBtnText), retryBtnText);
  await page.locator('.el-table__body').first().locator('.el-table__row').first().getByRole('button', { name: '历史' }).click();
  const fDrawer = page.locator('.el-drawer').filter({ hasText: '尝试历史' }).first();
  await fDrawer.waitFor({ state: 'visible', timeout: 8000 });
  const fRowLoc = fDrawer.locator('.el-table__body .el-table__row');
  // 抽屉可见 ≠ 表格已渲染：pure-table 的数据是抽屉打开后再拉的，
  // 非等待的 .count() 会读到 0（实测首跑就栽在这里）。先等出行，再用同一份文本计数。
  await waitFor(async () => ((await fRowLoc.count()) > 0 ? true : null), { label: '尝试历史行渲染', timeout: 15000 }).catch(() => {});
  const fTexts = (await fRowLoc.allInnerTexts()).map((t) => t.replace(/\s+/g, ' ').trim());
  ledger.equal('抽屉逐行展示每一次尝试', fTexts.length, beforeAttempts);
  const fAll = fTexts.join('\n');
  ledger.check('历史里写明失败原因原文', /invalid compressed file/.test(fAll), fTexts[0]?.slice(0, 180) ?? 'empty');
  ledger.check('历史里 Worker 容器名与版本逐次留痕', /lh-e2e-worker-1/.test(fAll) && /lh-e2e-worker-2/.test(fAll) && /0\.1\.0/.test(fAll), fTexts.slice(0, 2).join(' | ').slice(0, 220));
  const seq = fTexts.map((t) => t.trim().split(' ')[0]);
  ledger.check('按 attempt_no 升序返回', seq.join(',') === Array.from({ length: beforeAttempts }, (_, i) => String(i + 1)).join(','), seq.join(','));
  ledger.check('抽屉 footer 报真实条数', (await fDrawer.locator('.el-drawer__footer').innerText()).includes(`已加载 ${beforeAttempts} 次尝试`), (await fDrawer.locator('.el-drawer__footer').innerText()).replace(/\s+/g, ' '));
  await shot(page, ledger, 'detail_failed_attempts');
  ledger.check('失败历史抽屉可关闭', await closeDrawer(page), 'drawer still visible');

  await clearMessages(page);
  await retryBtn.click();
  const rBox = await readMessageBox(page);
  ledger.check('重试确认框正文如实说明会重置为待处理', rBox.body.startsWith('确认重试该作业全部失败的文件任务？') && rBox.body.includes('重新排队'), rBox.body);
  ledger.equal('重试确认框标题', rBox.title, '重试失败文件');
  await clickMessageBoxButton(page, '取消');
  await page.waitForTimeout(700);
  ledger.check('点「取消」不重新入队', (await st.api.get(`/jobs/${failJob.id}/files?limit=50`)).some((f) => f.status === 'failed'), '任务状态已被改动');
  await clearMessages(page);
  await retryBtn.click();
  await readMessageBox(page);
  await clickMessageBoxButton(page, '确定');
  const rMsg = await waitMessage(page, /重新排队|没有可重试/, 15000);
  ledger.equal('重试成功提示「已重新排队 1 个失败文件」', rMsg, '已重新排队 1 个失败文件');
  const retried = await waitFor(async () => {
    const j = await st.api.get(`/jobs/${failJob.id}`);
    const list = await st.api.get(`/jobs/${failJob.id}/files?limit=50`);
    const t = list.find((f) => f.id === failTask.id);
    const hist = await st.api.get(`/jobs/${failJob.id}/files/${failTask.id}/attempts`);
    return j.status === 'failed' && hist.length > beforeAttempts ? { job: j, task: t, hist } : null;
  }, { label: '重试后被重新领走并再次跑完', timeout: 300000, interval: 5000 });
  ledger.check('重试确实多跑了一次（attempts 不重置，历史变长）', retried.hist.length === beforeAttempts + 1, JSON.stringify({ before: beforeAttempts, after: retried.hist.length, attempts: retried.task.attempts }));
  ledger.equal('再跑一次仍失败，作业回到 failed', retried.job.status, 'failed');
  await go(page, `/logharvest/jobs/${failJob.id}`, { reload: true, waitText: `作业 #${failJob.id}` });
  await page.locator('.el-table__body').first().locator('.el-table__row').first().getByRole('button', { name: '历史' }).click();
  const fDrawer2 = page.locator('.el-drawer').filter({ hasText: '尝试历史' }).first();
  await fDrawer2.waitFor({ state: 'visible', timeout: 8000 });
  const gRowLoc = fDrawer2.locator('.el-table__body .el-table__row');
  await waitFor(async () => ((await gRowLoc.count()) === beforeAttempts + 1 ? true : null), { label: '新尝试行渲染到位', timeout: 15000 }).catch(() => {});
  ledger.equal('刷新后抽屉多出一行新尝试', await gRowLoc.count(), beforeAttempts + 1);
  await closeDrawer(page);
  await shot(page, ledger, 'detail_after_retry');
}

// ---------------------------------------------------------------------------
// U6 命中结果：/matches 的作用域契约（job_id 或成对时间范围，否则 422）在页面上的
// 落点——查询按钮先禁用 + alert 讲清契约；游标翻页只能顺着一屏的末行往后走
// （created_before + id_before 成对）；级别单值下推、每屏下推；行内「所属作业/所属文件」
// 回跳；?job_id= 深链进入即自动查询；窄屏退回卡片布局。
// ---------------------------------------------------------------------------
async function phase_matches(ledger, st) {
  ledger.phase(PHASE_LABEL.matches);
  const page = st.adminPage ?? await useSession(ledger, st, 'admin');
  const { api } = st;
  const clean = st.jobClean;
  const oracle = st.oracle;

  // 翻页演示需要一个"超过一屏（50）"的作业。优先复用库里已跑完的 /nas/cancel
  // （一个 zip、59 条命中，U5b 就是这么造的）；没有就现场跑一个真实作业。
  const bigCandidates = (await api.get('/jobs?limit=200&offset=0'))
    .filter((j) => j.status === 'succeeded' && j.source_path === '/nas/cancel')
    .sort((a, b) => b.id - a.id);
  let big = null;
  for (const j of bigCandidates) {
    const rows = await api.get(`/matches?job_id=${j.id}&limit=100`);
    if (rows.length > 50) { big = { job: j, total: rows.length }; break; }
  }
  if (!big) {
    const created = await api.send('POST', '/jobs', {
      name: jobName('e2e-matches-paging'),
      data_source_id: st.nasSource.id,
      source_path: '/nas/cancel',
      rule_set_id: st.canonical.id,
      file_filter: { recursive: true, max_depth: 5, max_files: 20000 },
    }, undefined);
    st.createdJobs.push(created.id);
    await waitJobTerminal(api, created.id, { timeout: 420000 });
    const rows = await api.get(`/matches?job_id=${created.id}&limit=200`);
    big = { job: created, total: rows.length };
  }
  ledger.check('前置：有一个超过一屏的作业用于游标翻页', big.total > 50, JSON.stringify({ id: big.job.id, total: big.total }));

  const dRows = page.locator('.el-table__body').first().locator('.el-table__row');
  const hintText = async () => (await page.locator('body').innerText()).replace(/\s+/g, ' ');
  const lastMatchesReq = async () => (await recentRequests(page, /\/api\/matches\?/)).at(-1)?.url ?? '';

  await go(page, '/logharvest/matches', { reload: true, waitText: '作用域必须二选一' });
  const searchBtn = page.getByRole('button', { name: '查询' });
  ledger.check('未选作用域对象时查询按钮是禁用的（不让人撞 422）', await searchBtn.isDisabled(), `disabled=${await searchBtn.isDisabled()}`);
  const gateAlert = (await page.locator('.el-alert').first().innerText()).replace(/\s+/g, ' ');
  ledger.check('alert 讲清按作业查询需要先选作业', gateAlert.includes('按作业查询需要选中一个作业'), gateAlert.slice(0, 200));
  ledger.check('alert 原文带后端契约与 422', gateAlert.includes('created_from') && gateAlert.includes('422'), gateAlert.slice(0, 260));
  ledger.check('未查询时脚注说明为什么不能翻', (await hintText()).includes('尚未查询：请先限定作业或时间范围'), (await hintText()).slice(0, 200));
  const emptyText = (await page.locator('.el-table__empty-text').first().innerText().catch(() => '')).trim();
  ledger.equal('桌面空态退回 el-table 自带「暂无数据」', emptyText, '暂无数据');
  ledger.skip('未查询空态文案两端不一致', '观察项：只有窄屏卡片分支自定义了 el-empty 描述「尚未查询」（matches/index.vue 的 v-if=isMobile 块），桌面表格分支用组件默认「暂无数据」，靠脚注解释「尚未查询：请先限定作业或时间范围」');
  ledger.check('说明段落写明接口返回裸数组、没有总数与页码', (await hintText()).includes('不带总数'), 'hint paragraph');

  // —— 时间范围：只填一半必须被界面拦住
  /** el-date-picker 的输入框可手输：fill（失败则全选后键入）+ Enter 才会写进 model */
  async function setDatePicker(placeholder, value) {
    const input = page.getByPlaceholder(placeholder);
    await input.click().catch(() => {});
    try {
      await input.fill(value);
    } catch {
      await input.press('Control+a');
      await input.type(value, { delay: 20 });
    }
    await page.keyboard.press('Enter');
    await page.waitForTimeout(500);
    return (await input.inputValue()).trim();
  }
  await page.getByText('按时间范围', { exact: true }).click();
  const fromVal = await setDatePicker('created_from', '2026-10-01 00:00:00');
  ledger.check('开始时间输入被组件接受（写进了 model）', /2026-10-01/.test(fromVal), fromVal);
  const halfAlert = (await page.locator('.el-alert').first().innerText()).replace(/\s+/g, ' ');
  ledger.check('时间范围只填一半时提示"必须成对"', halfAlert.includes('时间范围必须成对'), halfAlert.slice(0, 200));
  ledger.check('半填状态下查询仍然禁用', await searchBtn.isDisabled(), `disabled=${await searchBtn.isDisabled()}`);
  const toVal = await setDatePicker('created_to', '2026-10-31 23:59:59');
  ledger.check('结束时间输入被组件接受', /2026-10-31/.test(toVal), toVal);
  ledger.check('成对填写后查询按钮放开', !(await searchBtn.isDisabled()), `disabled=${await searchBtn.isDisabled()}`);
  await clearMessages(page);
  await searchBtn.click();
  await waitFor(async () => /created_to=/.test(await lastMatchesReq()), { label: '时间范围查询请求', timeout: 15000 });
  const rangeReq = decodeURIComponent(await lastMatchesReq());
  ledger.check('时间范围请求带 created_from 与 created_to', /created_from=.*created_to=/.test(rangeReq), rangeReq.slice(0, 240));
  ledger.check('时间是带 Z 的 UTC 串（前端 toApiDateTime 负责补时区）', /created_from=[^&]+Z(?:&|$)/.test(rangeReq) && /created_to=[^&]+Z(?:&|$)/.test(rangeReq), rangeReq.slice(0, 240));
  ledger.check('时间范围模式下不带 job_id', !/job_id=/.test(rangeReq), rangeReq.slice(0, 240));
  await waitFor(async () => ((await hintText()).includes('总数未知：第 1 段') ? true : null), { label: '时间范围查询出结果', timeout: 15000 }).catch(() => {});
  ledger.check('时间范围查询后脚注进入分段口径', (await hintText()).includes('总数未知：第 1 段'), (await hintText()).match(/总数未知：[^（]*（[^）]*）?/)?.[0] ?? '');
  await shot(page, ledger, 'matches_range_scope');

  // —— 切回按作业：另一侧的残留值必须被清掉（否则会带着看不见的条件查询）
  await page.getByText('按作业', { exact: true }).click();
  await pickOption(page, { formLabel: '作业', placeholder: '选择要查看的作业' }, new RegExp(`^#${esc(String(clean.id))} `));
  const jobSel = (await page.locator('.el-select').filter({ hasText: `#${clean.id}` }).first().innerText()).replace(/\s+/g, ' ').trim();
  ledger.check('作业下拉显示「#id 名称」', jobSel.includes(`#${clean.id}`) && jobSel.includes(clean.name), jobSel.slice(0, 120));
  await searchBtn.click();
  await waitFor(async () => /job_id=/.test(await lastMatchesReq()), { label: '按作业查询请求', timeout: 15000 });
  const jobReq = decodeURIComponent(await lastMatchesReq());
  ledger.check('按作业查询请求带 job_id 且不含时间参数', jobReq.includes(`job_id=${clean.id}`) && !/created_from=/.test(jobReq), jobReq.slice(0, 200));
  await waitFor(async () => (await dRows.count()) === oracle.expect_total_matches, { label: 'clean 作业命中行到位', timeout: 15000 }).catch(() => {});
  ledger.equal('按作业查出的行数=夹具 oracle 命中数', await dRows.count(), oracle.expect_total_matches);
  const segFoot = (await hintText());
  ledger.check('不足一屏时脚注写「第 1 段…（不足一屏，已到末尾）」', /总数未知：第 1 段，本段 14 条/.test(segFoot) && segFoot.includes('不足一屏，已到末尾'), segFoot.match(/总数未知[^ ]*( ?（[^）]*）)?/)?.[0] ?? segFoot.slice(0, 200));
  ledger.check('已到末尾时「下一页」禁用', await page.getByRole('button', { name: '下一页' }).isDisabled(), 'next button state');

  // —— 级别筛选（后端只接受单值）
  const cleanMatches = await api.get(`/matches?job_id=${clean.id}&limit=200`);
  const severities = [...new Set(cleanMatches.map((m) => m.severity))];
  const sevTagType = { info: 'el-tag--info', warning: 'el-tag--warning', error: 'el-tag--danger' };
  const pickSev = severities.includes('error') ? 'error' : severities[0];
  ledger.check('前置：clean 作业命中行里有可筛的级别', severities.length > 0 && Boolean(pickSev), JSON.stringify(severities));
  await pickOption(page, 'severity', new RegExp(`^${esc(pickSev)}$`));
  await searchBtn.click();
  await waitFor(async () => (await lastMatchesReq()).includes(`severity=${pickSev}`), { label: '级别筛选下推 severity=', timeout: 15000 });
  ledger.check('级别筛选请求下推 severity=（后端单值）', (await lastMatchesReq()).includes(`severity=${pickSev}`), decodeURIComponent(await lastMatchesReq()).slice(0, 200));
  await page.waitForTimeout(800);
  const sevCells = await dRows.locator('.el-tag').allInnerTexts();
  ledger.check('筛选后表格里只剩该级别', sevCells.length > 0 && sevCells.every((t) => t.trim() === pickSev), JSON.stringify(sevCells.slice(0, 6)));
  const sevClass = await dRows.first().locator('.el-tag').first().getAttribute('class');
  ledger.check(`级别 tag 配色符合映射（${pickSev}→${sevTagType[pickSev]?.replace('el-tag--', '')}）`, (sevClass ?? '').includes(sevTagType[pickSev] ?? 'el-tag'), sevClass ?? '');
  // 这页的级别/每屏下拉都没有 @change（不同于作业列表的状态筛选），清空只改 model，
  // 必须再点「查询」才生效——本页的一致行为，断言按它来。
  const reqsBeforeClear = (await recentRequests(page, /\/api\/matches\?/)).length;
  await clearSelect(page, pickSev);
  await page.waitForTimeout(900);
  ledger.equal('清空级别不会自动发请求（本页下拉无 @change）', (await recentRequests(page, /\/api\/matches\?/)).length, reqsBeforeClear);
  ledger.equal('清空后级别下拉回到 placeholder 态', await page.locator('.el-select').filter({ hasText: pickSev }).count(), 0);
  await searchBtn.click();
  await waitFor(async () => !/severity=/.test(await lastMatchesReq()), { label: '再查询后请求不带 severity', timeout: 15000 });
  await waitFor(async () => (await dRows.count()) === oracle.expect_total_matches, { label: '清空级别后行数回到全量', timeout: 15000 }).catch(() => {});
  ledger.check('再点查询后请求不带 severity 且行回到全量', !/severity=/.test(await lastMatchesReq()) && (await dRows.count()) === oracle.expect_total_matches, decodeURIComponent(await lastMatchesReq()).slice(0, 200));

  // —— 每屏条数
  await page.locator('.el-form-item').filter({ hasText: '每屏' }).locator('.el-select').first().click();
  await page.locator('.el-select-dropdown:visible .el-select-dropdown__item').filter({ hasText: '100' }).first().click();
  await page.waitForTimeout(400);
  await searchBtn.click();
  await waitFor(async () => (await lastMatchesReq()).includes('limit=100'), { label: '每屏 100 下推 limit=', timeout: 15000 });
  ledger.check('每屏选择下推 limit=100', (await lastMatchesReq()).includes('limit=100'), decodeURIComponent(await lastMatchesReq()).slice(0, 200));
  await page.locator('.el-form-item').filter({ hasText: '每屏' }).locator('.el-select').first().click();
  await page.locator('.el-select-dropdown:visible .el-select-dropdown__item').filter({ hasText: /^50$/ }).first().click();
  await page.waitForTimeout(400);

  // —— 游标翻页：只有"本页末行的 created_at + id"这一种往后走的方式
  await pickOption(page, { formLabel: '作业', placeholder: '选择要查看的作业' }, new RegExp(`^#${esc(String(big.job.id))} `));
  await searchBtn.click();
  await waitFor(async () => (await dRows.count()) === 50, { label: '大作业首屏 50 条', timeout: 20000 });
  const firstSeg = (await hintText());
  ledger.check('满一屏时脚注不带「已到末尾」', /第 1 段，本段 50 条/.test(firstSeg) && !firstSeg.includes('不足一屏'), firstSeg.slice(firstSeg.indexOf('总数未知'), firstSeg.indexOf('总数未知') + 90));
  const nextBtn = page.getByRole('button', { name: '下一页' });
  ledger.check('满一屏时「下一页」可点', !(await nextBtn.isDisabled()), 'next button state');
  await nextBtn.click();
  await waitFor(async () => /created_before=.*id_before=|id_before=.*created_before=/.test(decodeURIComponent(await lastMatchesReq())), { label: '翻页请求带成对游标', timeout: 20000 });
  const page2Req = decodeURIComponent(await lastMatchesReq());
  ledger.check('翻页请求游标成对（created_before + id_before）', /created_before=/.test(page2Req) && /id_before=/.test(page2Req), page2Req.slice(0, 240));
  await waitFor(async () => (await dRows.count()) === big.total - 50, { label: '第二段行数=总数-50', timeout: 20000 });
  const secondSeg = (await hintText());
  ledger.check('第二段脚注与行数一致', /第 2 段/.test(secondSeg) && secondSeg.includes(`本段 ${big.total - 50} 条`), secondSeg.slice(secondSeg.indexOf('总数未知'), secondSeg.indexOf('总数未知') + 90));
  ledger.check('翻到末尾后「下一页」禁用', await nextBtn.isDisabled(), 'next button state at tail');
  await shot(page, ledger, 'matches_segment2');
  await searchBtn.click();
  await waitFor(async () => !/created_before=/.test(decodeURIComponent(await lastMatchesReq())), { label: '重新查询清掉游标', timeout: 15000 });
  ledger.check('重新点「查询」回到第一屏（游标被清空）', !/created_before=/.test(decodeURIComponent(await lastMatchesReq())) && (await hintText()).includes('第 1 段'), decodeURIComponent(await lastMatchesReq()).slice(0, 200));
  // 上一行 waitFor 只验证了请求发出：响应落地前表格仍是第二段的行（第二段首行正是
  // 全量的第 51 行）。首行断言必须等首屏真的渲染完——第二段只有 big.total-50 行，
  // 行数回到 50 即首屏数据已到位，此时才允许取「翻页前的首行」基准。
  await waitFor(async () => ((await dRows.count()) === 50 ? true : null), { label: '重新查询后首屏渲染', timeout: 15000 });
  const firstRowOnSeg1 = (await dRows.first().innerText()).replace(/\s+/g, ' ').trim();
  const backBtn = page.getByRole('button', { name: '回到第一屏' });
  await nextBtn.click();
  await waitFor(async () => (await dRows.count()) === big.total - 50, { label: '再次翻到第二段', timeout: 20000 });
  await backBtn.click();
  await waitFor(async () => (await hintText()).includes('第 1 段') && (await dRows.count()) === 50, { label: '「回到第一屏」退回首屏', timeout: 20000 });
  const backReq = decodeURIComponent(await lastMatchesReq());
  ledger.check('「回到第一屏」请求不带游标', !/created_before=/.test(backReq) && !/id_before=/.test(backReq), backReq.slice(0, 200));
  const firstRowAfterBack = (await dRows.first().innerText()).replace(/\s+/g, ' ').trim();
  ledger.check('回到第一屏后首行与翻页前一致（同条件同排序）', firstRowAfterBack === firstRowOnSeg1, `${firstRowAfterBack.slice(0, 90)} | ${firstRowOnSeg1.slice(0, 90)}`);

  // —— 行内回跳：所属作业 → 详情；所属文件 → 详情并自动打开尝试历史
  await dRows.first().getByRole('button', { name: `#${big.job.id}` }).click();
  await page.waitForURL(new RegExp(`#\\/logharvest\\/jobs\\/${big.job.id}`), { timeout: 15000 }).catch(() => {});
  ledger.check('行内「所属作业」跳该作业详情', new RegExp(`#\\/logharvest\\/jobs\\/${big.job.id}`).test(page.url()), page.url());
  // 详情头是 `作业 #{{ job?.id }} · {{ job?.name ?? "加载中" }}`：fetch 未回来时 id 为空、
  // 名字是「加载中」。不等待就把"取数中"当成"落地页错了"，是 harness 的时序 bug 不是产品的。
  const jobHead = page.locator('.el-card__header').first();
  const readHead = async () => (await jobHead.innerText().catch(() => '')).replace(/\s+/g, ' ').trim();
  const headText = await waitFor(async () => {
    const t = await readHead();
    return t.includes(`作业 #${big.job.id}`) ? t : null;
  }, { label: '作业详情头渲染出作业号', timeout: 15000 }).catch(readHead);
  ledger.check('落地页就是那个作业', String(headText).includes(`作业 #${big.job.id}`), String(headText).slice(0, 140));

  await go(page, `/logharvest/matches?job_id=${clean.id}`, { reload: true, waitText: '作用域必须二选一' });
  await waitFor(async () => (await dRows.count()) === oracle.expect_total_matches, { label: '深链自动查询出结果', timeout: 20000 }).catch(() => {});
  ledger.check('?job_id= 深链进入即自动查询（无需点查询）', (await dRows.count()) === oracle.expect_total_matches, `${await dRows.count()} 行，URL=${page.url()}`);
  const deepReq = (await recentRequests(page, new RegExp(`/api/matches\\?.*job_id=${clean.id}`))).at(-1);
  ledger.check('深链确实发出了带 job_id 的请求', Boolean(deepReq), deepReq?.url.slice(0, 200) ?? await lastMatchesReq());
  const firstMatch = cleanMatches[0] ?? {};
  const fileName = firstMatch.file_name ?? (await dRows.first().innerText()).split(' ')[1];
  await dRows.first().getByRole('button', { name: fileName, exact: true }).click().catch(async () => {
    await dRows.first().locator('.el-button').nth(1).click();
  });
  await page.waitForURL(/#\/logharvest\/jobs\/\d+\?file_task=/, { timeout: 15000 }).catch(() => {});
  ledger.check('行内「所属文件」跳详情并带上 ?file_task=', /#\/logharvest\/jobs\/\d+\?file_task=\d+/.test(page.url()), page.url());
  const fileDrawer = page.locator('.el-drawer').filter({ hasText: '尝试历史' }).first();
  await fileDrawer.waitFor({ state: 'visible', timeout: 10000 }).catch(() => {});
  ledger.check('落地后自动打开该任务的尝试历史', await fileDrawer.isVisible().catch(() => false), (await page.locator('body').innerText()).replace(/\s+/g, ' ').slice(0, 160));
  // clean 夹具的顶层文件是归档（app.zip / svc.tar.gz），命中行的「所属文件」列显示的是**成员名**
  // （svc.log），而文件任务本身对应 NAS 上的**容器文件名**（svc.tar.gz）。所以这里核对的是
  // "抽屉打开的任务 id 是否就是这一行的 file_task_id"，以及"标题里的文件名是否等于任务名"——
  // 拿成员名去等标题会永远假失败（曾经就是这么一条 FAIL）。
  const drawerTitle = await waitFor(async () => {
    const t = (await fileDrawer.locator('.el-drawer__title').innerText()).replace(/\s+/g, ' ').trim();
    return t.includes(`文件任务 #${firstMatch.file_task_id}`) ? t : null;
  }, { label: '抽屉标题渲染出文件任务号', timeout: 12000 }).catch(() => null) ?? '(未渲染)';
  ledger.check('抽屉打开的就是该命中行所属的文件任务', String(drawerTitle).includes(`文件任务 #${firstMatch.file_task_id}`) && page.url().includes(`file_task=${firstMatch.file_task_id}`), `title=${String(drawerTitle).slice(0, 120)} url=${page.url()} 行成员名=${fileName}`);
  const containerName = (await api.get(`/jobs/${clean.id}/files?limit=500&offset=0`).catch(() => []))
    .find((t) => t.id === firstMatch.file_task_id)?.file_name;
  ledger.check('抽屉标题用容器文件名（归档成员不顶替任务名）', Boolean(containerName) && String(drawerTitle).includes(containerName), `title=${String(drawerTitle).slice(0, 120)} 任务文件=${containerName} 成员=${fileName}`);
  await closeDrawer(page);
  await shot(page, ledger, 'matches_file_task_link');

  // —— 窄屏（390）退回卡片布局：表格放不下，链接与字段都要保留
  await go(page, `/logharvest/matches?job_id=${clean.id}`, { reload: true, waitText: '作用域必须二选一' });
  await waitFor(async () => (await dRows.count()) === oracle.expect_total_matches, { label: '桌面视图出结果', timeout: 20000 }).catch(() => {});
  ledger.check('桌面视口下确实是表格（有 el-table 与表头）', (await page.locator('.el-table').count()) > 0 && (await page.locator('.el-table__header').count()) > 0, `el-table=${await page.locator('.el-table').count()}`);
  await page.setViewportSize({ width: 390, height: 844 });
  const mobileCards = await waitFor(async () => ((await page.locator('.match-raw').count()) >= oracle.expect_total_matches ? await page.locator('.match-raw').count() : null), { label: '窄屏卡片渲染', timeout: 15000 }).catch(() => null);
  ledger.check('390 视口下命中行改成卡片（一条一张）', mobileCards !== null && mobileCards >= oracle.expect_total_matches, `match-raw=${mobileCards} url=${page.url()}`);
  const mobileBody = (await page.locator('body').innerText()).replace(/\s+/g, ' ');
  ledger.check('卡片布局仍给分段脚注与「所属文件」链接', mobileBody.includes('总数未知：第 1 段') && mobileBody.includes('所属文件'), mobileBody.slice(0, 220));
  // index.vue 是 `v-if="isMobile"` / `v-else` 的表格分支：视口变化后 device 重算 + 重渲染是异步的，
  // 切换后立刻 count 一次会把"还没来得及卸载"读成"两个分支同时在"（这就是那条假 FAIL 的成因）。
  // 等它卸载再断言；超时才说明互斥真的失效。
  const tableGone = await waitFor(async () => ((await page.locator('.el-table').count()) === 0 ? 'gone' : null), { label: '窄屏下表格分支卸载', timeout: 10000 }).catch(() => null);
  ledger.check('卡片布局下表格整体消失（isMobile 分支互斥）', tableGone === 'gone', `el-table=${await page.locator('.el-table').count()} match-raw=${await page.locator('.match-raw').count()}`);
  await shot(page, ledger, 'matches_mobile_cards');
  await page.setViewportSize({ width: 1440, height: 900 });
  await waitFor(async () => (await dRows.count()) === oracle.expect_total_matches, { label: '回到桌面视口重出表格', timeout: 15000 }).catch(() => {});
  ledger.check('回到 1440 视口恢复表格', (await dRows.count()) === oracle.expect_total_matches, `${await dRows.count()} 行`);
}

// ==== 阶段注册表 ============================================================
// U7~U15 住在 e2e/ui_flow_{ops,rules,sys}.mjs 里，按"用到才 import"的方式挂载：
// 三个模块由不同的人并行填，静态 import 会让一个半成品文件拖垮整条流程。
const MODULES = {
  ops: './ui_flow_ops.mjs',   // U7 数据源 / U8 Worker / U9 任务统计 / U14 概览
  rules: './ui_flow_rules.mjs', // U10 规则集 / U11 规则维护
  sys: './ui_flow_sys.mjs',   // U12 系统管理 / U13 操作日志 / U15 登出收尾
};

const loaded = new Map();

function lazyPhase(modName, phaseName) {
  return async (ledger, st) => {
    if (!loaded.has(modName)) {
      loaded.set(modName, await import(MODULES[modName]).catch((err) => {
        throw new Error(`阶段模块 ${MODULES[modName]} 加载失败：${err.message.split('\n')[0]}`);
      }));
    }
    const fn = loaded.get(modName).phases?.[phaseName];
    if (!fn) {
      ledger.skip(PHASE_LABEL[phaseName], '该阶段尚未在模块里导出（phases 里没有这个键）');
      return;
    }
    await fn(ledger, st, { PHASE_LABEL });
  };
}

const ALL = [
  ['prep', phase_prep],
  ['login', phase_login],
  ['roles', phase_roles],
  ['wizard', phase_wizard],
  ['jobs', phase_jobs],
  ['detail', phase_detail],
  ['matches', phase_matches],
  ['datasources', lazyPhase('ops', 'datasources')],
  ['workers', lazyPhase('ops', 'workers')],
  ['stats', lazyPhase('ops', 'stats')],
  ['overview', lazyPhase('ops', 'overview')],
  ['rulesets', lazyPhase('rules', 'rulesets')],
  ['rules', lazyPhase('rules', 'rules')],
  ['system', lazyPhase('sys', 'system')],
  ['audit', lazyPhase('sys', 'audit')],
  ['logout', lazyPhase('sys', 'logout')],
];

const REGISTRY = Object.fromEntries(ALL);

function resolve(wanted) {
  const out = [];
  const seen = new Set();
  const visit = (name) => {
    if (seen.has(name)) return;
    seen.add(name);
    for (const d of DEPS[name] ?? []) visit(d);
    out.push(name);
  };
  for (const w of ALL.map(([n]) => n)) {
    if (wanted.includes(w)) visit(w);
  }
  return out;
}

function parseArgs(argv) {
  const i = argv.indexOf('--phase');
  if (i < 0) return ALL.map(([n]) => n);
  const raw = argv[i + 1] ?? '';
  const list = raw.split(',').map((s) => s.trim()).filter(Boolean);
  const unknown = list.filter((n) => !REGISTRY[n]);
  if (unknown.length) {
    console.error(`未知阶段 ${JSON.stringify(unknown)}；可选：${ALL.map(([n]) => n).join(', ')}`);
    process.exit(2);
  }
  return resolve(list);
}

async function main() {
  const wanted = parseArgs(process.argv.slice(2));
  const ledger = new Ledger('UI Flow');
  const st = {
    oracle: JSON.parse(fs.readFileSync(path.join(ARTIFACTS, 'fixtures.json'), 'utf-8')).cases.clean,
    api: new Api(),
    createdUsers: [],
    createdJobs: [],
    createdSources: [],
    createdRuleSets: [],
    createdRoles: [],
    createdMenus: [],
    drained: [],
  };
  console.log(`UI Flow 启动：web=${WEB} api=${API_BASE} 阶段=${wanted.join(',')} Playwright=${PW_PATH}`);
  st.browser = await chromium.launch({ channel: 'chrome', headless: true });
  let step = 'startup';
  try {
    for (const name of wanted) {
      step = name;
      try {
        await REGISTRY[name](ledger, st);
      } catch (err) {
        ledger.fail(`${PHASE_LABEL[name] ?? name} 阶段异常`, err);
        const page = st.loginPage ?? st.adminPage ?? sessions.get('admin')?.page;
        if (page) await shot(page, ledger, `crash_${name}`).catch(() => {});
      }
    }
  } finally {
    if (wanted.includes('logout')) {
      try {
        await phase_teardown(ledger, st);
      } catch (err) {
        ledger.fail('收尾异常', err);
      }
    } else {
      console.log('\n（跳过收尾：未跑 logout 阶段；--phase 里加上 logout 可强制清理）');
    }
    for (const { context } of sessions.values()) await context.close().catch(() => {});
    // U1 用的是独立 context（不在 sessions 表里），通过 page.context() 拿
    for (const page of [st.loginPage, st.adminPage, st.operatorPage, st.viewerPage]) {
      await page?.context()?.close().catch(() => {});
    }
    await st.browser.close().catch(() => {});
  }
  const res = ledger.summary();
  process.exit(res.failed > 0 ? 1 : 0);
}

async function phase_teardown(ledger, st) {
  // 收尾由各阶段模块自己挂：sys 模块的 logout 阶段把清理函数放进 st.teardownHook。
  if (typeof st.teardownHook === 'function') return st.teardownHook(ledger, st);
  ledger.skip('收尾', '未挂载 st.teardownHook（U15 负责清理 UI 造出的作业/数据源/用户/角色/菜单）');
  return undefined;
}

main().catch((err) => {
  console.error('UI Flow 未捕获异常:', err);
  process.exit(3);
});
