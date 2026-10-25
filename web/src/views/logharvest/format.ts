import dayjs from "dayjs";
import type { JobStatus, TaskStatus } from "@/api/logharvestTypes";

/**
 * 「没有数据」的显示值。
 *
 * 后端的这些列可以为 `NULL`，且**列存在不等于有值**：Worker 的
 * `cpu_percent`/`memory_bytes`/`temp_disk_free_bytes`/`last_error` 在 M2c Task 4 之前
 * 没有任何写入方（心跳语句 `WORKER_HEARTBEAT_SQL` 只改 status/current_task_id/
 * last_heartbeat），老版本 Worker 留下的行就是 `NULL`；尝试历史的 `finished_at`/
 * `error` 同理。界面把 `NULL` 显示成 `0` 会被读成「CPU 空闲 / 临时盘已满」这类
 * 假安心，所以未知一律走这个常量。
 */
export const UNKNOWN_TEXT = "—";

/** 文本/标识列的未知值（空串按「没有内容」处理，不用 `0` 顶替） */
export function textOrUnknown(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return UNKNOWN_TEXT;
  return String(value);
}

/** 计数字段的未知值：只有真正的数字才显示数字，`NULL` 绝不退化成 `0` */
export function countOrUnknown(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return UNKNOWN_TEXT;
  return String(value);
}

/** Worker CPU：一位小数 + `%`。psutil 在多核下可以 >100，这里不做截断 */
export function formatCpuPercent(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return UNKNOWN_TEXT;
  return `${value.toFixed(1)}%`;
}

/** Worker 进程内存 RSS：MB，一位小数 */
export function formatMemoryMB(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return UNKNOWN_TEXT;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

/** Worker 临时盘剩余空间：GB，两位小数（`—` 与真实的 `0.00 GB` 必须能区分） */
export function formatDiskFreeGB(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return UNKNOWN_TEXT;
  return `${(value / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

/** 后端字节数为 `BIGINT`，列表页统一按 GB 展示（保留两位） */
export function formatGB(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return "-";
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

export function formatMB(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return "-";
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
}

/**
 * 后端时间戳是 `without time zone` 的 UTC 墙上时间，序列化时不带偏移，
 * dayjs 会按本地时区解析——这里显式补 `Z` 让展示跟随浏览器本地时区。
 */
export function formatDateTime(value: string | null | undefined): string {
  if (!value) return "-";
  const normalized = /Z|[+-]\d{2}:?\d{2}$/.test(value) ? value : `${value}Z`;
  return dayjs(normalized).format("YYYY-MM-DD HH:mm:ss");
}

/** 相对时间（Worker 心跳用） */
export function fromNow(value: string | null | undefined): string {
  if (!value) return "-";
  const normalized = /Z|[+-]\d{2}:?\d{2}$/.test(value) ? value : `${value}Z`;
  const then = dayjs(normalized);
  const seconds = dayjs().diff(then, "second");
  if (seconds < 60) return `${seconds} 秒前`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return `${Math.floor(seconds / 86400)} 天前`;
}

type TagType = "primary" | "success" | "warning" | "info" | "danger";

const STATUS_TAG: Record<JobStatus | TaskStatus, TagType> = {
  pending: "info",
  running: "primary",
  succeeded: "success",
  partial: "warning",
  failed: "danger",
  cancelled: "warning"
};

export function statusTagType(status: string): TagType {
  return STATUS_TAG[status as JobStatus] ?? "info";
}

export const SEVERITY_TAG: Record<string, TagType> = {
  info: "info",
  warning: "warning",
  error: "danger"
};

/**
 * Worker 状态配色：`online|busy|draining|offline`（与后端 `Worker.status` 同一套取值，
 * 见 `service/src/domain/models/entities.py` 与 `src/infrastructure/task_queue.py`）。
 */
const WORKER_STATUS_TAG: Record<string, TagType> = {
  online: "success",
  busy: "primary",
  draining: "warning",
  offline: "info"
};

export function workerStatusTagType(status: string): TagType {
  return WORKER_STATUS_TAG[status] ?? "info";
}

export function severityTagType(severity: string): TagType {
  return SEVERITY_TAG[severity] ?? "info";
}

/**
 * 把日期选择器给出的本地时间转成后端可正确理解的 UTC 时间串（带 `Z`）。
 * 后端 `created_at` 存的是不带时区的 UTC 墙上时间，`routes_matches.py` 的 `_as_utc`
 * 会把「不带偏移」的入参当作 UTC——所以这里必须显式带上 `Z`，否则时间范围会按
 * 浏览器时区偏移（东八区会整整差 8 小时）。
 */
export function toApiDateTime(
  value: Date | string | number | null | undefined
): string | undefined {
  if (value === null || value === undefined || value === "") return undefined;
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return undefined;
  return date.toISOString();
}

/** 秒数转可读等待时长 */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "-";
  if (seconds < 60) return `${Math.round(seconds)} 秒`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分`;
  return `${Math.floor(seconds / 3600)} 小时 ${Math.floor((seconds % 3600) / 60)} 分`;
}

/**
 * 把后端错误压成可读的多行文本（422 的 `detail` 是 pydantic 校验错误数组，
 * 每一条都含 `loc` 与 `msg`；字符串 `detail` 原样返回）。
 * 后端已完成规则编译/pathspec/codec 校验，前端只转述、不重复实现。
 */
export function formatDetailError(error: unknown, fallback = "请求失败"): string {
  const detail = (error as any)?.response?.data?.detail;
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail)) {
    const lines = detail.map(item => {
      const loc: string = Array.isArray(item?.loc)
        ? item.loc.slice(1).join(".")
        : String(item?.loc ?? "");
      const msg = item?.msg ?? item?.detail ?? "校验失败";
      return loc ? `${loc}: ${msg}` : String(msg);
    });
    return lines.join("\n");
  }
  return fallback;
}
