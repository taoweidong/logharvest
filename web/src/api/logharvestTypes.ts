/**
 * LogHarvest 业务接口的响应类型。
 *
 * 逐字段核对自后端源码（`service/src/api/v1/schemas.py`、`service/src/api/v1/routes_*.py`）：
 * - 业务接口是**纯 REST 对象**，没有 `{code,message,data}` 信封，错误体是 `{"detail": "..."}`
 * - 列表接口（jobs / files / rule-sets / data-sources / workers / matches）返回**裸数组**，
 *   没有 `{items,total}` 包装，也没有 `total`，所以分页只能用 `limit`/`offset` 判断“是否还有下一页”
 * - `numeric_value` 是 `Decimal`，Pydantic 序列化为字符串；时间为 ISO 字符串
 */

export type JobStatus =
  | "pending"
  | "running"
  | "succeeded"
  | "partial"
  | "failed"
  | "cancelled";

/**
 * 文件任务状态。与作业同一套取值：后端目前只会写入
 * `pending|running|succeeded|failed|cancelled`，`partial`（单个压缩包内部分成员失败）
 * 仍未实现，但类型保留它，避免后端补齐时前端再改。
 */
export type TaskStatus = JobStatus;

export type Job = {
  id: number;
  name: string;
  data_source_id: number;
  source_path: string;
  rule_set_id: number;
  status: JobStatus;
  total_files: number;
  total_bytes: number;
  cancel_requested: boolean;
  created_by: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
};

export type JobFileFilter = {
  recursive: boolean;
  max_depth: number;
  suffixes?: string[] | null;
  max_files: number;
};

export type JobCreatePayload = {
  name: string;
  data_source_id: number;
  source_path: string;
  rule_set_id: number;
  file_filter?: Partial<JobFileFilter>;
};

export type FileTask = {
  id: number;
  job_id: number;
  source_path: string;
  file_name: string;
  size_bytes: number;
  status: TaskStatus;
  attempts: number;
  max_attempts: number;
  worker_id: string | null;
  scheduled_at: string;
  heartbeat_at: string | null;
  processed_lines: number;
  matched_rows: number;
  error: string | null;
};

/**
 * 一次文件任务尝试的历史记录（后端 `FileTaskAttemptRead`，M2c Task 3 新增只读端点）。
 * 字段逐列核对自 `service/src/domain/models/entities.py` 的 `FileTaskAttempt`：
 * `file_task_id`/`attempt_no`/`worker_id`/`worker_version`/`status`/`started_at`
 * /`finished_at`/`processed_lines`/`matched_rows`/`error`。
 *
 * 注意取值：`status` 除作业状态那套之外还会出现 `timeout`
 * （失联回收时由 `RECOVER_STALE_SQL` 写入），所以这里用 `string` 而不是收窄联合。
 * `finished_at`/`error` 可为 `null`：前者表示该次尝试仍在执行，后者表示没有错误。
 */
export type FileTaskAttempt = {
  file_task_id: number;
  attempt_no: number;
  worker_id: string;
  worker_version: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  processed_lines: number;
  matched_rows: number;
  error: string | null;
};

export type Match = {
  id: number;
  job_id: number;
  file_task_id: number;
  rule_id: string;
  rule_version: number;
  severity: string;
  tags: string[];
  source_path: string;
  member_path: string;
  file_name: string;
  line_no: number;
  raw_line: string;
  raw_truncated: boolean;
  fields: Record<string, string | null>;
  numeric_value: string | null;
  created_at: string;
};

export type WorkerStatus = "online" | "busy" | "draining" | "offline";

/**
 * `GET /api/workers` 的行，逐字段核对自 `service/src/api/v1/schemas.py` 的 `WorkerRead`。
 *
 * **可空字段的语义**：`cpu_percent`/`memory_bytes`/`temp_disk_free_bytes`/`last_error`
 * 这四列在表里一直存在，但 M2c Task 4 之前没有任何写入方（心跳语句只更新状态与
 * `last_heartbeat`），所以老版本 Worker 写下的行确实是 `NULL`。
 * `NULL` 表示「没有上报过」，不是 0：把空当 0 显示会读成「CPU 空闲 / 临时盘满了」，
 * 正是本项目反复踩过的假安心。界面必须显式区分两者。
 */
export type WorkerInfo = {
  id: string;
  hostname: string;
  version: string;
  status: WorkerStatus;
  drain_requested: boolean;
  current_task_id: number | null;
  max_concurrent_copies: number;
  cpu_percent: number | null;
  memory_bytes: number | null;
  temp_disk_free_bytes: number | null;
  last_heartbeat: string;
  started_at: string;
  last_error: string | null;
};

