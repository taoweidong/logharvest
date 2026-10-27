import { describe, expect, it } from "vitest";
import { filterMenus, filterRoles, menuTypeMeta } from "@/views/system/filters";
import { collectTreeIds, filterTree } from "@/utils/tree";
import type { MenuRecord, RoleRecord } from "@/api/rbacTypes";

function role(partial: Partial<RoleRecord> & Pick<RoleRecord, "id" | "name">): RoleRecord {
  return {
    code: "role_code",
    description: "",
    is_active: true,
    is_system: false,
    ...partial
  };
}

function menu(
  partial: Partial<MenuRecord> & Pick<MenuRecord, "id" | "title">
): MenuRecord {
  return {
    parent_id: null,
    name: "",
    menu_type: 1,
    path: "",
    component: "",
    icon: "",
    rank: 0,
    show_link: true,
    keep_alive: false,
    is_active: true,
    is_system: false,
    created_at: "",
    children: [],
    ...partial
  };
}

const tree: MenuRecord[] = [
  menu({
    id: 1,
    title: "系统管理",
    menu_type: 0,
    path: "/system",
    children: [
      menu({
        id: 2,
        title: "用户管理",
        path: "/system/users",
        children: [
          menu({ id: 3, title: "用户查询", menu_type: 2, name: "users:read" })
        ]
      }),
      menu({ id: 4, title: "角色管理", path: "/system/roles" })
    ]
  }),
  menu({ id: 5, title: "任务管理", menu_type: 0, path: "/jobs" })
];

describe("filterRoles", () => {
  const rows = [
    role({ id: 1, name: "管理员", code: "admin", is_active: true }),
    role({ id: 2, name: "操作员", code: "operator", is_active: true }),
    role({ id: 3, name: "只读值守", code: "viewer", is_active: false })
  ];

  it("空条件返回全量", () => {
    expect(filterRoles(rows, { name: "", code: "", status: "" })).toHaveLength(3);
  });

  it("名称与代码按子串匹配且忽略大小写", () => {
    expect(filterRoles(rows, { name: "管理", code: "", status: "" })).toHaveLength(1);
    expect(filterRoles(rows, { name: "", code: "OP", status: "" })[0].code).toBe(
      "operator"
    );
  });

  it("状态筛选区分启用与停用", () => {
    expect(filterRoles(rows, { name: "", code: "", status: "active" })).toHaveLength(2);
    expect(filterRoles(rows, { name: "", code: "", status: "inactive" })[0].id).toBe(3);
  });

  it("条件之间是交集", () => {
    expect(filterRoles(rows, { name: "员", code: "operator", status: "active" })).toHaveLength(
      1
    );
    // 「值守」只属于 viewer，「admin」只属于管理员，交集为空
    expect(filterRoles(rows, { name: "值守", code: "admin", status: "" })).toHaveLength(0);
  });
});

describe("filterTree", () => {
  it("命中叶子时保留整条祖先链", () => {
    const result = filterTree(tree, node => node.id === 3);
    expect(result).toHaveLength(1);
    expect(result[0].id).toBe(1);
    expect(result[0].children?.[0].id).toBe(2);
    expect(result[0].children?.[0].children?.[0].id).toBe(3);
    // 未命中的兄弟分支被剔除
    expect(result[0].children).toHaveLength(1);
  });

  it("命中父节点时保留其全部后代", () => {
    const result = filterTree(tree, node => node.id === 2);
    expect(result[0].children?.[0].children).toHaveLength(1);
  });

  it("全部未命中返回空数组，且不修改入参", () => {
    expect(filterTree(tree, node => node.id === 999)).toEqual([]);
    expect(tree[0].children).toHaveLength(2);
  });
});

describe("filterMenus", () => {
  it("空关键字返回原树引用，避免无谓重建", () => {
    expect(filterMenus(tree, "   ")).toBe(tree);
  });

  it("按标题、权限码、路径任一命中", () => {
    expect(filterMenus(tree, "用户管理")[0].children?.[0].id).toBe(2);
    expect(filterMenus(tree, "users:read")[0].children?.[0].children?.[0].id).toBe(3);
    expect(filterMenus(tree, "/system/roles")[0].children?.map(n => n.id)).toEqual([4]);
  });

  it("大小写不敏感", () => {
    expect(filterMenus(tree, "USERS:READ")).not.toHaveLength(0);
  });
});

describe("collectTreeIds", () => {
  it("深度优先收集全部节点 id", () => {
    expect(collectTreeIds(tree)).toEqual([1, 2, 3, 4, 5]);
  });

  it("空树返回空数组", () => {
    expect(collectTreeIds([])).toEqual([]);
  });
});

describe("menuTypeMeta", () => {
  it("已知类型返回标签与颜色", () => {
    expect(menuTypeMeta(0)).toEqual({ label: "目录", tag: "info" });
    expect(menuTypeMeta(2)).toEqual({ label: "权限", tag: "warning" });
  });

  it("未知类型降级为未知标签", () => {
    expect(menuTypeMeta(9).label).toBe("未知");
  });
});
