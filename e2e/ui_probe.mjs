// 只读的页面"照妖镜"：登录 → 逐个路由（可附带点按钮打开弹窗/抽屉）→ 把界面上真实存在
// 的文案、表头、按钮、表单项、下拉占位符打到 stdout。
//
// 写阶段用例之前先跑它，别靠猜：ui_flow 的断言全部是"文案即选择器"，差一个全角括号就红。
// 它只读业务数据（GET 列表 + 打开前端弹窗），不点任何"提交/保存/删除"，因此不改变环境状态，
// 可以和别人的 ui_run.sh 并行。
//
//   node e2e/ui_probe.mjs /logharvest/datasources
//   node e2e/ui_probe.mjs /logharvest/datasources --click 新建数据源
//   node e2e/ui_probe.mjs /logharvest/workers --mobile --as viewer
//   node e2e/ui_probe.mjs /system/users /system/roles --dump-rows 3
//
// 选项：
//   --as admin|operator|viewer   登录身份（默认 admin）
//   --click <按钮文本正则>        进入页面后点它（用于弹出对话框/抽屉），可重复
//   --mobile                     390x844 视口（看窄屏分支）
//   --dump-rows N                打印前 N 行表格文本（默认 2）
//   --wait-ms N                  每次导航后的额外等待（默认 1200）
//   --pick <占位符文本>           打开一个 el-select 并列出它的选项，可重复

import {
  chromium, WEB, ACCOUNTS, newPage, go, openSelect, bodyButtons, tableHeaderTexts,
} from './ui_lib.mjs';

const argv = process.argv.slice(2);
const routes = [];
const clicks = [];
const picks = [];
let who = 'admin';
let mobile = false;
let dumpRows = 2;
let waitMs = 1200;
for (let i = 0; i < argv.length; i += 1) {
  const a = argv[i];
  if (a === '--as') who = argv[++i];
  else if (a === '--click') clicks.push(argv[++i]);
  else if (a === '--pick') picks.push(argv[++i]);
  else if (a === '--mobile') mobile = true;
  else if (a === '--dump-rows') dumpRows = Number(argv[++i]);
  else if (a === '--wait-ms') waitMs = Number(argv[++i]);
  else routes.push(a);
}
if (!routes.length) {
  console.error('用法：node e2e/ui_probe.mjs <路由hash> [--click 文本] [--pick 占位符] [--as operator] [--mobile]');
  process.exit(2);
}

const oneLine = (t) => (t || '').replace(/\s+/g, ' ').trim();

