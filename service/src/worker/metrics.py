"""Worker 资源采样：失败一律降级为 None，绝不因为采集问题影响任务处理。"""

import shutil
from pathlib import Path

import psutil

from src.application.worker.ports import WorkerStats


def collect_worker_stats(work_dir: Path) -> WorkerStats:
    try:
        process = psutil.Process()
        cpu_percent = process.cpu_percent(interval=None)
        memory_bytes = process.memory_info().rss
    except Exception:  # noqa: BLE001 - 采集失败不能让 Worker 退出
        cpu_percent, memory_bytes = None, None
    try:
        free = shutil.disk_usage(str(work_dir)).free
    except OSError:
        free = None
    return WorkerStats(
        cpu_percent=None if cpu_percent is None else float(cpu_percent),
        memory_bytes=None if memory_bytes is None else int(memory_bytes),
        temp_disk_free_bytes=None if free is None else int(free),
    )
