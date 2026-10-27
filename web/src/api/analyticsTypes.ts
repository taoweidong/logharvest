/**
 * 任务统计与跨版本规则索引接口的响应类型。
 *
 * 与全站业务接口同一契约：纯 REST 对象、错误体 `{"detail": ...}`；
 * `/api/rules` 是列表接口，返回**裸数组无 total**，翻页只能窗口式 limit/offset。
 * 日期口径：`/api/stats/jobs` 按 UTC 日界归日（与后端 `func.date()` 一致），
 * `date` 为 `YYYY-MM-DD` 字符串，页面需标注「UTC 日界」避免与本地日期混淆。
 */

export type JobStatsParams = {
  /** 统计窗口天数，后端限定 1–90，默认 14 */
  days?: number;
  data_source_id?: number;
};

/**
 * totals 里的 `file_succeeded`/`file_failed` 是失败率的分母来源
 * （失败率 = files_failed / (file_succeeded + file_failed)，除零由前端处理）。
 */
export type JobStatsTotals = {
  jobs: number;
  succeeded: number;
  partial: number;
  failed: number;
  cancelled: number;
  running: number;
  files_total: number;
  bytes_total: number;
  matched_rows_total: number;
  file_succeeded: number;
  file_failed: number;
};

export type JobStatsDailyRow = {
  /** UTC 日界日期，YYYY-MM-DD */
  date: string;
  jobs_created: number;
  files_succeeded: number;
  files_failed: number;
  matched_rows: number;
  bytes_total: number;
};

export type JobStatsTopRule = {
  rule_id: string;
  rule_version: number;
  hits: number;
  jobs: number;
};

export type JobStatsResult = {
  days: number;
  data_source_id: number | null;
  totals: JobStatsTotals;
  daily: JobStatsDailyRow[];
  top_rules: JobStatsTopRule[];
};

/** `GET /api/rules` 的过滤参数（后端把各版本 config.rules 展平成只读索引） */
export type RuleIndexParams = {
  /** 匹配 rule.id、正则 pattern、关键字词 */
  keyword?: string;
  type?: "keyword" | "regex";
  rule_set_name?: string;
  limit?: number;
  offset?: number;
};

/** 跨版本检索的单条规则索引 */
export type RuleIndexRow = {
  rule_set_id: number;
  rule_set_name: string;
  version: number;
  rule_id: string;
  type: string;
  severity: string;
  summary: string;
};
