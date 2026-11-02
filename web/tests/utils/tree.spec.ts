import { describe, expect, it, vi } from "vitest";

// 树工具是菜单/权限树的核心纯逻辑；防御分支里的 console.warn 静音避免污染输出。
vi.spyOn(console, "warn").mockImplementation(() => {});

import {
  appendFieldByUniqueId,
  buildHierarchyTree,
  collectTreeIds,
  deleteChildren,
  extractPathList,
  filterTree,
  getNodeByUniqueId,
  handleTree
} from "@/utils/tree";

describe("extractPathList", () => {
  it("非数组入参返回空数组（防御分支）", () => {
    expect(extractPathList(null as any)).toEqual([]);
    expect(extractPathList("tree" as any)).toEqual([]);
  });

  it("空树返回空数组", () => {
    expect(extractPathList([])).toEqual([]);
  });

  it("仅收集顶层节点的 uniqueId（上游 pure-admin 语义：递归返回值被丢弃）", () => {
    const tree = [
      {
        uniqueId: "a",
        children: [{ uniqueId: "a-1" }, { uniqueId: "a-2", children: [{ uniqueId: "a-2-1" }] }]
      },
      { uniqueId: "b" }
    ];
    // 仓库内没有调用方；此断言特征化上游实现的实际行为，供未来重构时对照。
    expect(extractPathList(tree)).toEqual(["a", "b"]);
  });
});

describe("deleteChildren", () => {
  it("非数组入参返回空数组", () => {
    expect(deleteChildren(undefined as any)).toEqual([]);
  });

  it("单子节点合并：children 被删除，uniqueId 由路径组装", () => {
    const tree = [{ name: "root", children: [{ name: "only" }] }];
    const result = deleteChildren(tree);

    expect(result[0].id).toBe(0);
    expect(result[0].parentId).toBeNull();
    expect(result[0].pathList).toEqual([0]);
    expect(result[0].uniqueId).toBe(0);
    // 唯一子节点被并入父节点，children 不复存在
    expect(result[0].children).toBeUndefined();
    expect(result[0].name).toBe("root");
  });

  it("多子节点保留并递归编号，pathList 逐层累积", () => {
    const tree = [
      {
        name: "a",
        children: [
          { name: "a1" },
          { name: "a2", children: [{ name: "a2x" }] }
        ]
      },
      { name: "b" }
    ];
    const result = deleteChildren(tree);

    expect(result[0].uniqueId).toBe(0);
    expect(result[0].children).toHaveLength(2);
    // id 是节点在自己兄弟数组里的下标：a1 是第 1 个孩子 → id 0 → uniqueId "0-0"
    expect(result[0].children[0].uniqueId).toBe("0-0");
    expect(result[0].children[0].parentId).toBe(0);
    expect(result[0].children[0].pathList).toEqual([0, 0]);
    expect(result[0].children[1].uniqueId).toBe("0-1");
    // a2 只有唯一孩子 a2x：合并进 a2，children 不复存在
    expect(result[0].children[1].children).toBeUndefined();
    // b 是根层的第 2 个节点
    expect(result[1].uniqueId).toBe(1);
  });
});

describe("buildHierarchyTree", () => {
  it("标注 id/parentId/pathList 但不删除 children", () => {
    const tree = [{ name: "a", children: [{ name: "a1" }] }];
    const result = buildHierarchyTree(tree);

    expect(result[0].id).toBe(0);
    expect(result[0].children).toHaveLength(1);
    // id 是节点在兄弟数组里的下标 → a1 的 pathList 为 [0, 0]
    expect(result[0].children[0].pathList).toEqual([0, 0]);
    expect(result[0].children[0].uniqueId).toBeUndefined();
  });

  it("空树与非数组都返回空数组", () => {
    expect(buildHierarchyTree([])).toEqual([]);
    expect(buildHierarchyTree(42 as any)).toEqual([]);
  });
});

