"""把 uv.lock 的包源索引改写到指定源（版本与 sha256 不变，只换 URL 前缀）。

为什么需要：uv sync --frozen 按锁文件内的 URL 取包，UV_DEFAULT_INDEX 等环境变量
不会重写已锁定的 URL。生产构建要支持"pip/uv 源可配置"（PIP_INDEX_URL），就必须在
构建期把锁内主源 URL 一并改写到目标源——构建参数只改 pip 索引是不够的。

改写规则（与 e2e/mirror_lock.py 的主机替换同型，但目标源可参数化）：
    <旧源>/packages/<rest>  ->  <新源>/packages/<rest>
    source = { registry = "<旧源 simple>" }  ->  新源 simple
只改写锁内占比最大的主源（本仓库为 mirrors.aliyun.com，61 包 / 千余 artifact URL）；
显式声明在 [[tool.uv.index]] 的其他源不受影响（如 hyperscan 的 pypi.org，仅当它
恰为主源时才会被一并改写，本仓库不会出现）。PyPI 官方源的包文件主机是
files.pythonhosted.org，做特例映射；其余镜像站遵循 <base>/simple/ + <base>/packages/
的同构布局（aliyun / tsinghua / ustc / tencent 等）。
同一份 wheel 在各镜像站是同一文件，哈希不变；下载时 uv 按锁内 sha256 校验。

用法：
    python uv_lock_index.py <uv.lock> --index https://pypi.tuna.tsinghua.edu.cn/simple/
    python uv_lock_index.py <uv.lock> --index https://mirrors.aliyun.com/pypi/simple/
    # 主源已是目标源 -> no-op 直接返回 0（Dockerfile 默认路径即此情形）
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PYPI_PACKAGES_ROOT = "https://files.pythonhosted.org/"
PYPI_REGISTRY = "https://pypi.org/simple"


def split_index(url: str) -> tuple[str, str]:
    """index URL -> (registry 规范串, packages 根)。"""
    url = url.strip().rstrip("/")
    if url == PYPI_REGISTRY:
        return url, PYPI_PACKAGES_ROOT
    if not url.endswith("/simple"):
        raise SystemExit(f"不支持的 index 形态：{url}（应为 <base>/simple）")
    return url + "/", url[: -len("/simple")] + "/"


def main() -> int:
    parser = argparse.ArgumentParser(description="把 uv.lock 的包源索引改写到指定源")
    parser.add_argument("lock", help="uv.lock 路径")
    parser.add_argument("--index", required=True, help="目标包源（pip/uv 的 index URL）")
    parser.add_argument("--out", help="输出路径（缺省原地覆写）")
    args = parser.parse_args()

    lock = Path(args.lock)
    text = lock.read_text(encoding="utf-8")

    sources = re.findall(r'source = \{ registry = "([^"]+)" \}', text)
    if not sources:
        print("[uv_lock_index] 锁内没有 registry source 条目", file=sys.stderr)
        return 2
    primary = max(set(sources), key=sources.count)  # 占比最大的主源
    old_registry, old_pkgs = split_index(primary)
    new_registry, new_pkgs = split_index(args.index)

    if primary.rstrip("/") == new_registry.rstrip("/"):
        print(f"[uv_lock_index] 主源已是 {new_registry}，无需改写")
        return 0

    out = text.replace(old_pkgs + "packages/", new_pkgs + "packages/")
    out = out.replace(old_registry, new_registry)

    pattern = re.escape(old_pkgs) + r"[^\s\"']*"
    residual = sorted({m.group(0) for m in re.finditer(pattern, out)})
    if residual:
        print(f"[uv_lock_index] 残留旧源 URL（出现新写法，中止）：{residual[:5]}", file=sys.stderr)
        return 2

    dst = Path(args.out) if args.out else lock
    dst.write_text(out, encoding="utf-8", newline="\n")
    print(f"[uv_lock_index] {old_registry} -> {new_registry}，写入 {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
