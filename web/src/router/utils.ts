import {
  type Router,
  type RouterHistory,
  type RouteRecordRaw,
  type RouteComponent,
  createWebHistory,
  createWebHashHistory
} from "vue-router";
import { router } from "./index";
import { isProxy, toRaw } from "vue";
import { getAsyncRoutes } from "@/api/user";
import type { AsyncRouteRecord } from "@/api/rbacTypes";
import { useTimeoutFn } from "@vueuse/core";
import {
  isString,
  cloneDeep,
  isAllEmpty,
  intersection,
  storageLocal,
  isIncludeAllChildren
} from "@pureadmin/utils";
import { buildHierarchyTree } from "@/utils/tree";
import { userKey, type DataInfo } from "@/utils/auth";
import { type menuType, routerArrays } from "@/layout/types";
import { useMultiTagsStoreHook } from "@/store/modules/multiTags";
import { usePermissionStoreHook } from "@/store/modules/permission";
const Layout = () => import("@/layout/index.vue");
const IFrame = () => import("@/layout/frame.vue");
/** 目录路由（component 为空、带 children）的容器，避免嵌套完整 Layout */
const EmptyLayout = () => import("@/layout/empty-layout.vue");
// https://cn.vitejs.dev/guide/features.html#glob-import
const modulesRoutes = import.meta.glob("/src/views/**/*.{vue,tsx}");

const PAGE_NOT_FOUND_ROUTE_NAME = "PageNotFound" as const;

function handRank(routeInfo: any) {
  const { name, path, parentId, meta } = routeInfo;
  return isAllEmpty(parentId)
    ? isAllEmpty(meta?.rank) ||
      (meta?.rank === 0 && name !== "Home" && path !== "/")
      ? true
      : false
    : false;
}

/** 按照路由中meta下的rank等级升序来排序路由 */
function ascending(arr: any[]) {
  arr.forEach((v, index) => {
    // 当rank不存在时，根据顺序自动创建，首页路由永远在第一位
    if (handRank(v)) v.meta.rank = index + 2;
  });
  return arr.sort(
    (a: { meta: { rank: number } }, b: { meta: { rank: number } }) => {
      return a?.meta.rank - b?.meta.rank;
    }
  );
}

/** 过滤meta中showLink为false的菜单 */
function filterTree(data: RouteComponent[]) {
  const newTree = cloneDeep(data).filter(
    (v: { meta: { showLink: boolean } }) => v.meta?.showLink !== false
  );
  newTree.forEach(
    (v: { children }) => v.children && (v.children = filterTree(v.children))
  );
  return newTree;
}

/** 过滤children长度为0的的目录，当目录下没有菜单时，会过滤此目录，目录没有赋予roles权限，当目录下只要有一个菜单有显示权限，那么此目录就会显示 */
function filterChildrenTree(data: RouteComponent[]) {
  const newTree = cloneDeep(data).filter((v: any) => v?.children?.length !== 0);
  newTree.forEach(
    (v: { children }) => v.children && (v.children = filterTree(v.children))
  );
  return newTree;
}

/** 判断两个数组彼此是否存在相同值 */
function isOneOfArray(a: Array<string>, b: Array<string>) {
  return Array.isArray(a) && Array.isArray(b)
    ? intersection(a, b).length > 0
      ? true
      : false
    : true;
}

/** 从localStorage里取出当前登录用户的角色与权限，过滤无权限的菜单 */
function filterNoPermissionTree(data: RouteComponent[]) {
  const info = storageLocal().getItem<DataInfo<number>>(userKey);
  const currentRoles = info?.roles ?? [];
  const currentPermissions = info?.permissions ?? [];
  const newTree = cloneDeep(data).filter((v: any) => {
    if (!isOneOfArray(v.meta?.roles, currentRoles)) return false;
    // 静态路由可以用 meta.auths 声明自身需要的权限（例如首页概览需要
    // system:read）；动态路由的 meta.auths 是“按钮权限全集”，任一命中即放行。
    const required: string[] | undefined = v.meta?.auths;
    if (Array.isArray(required) && required.length) {
      if (!currentPermissions.includes("*:*:*")) {
        if (!required.some(code => currentPermissions.includes(code))) {
          return false;
        }
      }
    }
    return true;
  });
  newTree.forEach(
    (v: any) => v.children && (v.children = filterNoPermissionTree(v.children))
  );
  return filterChildrenTree(newTree);
}

