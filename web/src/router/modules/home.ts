import { $t } from "@/plugins/i18n";
import { home } from "@/router/enums";
const { VITE_HIDE_HOME } = import.meta.env;
const Layout = () => import("@/layout/index.vue");

/**
 * 骨架阶段的首页。
 * 说明：基座的 `views/welcome` 演示看板已随 Task 1 的瘦身删除，此处先指向一个
 * 只展示前端骨架自身信息的落地页；M2b Task 7 会把 `component` 换成
 * `@/views/logharvest/overview/index.vue`（对接 `GET /api/system/status`）。
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
      component: () => import("@/views/home/index.vue"),
      meta: {
        title: $t("menus.pureHome"),
        showLink: VITE_HIDE_HOME === "true" ? false : true
      }
    }
  ]
} satisfies RouteConfigsTable;
