import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/utils/http", () => ({
  http: { request: vi.fn() }
}));

import { http } from "@/utils/http";
import { listOperationLogs } from "@/api/audit";
import { getJobStats, listRuleIndex } from "@/api/analytics";
import {
  assignRoleMenus,
  assignUserRoles,
  createMenu,
  createRole,
  createUser,
  deleteMenu,
  deleteRole,
  deleteUser,
  listRoleMenus,
  listRoles,
  listUsers,
  menuTree,
  resetUserPassword,
  updateMenu,
  updateRole,
  updateUser
} from "@/api/rbac";
import { getAsyncRoutes, login, logout, refreshToken } from "@/api/user";

const request = vi.mocked(http.request);

describe("认证客户端契约", () => {
  beforeEach(() => request.mockClear());

  it("登录/刷新/动态路由/登出路径与方法", () => {
    login({ username: "admin", password: "secret" });
    expect(request).toHaveBeenLastCalledWith("post", "/login", {
      data: { username: "admin", password: "secret" }
    });

    refreshToken({ refreshToken: "refresh" });
    expect(request).toHaveBeenLastCalledWith("post", "/refresh-token", {
      data: { refreshToken: "refresh" }
    });

    getAsyncRoutes();
    expect(request).toHaveBeenLastCalledWith("get", "/auth/routes");

    logout();
    expect(request).toHaveBeenLastCalledWith("post", "/logout");
  });
});

describe("RBAC 用户客户端契约", () => {
  beforeEach(() => request.mockClear());

  it("列表默认参数、CRUD、角色覆盖与密码重置", () => {
    listUsers({ keyword: "ad", is_active: true });
    expect(request).toHaveBeenLastCalledWith("get", "/users", {
      params: { keyword: "ad", is_active: true }
    });

    createUser({ username: "u", password: "Passw0rd!", nickname: "U", role_ids: [1] });
    expect(request).toHaveBeenLastCalledWith("post", "/users", {
      data: { username: "u", password: "Passw0rd!", nickname: "U", role_ids: [1] }
    });

    updateUser(3, { nickname: "N", is_active: false });
    expect(request).toHaveBeenLastCalledWith("put", "/users/3", {
      data: { nickname: "N", is_active: false }
    });

    assignUserRoles(3, [1, 2]);
    expect(request).toHaveBeenLastCalledWith("put", "/users/3/roles", {
      data: { role_ids: [1, 2] }
    });

    resetUserPassword(3, "New-Passw0rd!");
    expect(request).toHaveBeenLastCalledWith("post", "/users/3/reset-password", {
      data: { password: "New-Passw0rd!" }
    });

    deleteUser(3);
    expect(request).toHaveBeenLastCalledWith("delete", "/users/3");
  });
});

describe("RBAC 角色/菜单客户端契约", () => {
  beforeEach(() => request.mockClear());

  it("角色列表一次取满 500，CRUD 与菜单授权", () => {
    listRoles();
    expect(request).toHaveBeenLastCalledWith("get", "/roles", {
      params: { limit: 500 }
    });

    createRole({ name: "r", code: "r", description: "" });
    expect(request).toHaveBeenLastCalledWith("post", "/roles", {
      data: { name: "r", code: "r", description: "" }
    });

    updateRole(2, { name: "r2", description: "" });
    expect(request).toHaveBeenLastCalledWith("put", "/roles/2", {
      data: { name: "r2", description: "" }
    });

    listRoleMenus(2);
    expect(request).toHaveBeenLastCalledWith("get", "/roles/2/menus");

    assignRoleMenus(2, [1, 2]);
    expect(request).toHaveBeenLastCalledWith("put", "/roles/2/menus", {
      data: { menu_ids: [1, 2] }
    });

    deleteRole(2);
    expect(request).toHaveBeenLastCalledWith("delete", "/roles/2");
  });

  it("菜单树与 CRUD 路径", () => {
    menuTree();
    expect(request).toHaveBeenLastCalledWith("get", "/menus/tree");

    const payload = {
      parent_id: null,
      name: "jobs:read",
      title: "作业查询",
      menu_type: 2 as const,
      path: "",
      component: "",
      icon: "",
      rank: 0,
      show_link: true,
      keep_alive: false,
      is_active: true
    };
    createMenu(payload);
    expect(request).toHaveBeenLastCalledWith("post", "/menus", { data: payload });

    updateMenu(4, payload);
    expect(request).toHaveBeenLastCalledWith("put", "/menus/4", { data: payload });

    deleteMenu(4);
    expect(request).toHaveBeenLastCalledWith("delete", "/menus/4");
  });
});

describe("审计/统计/规则索引客户端契约", () => {
  beforeEach(() => request.mockClear());

  it("操作日志接受查询参数并走 GET", () => {
    listOperationLogs({ limit: 50, offset: 100, action: "auth.login" });
    expect(request).toHaveBeenLastCalledWith("get", "/operation-logs", {
      params: { limit: 50, offset: 100, action: "auth.login" }
    });
  });

  it("统计与规则索引路径", () => {
    getJobStats({ days: 14, data_source_id: 2 });
    expect(request).toHaveBeenLastCalledWith("get", "/stats/jobs", {
      params: { days: 14, data_source_id: 2 }
    });

    listRuleIndex({ limit: 20, offset: 0 });
    expect(request).toHaveBeenLastCalledWith("get", "/rules", {
      params: { limit: 20, offset: 0 }
    });
  });
});
