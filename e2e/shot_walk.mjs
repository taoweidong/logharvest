// 逐页截图走查：登录 admin → 逐路由截图（桌面 1440 与窄屏 390）。
//   node e2e/shot_walk.mjs            # 全部已注册路由
//   node e2e/shot_walk.mjs /system/users /logharvest/overview
import fs from 'node:fs';
import path from 'node:path';
import { chromium, WEB, ACCOUNTS, newPage, go, UI_DIR } from './ui_lib.mjs';

const ROUTES = [
  ['/logharvest/overview', 'overview'],
  ['/logharvest/datasources', 'datasources'],
  ['/logharvest/jobs', 'jobs'],
  ['/logharvest/jobs/1', 'job-detail'],
  ['/logharvest/matches', 'matches'],
  ['/logharvest/stats', 'stats'],
  ['/logharvest/workers', 'workers'],
  ['/logharvest/rulesets', 'rulesets'],
  ['/rules/maintenance', 'rule-maintenance'],
  ['/system/users', 'users'],
  ['/system/roles', 'roles'],
  ['/system/menus', 'menus'],
  ['/system/audit-logs', 'audit-logs'],
];

const args = process.argv.slice(2);
const mobile = args.includes('--mobile');
let targets = ROUTES;
const routeArgs = args.filter((a) => !a.startsWith('--'));
if (routeArgs.length) {
  targets = ROUTES.filter(([route]) => routeArgs.some((a) => route.startsWith(a)));
}

fs.mkdirSync(UI_DIR, { recursive: true });
const browser = await chromium.launch({ channel: 'chrome', headless: true });
const { context, page } = await newPage(browser);

const login = async (viewport) => {
  await page.setViewportSize(viewport);
  await page.goto(`${WEB}#/login`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1500);
  // 同一 context 已登录时 #/login 会被路由弹走，找不到输入框，直接视为已登录
  if (!/login/.test(page.url())) return;
  const account = ACCOUNTS.admin;
  await page.getByPlaceholder('账号').fill(account.username);
  await page.getByPlaceholder('密码').fill(account.password);
  await page.getByRole('button', { name: '登录' }).click();
  await page.waitForURL(/#\/(home|access-denied)/, { timeout: 30000 }).catch(() => null);
  await page.waitForTimeout(1000);
};

const walk = async (label, viewport) => {
  await login(viewport);
  for (const [route, name] of targets) {
    await go(page, route, { reload: true });
    await page.waitForTimeout(1600);
    const file = path.join(UI_DIR, `walk_${name}_${label}.png`);
    await page.screenshot({ path: file, fullPage: false }).catch(() => {});
    console.log(`${file}`);
  }
};

if (!mobile) await walk('desktop', { width: 1440, height: 900 });
await walk('mobile', { width: 390, height: 844 });
await browser.close();
console.log('done');