/** 通过指定 `key` 获取父级路径集合，默认 `key` 为 `path` */
function getParentPaths(value: string, routes: RouteRecordRaw[], key = "path") {
  // 深度遍历查找
  function dfs(routes: RouteRecordRaw[], value: string, parents: string[]) {
    for (let i = 0; i < routes.length; i++) {
      const item = routes[i];
      // 返回父级path
      if (item[key] === value) return parents;
      // children不存在或为空则不递归
      if (!item.children || !item.children.length) continue;
      // 往下查找时将当前path入栈
      parents.push(item.path);

      if (dfs(item.children, value, parents).length) return parents;
      // 深度遍历查找未找到时当前path 出栈
      parents.pop();
    }
    // 未找到时返回空数组
    return [];
  }

  return dfs(routes, value, []);
}

/** 查找对应 `path` 的路由信息 */
function findRouteByPath(path: string, routes: RouteRecordRaw[]) {
  let res = routes.find((item: { path: string }) => item.path == path);
  if (res) {
    return isProxy(res) ? toRaw(res) : res;
  } else {
    for (let i = 0; i < routes.length; i++) {
      if (
        routes[i].children instanceof Array &&
        routes[i].children.length > 0
      ) {
        res = findRouteByPath(path, routes[i].children);
        if (res) {
          return isProxy(res) ? toRaw(res) : res;
        }
      }
    }
    return null;
  }
}

/** 动态路由注册完成后，再添加全屏404（页面不存在）页面，避免刷新动态路由页面时误跳转到404页面 */
function addPathMatch() {
  // 按路由 name 判重：vue-router 的 addRoute 对同名路由是替换，
  // 但对同名之外的通配 path 会叠加匹配器。原先校验的名字从未注册过，
  // 导致每次进动态路由都追加一条 404 通配路由。
  if (router.hasRoute(PAGE_NOT_FOUND_ROUTE_NAME)) return;
  router.addRoute({
    path: "/:pathMatch(.*)*",
    name: PAGE_NOT_FOUND_ROUTE_NAME,
    component: () => import("@/views/error/404.vue"),
    meta: {
      title: "menus.purePageNotFound",
      showLink: false
    }
  });
}

/** 处理动态路由（后端返回的路由） */
function handleAsyncRoutes(routeList: AsyncRouteRecord[]) {
  if (routeList.length === 0) {
    usePermissionStoreHook().handleWholeMenus(routeList);
  } else {
    formatFlatteningRoutes(addAsyncRoutes(routeList) ?? []).map(
      (v: RouteRecordRaw) => {
        // 防止重复添加路由
        if (
          router.options.routes[0].children.findIndex(
            value => value.path === v.path
          ) !== -1
        ) {
          return;
        } else {
          // 切记将路由push到routes后还需要使用addRoute，这样路由才能正常跳转
          router.options.routes[0].children.push(v);
          // 最终路由进行升序
          ascending(router.options.routes[0].children);
          if (!router.hasRoute(v?.name)) router.addRoute(v);
          const flattenRouters: any = router
            .getRoutes()
            .find(n => n.path === "/");
          // 保持router.options.routes[0].children与path为"/"的children一致，防止数据不一致导致异常
          flattenRouters.children = router.options.routes[0].children;
          router.addRoute(flattenRouters);
        }
      }
    );
    usePermissionStoreHook().handleWholeMenus(routeList);
  }
  if (!useMultiTagsStoreHook().getMultiTagsCache) {
    // 首页概览需要 system:read；无此权限时 wholeMenus 里不会有 /home，
    // 默认标签也不能留一个点进去只会 403 的「概览」。
    const homeVisible = usePermissionStoreHook().wholeMenus.some(
      menu =>
        menu.path === "/home" ||
        menu.children?.some(child => child.path === "/home")
    );
    const defaultTags = routerArrays.filter(
      tag => tag.path !== "/home" || homeVisible
    );
    useMultiTagsStoreHook().handleTags("equal", [
      ...defaultTags,
      ...usePermissionStoreHook().flatteningRoutes.filter(
        v => v?.meta?.fixedTag
      )
    ]);
  }
  addPathMatch();
}

/**
 * 初始化路由：登录完成或刷新页面时拉取 `GET /api/auth/routes`，
 * 把后端按角色过滤后的菜单树注册进 router。
 *
 * 容错：请求失败或信封 `code !== 0` 时回退为空数组（只保留静态首页菜单），
 * 并照常 resolve(router)——守卫在 `initRouter().then()` 里继续走，不会卡死。
 */
