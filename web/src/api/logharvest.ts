import { http } from "@/utils/http";
import type {
  DrainVersionResult,
  FileTask,
  FileTaskAttempt,
  Job,
  JobCreatePayload,
  MaintenanceResult,
  Match,
  Overview,
  SystemStatus,
  WorkerActionRecord,
  WorkerInfo
} from "./logharvestTypes";

/** 列表分页参数：后端只认 `limit`/`offset`，且响应是裸数组（没有 total） */
export type ListParams = { limit?: number; offset?: number };
export type FileTaskListParams = ListParams & { status?: string };

/**
 * 取 FastAPI 的错误文案。
 * 业务接口的错误体是 `{"detail": "..."}`（`detail` 可能是字符串，
 * 也可能是 pydantic 的校验错误数组，这里统一压成一行可读的文本）。
 */
export function apiErrorDetail(error: unknown, fallback = "请求失败"): string {
  const detail = (error as any)?.response?.data?.detail;
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail)) {
    const first = detail[0];
    const msg = first?.msg ?? first?.detail;
    if (typeof msg === "string" && msg) return msg;
  }
  return fallback;
}

/**
 * 取 HTTP 状态码（无响应 = 断网/超时时为 `undefined`）。
 * 只读抽屉需要区分「404：后端还没有这个端点」与「500/断网」，文案不同，
 * 不能都压成一句「加载失败」。
 */
export function apiErrorStatus(error: unknown): number | undefined {
  const status = (error as any)?.response?.status;
  return typeof status === "number" ? status : undefined;
}

/** 作业列表（按创建时间倒序） */
export const listJobs = (params: ListParams = {}) =>
  http.request<Job[]>("get", "/jobs", { params });

/** 创建作业（同步扫描目录；相同 Idempotency-Key 且请求体一致时回放已有作业） */
export const createJob = (data: JobCreatePayload, idempotencyKey: string) =>
  http.request<Job>("post", "/jobs", {
    data,
    headers: { "Idempotency-Key": idempotencyKey }
  });

/** 作业详情 */
export const getJob = (id: number) => http.request<Job>("get", `/jobs/${id}`);

/**
 * 作业的文件任务列表。
 * `status` 由后端过滤（枚举校验，非法值返回 422）；仍只有 `limit`/`offset`，
 * 没有总数，所以界面是窗口式翻页。
 */
export const listJobFiles = (id: number, params: FileTaskListParams = {}) =>
  http.request<FileTask[]>("get", `/jobs/${id}/files`, { params });

/** 请求取消作业：返回取消后的作业（终态作业不会被改写） */
export const cancelJob = (id: number) =>
  http.request<Job>("post", `/jobs/${id}/cancel`);

/**
 * 某个文件任务的尝试历史（返回**裸数组**，按 `attempt_no` 升序，与全站列表一致）。
 * 契约：`GET /api/jobs/{job_id}/files/{task_id}/attempts` → `list[FileTaskAttemptRead]`（M2c Task 3）。
 * 后端区分两种「没有数据」：
 * - 任务存在且属于该作业但没有历史 → `200 []`（界面显示「该任务尚无尝试记录」）
 * - 任务不存在或不属于该作业 → `404 {"detail": "file task not found"}`（界面显示错误）
 * 该端点在当前运行的后端上**尚未存在**，界面必须把失败如实显示出来，不能退化成空表。
 */
export const listTaskAttempts = (jobId: number, taskId: number) =>
  http.request<FileTaskAttempt[]>(
    "get",
    `/jobs/${jobId}/files/${taskId}/attempts`
  );

/**
 * 重试作业内全部失败的文件任务（把 `failed` 重置回 `pending` 等待 Worker 领取）。
 * 契约：`POST /api/jobs/{id}/files/retry-failed` → `{"retried": n}`。
 * 该端点由后端 M2a 并行轨道补充实现，前端按其约定先行对接。
 */
export const retryFailedFiles = (id: number) =>
  http.request<{ retried: number }>("post", `/jobs/${id}/files/retry-failed`);

/**
 * 命中结果查询（游标分页）。
 * 后端约束：必须给 `job_id`，或同时给 `created_from` + `created_to`；
 * `created_before` + `id_before` 必须成对提供（取上一页最后一行的 `created_at` 与 `id`）；
 * 排序固定 `created_at DESC, id DESC`，`limit` 上限 500。
 */
export type MatchQuery = {
  job_id?: number;
  rule_id?: string;
  file_name?: string;
  severity?: string;
  created_from?: string;
  created_to?: string;
  created_before?: string;
  id_before?: number;
  limit?: number;
};

/** 返回裸数组；下一页游标由调用方从最后一行 `{created_at, id}` 取 */
export const listMatches = (params: MatchQuery) =>
  http.request<Match[]>("get", "/matches", { params });

/** 系统状态（文件任务计数、最老 pending 等待秒数、在线 Worker 数） */
export const systemStatus = () =>
  http.request<SystemStatus>("get", "/system/status");

/**
 * 概览看板数据源。
 * 契约：`GET /api/system/overview` → `{jobs, file_tasks, matches_24h,
 * workers:{online, by_version}, oldest_pending_seconds}`。
 * 已在合流后的后端上实测通过；若请求失败界面退回 `systemStatus()`。
 */
export const overview = () =>
  http.request<Overview>("get", "/system/overview");

/**
 * 手动触发一次维护（回收悬挂任务、降级失联 Worker、收尾停滞作业）。
 * 契约：`POST /api/system/maintenance` → `{recovered, workers_demoted, jobs_finalized}`。
 * 回收语句是 PostgreSQL 专有语法，所以后端在 SQLite 等非 PostgreSQL 连接上返回
 * **503** 并说明实际方言；界面直接把 `detail` 显示出来，不当作前端错误。
 */
export const runMaintenance = () =>
  http.request<MaintenanceResult>("post", "/system/maintenance");

/**
 * 数据源/规则集的列表与维护接口在 `logharvestAdmin.ts`（Task 5），
 * 这里只保留作业、命中、系统状态与 Worker 相关的客户端。
 */

/** Worker 列表（`limit` 上限 200） */
export const listWorkers = (params: ListParams = {}) =>
  http.request<WorkerInfo[]>("get", "/workers", { params });

/** 排空单台 Worker；离线 Worker 只记录标记，状态保持 `offline` */
export const drainWorker = (id: string) =>
  http.request<WorkerActionRecord>("post", `/workers/${id}/drain`);

/** 恢复领取；对 `offline` Worker 后端返回 409 */
export const resumeWorker = (id: string) =>
  http.request<WorkerActionRecord>("post", `/workers/${id}/resume`);

/** 按版本排空（滚动升级用）：`drained` 在线转 draining 的数量，`flagged` 仅记录标记的离线数量 */
export const drainVersion = (version: string) =>
  http.request<DrainVersionResult>("post", "/workers/drain-version", {
    params: { version }
  });
