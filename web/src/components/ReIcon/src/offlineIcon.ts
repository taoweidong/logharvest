// 这里存放本地图标，在 src/layout/index.vue 文件中加载，避免在首启动加载
import { getSvgInfo } from "@pureadmin/utils";
import { addIcon } from "@iconify/vue/dist/offline";
import { OFFLINE_ICON_NAMES } from "./offlineIconNames";

// https://icon-sets.iconify.design/ep/?keyword=ep
import EpMenu from "~icons/ep/menu?raw";
import EpEdit from "~icons/ep/edit?raw";
import EpGuide from "~icons/ep/guide?raw";
import EpSetUp from "~icons/ep/set-up?raw";
import EpMonitor from "~icons/ep/monitor?raw";
import EpLollipop from "~icons/ep/lollipop?raw";
import EpHistogram from "~icons/ep/histogram?raw";
import EpHomeFilled from "~icons/ep/home-filled?raw";

// https://icon-sets.iconify.design/ri/?keyword=ri
import RiMindMap from "~icons/ri/mind-map?raw";
import RiAdminFill from "~icons/ri/admin-fill?raw";
import RiTableLine from "~icons/ri/table-line?raw";
import RiLinksFill from "~icons/ri/links-fill?raw";
import RiAdminLine from "~icons/ri/admin-line?raw";
import RiListCheck from "~icons/ri/list-check?raw";
import RiSearchLine from "~icons/ri/search-line?raw";
import RiWindowLine from "~icons/ri/window-line?raw";
import RiUbuntuFill from "~icons/ri/ubuntu-fill?raw";
import RiHistoryFill from "~icons/ri/history-fill?raw";
import RiEditBoxLine from "~icons/ri/edit-box-line?raw";
import RiCodeBoxLine from "~icons/ri/code-box-line?raw";
import RiArtboardLine from "~icons/ri/artboard-line?raw";
import RiMarkdownLine from "~icons/ri/markdown-line?raw";
import RiFileInfoLine from "~icons/ri/file-info-line?raw";
import RiBankCardLine from "~icons/ri/bank-card-line?raw";
import RiFilePpt2Line from "~icons/ri/file-ppt-2-line?raw";
import RiGitBranchLine from "~icons/ri/git-branch-line?raw";
import RiSettings3Line from "~icons/ri/settings-3-line?raw";
import RiUserVoiceLine from "~icons/ri/user-voice-line?raw";
import RiBookmark2Line from "~icons/ri/bookmark-2-line?raw";
import RiFileSearchLine from "~icons/ri/file-search-line?raw";
import RiChatSearchLine from "~icons/ri/chat-search-line?raw";
import RiInformationLine from "~icons/ri/information-line?raw";
import RiTerminalWindowLine from "~icons/ri/terminal-window-line?raw";
import RiCheckboxCircleLine from "~icons/ri/checkbox-circle-line?raw";
import RiBarChartHorizontalLine from "~icons/ri/bar-chart-horizontal-line?raw";

/*
 * 以下 11 个图标是 sys_menumeta 中实际在用的菜单图标，此前未在此注册，
 * 导致 useRenderIcon 只能退化为 IconifyIconOnline，运行时去
 * api.iconify.design 拉数据（弱网/内网下首屏被拖到 20s+ 甚至挂起）。
 * 补齐后菜单图标可全部走本地渲染，首屏不再依赖外网。
 */
import RiBookReadLine from "~icons/ri/book-read-line?raw";
import RiShieldKeyholeLine from "~icons/ri/shield-keyhole-line?raw";
import RiSettings4Line from "~icons/ri/settings-4-line?raw";
import RiNotificationLine from "~icons/ri/notification-line?raw";
import RiIdCardLine from "~icons/ri/id-card-line?raw";
import RiServerLine from "~icons/ri/server-line?raw";
import RiDatabase2Line from "~icons/ri/database-2-line?raw";
import RiFileListLine from "~icons/ri/file-list-line?raw";
import RiWindow2Line from "~icons/ri/window-2-line?raw";
import RiRadioButtonLine from "~icons/ri/radio-button-line?raw";
import RiStackLine from "~icons/ri/stack-line?raw";

