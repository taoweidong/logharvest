import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/utils/http", () => ({
  http: { request: vi.fn() }
}));

import { http } from "@/utils/http";
import {
  cancelJob,
  createJob,
  drainVersion,
  drainWorker,
  getJob,
  listJobFiles,
  listJobs,
  listMatches,
  listTaskAttempts,
  listWorkers,
  overview,
  resumeWorker,
  retryFailedFiles,
  runMaintenance,
  systemStatus
} from "@/api/logharvest";
import {
  createDataSource,
  createRuleSet,
  getRuleSet,
  listDataSources,
  listRuleSets,
  testDataSource
} from "@/api/logharvestAdmin";

const request = vi.mocked(http.request);

describe("作业/命中/Worker 客户端契约", () => {
  beforeEach(() => {
    request.mockClear();
  });

  it("列表读取使用 GET + 路径 + 分页参数", () => {
    listJobs({ status: "failed", limit: 20, offset: 40 });
    expect(request).toHaveBeenLastCalledWith("get", "/jobs", {
      params: { status: "failed", limit: 20, offset: 40 }
    });

    listJobFiles(7, { status: "succeeded", limit: 10 });
    expect(request).toHaveBeenLastCalledWith("get", "/jobs/7/files", {
      params: { status: "succeeded", limit: 10 }
    });

    listWorkers({ limit: 200 });
    expect(request).toHaveBeenLastCalledWith("get", "/workers", {
      params: { limit: 200 }
    });
  });

  it("详情/尝试历史/取消/重试/维护/概览路径", () => {
    getJob(3);
    expect(request).toHaveBeenLastCalledWith("get", "/jobs/3");

    listTaskAttempts(3, 9);
    expect(request).toHaveBeenLastCalledWith("get", "/jobs/3/files/9/attempts");

    cancelJob(3);
    expect(request).toHaveBeenLastCalledWith("post", "/jobs/3/cancel");

    retryFailedFiles(3);
    expect(request).toHaveBeenLastCalledWith("post", "/jobs/3/files/retry-failed");

    runMaintenance();
    expect(request).toHaveBeenLastCalledWith("post", "/system/maintenance");

    systemStatus();
    expect(request).toHaveBeenLastCalledWith("get", "/system/status");

    overview();
    expect(request).toHaveBeenLastCalledWith("get", "/system/overview");
  });

  it("createJob 带幂等键，并把客户端超时放大到 60s", () => {
    const payload = {
      name: "j",
      data_source_id: 1,
      source_path: "/nas/logs",
      rule_set_id: 2
    };
    createJob(payload, "idem-1");
    expect(request).toHaveBeenLastCalledWith("post", "/jobs", {
      data: payload,
      headers: { "Idempotency-Key": "idem-1" },
      timeout: 60000
    });
  });

  it("命中查询原样传递游标参数", () => {
    listMatches({
      job_id: 4,
      created_before: "2026-10-29T00:00:00.000Z",
      id_before: 99,
      limit: 200
    });
    expect(request).toHaveBeenLastCalledWith("get", "/matches", {
      params: {
        job_id: 4,
        created_before: "2026-10-29T00:00:00.000Z",
        id_before: 99,
        limit: 200
      }
    });
  });

  it("Worker 动作路径使用 POST，按版本排空把 version 放进 query", () => {
    drainWorker("w-1");
    expect(request).toHaveBeenLastCalledWith("post", "/workers/w-1/drain");

    resumeWorker("w-1");
    expect(request).toHaveBeenLastCalledWith("post", "/workers/w-1/resume");

    drainVersion("0.2.0");
    expect(request).toHaveBeenLastCalledWith("post", "/workers/drain-version", {
      params: { version: "0.2.0" }
    });
  });
});

describe("数据源/规则集客户端契约", () => {
  beforeEach(() => {
    request.mockClear();
  });

  it("列表默认分页与详情路径", () => {
    listDataSources();
    expect(request).toHaveBeenLastCalledWith("get", "/data-sources", {
      params: { limit: 500, offset: 0 }
    });

    listRuleSets();
    expect(request).toHaveBeenLastCalledWith("get", "/rule-sets", {
      params: { limit: 20, offset: 0 }
    });

    getRuleSet(8);
    expect(request).toHaveBeenLastCalledWith("get", "/rule-sets/8");
  });

  it("创建/测试使用 POST 且携带请求体", () => {
    const dataSource = {
      name: "nas",
      type: "mount",
      root_path: "/nas",
      read_only: true,
      copy_to_local: true
    };
    createDataSource(dataSource);
    expect(request).toHaveBeenLastCalledWith("post", "/data-sources", {
      data: dataSource
    });

    testDataSource(5);
    expect(request).toHaveBeenLastCalledWith("post", "/data-sources/5/test");

    const ruleSet = {
      name: "rs",
      config: {
        name: "rs",
        file_glob: "**/*.log",
        encoding: "utf-8",
        fallback_encoding: "gb18030",
        rules: []
      }
    };
    createRuleSet(ruleSet);
    expect(request).toHaveBeenLastCalledWith("post", "/rule-sets", { data: ruleSet });
  });
});
