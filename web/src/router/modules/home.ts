import { $t } from "@/plugins/i18n";
import { home } from "@/router/enums";
const { VITE_HIDE_HOME } = import.meta.env;
const Layout = () => import("@/layout/index.vue");

/**
 * 首页即运行概览（M2b Task 7）。
 * 说明：基座的 `views/welcome` 演示看板已随 Task 1 的瘦身删除，这里直接复用业务概览页
 * `@/views/logharvest/overview/index.vue`（主数据源 `GET /api/system/overview`，
 * 不可用时退回 `GET /api/system/status`）。路由 `name` 保持 `Welcome`，
 * 与 `src/layout/types.ts` 里固定的首页标签对应。
 */
export default {
  path: "/",
  name: "Home",
  component: Layout,
  redirect: "/home",
  meta: {
    icon: "ep/home-filled",
    title: $t("menus.pureHome"),
    rank: home
  },
  children: [
    {
      path: "/home",
      name: "Welcome",
      component: () => import("@/views/logharvest/overview/index.vue"),
      meta: {
        title: "概览",
        showLink: VITE_HIDE_HOME === "true" ? false : true
      }
    }
  ]
} satisfies RouteConfigsTable;
