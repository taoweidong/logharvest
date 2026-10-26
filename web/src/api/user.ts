import { http } from "@/utils/http";
import type { Envelope, LoginData, RefreshData } from "./types";
import type { AsyncRouteRecord } from "./rbacTypes";

export type LoginPayload = { username: string; password: string };

/**
 * 后端只在认证接口返回 `{code, message, data}` 信封，业务接口是纯 REST 对象。
 * `baseURL` 为 `/api`（见 `src/utils/http/index.ts`），所以此处路径不带 `/api` 前缀。
 */

/** 登录 */
export const login = (data: LoginPayload) =>
  http.request<Envelope<LoginData>>("post", "/login", { data });

/** 刷新 `accessToken` */
export const refreshToken = (data: { refreshToken: string }) =>
  http.request<Envelope<RefreshData>>("post", "/refresh-token", { data });

/**
 * 当前登录用户可见的动态路由（服务端按角色过滤后的菜单树）。
 * 目录路由 `component` 为空且有 `children`，页面路由 `component` 为
 * `"logharvest/jobs/index"` 这类 `src/views` 下的相对路径。
 * `initRouter` 在登录完成与刷新页面时调用它。
 */
export const getAsyncRoutes = () =>
  http.request<Envelope<AsyncRouteRecord[]>>("get", "/auth/routes");

/** 登出（后端仅记录审计，前端本地清 token 才是生效的那一步） */
export const logout = () => http.request<Envelope<null>>("post", "/logout");
