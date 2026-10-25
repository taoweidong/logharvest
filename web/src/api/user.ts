import { http } from "@/utils/http";
import type { Envelope, LoginData, RefreshData } from "./types";

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
 * 服务端菜单接口：M2a 后端固定返回空数组（前端静态定义菜单），
 * 保留导出仅为契约可见，`initRouter` 不再调用它。
 */
export const getAsyncRoutes = () =>
  http.request<Envelope<unknown[]>>("get", "/auth/routes");

/** 登出（后端仅记录审计，前端本地清 token 才是生效的那一步） */
export const logout = () => http.request<Envelope<null>>("post", "/logout");
