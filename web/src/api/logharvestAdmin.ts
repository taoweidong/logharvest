/**
 * 规则集与数据源的维护客户端（M2b Task 5）。
 * 后端契约（`service/src/api/v1/routes_rule_sets.py`、`routes_data_sources.py`）：
 * - 列表都是裸数组 + `limit`/`offset`，无 `total`
 * - `POST /rule-sets` 同名生成新版本（max+1）；校验失败 422、版本冲突 409，错误体 `{"detail": ...}`
 * - `POST /data-sources` 名称冲突 409；`read_only` 只接受 `true`
 * - `POST /data-sources/{id}/test` 返回 `{exists, is_dir, readable}` 三个布尔
 */
import { http } from "@/utils/http";
import type { ListParams } from "./logharvest";
import type {
  DataSource,
  DataSourceCreatePayload,
  DataSourceTestResult,
  RuleSet,
  RuleSetCreatePayload,
  RuleSetDetail
} from "./logharvestTypes";

/**
 * 数据源列表（也供作业创建对话框的下拉使用）。
 * 数据源数量是个位数，默认一次取满一页 500 条。
 */
export const listDataSources = (params: ListParams = { limit: 500, offset: 0 }) =>
  http.request<DataSource[]>("get", "/data-sources", { params });

export const createDataSource = (data: DataSourceCreatePayload) =>
  http.request<DataSource>("post", "/data-sources", { data });

export const testDataSource = (id: number) =>
  http.request<DataSourceTestResult>("post", `/data-sources/${id}/test`);

/** 规则集轻量列表（不含 `config`）；规则数需要逐条取详情 */
export const listRuleSets = (params: ListParams = { limit: 20, offset: 0 }) =>
  http.request<RuleSet[]>("get", "/rule-sets", { params });

export const getRuleSet = (id: number) =>
  http.request<RuleSetDetail>("get", `/rule-sets/${id}`);

/** 新建规则集（同名版本 +1）；成功返回 `RuleSetRead`（含新 `version`） */
export const createRuleSet = (data: RuleSetCreatePayload) =>
  http.request<RuleSet>("post", "/rule-sets", { data });
