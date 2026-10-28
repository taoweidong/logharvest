import { describe, expect, it } from "vitest";
import {
  JOB_STATUS_OPTIONS,
  SEVERITY_OPTIONS,
  TERMINAL_JOB_STATUSES
} from "@/api/logharvestTypes";
import { SEVERITY_TAG } from "@/views/logharvest/format";

describe("作业状态常量", () => {
  it("终态列表是全部状态的无重复子集", () => {
    expect(new Set(JOB_STATUS_OPTIONS).size).toBe(JOB_STATUS_OPTIONS.length);
    expect(new Set(TERMINAL_JOB_STATUSES).size).toBe(TERMINAL_JOB_STATUSES.length);
    for (const status of TERMINAL_JOB_STATUSES) {
      expect(JOB_STATUS_OPTIONS).toContain(status);
    }
  });

  it("非终态仍在全量列表里", () => {
    expect(JOB_STATUS_OPTIONS).toEqual([
      "pending",
      "running",
      "succeeded",
      "partial",
      "failed",
      "cancelled"
    ]);
    expect(TERMINAL_JOB_STATUSES).not.toContain("pending");
    expect(TERMINAL_JOB_STATUSES).not.toContain("running");
  });
});

describe("严重度常量", () => {
  it("选项与颜色映射键一致", () => {
    expect([...SEVERITY_OPTIONS].sort()).toEqual(Object.keys(SEVERITY_TAG).sort());
  });
});