describe("getNodeByUniqueId", () => {
  const tree = [
    { uniqueId: 0, children: [{ uniqueId: "0-1" }] },
    { uniqueId: 2 }
  ];

  it("命中根层节点", () => {
    expect(getNodeByUniqueId(tree, 2)).toEqual({ uniqueId: 2 });
  });

  it("递归命中子层节点", () => {
    expect(getNodeByUniqueId(tree, "0-1")).toEqual({ uniqueId: "0-1" });
  });

  it("找不到时最终返回 []（递归到叶子）", () => {
    expect(getNodeByUniqueId(tree, "missing")).toEqual([]);
  });
});

describe("appendFieldByUniqueId", () => {
  it("只向命中节点追加字段，子层同样生效", () => {
    const tree = [
      { uniqueId: 0, children: [{ uniqueId: "0-1", children: [{ uniqueId: "0-1-1" }] }] }
    ];
    appendFieldByUniqueId(tree, "0-1-1", { showLink: true });

    expect(tree[0].showLink).toBeUndefined();
    expect(tree[0].children[0].showLink).toBeUndefined();
    expect(tree[0].children[0].children[0].showLink).toBe(true);
  });
});

describe("filterTree", () => {
  const tree = [
    {
      name: "系统",
      children: [
        { name: "用户", children: [{ name: "新增用户" }] },
        { name: "无关" }
      ]
    },
    { name: "作业" }
  ];

  it("命中叶子：保留其完整祖先链，剔除未命中的兄弟", () => {
    const result = filterTree(tree, node => node.name === "新增用户");

    expect(result).toHaveLength(1);
    expect(result[0].name).toBe("系统");
    expect(result[0].children).toHaveLength(1);
    expect(result[0].children[0].name).toBe("用户");
    expect(result[0].children[0].children[0].name).toBe("新增用户");
  });

  it("命中目录：整棵子树一并保留", () => {
    const result = filterTree(tree, node => node.name === "系统");

    expect(result[0].children).toHaveLength(2);
    // 保留子树但不动入参：返回的是浅拷贝
    expect(result[0]).not.toBe(tree[0]);
    expect(tree[0].children).toHaveLength(2);
  });

  it("全不命中返回空数组；非数组入参返回空数组", () => {
    expect(filterTree(tree, () => false)).toEqual([]);
    expect(filterTree(null as any, () => true)).toEqual([]);
  });
});

describe("collectTreeIds", () => {
  it("深度优先收集所有节点 id（含根）", () => {
    const tree = [
      { id: 1, children: [{ id: 11, children: [{ id: 111 }] }, { id: 12 }] },
      { id: 2 }
    ];
    expect(collectTreeIds(tree)).toEqual([1, 11, 111, 12, 2]);
  });

  it("空树与非数组返回空数组", () => {
    expect(collectTreeIds([])).toEqual([]);
    expect(collectTreeIds("x" as any)).toEqual([]);
  });
});

describe("handleTree", () => {
  it("按 parentId 把平铺列表组装成树", () => {
    const flat = [
      { id: 1, parentId: 0, name: "系统" },
      { id: 2, parentId: 1, name: "用户" },
      { id: 3, parentId: 0, name: "作业" }
    ];
    const tree = handleTree(flat);

    expect(tree).toHaveLength(2);
    expect(tree[0].name).toBe("系统");
    expect(tree[0].children).toHaveLength(1);
    expect(tree[0].children[0].name).toBe("用户");
    expect(tree[1].name).toBe("作业");
  });

  it("支持自定义 id/parentId/children 字段名", () => {
    const flat = [
      { key: 1, parent: 0 },
      { key: 2, parent: 1 }
    ];
    const tree = handleTree(flat, "key", "parent", "kids");

    expect(tree).toHaveLength(1);
    expect(tree[0].kids[0].key).toBe(2);
  });

  it("非数组入参返回空数组", () => {
    expect(handleTree("flat" as any)).toEqual([]);
  });
});