const icons = [
  // Element Plus Icon: https://github.com/element-plus/element-plus-icons
  ["ep/menu", EpMenu],
  ["ep/edit", EpEdit],
  ["ep/guide", EpGuide],
  ["ep/set-up", EpSetUp],
  ["ep/monitor", EpMonitor],
  ["ep/lollipop", EpLollipop],
  ["ep/histogram", EpHistogram],
  ["ep/home-filled", EpHomeFilled],
  // Remix Icon: https://github.com/Remix-Design/RemixIcon
  ["ri/mind-map", RiMindMap],
  ["ri/admin-fill", RiAdminFill],
  ["ri/table-line", RiTableLine],
  ["ri/links-fill", RiLinksFill],
  ["ri/admin-line", RiAdminLine],
  ["ri/list-check", RiListCheck],
  ["ri/search-line", RiSearchLine],
  ["ri/window-line", RiWindowLine],
  ["ri/ubuntu-fill", RiUbuntuFill],
  ["ri/history-fill", RiHistoryFill],
  ["ri/edit-box-line", RiEditBoxLine],
  ["ri/code-box-line", RiCodeBoxLine],
  ["ri/artboard-line", RiArtboardLine],
  ["ri/markdown-line", RiMarkdownLine],
  ["ri/file-info-line", RiFileInfoLine],
  ["ri/bank-card-line", RiBankCardLine],
  ["ri/file-ppt-2-line", RiFilePpt2Line],
  ["ri/git-branch-line", RiGitBranchLine],
  ["ri/settings-3-line", RiSettings3Line],
  ["ri/user-voice-line", RiUserVoiceLine],
  ["ri/bookmark-2-line", RiBookmark2Line],
  ["ri/file-search-line", RiFileSearchLine],
  ["ri/chat-search-line", RiChatSearchLine],
  ["ri/information-line", RiInformationLine],
  ["ri/terminal-window-line", RiTerminalWindowLine],
  ["ri/checkbox-circle-line", RiCheckboxCircleLine],
  ["ri/bar-chart-horizontal-line", RiBarChartHorizontalLine],
  ["ri/book-read-line", RiBookReadLine],
  ["ri/shield-keyhole-line", RiShieldKeyholeLine],
  ["ri/settings-4-line", RiSettings4Line],
  ["ri/notification-line", RiNotificationLine],
  ["ri/id-card-line", RiIdCardLine],
  ["ri/server-line", RiServerLine],
  ["ri/database-2-line", RiDatabase2Line],
  ["ri/file-list-line", RiFileListLine],
  ["ri/window-2-line", RiWindow2Line],
  ["ri/radio-button-line", RiRadioButtonLine],
  ["ri/stack-line", RiStackLine]
];

/*
 * 本地菜单图标：后端在路由的 icon 中返回对应的图标字符串，
 * 前端在此处使用 addIcon 添加即可渲染菜单图标。
 *
 * 清单以 offlineIconNames.ts 为唯一来源；这里只负责把名字映射到 SVG 原始内容。
 * 开发模式下校验两侧一致，避免"清单里有、但忘了导入 SVG"导致图标空白。
 */
const rawIcons = new Map(icons.map(([name, icon]) => [name as string, icon]));

if (import.meta.env.DEV) {
  const missing = OFFLINE_ICON_NAMES.filter(name => !rawIcons.has(name));
  const extra = [...rawIcons.keys()].filter(
    name => !OFFLINE_ICON_NAMES.includes(name)
  );
  if (missing.length || extra.length) {
    console.warn(
      `[ReIcon] offlineIconNames 与 offlineIcon 的 SVG 导入不一致，` +
        `缺失=${missing.join(",") || "无"}，多余=${extra.join(",") || "无"}`
    );
  }
}

OFFLINE_ICON_NAMES.forEach(name => {
  const raw = rawIcons.get(name);
  if (raw) addIcon(name, getSvgInfo(raw as string));
});
