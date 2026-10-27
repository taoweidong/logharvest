/**
 * 操作日志查询客户端（权限 `audit:read`）。
 * 只读接口：日志只追加，无写、无删除端点。
 * 列表返回裸数组、无 total，翻页用窗口式 limit/offset（后端 limit 上限 500）。
 */
import { http } from "@/utils/http";
import type { OperationLog, OperationLogQuery } from "./auditTypes";

export const listOperationLogs = (params: OperationLogQuery = {}) =>
  http.request<OperationLog[]>("get", "/operation-logs", { params });
