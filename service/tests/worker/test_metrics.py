from pathlib import Path

from src.worker.metrics import collect_worker_stats


def test_collect_returns_disk_free_for_existing_dir(tmp_path: Path) -> None:
    stats = collect_worker_stats(tmp_path)

    assert stats.temp_disk_free_bytes is not None
    assert stats.temp_disk_free_bytes > 0
    assert stats.memory_bytes is None or stats.memory_bytes > 0


def test_collect_survives_missing_work_dir(tmp_path: Path) -> None:
    stats = collect_worker_stats(tmp_path / "does-not-exist")

    assert stats.temp_disk_free_bytes is None
