/**
 * 操作日志（`operation_logs` 表）的响应类型。
 *
 * 契约：`GET /api/operation-logs` 返回裸数组、按 `created_at DESC, id DESC`，无 total；
 * 时间过滤 `created_from`/`created_to` **必须成对**给出，否则后端 422；
 * `detail` 是变更前后/参数摘要的 JSON（不含口令等敏感字段）。
 * `actor_id` 可空：外键 ON DELETE SET NULL，删号后靠 `actor_name`/`role_hint` 快照仍可读。
 */

export type OperationLogQuery = {
  limit?: number;
  offset?: number;
  actor_id?: number;
  /** 点分动作，后端前缀匹配：`user.` 即整族 */
  action?: string;
  resource_type?: string;
  resource_id?: string;
  /** UTC ISO 字符串，与 created_to 成对提交 */
  created_from?: string;
  created_to?: string;
};

export type OperationLog = {
  id: number;
  actor_id: number | null;
  actor_name: string;
  /** 成功登录时的角色码快照，逗号分隔 */
  role_hint: string;
  action: string;
  resource_type: string | null;
  resource_id: string | null;
  summary: string;
  detail: unknown;
  origin_ip: string;
  created_at: string;
};

/** 动作族预置项（与后端埋点矩阵 §6 一一对应，前缀匹配整族） */
export const OPERATION_ACTION_FAMILIES = [
  "auth.",
  "job.",
  "user.",
  "role.",
  "menu.",
  "rule_set.",
  "data_source.",
  "worker.",
  "system."
] as const;

/** 对象类型预置项（resource_type 列的取值集合） */
export const OPERATION_RESOURCE_TYPES = [
  "job",
  "rule_set",
  "data_source",
  "user",
  "role",
  "menu",
  "worker",
  "system"
] as const;