function initRouter(): Promise<Router> {
  return new Promise(resolve => {
    getAsyncRoutes()
      .then(res => {
        if (res?.code === 0 && Array.isArray(res.data)) {
          handleAsyncRoutes(cloneDeep(res.data));
        } else {
          console.error(
            "[router] GET /api/auth/routes 返回异常，已回退为空菜单：",
            res?.code,
            res?.message
          );
          handleAsyncRoutes([]);
        }
        resolve(router);
      })
      .catch(error => {
        console.error(
          "[router] GET /api/auth/routes 请求失败，已回退为空菜单：",
          error
        );
        handleAsyncRoutes([]);
        resolve(router);
      });
  });
}

/**
 * 将多级嵌套路由处理成一维数组
 * @param routesList 传入路由
 * @returns 返回处理后的一维路由
 */
function formatFlatteningRoutes(routesList: RouteRecordRaw[]) {
  if (routesList.length === 0) return routesList;
  let hierarchyList = buildHierarchyTree(routesList);
  for (let i = 0; i < hierarchyList.length; i++) {
    if (hierarchyList[i].children) {
      hierarchyList = hierarchyList
        .slice(0, i + 1)
        .concat(hierarchyList[i].children, hierarchyList.slice(i + 1));
    }
  }
  return hierarchyList;
}

/**
 * 一维数组处理成多级嵌套数组（三级及以上的路由全部拍成二级，keep-alive 只支持到二级缓存）
 * https://github.com/pure-admin/vue-pure-admin/issues/67
 * @param routesList 处理后的一维路由菜单数组
 * @returns 返回将一维数组重新处理成规定路由的格式
 */
function formatTwoStageRoutes(routesList: RouteRecordRaw[]) {
  if (routesList.length === 0) return routesList;
  const newRoutesList: RouteRecordRaw[] = [];
  routesList.forEach((v: RouteRecordRaw) => {
    if (v.path === "/") {
      newRoutesList.push({
        component: v.component,
        name: v.name,
        path: v.path,
        redirect: v.redirect,
        meta: v.meta,
        children: []
      });
    } else {
      newRoutesList[0]?.children.push({ ...v });
    }
  });
  return newRoutesList;
}

/** 处理缓存路由（添加、删除、刷新） */
function handleAliveRoute({ name }: ToRouteType, mode?: string) {
  switch (mode) {
    case "add":
      usePermissionStoreHook().cacheOperate({
        mode: "add",
        name
      });
      break;
    case "delete":
      usePermissionStoreHook().cacheOperate({
        mode: "delete",
        name
      });
      break;
    case "refresh":
      usePermissionStoreHook().cacheOperate({
        mode: "refresh",
        name
      });
      break;
    default:
      usePermissionStoreHook().cacheOperate({
        mode: "delete",
        name
      });
      useTimeoutFn(() => {
        usePermissionStoreHook().cacheOperate({
          mode: "add",
          name
        });
      }, 100);
  }
}

/**
 * 把后端返回的页面组件路径（如 `"logharvest/jobs/index"`）映射到
 * `src/views` 下的懒加载组件。先做后缀精确匹配，再退回 `includes`，
 * 避免 `jobs` 之类的短路径误命中别的页面。
 */
function findPageComponent(component: string) {
  const normalized = component
    .trim()
    .replace(/^\/+/, "")
    .replace(/\.(vue|tsx)$/i, "");
  if (!normalized) return undefined;
  const modulesRoutesKeys = Object.keys(modulesRoutes);
  const exact = modulesRoutesKeys.find(key =>
    [`/views/${normalized}.vue`, `/views/${normalized}.tsx`].some(suffix =>
      key.endsWith(suffix)
    )
  );
  if (exact) return modulesRoutes[exact];
  const fuzzy = modulesRoutesKeys.find(key => key.includes(normalized));
  return fuzzy ? modulesRoutes[fuzzy] : undefined;
}