const browser = await chromium.launch({ channel: 'chrome', headless: true });
const { context, page } = await newPage(browser);
if (mobile) await page.setViewportSize({ width: 390, height: 844 });
const account = ACCOUNTS[who];
await page.goto(`${WEB}#/login`, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(1500);
await page.getByPlaceholder('账号').fill(account.username);
await page.getByPlaceholder('密码').fill(account.password);
await page.getByRole('button', { name: '登录' }).click();
await page.waitForURL(/#\/(home|access-denied)/, { timeout: 30000 }).catch(() => null);
await page.waitForTimeout(1000);
console.log(`登录身份=${who} 落地=${page.url()}`);

const items = async (sel) => (await page.locator(sel).allInnerTexts()).map(oneLine).filter(Boolean);

for (const route of routes) {
  console.log(`\n######## ${route} (${mobile ? '390' : '1440'}) ########`);
  await go(page, route, { reload: true });
  await page.waitForTimeout(waitMs);
  const body = oneLine(await page.locator('body').innerText().catch(() => ''));
  console.log(`URL: ${page.url()}`);
  console.log(`\n[BODY]（${body.length} 字）\n${body.slice(0, 4000)}`);
  console.log(`\n[表头] ${JSON.stringify(await tableHeaderTexts(page))}`);
  const tables = await page.locator('.el-table').count();
  console.log(`[表格数] ${tables}  [行数] ${await page.locator('.el-table__body .el-table__row').count()}`);
  for (let r = 0; r < dumpRows; r += 1) {
    const t = oneLine(await page.locator('.el-table__body .el-table__row').nth(r).innerText().catch(() => ''));
    if (t) console.log(`[行${r}] ${t}`);
  }
  console.log(`[空态] el-empty=${await page.locator('.el-empty').count()} el-table__empty-text=${JSON.stringify(await items('.el-table__empty-text'))}`);
  console.log(`[按钮] ${JSON.stringify(await bodyButtons(page))}`);
  console.log(`[tag] ${JSON.stringify((await page.locator('.el-tag').allInnerTexts()).map(oneLine).filter(Boolean).slice(0, 30))}`);
  console.log(`[alert] ${JSON.stringify(await items('.el-alert')).slice(0, 1200)}`);
  console.log(`[卡片头] ${JSON.stringify(await items('.el-card__header')).slice(0, 1200)}`);
  console.log(`[描述表] ${JSON.stringify((await page.locator('.el-descriptions__cell').allInnerTexts()).map(oneLine).filter(Boolean).slice(0, 40))}`);
  console.log(`[占位符] ${JSON.stringify((await page.locator('input').evaluateAll((els) => els.map((e) => e.placeholder).filter(Boolean))).slice(0, 30))}`);
  console.log(`[表单label] ${JSON.stringify(await items('.el-form-item__label')).slice(0, 1500)}`);
  console.log(`[分页/脚注] ${JSON.stringify(await items('.el-pagination, .text-gray-500')).slice(0, 1200)}`);

  for (const anchor of picks) {
    try {
      const { items: opts } = await openSelect(page, anchor);
      console.log(`[下拉「${anchor}」选项] ${JSON.stringify((await opts.allInnerTexts()).map(oneLine).slice(0, 40))}`);
      await page.keyboard.press('Escape');
      await page.waitForTimeout(300);
    } catch (err) {
      console.log(`[下拉「${anchor}」失败] ${err.message.slice(0, 200)}`);
    }
  }

  for (const name of clicks) {
    const re = new RegExp(name);
    const btn = page.getByRole('button', { name: re }).first();
    if ((await btn.count()) === 0) {
      console.log(`[点击「${name}」] 没有这个按钮`);
      continue;
    }
    await btn.click({ timeout: 8000 }).catch((e) => console.log(`[点击「${name}」] ${e.message.slice(0, 150)}`));
    await page.waitForTimeout(1200);
    const dlg = page.locator('.el-dialog:visible, .el-drawer:visible').last();
    if (await dlg.count()) {
      const title = oneLine(await dlg.locator('.el-dialog__title, .el-drawer__title').first().innerText().catch(() => ''));
      console.log(`\n---- 弹出层「${title}」 ----`);
      console.log(`[弹窗BODY] ${oneLine(await dlg.innerText()).slice(0, 2500)}`);
      console.log(`[弹窗表单label] ${JSON.stringify((await dlg.locator('.el-form-item__label').allInnerTexts()).map(oneLine))}`);
      console.log(`[弹窗按钮] ${JSON.stringify((await dlg.locator('button').allInnerTexts()).map(oneLine).filter(Boolean))}`);
      console.log(`[弹窗占位符] ${JSON.stringify((await dlg.locator('input').evaluateAll((els) => els.map((e) => e.placeholder).filter(Boolean))).slice(0, 30))}`);
    }
    const msg = oneLine(await page.locator('.el-message').first().innerText().catch(() => ''));
    if (msg) console.log(`[消息] ${msg}`);
    await page.keyboard.press('Escape');
    await page.waitForTimeout(500);
    await dlg.locator('.el-dialog__headerbtn, .el-drawer__close-btn').first().click().catch(() => {});
    await page.waitForTimeout(400);
  }
}

await context.close();
await browser.close();
