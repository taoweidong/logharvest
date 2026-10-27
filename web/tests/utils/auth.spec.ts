import { beforeEach, describe, expect, it, vi } from "vitest";

// auth.ts 引入 user store（连带路由/Element Plus 全家桶），单测里用假 hook 隔离。
const storeState: { permissions: string[] } = { permissions: [] };
const noop = () => undefined;
vi.mock("@/store/modules/user", () => ({
  useUserStoreHook: () => ({
    ...storeState,
    SET_AVATAR: noop,
    SET_USERNAME: noop,
    SET_NICKNAME: noop,
    SET_ROLES: noop,
    SET_PERMISSIONS: noop
  })
}));

import {
  formatToken,
  getToken,
  hasPerms,
  removeToken,
  setToken
} from "@/utils/auth";

describe("hasPerms", () => {
  it("空权限与空值一律拒绝", () => {
    storeState.permissions = [];
    expect(hasPerms("jobs:read")).toBe(false);
    expect(hasPerms(["jobs:read"])).toBe(false);
    expect(hasPerms("")).toBe(false);
    // hasPerms([]) 是"不要求任何权限"的空真值（调用方不会这么传），不断言。
  });

  it("通配码不必是唯一条目（与后端 require_permission 短路语义一致）", () => {
    storeState.permissions = ["*:*:*"];
    expect(hasPerms("anything:at:all")).toBe(true);
    storeState.permissions = ["*:*:*", "jobs:read"];
    expect(hasPerms("anything:at:all")).toBe(true);
  });

  it("字符串权限按精确包含判断", () => {
    storeState.permissions = ["jobs:read", "roles:write"];
    expect(hasPerms("jobs:read")).toBe(true);
    expect(hasPerms("jobs:write")).toBe(false);
  });

  it("数组权限要求全部包含（AND 语义）", () => {
    storeState.permissions = ["jobs:read", "jobs:write"];
    expect(hasPerms(["jobs:read", "jobs:write"])).toBe(true);
    expect(hasPerms(["jobs:read", "roles:write"])).toBe(false);
  });
});

describe("getToken 对损坏 cookie 的自愈", () => {
  beforeEach(() => {
    removeToken();
  });

  it("cookie 缺失时退回 localStorage（未登录返回 undefined）", () => {
    expect(getToken()).toBeUndefined();
  });

  it("JSON 损坏的 cookie 不抛异常：清掉坏 cookie 并退回未登录态", () => {
    document.cookie = "authorized-token={broken-json; path=/";
    expect(() => getToken()).not.toThrow();
    expect(getToken()).toBeUndefined();
    // 坏 cookie 已被清除，下一次读取不再走 parse 失败路径。
    expect(document.cookie).not.toContain("authorized-token={broken-json");
  });

  it("合法 cookie 原样解析", () => {
    setToken({
      accessToken: "access",
      refreshToken: "refresh",
      expires: Date.now() + 60_000,
      username: "admin",
      roles: ["admin"],
      permissions: ["*:*:*"]
    });
    const data = getToken();
    expect(data?.accessToken).toBe("access");
    expect(data?.refreshToken).toBe("refresh");
  });
});

describe("formatToken", () => {
  it("拼接 Bearer 前缀", () => {
    expect(formatToken("abc")).toBe("Bearer abc");
  });
});
