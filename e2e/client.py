"""LogHarvest E2E 公共客户端与断言台账（仅用标准库）。

拆成独立模块的原因：``api_flow.py`` / ``limits_flow.py`` / ``upgrade_flow.py`` 走的是
同一套契约（见 e2e/refs/api-contract.md），断言口径必须一致，报告也要合并成一份。

约定（都来自契约，不要"顺手"改）：
- 只有 login / refresh-token / auth/routes / logout 返回 ``{code,message,data}`` 信封，
  业务接口是裸 JSON。
- 业务错误 ``detail`` 是字符串，框架校验错误的 ``detail`` 是数组，两种 422 形状不同。
- 认证统一 ``Authorization: Bearer <accessToken>``，SSE 也用同一个头。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

E2E_DIR = Path(__file__).resolve().parent
ARTIFACTS_DIR = E2E_DIR / "artifacts"

API_BASE = os.environ.get("E2E_API_BASE", "http://127.0.0.1:18080/api")
# 绕过 nginx 直连 uvicorn 的 base。只用于"必须看到应用原始响应头"的断言：
# nginx 把 X-Accel-* 当成自己的加速指令消费掉、不转给客户端（见 api_flow P6 SSE），
# 走代理断言这些头必然拿到 None，那不是缺陷。业务接口一律仍走 API_BASE。
API_DIRECT = os.environ.get("E2E_API_DIRECT", "http://127.0.0.1:18000/api")
WEB_BASE = os.environ.get("E2E_WEB_BASE", "http://127.0.0.1:18080/")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "E2eAdmin#2026")

TERMINAL_JOB_STATUSES = {"succeeded", "partial", "failed", "cancelled"}


class ApiError(RuntimeError):
    """带状态码与解析后响应体的 HTTP 错误。"""

    def __init__(self, status: int, body: Any, method: str, path: str) -> None:
        self.status = status
        self.body = body
        super().__init__(f"{method} {path} -> HTTP {status}: {json.dumps(body, ensure_ascii=False)[:400]}")

    @property
    def detail(self) -> Any:
        return self.body.get("detail") if isinstance(self.body, dict) else None


class ApiClient:
    """极小的 JSON HTTP 客户端：契约里所有接口都是 JSON，不需要 requests。"""

    def __init__(self, base: str = API_BASE, token: str | None = None, timeout: float = 30.0) -> None:
        self.base = base.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.calls = 0

    # ---- 底层 --------------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: Any = None,
        headers: dict[str, str] | None = None,
        expect_status: int | tuple[int, ...] | None = None,
        raw: bool = False,
        validate: bool = True,
    ) -> Any:
        url = f"{self.base}{path}" if path.startswith("/") else f"{self.base}/{path}"
        data = None
        send = dict(headers or {})
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            send.setdefault("Content-Type", "application/json")
        if self.token:
            send.setdefault("Authorization", f"Bearer {self.token}")
        request = urllib.request.Request(url, data=data, method=method, headers=send)
        self.calls += 1
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status, body_bytes = response.status, response.read()
                headers = {k.lower(): v for k, v in response.headers.items()}
        except urllib.error.HTTPError as exc:
            status, body_bytes = exc.code, exc.read()
            headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
        body: Any
        if raw:
            body = body_bytes.decode("utf-8", errors="replace")
        elif not body_bytes:
            body = None
        else:
            try:
                body = json.loads(body_bytes.decode("utf-8"))
            except json.JSONDecodeError:
                body = body_bytes.decode("utf-8", errors="replace")
        if expect_status is not None:
            allowed = expect_status if isinstance(expect_status, tuple) else (expect_status,)
            if status not in allowed:
                raise ApiError(status, body, method, path)
        elif validate and not 200 <= status < 300:
            # get/post/put/delete 是"裸 2xx"语义：不显式传 expect_status 时也必须拦下非 2xx。
            # 曾经只在校验了 expect_status 时才抛，导致 expect_error(api.get("/jobs"), 401)
            # 把 401 响应体当成成功返回、断言写成"实际成功返回 {...}"，掩盖了真实状态码。
            raise ApiError(status, body, method, path)
        return {"status": status, "body": body, "headers": headers}

    def get(self, path: str, **kw: Any) -> Any:
        """裸 200 语义：非 2xx 抛 ApiError。"""
        return self._request("GET", path, **kw)["body"]

    def post(self, path: str, payload: Any = None, **kw: Any) -> Any:
        return self._request("POST", path, payload=payload, **kw)["body"]

    def put(self, path: str, payload: Any = None, **kw: Any) -> Any:
        return self._request("PUT", path, payload=payload, **kw)["body"]

    def delete(self, path: str, **kw: Any) -> Any:
        return self._request("DELETE", path, **kw)["body"]

    def call(self, method: str, path: str, payload: Any = None, **kw: Any) -> dict[str, Any]:
        """不校验状态码，返回 {status, body}，用于负面用例。"""
        return self._request(method, path, payload=payload, validate=False, **kw)

    # ---- 认证 --------------------------------------------------------------
    def login(self, username: str, password: str) -> str:
        """登录并返回 accessToken（信封只在认证系列出现）。"""
        body = self.post("/login", {"username": username, "password": password}, expect_status=200)
        token = body["data"]["accessToken"]
        self.token = token
        return token

    def refresh(self, refresh_token: str) -> dict[str, Any]:
        body = self.post(
            "/refresh-token", {"refreshToken": refresh_token}, expect_status=200
        )
        return body["data"]

    # ---- SSE ---------------------------------------------------------------
    def job_events(self, job_id: int, *, max_events: int = 100) -> Iterator[dict[str, Any]]:
        """逐帧解析 ``event: progress`` 流；终态帧后服务端自然结束流。"""
        url = f"{self.base}/jobs/{job_id}/events?max_events={max_events}"
        request = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self.token}", "Accept": "text/event-stream"}
        )
        with urllib.request.urlopen(request, timeout=180) as response:
            event_name: str | None = None
            data_lines: list[str] = []
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line:
                    if data_lines:
                        payload = json.loads("\n".join(data_lines))
                        if event_name in (None, "progress"):
                            yield payload
                    event_name, data_lines = None, []
                    continue
                if line.startswith("event:"):
                    event_name = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    data_lines.append(line.split(":", 1)[1].strip())

    # ---- 轮询 --------------------------------------------------------------
    def wait_job_terminal(self, job_id: int, *, timeout: float = 900.0, interval: float = 2.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.get(f"/jobs/{job_id}", expect_status=200)
            if job["status"] in TERMINAL_JOB_STATUSES:
                return job
            time.sleep(interval)
        raise TimeoutError(f"job {job_id} 未在 {timeout}s 内进入终态（最后状态 {job['status']}）")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    phase: str = ""


@dataclass
class Ledger:
    """断言台账：记录每条检查的证据，最后落成 JSON + 控制台摘要。

    ``skip`` 与 ``fail`` 分开统计，是为了让"环境覆盖不到"（例如解压比攻击需要 64MiB
    以上数据）不被粉饰成"通过"。
    """

    title: str
    checks: list[Check] = field(default_factory=list)
    _phase: str = ""

    def phase(self, name: str) -> None:
        self._phase = name
        print(f"\n---- {name} " + "-" * max(0, 56 - len(name)), flush=True)

    def check(self, name: str, ok: bool, detail: Any = "") -> bool:
        text = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False, default=str)
        self.checks.append(Check(name=name, ok=bool(ok), detail=text[:500], phase=self._phase))
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name}" + (f"  <- {text[:180]}" if text and not ok else ""), flush=True)
        return bool(ok)

    def equal(self, name: str, actual: Any, expected: Any) -> bool:
        return self.check(name, actual == expected, f"actual={actual!r} expected={expected!r}")

    def skip(self, name: str, reason: str) -> None:
        self.checks.append(Check(name=name, ok=True, detail=f"SKIPPED: {reason}", phase=self._phase))
        print(f"  [SKIP] {name}  ({reason})", flush=True)

    def fail_phase(self, name: str, exc: BaseException) -> None:
        self.checks.append(Check(name=name, ok=False, detail=f"{type(exc).__name__}: {exc}", phase=self._phase))
        print(f"  [FAIL] {name}  <- {type(exc).__name__}: {exc}", flush=True)

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    def summary(self) -> dict[str, Any]:
        passed = sum(1 for c in self.checks if c.ok and not c.detail.startswith("SKIPPED"))
        skipped = sum(1 for c in self.checks if c.ok and c.detail.startswith("SKIPPED"))
        failed = len(self.failed)
        result = {
            "title": self.title,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "total": len(self.checks),
            "passed": passed,
            "skipped": skipped,
            "failed": failed,
            "checks": [c.__dict__ for c in self.checks],
        }
        ARTIFACTS_DIR.mkdir(exist_ok=True)
        slug = self.title.lower().replace(" ", "_")
        out = ARTIFACTS_DIR / f"ledger_{slug}.json"
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            f"\n==== {self.title}: {passed} passed / {skipped} skipped / {failed} failed"
            f"（共 {len(self.checks)} 条）-> {out}",
            flush=True,
        )
        for check in self.failed[:20]:
            print(f"  !! [{check.phase}] {check.name}: {check.detail}", flush=True)
        return result


def load_fixtures() -> dict[str, Any]:
    """读取 NAS 生成器的数值oracle（e2e/artifacts/fixtures.json）。"""
    path = ARTIFACTS_DIR / "fixtures.json"
    if not path.exists():
        raise SystemExit(f"缺少 {path}，先运行 python e2e/make_nas.py")
    return json.loads(path.read_text(encoding="utf-8"))


def load_ruleset_payload(name: str) -> dict[str, Any]:
    return json.loads((ARTIFACTS_DIR / name).read_text(encoding="utf-8"))
