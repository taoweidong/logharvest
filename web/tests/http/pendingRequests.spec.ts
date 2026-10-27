import { afterEach, describe, expect, it, vi } from "vitest";
import {
  clearPendingRequests,
  enqueuePendingRequest,
  flushPendingRequests,
  pendingRequestCount,
  rejectPendingRequests
} from "@/utils/http/pendingRequests";

afterEach(() => {
  clearPendingRequests();
});

describe("enqueue/flush/reject 队列契约", () => {
  it("刷新成功：按排队顺序逐个补 token 重放", async () => {
    const replayed: string[] = [];
    const first = enqueuePendingRequest(token => {
      replayed.push(token);
      return `config-first-${token}`;
    });
    const second = enqueuePendingRequest(token => {
      replayed.push(token);
      return `config-second-${token}`;
    });
    expect(pendingRequestCount()).toBe(2);

    flushPendingRequests("new-token");

    await expect(first).resolves.toBe("config-first-new-token");
    await expect(second).resolves.toBe("config-second-new-token");
    expect(replayed).toEqual(["new-token", "new-token"]);
    expect(pendingRequestCount()).toBe(0);
  });

  it("刷新失败：等待者必须被逐个 reject，绝不永久挂起", async () => {
    const first = enqueuePendingRequest(() => "never");
    const second = enqueuePendingRequest(() => "never");

    const reason = new Error("refresh failed: 401");
    rejectPendingRequests(reason);

    await expect(first).rejects.toBe(reason);
    await expect(second).rejects.toBe(reason);
    expect(pendingRequestCount()).toBe(0);
  });

  it("失败后再 flush 不会叫醒已被 reject 的等待者", async () => {
    const pending = enqueuePendingRequest(() => "never");
    rejectPendingRequests(new Error("gone"));
    // 队列已清空，flush 是空操作。
    flushPendingRequests("token");
    await expect(pending).rejects.toBeInstanceOf(Error);
    expect(pendingRequestCount()).toBe(0);
  });

  it("clearPendingRequests 只清空、不通知（测试隔离用）", () => {
    enqueuePendingRequest(() => "x");
    clearPendingRequests();
    expect(pendingRequestCount()).toBe(0);
  });
});
