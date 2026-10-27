import type { MenuRecord, MenuType, RoleRecord } from "@/api/rbacTypes";
import { filterTree } from "@/utils/tree";

export type TagType = "primary" | "success" | "warning" | "info" | "danger";

/** 菜单三态：0 目录、1 页面、2 权限码 */
export const MENU_TYPE_META: Record<MenuType, { label: string; tag: TagType }> = {
  0: { label: "目录", tag: "info" },
  1: { label: "页面", tag: "primary" },
  2: { label: "权限", tag: "warning" }
};

export function menuTypeMeta(type: MenuType | number) {
  return (
    MENU_TYPE_META[type as MenuType] ?? { label: "未知", tag: "info" as TagType }
  );
}

export type RoleStatusFilter = "" | "active" | "inactive";

export type RoleFilters = {
  name: string;
  code: string;
  status: RoleStatusFilter;
};

export const EMPTY_ROLE_FILTERS: RoleFilters = {
  name: "",
  code: "",
  status: ""
};

/**
 * 角色列表的客户端筛选。
 *
 * `listRoles` 一次取足（limit 500），所以这里是对**全量**过滤，不会出现
 * “只筛了当前页”的假结果；后端暂无同名查询参数，过滤条件不值得为它加接口。
 */
export function filterRoles(
  rows: RoleRecord[],
  filters: RoleFilters
): RoleRecord[] {
  const name = filters.name.trim().toLowerCase();
  const code = filters.code.trim().toLowerCase();
  return rows.filter(row => {
    if (name && !row.name.toLowerCase().includes(name)) return false;
    if (code && !row.code.toLowerCase().includes(code)) return false;
    if (filters.status === "active" && !row.is_active) return false;
    if (filters.status === "inactive" && row.is_active) return false;
    return true;
  });
}

/** 菜单树/权限树的关键字筛选：命中节点保留整条祖先链，空关键字返回原树 */
export function filterMenus(
  nodes: MenuRecord[],
  keyword: string
): MenuRecord[] {
  const kw = keyword.trim().toLowerCase();
  if (!kw) return nodes;
  return filterTree(nodes, node =>
    [node.title, node.name, node.path]
      .filter(Boolean)
      .some(text => String(text).toLowerCase().includes(kw))
  );
}
