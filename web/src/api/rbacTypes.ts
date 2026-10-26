/**
 * RBAC 管理接口与动态路由的共享类型。
 *
 * 管理接口是纯 REST：列表直接返回裸数组（没有 `{code,message,data}` 信封），
 * 错误体为 FastAPI 的 `{detail}`（见 `service/README.md`）。
 */

/** 用户列表里内嵌的角色摘要 */
export type RoleBrief = { id: number; code: string; name: string };

/** `GET /api/users` 的列表项 / 创建与更新接口的返回体 */
export type UserRecord = {
  id: number;
  username: string;
  nickname: string;
  is_active: boolean;
  created_at: string;
  roles: RoleBrief[];
};

/** `GET /api/roles` 的列表项 */
export type RoleRecord = {
  id: number;
  code: string;
  name: string;
  description: string;
  is_active: boolean;
  is_system: boolean;
};

/** 0 = 目录，1 = 菜单（页面），2 = 按钮 */
export type MenuType = 0 | 1 | 2;

/** `GET /api/menus` / `GET /api/menus/tree` 的节点（树接口带 children） */
export type MenuRecord = {
  id: number;
  parent_id: number | null;
  name: string;
  menu_type: MenuType;
  path: string;
  component: string;
  title: string;
  icon: string;
  rank: number;
  show_link: boolean;
  keep_alive: boolean;
  is_active: boolean;
  is_system: boolean;
  created_at: string;
  children: MenuRecord[];
};

/**
 * `GET /api/auth/routes` 返回的动态路由（`data` 字段）。
 * 目录路由 `component` 为空且有 `children`；页面路由 `component`
 * 形如 `"logharvest/jobs/index"`，由前端映射到 `src/views` 下的组件。
 */
export type AsyncRouteRecord = {
  path: string;
  name: string;
  component?: string;
  redirect?: string;
  meta: {
    title?: string;
    icon?: string;
    rank?: number;
    showLink?: boolean;
    keepAlive?: boolean;
    auths?: string[];
  };
  children?: AsyncRouteRecord[];
};

export type UserListParams = { limit?: number; offset?: number; keyword?: string };

export type UserCreatePayload = {
  username: string;
  password: string;
  nickname?: string;
  is_active?: boolean;
  role_ids?: number[];
};

export type UserUpdatePayload = { nickname?: string; is_active?: boolean };

export type RoleCreatePayload = {
  code: string;
  name: string;
  description?: string;
  is_active?: boolean;
};

export type RoleUpdatePayload = {
  name?: string;
  description?: string;
  is_active?: boolean;
};

export type MenuPayload = {
  parent_id?: number | null;
  name: string;
  menu_type: MenuType;
  path?: string;
  component?: string;
  title: string;
  icon?: string;
  rank?: number;
  show_link?: boolean;
  keep_alive?: boolean;
  is_active?: boolean;
};
