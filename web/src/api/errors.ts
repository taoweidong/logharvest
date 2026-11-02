/**
 * FastAPI 错误体的统一提取工具。
 *
 * 业务与认证接口的错误体都是 `{"detail": "..."}`（`detail` 可能是字符串，
 * 也可能是 pydantic 的校验错误数组）。这里收拢"怎么把错误变成一句可读文案"
 * 的唯一实现，供 api 模块、store 与视图共用；本模块必须保持零依赖——
 * api/logharvest 与 store/modules/user 都引用它，一旦引入 http/store 会成环。
 */

/** 取 FastAPI 错误体的 `detail` 字段原始值（字符串 / 校验错误数组 / undefined）。
 *
 * "detail 在 `response.data.detail`"这一契约知识只此一处；单行文案
 * （apiErrorDetail）与多行表单校验展示（formatDetailError）各自的排版策略
 * 建立在这个读取点之上。
 */
export function responseDetail(error: unknown): unknown {
  return (error as any)?.response?.data?.detail;
}

/** 取 FastAPI 的错误文案；取不到时返回 fallback。 */
export function apiErrorDetail(error: unknown, fallback = "请求失败"): string {
  const detail = responseDetail(error);
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail)) {
    const first = detail[0];
    const msg = first?.msg ?? first?.detail;
    if (typeof msg === "string" && msg) return msg;
  }
  return fallback;
}

/** 取 HTTP 状态码（无响应 = 断网/超时时为 `undefined`）。 */
export function apiErrorStatus(error: unknown): number | undefined {
  const status = (error as any)?.response?.status;
  return typeof status === "number" ? status : undefined;
}
