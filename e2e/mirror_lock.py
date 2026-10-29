"""把 service/uv.lock 里的镜像源改写成官方源，供 E2E 构建使用。

为什么要改写：uv.lock 里 1389 处依赖 URL 全部指向
`https://mirrors.aliyun.com/pypi/...`，该镜像今天对本机所有路径返回 403，
`uv sync --frozen` 因此无法完成（UV_DEFAULT_INDEX 不会重写已锁定的 URL）。

改写是纯主机替换，不动版本、不动 hash：
    https://mirrors.aliyun.com/pypi/packages/<rest>  ->  https://files.pythonhosted.org/packages/<rest>
    https://mirrors.aliyun.com/pypi/simple/          ->  https://pypi.org/simple
同一份 wheel 在两个源上是同一文件，已实测抽样校验（alembic-1.20.1-py3-none-any.whl
从 files.pythonhosted.org 下载后 sha256 与锁内一致）。

产物只写到 e2e/artifacts/ctx/service/uv.lock，生产 service/uv.lock 保持不动
（工作树与他人自动化共享，不能改）。

用法：
    uv run --python 3.12 python e2e/mirror_lock.py [--src service/uv.lock] [--dst PATH]
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import urllib.request
from pathlib import Path

E2E_DIR = Path(__file__).resolve().parent
REPO_DIR = E2E_DIR.parent

PACKAGES_OLD = "https://mirrors.aliyun.com/pypi/packages/"
PACKAGES_NEW = "https://files.pythonhosted.org/packages/"
REGISTRY_OLD = "https://mirrors.aliyun.com/pypi/simple/"
REGISTRY_NEW = "https://pypi.org/simple"


def rewrite(text: str) -> tuple[str, int, int]:
    pkgs = text.count(PACKAGES_OLD)
    registry = text.count(REGISTRY_OLD)
    out = text.replace(PACKAGES_OLD, PACKAGES_NEW).replace(REGISTRY_OLD, REGISTRY_NEW)
    return out, pkgs, registry


def find_residual(text: str) -> list[str]:
    """改写后仍残留的 aliyun 地址（应为空，非空说明有新写法）。"""
    return sorted({m.group(0) for m in re.finditer(r"https://[a-z.]*aliyun\.com[^\s\"']*", text)})


def spot_check(dst: Path, count: int) -> list[str]:
    """从改写后的 lock 里抽样若干 wheel URL，HEAD 请求确认可达（不下载全文）。"""
    urls = re.findall(r'url = "(https://files\.pythonhosted\.org/packages/[^"]+\.whl)"', dst.read_text(encoding="utf-8"))
    if not urls:
        return ["<no wheel urls>"]
    step = max(1, len(urls) // max(1, count))
    failures: list[str] = []
    for url in urls[::step][:count]:
        try:
            request = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.status != 200:
                    failures.append(f"{response.status} {url}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{type(exc).__name__} {url} {str(exc)[:80]}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", default=str(REPO_DIR / "service" / "uv.lock"))
    parser.add_argument("--dst", default=str(E2E_DIR / "artifacts" / "ctx" / "service" / "uv.lock"))
    parser.add_argument("--check", type=int, default=0, help="抽样 N 个改写后的 URL 做可达性 HEAD")
    args = parser.parse_args()

    src, dst = Path(args.src), Path(args.dst)
    text = src.read_text(encoding="utf-8")
    out, pkgs, registry = rewrite(text)
    residual = find_residual(out)
    if residual:
        print(f"[mirror_lock] 残留非预期写法：{residual}", file=sys.stderr)
        return 2
    if pkgs == 0 and registry == 0:
        print("[mirror_lock] 未发现任何 aliyun URL，src 可能已被改写或不是同一份 lock", file=sys.stderr)
        return 2

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(out, encoding="utf-8", newline="\n")
    print(
        f"[mirror_lock] {src} -> {dst}："
        f"改写 packages {pkgs} 处、registry {registry} 处；"
        f"sha256(src)={hashlib.sha256(text.encode()).hexdigest()[:12]} "
        f"sha256(dst)={hashlib.sha256(out.encode()).hexdigest()[:12]}"
    )

    if args.check:
        failures = spot_check(dst, args.check)
        if failures:
            print("[mirror_lock] 抽样可达性失败：", file=sys.stderr)
            for line in failures:
                print("   ", line, file=sys.stderr)
            return 3
        print(f"[mirror_lock] 抽样 {args.check} 个改写 URL 全部 200 可达")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
