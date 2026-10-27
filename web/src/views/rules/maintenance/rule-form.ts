/**
 * 规则维护页的行模型：把后端的判别联合 `Rule` 摊平成可编辑字段，提交时组装回去。
 * 字段语义与 `views/logharvest/rulesets/edit-drawer.vue` 保持同源
 * （`keyword.any|all` 互斥、regex 仅 i/m/s 标志、threshold 依赖正则命名分组）。
 */
import type { KeywordMatch, RegexMatch, Rule, Threshold } from "@/api/logharvestTypes";

export type RuleForm = {
  id: string;
  name: string;
  severity: string;
  enabled: boolean;
  tagsText: string;
  matchType: "keyword" | "regex";
  /** keyword.any 与 keyword.all 互斥，用单选决定关键词写进哪一侧 */
  kwMode: "any" | "all";
  keywordsText: string;
  caseSensitive: boolean;
  pattern: string;
  flagI: boolean;
  flagM: boolean;
  flagS: boolean;
  timeoutMs: number;
  useThreshold: boolean;
  thresholdField: string;
  thresholdOp: "gt" | "gte" | "lt" | "lte" | "eq";
  thresholdValue: string;
};

export const SEVERITIES = ["info", "warning", "error"];
export const THRESHOLD_OPS: Threshold["op"][] = ["gt", "gte", "lt", "lte", "eq"];

export function newRule(): RuleForm {
  return {
    id: "",
    name: "",
    severity: "info",
    enabled: true,
    tagsText: "",
    matchType: "keyword",
    kwMode: "any",
    keywordsText: "",
    caseSensitive: false,
    pattern: "",
    flagI: false,
    flagM: false,
    flagS: false,
    timeoutMs: 50,
    useThreshold: false,
    thresholdField: "",
    thresholdOp: "gt",
    thresholdValue: ""
  };
}

export function splitList(text: string): string[] {
  return text
    .split(/[\n,，]/)
    .map(item => item.trim())
    .filter(Boolean);
}

export function toRule(row: RuleForm): Rule {
  const keywords = splitList(row.keywordsText);
  const match: KeywordMatch | RegexMatch =
    row.matchType === "keyword"
      ? {
          type: "keyword",
          any: row.kwMode === "any" ? keywords : [],
          all: row.kwMode === "all" ? keywords : [],
          case_sensitive: row.caseSensitive
        }
      : {
          type: "regex",
          pattern: row.pattern,
          flags: `${row.flagI ? "i" : ""}${row.flagM ? "m" : ""}${row.flagS ? "s" : ""}`,
          timeout_ms: row.timeoutMs
        };
  return {
    id: row.id.trim(),
    name: row.name,
    enabled: row.enabled,
    severity: row.severity,
    tags: splitList(row.tagsText),
    match,
    threshold: row.useThreshold
      ? {
          field: row.thresholdField.trim(),
          op: row.thresholdOp,
          // 后端字段是 Decimal，字符串形式避免精度丢失
          value: row.thresholdValue.trim()
        }
      : null
  };
}

export function fromRule(rule: Rule): RuleForm {
  const base = newRule();
  base.id = rule.id;
  base.name = rule.name;
  base.severity = rule.severity;
  base.enabled = rule.enabled;
  base.tagsText = rule.tags.join(", ");
  if (rule.match.type === "keyword") {
    base.matchType = "keyword";
    base.kwMode = rule.match.all.length ? "all" : "any";
    base.keywordsText = (rule.match.all.length
      ? rule.match.all
      : rule.match.any
    ).join("\n");
    base.caseSensitive = rule.match.case_sensitive;
  } else {
    base.matchType = "regex";
    base.pattern = rule.match.pattern;
    base.flagI = rule.match.flags.includes("i");
    base.flagM = rule.match.flags.includes("m");
    base.flagS = rule.match.flags.includes("s");
    base.timeoutMs = rule.match.timeout_ms;
  }
  if (rule.threshold) {
    base.useThreshold = true;
    base.thresholdField = rule.threshold.field;
    base.thresholdOp = rule.threshold.op;
    base.thresholdValue = String(rule.threshold.value);
  }
  return base;
}

/**
 * 单行的互斥/必填校验（逐行标红用）。
 * 后端编译/pathspec/codec 校验仍是唯一裁判，这里只拦前端能确定的错误。
 */
export function rowError(row: RuleForm): string | null {
  if (!row.id.trim()) return "规则 id 必填";
  if (row.matchType === "keyword" && !splitList(row.keywordsText).length)
    return `关键词（${row.kwMode === "any" ? "任一命中" : "全部命中"}）不能为空`;
  if (row.matchType === "regex" && !row.pattern.trim())
    return "正则模式不能为空";
  if (row.timeoutMs <= 0) return "超时须大于 0 毫秒";
  if (row.useThreshold && row.matchType === "keyword")
    return "阈值依赖正则命名分组，关键字规则没有可提取字段";
  if (row.useThreshold && !row.thresholdField.trim())
    return "请填写阈值的字段名（正则命名分组）";
  return null;
}

/** 整表校验：行级问题 + 规则 id 全局唯一 */
export function validateRules(rows: RuleForm[]): string | null {
  if (!rows.length) return "至少需要一条规则";
  const ids = rows.map(row => row.id.trim());
  if (new Set(ids).size !== ids.length) return "规则 id 不能重复";
  for (const row of rows) {
    const error = rowError(row);
    if (error) return `规则 ${row.id.trim() || "(未命名)"}：${error}`;
  }
  return null;
}
