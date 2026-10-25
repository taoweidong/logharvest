"""Worker 进程唯一一次的日志配置：stdout 上的 JSON 行，带 worker_id 绑定。"""

import logging

import structlog


def configure_worker_logging(
    *, worker_id: str, level: str = "INFO"
) -> structlog.stdlib.BoundLogger:
    """规格 §7.4：Worker 需要可读的结构化日志，此前永久失败只能反查 file_tasks.error。

    structlog 26 的 ``configure()`` 只收全局处理器，不接受初始绑定值，
    所以 worker_id 在返回的 logger 上 bind。
    """
    level_code = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(level=level_code, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level_code),
    )
    return structlog.get_logger().bind(worker_id=worker_id)
