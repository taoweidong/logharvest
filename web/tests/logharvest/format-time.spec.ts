import { afterEach, describe, expect, it, vi } from "vitest";
import {
  formatDateTime,
  formatDetailError,
  formatDiskFreeGB,
  formatDuration,
  formatGB,
  formatMB,
  formatMemoryMB,
  fromNow,
  toApiDateTime
} from "@/views/logharvest/format";

describe("fromNow 的相对时间边界", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("空值显示 -", () => {
    expect(fromNow(null)).toBe("-");
    expect(fromNow(undefined)).toBe("-");
    expect(fromNow("")).toBe("-");
  });

  it("按秒/分/小时/天分段", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-10-29T12:00:00Z"));
    expect(fromNow("2026-10-29T11:59:30Z")).toBe("30 秒前");
    expect(fromNow("2026-10-29T11:58:30Z")).toBe("1 分钟前");
    expect(fromNow("2026-10-29T10:00:00Z")).toBe("2 小时前");
    expect(fromNow("2026-10-26T12:00:00Z")).toBe("3 天前");
  });

  it("无时区串按 UTC 解析，不因宿主时区偏移", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-10-29T12:00:30Z"));
    process.env.TZ = "Asia/Shanghai";
    expect(fromNow("2026-10-29T12:00:00")).toBe("30 秒前");
  });
});

describe("formatDuration 的分段进位边界", () => {
  it("0/59/60/3599/3600/86399", () => {
    expect(formatDuration(0)).toBe("0 秒");
    expect(formatDuration(59)).toBe("59 秒");
    expect(formatDuration(60)).toBe("1 分");
    expect(formatDuration(3599)).toBe("59 分");
    expect(formatDuration(3600)).toBe("1 小时 0 分");
    expect(formatDuration(86399)).toBe("23 小时 59 分");
  });
});

describe("数值格式化的零值不是未知值", () => {
  it("真实的 0 保留单位，null/undefined 才是占位", () => {
    expect(formatGB(0)).toBe("0.00 GB");
    expect(formatMB(0)).toBe("0.00 MB");
    expect(formatDiskFreeGB(0)).toBe("0.00 GB");
    expect(formatMemoryMB(0)).toBe("0.0 MB");
    expect(formatGB(null)).toBe("-");
  });
});

describe("toApiDateTime 的零值/空值", () => {
  it("epoch 0 是合法日期，空值才丢弃", () => {
    expect(toApiDateTime(0)).toBe("1970-01-01T00:00:00.000Z");
    expect(toApiDateTime("")).toBeUndefined();
    expect(toApiDateTime(" ")).toBeUndefined();
  });
});

describe("formatDetailError 的非字符串边界", () => {
  it("空字符串 detail 退回 fallback", () => {
    expect(formatDetailError({ response: { data: { detail: "" } } }, "保存失败")).toBe(
      "保存失败"
    );
  });

  it("对象 detail 退回 fallback；数组元素缺字段时给出兜底文案", () => {
    expect(formatDetailError({ response: { data: { detail: { x: 1 } } } }, "保存失败")).toBe(
      "保存失败"
    );
    expect(
      formatDetailError({ response: { data: { detail: [{ loc: ["body", "name"] }] } } })
    ).toBe("name: 校验失败");
  });
});

describe("formatDateTime 的边界", () => {
  it("带毫秒与偏移的输入不重复补 Z", () => {
    process.env.TZ = "Asia/Shanghai";
    expect(formatDateTime("2026-10-29T00:00:00.000Z")).toBe("2026-10-29 08:00:00");
    expect(formatDateTime("2026-10-29T00:00:00.000+08:00")).toBe("2026-10-29 00:00:00");
  });
});
