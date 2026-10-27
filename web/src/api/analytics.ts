/**
 * 任务统计与跨版本规则索引客户端。
 *
 * 契约（后端并行实现中，形状以本文件类型为准）：
 * - `GET /api/stats/jobs?days=14&data_source_id=`（days 1–90）→ 聚合对象
 * - `GET /api/rules` → 裸数组，无 total，窗口式 limit/offset 翻页
 */
import { http } from "@/utils/http";
import type {
  JobStatsParams,
  JobStatsResult,
  RuleIndexParams,
  RuleIndexRow
} from "./analyticsTypes";

export const getJobStats = (params: JobStatsParams = {}) =>
  http.request<JobStatsResult>("get", "/stats/jobs", { params });

export const listRuleIndex = (params: RuleIndexParams = {}) =>
  http.request<RuleIndexRow[]>("get", "/rules", { params });
