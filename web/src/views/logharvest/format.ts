import dayjs from "dayjs";
import type { JobStatus, TaskStatus } from "@/api/logharvestTypes";

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

export function severityTagType(severity: string): TagType {
  return SEVERITY_TAG[severity] ?? "info";
}

/** 秒数转可读等待时长 */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "-";
  if (seconds < 60) return `${Math.round(seconds)} 秒`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分`;
  return `${Math.floor(seconds / 3600)} 小时 ${Math.floor((seconds % 3600) / 60)} 分`;
}
