import { beforeEach, describe, expect, it, vi } from "vitest";

// 只验证"客户端函数把哪些参数发给 http"，不发真实请求。
// vi.mock 工厂会被提升到文件顶部，捕获的 request 必须经 vi.hoisted 声明。
const { request } = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock("@/utils/http", () => ({ http: { request } }));

import { listDataSources, listRuleSets } from "@/api/logharvestAdmin";
import { listJobs, listJobFiles, listWorkers } from "@/api/logharvest";
import { listRuleIndex } from "@/api/analytics";

beforeEach(() => {
  request.mockClear();
});

describe("api 默认分页参数契约", () => {
  it("listRuleSets 默认 limit 20（轻量列表，规则数逐条取详情）", async () => {
    await listRuleSets();
    expect(request).toHaveBeenCalledWith(
      "get",
      "/rule-sets",
      expect.objectContaining({ params: { limit: 20, offset: 0 } })
    );
  });

  it("listDataSources 默认一次取满 500（数据源个位数）", async () => {
    await listDataSources();
    expect(request).toHaveBeenCalledWith(
      "get",
      "/data-sources",
      expect.objectContaining({ params: { limit: 500, offset: 0 } })
    );
  });

  it("listJobs 默认不带分页参数（后端默认窗口）", async () => {
    await listJobs();
    expect(request).toHaveBeenCalledWith(
      "get",
      "/jobs",
      expect.objectContaining({ params: {} })
    );
  });

  it("显式传入的参数原样下推（status 筛选必须进请求，不能前端假筛选）", async () => {
    await listJobs({ status: "failed", limit: 10, offset: 20 });
    expect(request).toHaveBeenCalledWith(
      "get",
      "/jobs",
      expect.objectContaining({
        params: { status: "failed", limit: 10, offset: 20 }
      })
    );
  });

  it("listJobFiles 把作业 id 拼进路径、参数进 query", async () => {
    await listJobFiles(7, { status: "pending", limit: 5 });
    expect(request).toHaveBeenCalledWith(
      "get",
      "/jobs/7/files",
      expect.objectContaining({
        params: { status: "pending", limit: 5 }
      })
    );
  });

  it("listWorkers 与 listRuleIndex 默认参数为空对象", async () => {
    await listWorkers();
    await listRuleIndex();
    expect(request).toHaveBeenNthCalledWith(
      1,
      "get",
      "/workers",
      expect.objectContaining({ params: {} })
    );
    expect(request).toHaveBeenNthCalledWith(
      2,
      "get",
      "/rules",
      expect.objectContaining({ params: {} })
    );
  });
});
