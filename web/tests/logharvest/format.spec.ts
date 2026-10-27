import { describe, expect, it } from "vitest";
import { countOrUnknown, formatCpuPercent, textOrUnknown } from "@/views/logharvest/format";

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
