"""纯文本工具：SQL LIKE 模式的字面量转义。

应用服务里做"用户输入 → LIKE 模式"拼接的地方（RBAC 关键词、审计动作前缀、
规则集名称）共用这一份实现：反斜杠最先转义，再转 ``%``/``_``，配合
``ilike(..., escape="\\")`` 使用。历史上它以 ``_escape_like`` 的形式在三个
服务里各存一份拷贝，口径一旦改动就可能出现三处行为漂移。
"""


def escape_like(value: str) -> str:
    """把用户输入转成 LIKE 模式里的字面量；调用方需 ``escape="\\"``。"""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
