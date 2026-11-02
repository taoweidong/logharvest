import { describe, expect, it, vi } from "vitest";

// apiErrorDetail/apiErrorStatus 是纯函数，但模块顶层的 http 实例会连带
// user store / i18n / router；单测里用空实现切断依赖链。
vi.mock("@/utils/http", () => ({ http: {} }));

import {
  apiErrorDetail,
  apiErrorStatus,
  responseDetail
} from "@/api/errors";
// re-export 路径必须与直接导入行为一致：视图都从 @/api/logharvest 引入。
import {
  apiErrorDetail as reexportedDetail,
  apiErrorStatus as reexportedStatus
} from "@/api/logharvest";

describe("apiErrorDetail", () => {
  it("字符串 detail 原样返回", () => {
    const error = { response: { data: { detail: "job not found" } } };
    expect(apiErrorDetail(error)).toBe("job not found");
  });

  it("pydantic 校验错误数组取第一条的 msg", () => {
    const error = {
      response: {
        data: {
          detail: [
            { loc: ["body", "name"], msg: "String should have at least 1 character" },
            { loc: ["body", "rule_set_id"], msg: "Input should be a valid integer" }
          ]
        }
      }
    };
    expect(apiErrorDetail(error)).toBe("String should have at least 1 character");
  });

  it("数组元素缺 msg 时退回 detail 字段", () => {
    const error = {
      response: { data: { detail: [{ loc: ["body"], detail: "fallback text" }] } }
    };
    expect(apiErrorDetail(error)).toBe("fallback text");
  });

  it("空数组/无响应/字段缺失时返回 fallback", () => {
    expect(apiErrorDetail({ response: { data: { detail: [] } } }, "保存失败")).toBe(
      "保存失败"
    );
    expect(apiErrorDetail(new Error("network down"), "保存失败")).toBe("保存失败");
    expect(apiErrorDetail(undefined, "保存失败")).toBe("保存失败");
  });

  it("默认 fallback 是「请求失败」", () => {
    expect(apiErrorDetail({})).toBe("请求失败");
  });
});

describe("apiErrorStatus", () => {
  it("有响应时返回状态码", () => {
    expect(apiErrorStatus({ response: { status: 404 } })).toBe(404);
  });

  it("断网/超时（无 response）返回 undefined，供文案区分", () => {
    expect(apiErrorStatus(new Error("timeout"))).toBeUndefined();
    expect(apiErrorStatus({ response: {} })).toBeUndefined();
  });
});

describe("re-export 一致性（@/api/logharvest 与 @/api/errors）", () => {
  it("两个入口解析到同一实现", () => {
    expect(reexportedDetail).toBe(apiErrorDetail);
    expect(reexportedStatus).toBe(apiErrorStatus);
  });
});

describe("responseDetail（契约读取点）", () => {
  it("返回 detail 原始值：字符串、数组、缺失三种形态", () => {
    expect(responseDetail({ response: { data: { detail: "boom" } } })).toBe("boom");
    expect(responseDetail({ response: { data: { detail: [{ msg: "m" }] } } })).toEqual([
      { msg: "m" }
    ]);
    expect(responseDetail({ response: { data: {} } })).toBeUndefined();
    expect(responseDetail(undefined)).toBeUndefined();
  });
});
