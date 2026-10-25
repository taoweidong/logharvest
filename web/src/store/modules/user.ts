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
import { type DataInfo, getToken, setToken, removeToken, userKey } from "@/utils/auth";

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
    /** 前端登出（清理本地 token 与标签页；后端的 /logout 只用于审计，失败不影响登出）
     *
     * 返回 Promise：401 拦截器要等它落地才能复位 isLoggingOut，否则 /logout 自身
     * 返回的 401 会再次触发登出，形成无限 POST 与重复提示。
     */
    logOut() {
      // 没有 token 时不再打这个电话：后端会回 401，纯浪费一次往返。
      const notify = getToken() ? logoutApi().catch(() => undefined) : Promise.resolve();
      this.username = "";
      this.roles = [];
      this.permissions = [];
      removeToken();
      useMultiTagsStoreHook().handleTags("equal", [...routerArrays]);
      resetRouter();
      router.push("/login");
      return notify;
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
