import { http } from "@/utils/http";
import type {
  MenuPayload,
  MenuRecord,
  RoleCreatePayload,
  RoleRecord,
  RoleUpdatePayload,
  UserCreatePayload,
  UserListParams,
  UserRecord,
  UserUpdatePayload
} from "./rbacTypes";

/**
 * RBAC 管理接口。
 *
 * 这些接口是纯 REST：列表返回裸数组，错误体为 `{detail}`，
 * 所以这里**不**拆 `{code,message,data}` 信封（`baseURL` 为 `/api`，
 * 见 `src/utils/http/index.ts`）。`DELETE` 成功时后端返回 `204`，
 * axios 封装最终拿到空字符串——按 `Promise<void>` 处理即可，不是错误。
 */

// ---------------------------------------------------------------- 用户

/** 用户列表（裸数组，无 total） */
export const listUsers = (params: UserListParams = {}) =>
  http.request<UserRecord[]>("get", "/users", { params });

/** 新建用户 */
export const createUser = (data: UserCreatePayload) =>
  http.request<UserRecord>("post", "/users", { data });

/** 更新用户基础信息（昵称 / 启用状态） */
export const updateUser = (id: number, data: UserUpdatePayload) =>
  http.request<UserRecord>("put", `/users/${id}`, { data });

/** 删除用户（204） */
export const deleteUser = (id: number) =>
  http.request<void>("delete", `/users/${id}`);

/** 覆盖用户的角色集合 */
export const assignUserRoles = (id: number, role_ids: number[]) =>
  http.request<UserRecord>("put", `/users/${id}/roles`, {
    data: { role_ids }
  });

/** 重置用户密码（204） */
export const resetUserPassword = (id: number, password: string) =>
  http.request<void>("post", `/users/${id}/reset-password`, {
    data: { password }
  });

// ---------------------------------------------------------------- 角色

/** 角色列表（裸数组；角色通常很少，一次取足避免默认 50 条截断） */
export const listRoles = () =>
  http.request<RoleRecord[]>("get", "/roles", { params: { limit: 500 } });

/** 新建角色 */
export const createRole = (data: RoleCreatePayload) =>
  http.request<RoleRecord>("post", "/roles", { data });

/** 更新角色 */
export const updateRole = (id: number, data: RoleUpdatePayload) =>
  http.request<RoleRecord>("put", `/roles/${id}`, { data });

/** 删除角色（204） */
export const deleteRole = (id: number) =>
  http.request<void>("delete", `/roles/${id}`);

/** 角色已授权的菜单 id 列表（裸数组） */
export const listRoleMenus = (id: number) =>
  http.request<number[]>("get", `/roles/${id}/menus`);

/** 覆盖角色的菜单授权（204） */
export const assignRoleMenus = (id: number, menu_ids: number[]) =>
  http.request<void>("put", `/roles/${id}/menus`, { data: { menu_ids } });

// ---------------------------------------------------------------- 菜单

/** 菜单列表（裸数组；平铺，前端自行组树） */
export const listMenus = () => http.request<MenuRecord[]>("get", "/menus");

/** 菜单树（裸数组，节点自带 children） */
export const menuTree = () => http.request<MenuRecord[]>("get", "/menus/tree");

/** 新建菜单 */
export const createMenu = (data: MenuPayload) =>
  http.request<MenuRecord>("post", "/menus", { data });

/** 更新菜单 */
export const updateMenu = (id: number, data: MenuPayload) =>
  http.request<MenuRecord>("put", `/menus/${id}`, { data });

/** 删除菜单（204） */
export const deleteMenu = (id: number) =>
  http.request<void>("delete", `/menus/${id}`);