/** 子路由允许写相对 path；统一成绝对 path，菜单与 `getTopMenu` 的跳转才不会偏 */
function normalizeRoutePath(path: string | undefined, parentPath?: string) {
  const value = (path ?? "").trim();
  if (!value) return parentPath || "/";
  if (/^https?:\/\//.test(value) || value.startsWith("/")) return value;
  if (!parentPath || parentPath === "/") return `/${value}`;
  return `${parentPath.replace(/\/+$/, "")}/${value}`;
}

/** 过滤后端传来的动态路由 重新生成规范路由 */
function addAsyncRoutes(
  arrRoutes: Array<Record<string, any>>,
  parentPath?: string
): RouteRecordRaw[] | undefined {
  if (!arrRoutes || !arrRoutes.length) return;
  arrRoutes.forEach((v: Record<string, any>) => {
    const hasChildren = Array.isArray(v.children) && v.children.length > 0;
    v.path = normalizeRoutePath(v.path, parentPath);
    v.meta = v.meta ?? {};
    // 将backstage属性加入meta，标识此路由为后端返回路由
    v.meta.backstage = true;
    // 父级的name属性取值：如果子级存在且父级的name属性不存在，默认取第一个子级的name；如果子级存在且父级的name属性存在，取存在的name属性，会覆盖默认值（注意：测试中发现父级的name不能和子级name重复，如果重复会造成重定向无效（跳转404），所以这里给父级的name起名的时候后面会自动加上`Parent`，避免重复）
    if (hasChildren && !v.name) v.name = `${v.children[0].name}Parent`;

    if (v.meta?.frameSrc) {
      v.component = IFrame;
    } else if (v.component === "Layout") {
      // 后端约定：顶级页面路由的容器写 "Layout"
      v.component = Layout;
    } else if (v.component) {
      const matched = findPageComponent(v.component);
      if (matched) {
        v.component = matched;
      } else {
        // 不静默置空，否则 vue-router 会报错且页面白屏无从排查
        console.error(
          `[router] 动态路由 "${v.path}" 的 component "${v.component}" 在 src/views 下找不到，已用 EmptyLayout 兜底（该页面会空白）`
        );
        v.component = EmptyLayout;
      }
    } else if (hasChildren) {
      // 目录路由：只用 router-view 占位，不能复用完整 Layout（否则嵌套侧边栏）
      v.component = EmptyLayout;
    } else {
      console.error(
        `[router] 动态路由 "${v.path}" 既没有 component 也没有 children，已用 EmptyLayout 兜底（该页面会空白）`
      );
      v.component = EmptyLayout;
    }

    if (hasChildren) {
      addAsyncRoutes(v.children, v.path);
      // 父级的redirect：子级路径已归一化，缺省取第一个子级；后端显式给的相对路径也补齐
      v.redirect = normalizeRoutePath(v.redirect || v.children[0].path, v.path);
    }
  });
  return arrRoutes as unknown as RouteRecordRaw[];
}

/** 获取路由历史模式 https://next.router.vuejs.org/zh/guide/essentials/history-mode.html */
function getHistoryMode(routerHistory): RouterHistory {
  // len为1 代表只有历史模式 为2 代表历史模式中存在base参数 https://next.router.vuejs.org/zh/api/#%E5%8F%82%E6%95%B0-1
  const historyMode = routerHistory.split(",");
  const leftMode = historyMode[0];
  const rightMode = historyMode[1];
  // no param
  if (historyMode.length === 1) {
    if (leftMode === "hash") {
      return createWebHashHistory("");
    } else if (leftMode === "h5") {
      return createWebHistory("");
    }
  } //has param
  else if (historyMode.length === 2) {
    if (leftMode === "hash") {
      return createWebHashHistory(rightMode);
    } else if (leftMode === "h5") {
      return createWebHistory(rightMode);
    }
  }
}

/** 获取当前页面按钮级别的权限 */
function getAuths(): Array<string> {
  return router.currentRoute.value.meta.auths as Array<string>;
}

/** 是否有按钮级别的权限（根据路由`meta`中的`auths`字段进行判断）*/
function hasAuth(value: string | Array<string>): boolean {
  if (!value) return false;
  /** 从当前路由的`meta`字段里获取按钮级别的所有自定义`code`值 */
  const metaAuths = getAuths();
  if (!metaAuths) return false;
  const isAuths = isString(value)
    ? metaAuths.includes(value)
    : isIncludeAllChildren(value, metaAuths);
  return isAuths ? true : false;
}

function handleTopMenu(route) {
  if (route?.children && route.children.length > 1) {
    if (route.redirect) {
      return route.children.filter(cur => cur.path === route.redirect)[0];
    } else {
      return route.children[0];
    }
  } else {
    return route;
  }
}

/** 获取所有菜单中的第一个菜单（顶级菜单）；没有任何可见菜单时返回 undefined */
function getTopMenu(tag = false): menuType | undefined {
  const topMenu = handleTopMenu(
    usePermissionStoreHook().wholeMenus[0]?.children?.[0]
  );
  if (!topMenu) return undefined;
  tag && useMultiTagsStoreHook().handleTags("push", topMenu);
  return topMenu;
}

export {
  hasAuth,
  getAuths,
  ascending,
  filterTree,
  initRouter,
  getTopMenu,
  addPathMatch,
  isOneOfArray,
  getHistoryMode,
  addAsyncRoutes,
  getParentPaths,
  findRouteByPath,
  handleAliveRoute,
  formatTwoStageRoutes,
  formatFlatteningRoutes,
  filterNoPermissionTree
};
