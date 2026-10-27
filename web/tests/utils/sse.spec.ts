import { describe, expect, it, vi } from "vitest";

// sse.ts 从 auth 拿 token；store 全家桶在单测里用假 hook 隔离。
vi.mock("@/utils/auth", () => ({
  getToken: () => ({ accessToken: "token-1" })
}));

import { parseSseChunk, streamJobProgress } from "@/utils/sse";
import type { ProgressEvent } from "@/api/logharvestTypes";

const event = (overrides: Partial<ProgressEvent> = {}): ProgressEvent =>
  {
    return {
      job_id: 1,
      status: "running",
      total: 10,
      by_status: { running: 10 },
      processed_lines: 5,
      matched_rows: 1,
      ...overrides
    } as ProgressEvent;
  };

describe("parseSseChunk", () => {
  it("解析以空行结尾的完整帧", () => {
    const { events, rest } = parseSseChunk(
      `event: progress\ndata: ${JSON.stringify(event())}\n\n`
    );
    expect(events).toHaveLength(1);
    expect(events[0].status).toBe("running");
    expect(rest).toBe("");
  });

  it("一次解析多帧，保留顺序", () => {
    const payload = [
      `event: progress\ndata: ${JSON.stringify(event({ status: "pending" }))}\n\n`,
      `event: progress\ndata: ${JSON.stringify(event({ status: "running" }))}\n\n`
    ].join("");
    const { events, rest } = parseSseChunk(payload);
    expect(events.map(item => item.status)).toEqual(["pending", "running"]);
    expect(rest).toBe("");
  });

  it("CRLF 帧分隔符归一后再解析", () => {
    const { events } = parseSseChunk(
      `event: progress\r\ndata: ${JSON.stringify(event())}\r\n\r\n`
    );
    expect(events).toHaveLength(1);
  });

  it("未以空行结尾的残帧放进 rest，不产出半截事件", () => {
    const { events, rest } = parseSseChunk(
      `event: progress\ndata: ${JSON.stringify(event())}\n\nevent: progress\ndata: {"job_id":`
    );
    expect(events).toHaveLength(1);
    expect(rest).toBe(`event: progress\ndata: {"job_id":`);
  });

  it("空缓冲返回空结果", () => {
    expect(parseSseChunk("")).toEqual({ events: [], rest: "" });
  });

  it("没有 data: 行的帧（如注释行）被跳过", () => {
    const { events } = parseSseChunk(": keep-alive\n\n");
    expect(events).toEqual([]);
  });

  it("多行帧只取第一条 data: 行作为载荷", () => {
    const payload = JSON.stringify(event());
    const { events } = parseSseChunk(
      `event: progress\ndata: ${payload}\ndata: ignored\n\n`
    );
    expect(events).toHaveLength(1);
    expect(events[0]).toEqual(event());
  });

  it("载荷不是合法 JSON 时抛错（调用方决定降级策略）", () => {
    expect(() => parseSseChunk("event: progress\ndata: {broken\n\n")).toThrow();
  });
});

describe("streamJobProgress", () => {
  /** 手搓最小 ReadableStream 响应：streamJobProgress 只依赖 ok/body.getReader()。 */
  function fakeResponse(chunks: string[]) {
    const encoder = new TextEncoder();
    const queue = chunks.map(chunk => encoder.encode(chunk));
    return {
      ok: true,
      body: {
        getReader() {
          return {
            read: async () =>
              queue.length
                ? { done: false, value: queue.shift() }
                : { done: true, value: undefined }
          };
        }
      }
    };
  }

  function stubFetch(response: unknown) {
    vi.stubGlobal("fetch", vi.fn(async () => response));
  }

  it("逐帧回调并返回最后一个事件", async () => {
    stubFetch(
      fakeResponse([
        `event: progress\ndata: ${JSON.stringify(event({ status: "running" }))}\n\n`,
        `event: progress\ndata: ${JSON.stringify(event({ status: "succeeded" }))}\n\n`
      ])
    );
    const seen: string[] = [];
    const last = await streamJobProgress(1, item => seen.push(item.status), new AbortController().signal);
    expect(seen).toEqual(["running", "succeeded"]);
    expect(last?.status).toBe("succeeded");
  });

  it("服务端最后一帧没有空行结尾时，收尾 flush 仍能解析", async () => {
    stubFetch(
      fakeResponse([`event: progress\ndata: ${JSON.stringify(event({ status: "failed" }))}`])
    );
    const seen: string[] = [];
    const last = await streamJobProgress(1, item => seen.push(item.status), new AbortController().signal);
    expect(seen).toEqual(["failed"]);
    expect(last?.status).toBe("failed");
  });

  it("流在观察到终态前结束：返回 undefined，调用方必须回退轮询", async () => {
    stubFetch(fakeResponse([]));
    const last = await streamJobProgress(1, () => {}, new AbortController().signal);
    expect(last).toBeUndefined();
  });

  it("尾部残留不是完整 JSON 时静默丢弃，不把正常关流变成异常", async () => {
    stubFetch(
      fakeResponse([
        `event: progress\ndata: ${JSON.stringify(event())}\n\n`,
        ": proxy truncated da"
      ])
    );
    const seen: number[] = [];
    const last = await streamJobProgress(1, () => seen.push(1), new AbortController().signal);
    expect(seen).toHaveLength(1);
    expect(last?.status).toBe("running");
  });

  it("非 2xx 抛错（401/404 由调用方提示并回退轮询）", async () => {
    stubFetch({ ok: false, status: 404, body: null });
    await expect(
      streamJobProgress(1, () => {}, new AbortController().signal)
    ).rejects.toThrow("progress stream failed: 404");
  });
});
