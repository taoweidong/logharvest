import { describe, expect, it } from "vitest";
import {
  countOrUnknown,
  formatCpuPercent,
  formatDateTime,
  formatDetailError,
  formatDiskFreeGB,
  formatDuration,
  formatGB,
  formatMB,
  formatMemoryMB,
  severityTagType,
  statusTagType,
  textOrUnknown,
  toApiDateTime,
  workerStatusTagType
} from "@/views/logharvest/format";

describe("textOrUnknown", () => {
  it("将 null/undefined/空串 显示为未知占位", () => {
    expect(textOrUnknown(null)).toBe("—");
    expect(textOrUnknown(undefined)).toBe("—");
    expect(textOrUnknown("")).toBe("—");
  });

  it("保留真实文本（含 0 字符串与空白串）", () => {
    expect(textOrUnknown("worker-1")).toBe("worker-1");
    expect(textOrUnknown("0")).toBe("0");
    expect(textOrUnknown(" ")).toBe(" ");
  });

  it("数字会被字符串化", () => {
    expect(textOrUnknown(42)).toBe("42");
  });
});

describe("countOrUnknown", () => {
  it("null/undefined/非有限数 一律未知，绝不退化成 0", () => {
    expect(countOrUnknown(null)).toBe("—");
    expect(countOrUnknown(undefined)).toBe("—");
    expect(countOrUnknown(Number.NaN)).toBe("—");
    expect(countOrUnknown(Number.POSITIVE_INFINITY)).toBe("—");
  });

  it("真实数字正常显示", () => {
    expect(countOrUnknown(0)).toBe("0");
    expect(countOrUnknown(12345)).toBe("12345");
  });
});

describe("formatCpuPercent", () => {
  it("null/undefined/非有限数 显示未知", () => {
    expect(formatCpuPercent(null)).toBe("—");
    expect(formatCpuPercent(undefined)).toBe("—");
    expect(formatCpuPercent(Number.NaN)).toBe("—");
  });

  it("保留一位小数并带百分号；多核 >100 不截断", () => {
    expect(formatCpuPercent(37.5)).toBe("37.5%");
    expect(formatCpuPercent(0)).toBe("0.0%");
    expect(formatCpuPercent(321.4)).toBe("321.4%");
  });
});

describe("formatDateTime 的 UTC 补 Z 契约", () => {
  it("无时区的时间串按 UTC 解析（东八区显示 +8h）", () => {
    // 2026-10-28T00:00:00Z 在 UTC+8 是 08:00:00
    process.env.TZ = "Asia/Shanghai";
    expect(formatDateTime("2026-10-28T00:00:00")).toBe("2026-10-28 08:00:00");
    expect(formatDateTime("2026-10-28 00:00:00")).toBe("2026-10-28 08:00:00");
  });

  it("已带偏移/Z 的时间串不再补 Z", () => {
    process.env.TZ = "Asia/Shanghai";
    expect(formatDateTime("2026-10-28T00:00:00Z")).toBe("2026-10-28 08:00:00");
    expect(formatDateTime("2026-10-28T00:00:00+02:00")).toBe("2026-10-28 06:00:00");
  });

  it("空值显示 -", () => {
    expect(formatDateTime(null)).toBe("-");
    expect(formatDateTime(undefined)).toBe("-");
    expect(formatDateTime("")).toBe("-");
  });
});

describe("toApiDateTime", () => {
  it("空值与非法日期丢弃（返回 undefined，让请求参数省略该字段）", () => {
    expect(toApiDateTime(null)).toBeUndefined();
    expect(toApiDateTime(undefined)).toBeUndefined();
    expect(toApiDateTime("")).toBeUndefined();
    expect(toApiDateTime("not-a-date")).toBeUndefined();
  });

  it("合法值输出 ISO UTC（带 Z 与毫秒）", () => {
    process.env.TZ = "Asia/Shanghai";
    expect(toApiDateTime("2026-10-28T08:00:00")).toBe("2026-10-28T00:00:00.000Z");
    expect(toApiDateTime(new Date("2026-10-28T00:00:00Z"))).toBe("2026-10-28T00:00:00.000Z");
  });
});

describe("formatDetailError（422 多行展开）", () => {
  it("字符串 detail 原样返回", () => {
    expect(formatDetailError({ response: { data: { detail: "boom" } } })).toBe("boom");
  });

  it("422 数组展开为多行「loc: msg」", () => {
    const error = {
      response: {
        data: {
          detail: [
            { loc: ["body", "name"], msg: "at least 1 character" },
            { loc: ["body", "file_filter", "max_depth"], msg: "less or equal to 20" }
          ]
        }
      }
    };
    expect(formatDetailError(error)).toBe(
      "name: at least 1 character\nfile_filter.max_depth: less or equal to 20"
    );
  });

  it("无 detail 时退回 fallback", () => {
    expect(formatDetailError({}, "规则保存失败")).toBe("规则保存失败");
  });
});

describe("formatGB / formatMB / formatDuration 的空值与换算", () => {
  it("null/undefined 显示 -（与「真实的 0 GB」可区分）", () => {
    expect(formatGB(null)).toBe("-");
    expect(formatGB(undefined)).toBe("-");
    expect(formatMB(undefined)).toBe("-");
    expect(formatDuration(null)).toBe("-");
  });

  it("换算保留两位（GB）/（MB）", () => {
    expect(formatGB(2 * 1024 ** 3)).toBe("2.00 GB");
    expect(formatGB(1024 ** 3 + 512 * 1024 ** 2)).toBe("1.50 GB");
    expect(formatMB(512 * 1024 * 1024)).toBe("512.00 MB");
  });

  it("formatDuration 分段进位", () => {
    expect(formatDuration(59)).toBe("59 秒");
    expect(formatDuration(60)).toBe("1 分");
    expect(formatDuration(3660)).toBe("1 小时 1 分");
  });
});

describe("状态标签的未知值兜底", () => {
  it("statusTagType/workerStatusTagType/severityTagType 未知值降级为 info", () => {
    expect(statusTagType("succeeded")).toBe("success");
    expect(statusTagType("future-status")).toBe("info");
    expect(workerStatusTagType("draining")).toBe("warning");
    expect(workerStatusTagType("unknown-status")).toBe("info");
    expect(severityTagType("error")).toBe("danger");
    expect(severityTagType("critical")).toBe("info");
  });
});

describe("formatDiskFreeGB 与 formatMemoryMB", () => {
  it("未知值与换算", () => {
    expect(formatDiskFreeGB(null)).toBe("—");
    expect(formatDiskFreeGB(3 * 1024 ** 3)).toBe("3.00 GB");
    expect(formatMemoryMB(2 * 1024 * 1024)).toBe("2.0 MB");
    expect(formatMemoryMB(null)).toBe("—");
  });
});
