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
    },
    {
      path: "/logharvest/rulesets",
      name: "RuleSets",
      component: () => import("@/views/logharvest/rulesets/index.vue"),
      meta: {
        title: "规则集"
      }
    },
    {
      path: "/logharvest/datasources",
      name: "DataSources",
      component: () => import("@/views/logharvest/datasources/index.vue"),
      meta: {
        title: "数据源"
      }
    },
    {
      path: "/logharvest/matches",
      name: "Matches",
      component: () => import("@/views/logharvest/matches/index.vue"),
      meta: {
        title: "命中结果"
      }
    },
    {
      path: "/logharvest/workers",
      name: "Workers",
      component: () => import("@/views/logharvest/workers/index.vue"),
      meta: {
        title: "Worker 管理"
      }
    },
    {
      // 概览组件同时是首页（见 router/modules/home.ts）；这里再暴露一个业务路径
      // 便于深链与「日志分析」菜单内部跳转，菜单里不重复展示。
      path: "/logharvest/overview",
      name: "Overview",
      component: () => import("@/views/logharvest/overview/index.vue"),
      meta: {
        title: "运行概览",
        showLink: false
      }
    }
  ]
} satisfies RouteConfigsTable;

export default LogHarvest;
