import { defineStore } from "pinia";
import {
  type userType,
  store,
  router,
  resetRouter,
  routerArrays,
  storageLocal
} from "../utils";
import {
  type LoginPayload,
  login,
  logout as logoutApi,
  refreshToken as refreshTokenApi
} from "@/api/user";
import type { Envelope, LoginData, RefreshData } from "@/api/types";
import { useMultiTagsStoreHook } from "./multiTags";
import { type DataInfo, setToken, removeToken, userKey } from "@/utils/auth";

/**
 * 业务/认证接口的错误都可能是 `{"detail": "..."}`（FastAPI），
 * 登录失败要提示的是 `detail`，不是 `message`。
 */
function extractDetail(error: any): string {
  const detail = error?.response?.data?.detail ?? error?.message;
  return typeof detail === "string" && detail ? detail : "登录失败";
}

export const useUserStore = defineStore("pure-user", {
  state: (): userType => ({
    // 头像
    avatar: storageLocal().getItem<DataInfo<number>>(userKey)?.avatar ?? "",
    // 用户名
    username: storageLocal().getItem<DataInfo<number>>(userKey)?.username ?? "",
    // 昵称
    nickname: storageLocal().getItem<DataInfo<number>>(userKey)?.nickname ?? "",
    // 页面级别权限
    roles: storageLocal().getItem<DataInfo<number>>(userKey)?.roles ?? [],
    // 按钮级别权限（基于菜单name）
    permissions:
      storageLocal().getItem<DataInfo<number>>(userKey)?.permissions ?? [],
    // 是否勾选了登录页的免登录
    isRemembered: false,
    // 登录页的免登录存储几天，默认7天
    loginDay: 7
  }),
  actions: {
    /** 存储头像 */
    SET_AVATAR(avatar: string) {
      this.avatar = avatar;
    },
    /** 存储用户名 */
    SET_USERNAME(username: string) {
      this.username = username;
    },
    /** 存储昵称 */
    SET_NICKNAME(nickname: string) {
      this.nickname = nickname;
    },
    /** 存储角色 */
    SET_ROLES(roles: Array<string>) {
      this.roles = roles;
    },
    /** 存储按钮级别权限（基于菜单name） */
    SET_PERMISSIONS(permissions: Array<string>) {
      this.permissions = permissions;
    },
    /** 存储是否勾选了登录页的免登录 */
    SET_ISREMEMBERED(bool: boolean) {
      this.isRemembered = bool;
    },
    /** 设置登录页的免登录存储几天 */
    SET_LOGINDAY(value: number) {
      this.loginDay = Number(value);
    },
    /** 登入 */
    async loginByUsername(data: LoginPayload) {
      return new Promise<Envelope<LoginData>>((resolve, reject) => {
        login(data)
          .then(res => {
            if (res?.code === 0 && res.data?.accessToken) {
              setToken(res.data);
              resolve(res);
            } else {
              reject(new Error(res?.message || "登录失败"));
            }
          })
          .catch(error => {
            reject(new Error(extractDetail(error)));
          });
      });
    },
    /** 前端登出（清理本地 token 与标签页；后端的 /logout 只用于审计，失败不影响登出） */
    logOut() {
      logoutApi().catch(() => undefined);
      this.username = "";
      this.roles = [];
      this.permissions = [];
      removeToken();
      useMultiTagsStoreHook().handleTags("equal", [...routerArrays]);
      resetRouter();
      router.push("/login");
    },
    /** 刷新`token` */
    async handRefreshToken(data: { refreshToken: string }) {
      return new Promise<Envelope<RefreshData>>((resolve, reject) => {
        refreshTokenApi(data)
          .then(res => {
            if (res?.code === 0 && res.data?.accessToken) {
              setToken(res.data);
              resolve(res);
            } else {
              reject(new Error(res?.message || "token 刷新失败"));
            }
          })
          .catch(error => {
            reject(new Error(extractDetail(error)));
          });
      });
    }
  }
});

export function useUserStoreHook() {
  return useUserStore(store);
}
