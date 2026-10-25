/**
 * 已本地注册的图标名清单（前缀/名称 形式，如 `ri/search-line`）。
 *
 * 为什么要单独拆一个只含字符串的轻量模块：
 * - `useRenderIcon`（ReIcon/src/hooks.ts）需要在渲染前判断某个图标**能否本地渲染**，
 *   能就用离线组件，不能才回退在线组件；
 * - 但它不能 import `offlineIcon.ts`——那个模块带 46 个 SVG 的原始导入，
 *   会把这批资源拖进每个用到 useRenderIcon 的路由 chunk（包括登录页），
 *   破坏 pure-admin 原本"在 layout 里延迟加载本地图标"的意图。
 *
 * 本模块只有字符串，体积极小；`offlineIcon.ts` 以它为唯一清单来源执行 addIcon，
 * 并在开发模式下校验两侧是否一致（见该文件）。
 */
export const OFFLINE_ICON_NAMES: readonly string[] = [
  // Element Plus Icons
  "ep/menu",
  "ep/edit",
  "ep/guide",
  "ep/set-up",
  "ep/monitor",
  "ep/lollipop",
  "ep/histogram",
  "ep/home-filled",
  // Remix Icons —— 菜单 / 工具栏在用的图标
  "ri/mind-map",
  "ri/admin-fill",
  "ri/table-line",
  "ri/links-fill",
  "ri/admin-line",
  "ri/list-check",
  "ri/search-line",
  "ri/window-line",
  "ri/ubuntu-fill",
  "ri/history-fill",
  "ri/edit-box-line",
  "ri/code-box-line",
  "ri/artboard-line",
  "ri/markdown-line",
  "ri/file-info-line",
  "ri/bank-card-line",
  "ri/file-ppt-2-line",
  "ri/git-branch-line",
  "ri/settings-3-line",
  "ri/user-voice-line",
  "ri/bookmark-2-line",
  "ri/file-search-line",
  "ri/chat-search-line",
  "ri/information-line",
  "ri/terminal-window-line",
  "ri/checkbox-circle-line",
  "ri/bar-chart-horizontal-line",
  // Remix Icons —— sys_menumeta 中在用的菜单图标（此前缺失，导致退化为在线请求）
  "ri/book-read-line",
  "ri/shield-keyhole-line",
  "ri/settings-4-line",
  "ri/notification-line",
  "ri/id-card-line",
  "ri/server-line",
  "ri/database-2-line",
  "ri/file-list-line",
  "ri/window-2-line",
  "ri/radio-button-line",
  "ri/stack-line"
];
