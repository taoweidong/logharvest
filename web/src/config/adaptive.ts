/**
 * 表格自适应高度：容器底部预留量（pure-table 的 adaptiveConfig.offsetBottom）
 *
 * 此前 14 个列表页直接写魔法数字 45 / 108 / 80，看不出差异来源，
 * 新增页面时只能"抄一个看起来对的"。这里按**页面实际占据的高度**分档命名，
 * 数值保持不变，仅让含义显式化。
 */
export const ADAPTIVE_OFFSET = {
  /** 搜索表单 + 工具栏（无额外操作条）—— 多数列表页 */
  base: 45,

  /** 在 base 之上多一条批量选择操作条（"已选 N 项 / 取消选择 / 批量删除"，约 +63px） */
  withSelectionBar: 108,

  /** 左右分栏且无分页器的场景（如角色 — 权限配置） */
  split: 80
} as const;
