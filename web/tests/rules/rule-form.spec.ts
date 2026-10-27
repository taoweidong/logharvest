import { describe, expect, it } from "vitest";
import {
  fromRule,
  newRule,
  rowError,
  splitList,
  toRule,
  validateRules
} from "@/views/rules/maintenance/rule-form";
import type { Rule } from "@/api/logharvestTypes";

const keywordRule: Rule = {
  id: "err-500",
  name: "错误关键字",
  enabled: true,
  severity: "error",
  tags: ["prod", "api"],
  match: { type: "keyword", any: ["FATAL", "ERROR"], all: [], case_sensitive: false },
  threshold: null
};

const regexRule: Rule = {
  id: "cpu-high",
  name: "CPU 超阈值",
  enabled: true,
  severity: "warning",
  tags: [],
  match: {
    type: "regex",
    pattern: "cpu=(?P<cpu>\\d+)",
    flags: "i",
    timeout_ms: 80
  },
  threshold: { field: "cpu", op: "gt", value: "90" }
};

describe("splitList", () => {
  it("按换行/中英逗号拆分并去空", () => {
    expect(splitList("a, b\n\nc，")).toEqual(["a", "b", "c"]);
  });
});

describe("toRule/fromRule 往返", () => {
  it("keyword 行：any 模式写进 any，all 恒为空数组（互斥不变量）", () => {
    const row = { ...newRule(), id: "k1", keywordsText: "a\nb", kwMode: "any" as const };
    const rule = toRule(row);
    expect(rule.match).toEqual({
      type: "keyword",
      any: ["a", "b"],
      all: [],
      case_sensitive: false
    });
  });

  it("keyword all 模式写进 all；大小写敏感透传", () => {
    const row = {
      ...newRule(),
      id: "k2",
      kwMode: "all" as const,
      keywordsText: "x,y",
      caseSensitive: true
    };
    const match = toRule(row).match;
    expect(match).toMatchObject({ type: "keyword", all: ["x", "y"], any: [], case_sensitive: true });
  });

  it("regex 行：flags 只取 i/m/s 且按序拼接；timeout 透传", () => {
    const row = {
      ...newRule(),
      matchType: "regex" as const,
      pattern: "ERROR.*",
      flagI: true,
      flagM: false,
      flagS: true,
      timeoutMs: 120
    };
    const match = toRule(row).match;
    expect(match).toEqual({
      type: "regex",
      pattern: "ERROR.*",
      flags: "is",
      timeout_ms: 120
    });
  });

  it("threshold 只在开关打开时携带，字段名做 trim", () => {
    const row = {
      ...newRule(),
      matchType: "regex" as const,
      pattern: "(?P<v>\\d+)",
      useThreshold: true,
      thresholdField: " v ",
      thresholdOp: "gte" as const,
      thresholdValue: " 12.5 "
    };
    const rule = toRule(row);
    expect(rule.threshold).toEqual({ field: "v", op: "gte", value: "12.5" });
    expect(toRule({ ...row, useThreshold: false }).threshold).toBeNull();
  });

  it("fromRule 还原 keyword 行：all 非空时选 all 模式", () => {
    const row = fromRule({
      ...keywordRule,
      match: { type: "keyword", any: [], all: ["a", "b"], case_sensitive: true }
    });
    expect(row.kwMode).toBe("all");
    expect(row.keywordsText).toBe("a\nb");
    expect(row.caseSensitive).toBe(true);
  });

  it("fromRule 还原 regex 行：从 flags 拼回三个布尔", () => {
    const row = fromRule(regexRule);
    expect(row.matchType).toBe("regex");
    expect(row.pattern).toBe("cpu=(?P<cpu>\\d+)");
    expect(row.flagI).toBe(true);
    expect(row.flagM).toBe(false);
    expect(row.flagS).toBe(false);
    expect(row.timeoutMs).toBe(80);
    expect(row.useThreshold).toBe(true);
    expect(row.thresholdValue).toBe("90");
  });

  it("toRule ∘ fromRule 对 keyword/regex 两类规则保持往返稳定", () => {
    expect(toRule(fromRule(keywordRule))).toEqual(keywordRule);
    expect(toRule(fromRule(regexRule))).toEqual(regexRule);
  });
});

describe("rowError / validateRules 前端校验闸门", () => {
  it("id 必填；keyword 必须有关键词；regex 必须有 pattern", () => {
    expect(rowError({ ...newRule(), id: "" })).toContain("id 必填");
    expect(rowError({ ...newRule(), id: "a" })).toContain("不能为空");
    expect(
      rowError({ ...newRule(), id: "a", matchType: "regex", pattern: "" })
    ).toContain("正则模式不能为空");
  });

  it("timeout 必须为正；阈值依赖正则", () => {
    expect(
      rowError({
        ...newRule(),
        id: "a",
        matchType: "regex",
        pattern: "x",
        timeoutMs: 0
      })
    ).toContain("大于 0");
    expect(
      rowError({
        ...newRule(),
        id: "a",
        keywordsText: "ERROR",
        useThreshold: true
      })
    ).toContain("阈值依赖正则命名分组");
    expect(
      rowError({
        ...newRule(),
        id: "a",
        matchType: "regex",
        pattern: "(?P<v>\\d+)",
        useThreshold: true,
        thresholdField: ""
      })
    ).toContain("字段名");
  });

  it("整表校验：空表、重复 id、行级错误都要拦下", () => {
    expect(validateRules([])).toContain("至少需要一条规则");
    expect(validateRules([{ ...newRule(), id: "dup" }, { ...newRule(), id: "dup" }])).toContain(
      "不能重复"
    );
    expect(validateRules([{ ...newRule(), id: "" }])).toContain("id 必填");

    const good: ReturnType<typeof newRule> = {
      ...newRule(),
      id: "ok",
      keywordsText: "ERROR"
    };
    expect(validateRules([good])).toBeNull();
  });
});