/** `GET /api/system/status`：只有文件任务计数、最老 pending 等待秒数与在线 Worker 数 */
export type SystemStatus = {
  file_tasks: Partial<Record<TaskStatus, number>>;
  pending_oldest_seconds: number | null;
  workers_online: number;
};

/**
 * `GET /api/system/overview`（后端 M2a Task 5 新增）：作业状态分布、文件任务状态分布、
 * 24 小时命中数、Worker 在线数与版本分布、最老 pending 等待秒数。
 * 字段已在合流后的后端实测一致（`service/src/api/v1/schemas.py` 的 SystemOverviewRead）。
 */
export type Overview = {
  jobs: Partial<Record<JobStatus, number>>;
  file_tasks: Partial<Record<TaskStatus, number>>;
  matches_24h: number;
  workers: { online: number; by_version: Record<string, number> };
  oldest_pending_seconds: number | null;
};

/**
 * `POST /api/system/maintenance`：`recovered` 回收的悬挂文件任务数、
 * `workers_demoted` 降级为离线的 Worker 数、`jobs_finalized` 被收尾的作业数。
 * 后端在队列不是 PostgreSQL 时返回 503（回收语句是 PostgreSQL 专有语法）。
 */
export type MaintenanceResult = {
  recovered: number;
  workers_demoted: number;
  jobs_finalized: number;
};

/** `DataSourceRead`（service/src/api/v1/schemas.py） */
export type DataSource = {
  id: number;
  name: string;
  /** 后端当前固定为 `mount`（企业磁盘挂载点） */
  type: string;
  root_path: string;
  read_only: boolean;
  copy_to_local: boolean;
  enabled: boolean;
  description: string;
  created_at: string;
};

/** `RuleSetRead`：列表接口不返回 `config` 与 `created_by` */
export type RuleSet = {
  id: number;
  name: string;
  version: number;
  created_at: string;
};

/** `RuleSetDetailRead`：详情接口才带完整配置与创建者 */
export type RuleSetDetail = RuleSet & {
  config: RuleSetConfig;
  created_by: string;
};

/** 与后端 `src/domain/rules/models.py` 的 `RuleSetConfig` 对齐 */
export type RuleSetConfig = {
  name: string;
  file_glob: string;
  encoding: string;
  fallback_encoding: string;
  rules: Rule[];
};

export type Rule = {
  id: string;
  name: string;
  enabled: boolean;
  severity: string;
  tags: string[];
  match: KeywordMatch | RegexMatch;
  threshold: Threshold | null;
};

export type KeywordMatch = {
  type: "keyword";
  any: string[];
  all: string[];
  case_sensitive: boolean;
};

export type RegexMatch = {
  type: "regex";
  pattern: string;
  flags: string;
  timeout_ms: number;
};

export type Threshold = {
  field: string;
  op: "gt" | "gte" | "lt" | "lte" | "eq";
  value: string | number;
};

/** `POST /api/data-sources/{id}/test` */
export type DataSourceTestResult = {
  exists: boolean;
  is_dir: boolean;
  readable: boolean;
};

/** `DataSourceCreate`：`read_only` 后端是 `Literal[True]`，只接受 `true` */
export type DataSourceCreatePayload = {
  name: string;
  root_path: string;
  read_only: true;
  description?: string;
};

/** `RuleSetCreate`：同名会自动 +1 生成新版本（后端 max+1） */
export type RuleSetCreatePayload = {
  name: string;
  config: RuleSetConfig;
};

/** Worker 管理写接口的返回（drain / resume 共用） */
export type WorkerActionRecord = {
  id: string;
  drain_requested: boolean;
  status: WorkerStatus;
};

/** `POST /api/workers/drain-version` */
export type DrainVersionResult = { drained: number; flagged: number };

/**
 * SSE `event: progress` 载荷，与 `service/src/api/v1/routes_events.py` 的快照一致：
 * `total` 是该作业文件任务总数，`by_status` 是按状态的数量。
 */
export type ProgressEvent = {
  job_id: number;
  status: JobStatus;
  total: number;
  by_status: Record<string, number>;
  processed_lines: number;
  matched_rows: number;
};

/** 终态作业状态（取消按钮等据此禁用） */
export const TERMINAL_JOB_STATUSES: JobStatus[] = [
  "succeeded",
  "partial",
  "failed",
  "cancelled"
];

export const JOB_STATUS_OPTIONS: JobStatus[] = [
  "pending",
  "running",
  "succeeded",
  "partial",
  "failed",
  "cancelled"
];

export const SEVERITY_OPTIONS = ["info", "warning", "error"] as const;
