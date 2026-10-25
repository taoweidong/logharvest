import { logharvest } from "@/router/enums";

const Layout = () => import("@/layout/index.vue");

/**
 * LogHarvest 业务菜单：全部在前端静态定义（后端没有服务端菜单接口，
 * `GET /api/auth/routes` 固定返回空数组）。
 * 说明：`rank` 只对顶级路由生效（见 types/router.d.ts 的 RouteConfigsTable），
 * 子菜单顺序即数组顺序。
 */
const LogHarvest = {
  path: "/logharvest",
  name: "LogHarvest",
  component: Layout,
  redirect: "/logharvest/jobs",
  meta: {
    icon: "ep/search",
    title: "日志分析",
    rank: logharvest
  },
  children: [
    {
      path: "/logharvest/jobs",
      name: "Jobs",
      component: () => import("@/views/logharvest/jobs/index.vue"),
      meta: {
        title: "分析作业"
      }
    },
    {
      path: "/logharvest/jobs/:id",
      name: "JobDetail",
      component: () => import("@/views/logharvest/jobs/detail.vue"),
      meta: {
        title: "作业详情",
        showLink: false
      }
    }
  ]
} satisfies RouteConfigsTable;

export default LogHarvest;
