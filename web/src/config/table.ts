/**
 * 列表页表格的统一视觉约定。
 *
 * 表格高度不再用 pure-table 的 adaptive：它按 `window.innerHeight - 顶部位置 -
 * offsetBottom` 写死像素高，offsetBottom 只能逐页手调（此前 14 个列表页各写一份
 * 45 / 96 / 108，同一套面板底部留白差 12~63px），提示行一换行就要重算，空结果时
 * 还会把「暂无数据」拉成一整屏空白。
 *
 * 现在统一写 height="100%"，由 style/layout.scss 的 .page-panel--fill / .page-body
 * 用 flex 决定可用高度：面板多高表格就多高，行数多少、页脚有无都不需要额外常量。
 */

/** 表头底色与文字色：所有列表页共用，避免逐页写死灰色 */
export const TABLE_HEADER_CELL_STYLE = {
  background: "var(--el-fill-color-light)",
  color: "var(--el-text-color-primary)",
};
