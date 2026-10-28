"""Tests for the no-DB per-stage task overhead probe."""

import importlib.util
import json
import math
from statistics import pstdev

import pytest

from scripts import probe_task_overhead as probe
from src.application.worker.ports import MatchRecord

# 两组行数的差值用来分离固定成本与逐行成本；这里刻意用极小的语料保证套件秒级完成。
TINY_CELLS = ((1, 120), (1, 400))


def make_report(tmp_path, **overrides):
    options = {
        "cells": TINY_CELLS,
        "files": 2,
        "line_bytes": 64,
        "seed": 7,
        "tmp_root": tmp_path,
        "deadline_seconds": 60.0,
    }
    options.update(overrides)
    return probe.run_probe(**options)


def cli_args(tmp_path, extra=()):
    return [
        "--cells",
        "1:120,1:400",
        "--files",
        "2",
        "--line-bytes",
        "64",
        "--seed",
        "7",
        "--tmp-dir",
        str(tmp_path),
        "--deadline-seconds",
        "60",
        *extra,
    ]


def test_module_is_importable_as_scripts_member():
    assert importlib.util.find_spec("scripts.probe_task_overhead") is not None
    assert len(set(probe.STAGES)) == len(probe.STAGES) >= 8


def test_every_cell_reports_every_stage_and_stays_inside_tolerance(tmp_path):
    report = make_report(tmp_path)
    assert len(report["cells"]) == 2
    tolerance = report["tolerance"]
    assert tolerance["overlap_fraction"] > 0 and tolerance["unaccounted_fraction"] > 0
    for cell in report["cells"]:
        stages = cell["timings"]["stages"]
        assert set(stages) == set(probe.STAGES)
        assert all(value >= 0.0 for value in stages.values())
        total = cell["timings"]["total_task_seconds"]
        assert total > 0.0
        measured = sum(stages.values())
        slack = tolerance["absolute_seconds"]
        # 防重复计时：各阶段之和不得超过总耗时（只允许时钟粒度的正负余量）。
        assert measured <= total * (1.0 + tolerance["overlap_fraction"]) + slack
        # 防漏计时：未被阶段解释的剩余必须是正数且占比很小。
        unaccounted = cell["timings"]["unaccounted_seconds"]
        assert unaccounted >= -slack
        assert unaccounted <= total * tolerance["unaccounted_fraction"] + slack
        assert cell["status"] == "succeeded"


def test_trivial_ruleset_task_succeeds_and_reports_real_work(tmp_path):
    report = make_report(tmp_path)
    for index, cell in enumerate(report["cells"]):
        assert cell["rules"] == 1
        assert cell["files"] == 2
        assert cell["lines"] == TINY_CELLS[index][1]
        assert cell["processed_lines"] == cell["lines"]
        assert cell["bytes"] > 0 and len(cell["source_sha256"]) == 64
        assert len(cell["config_sha256"]) == 64
        assert cell["matched_rows"] > 0
        assert cell["matched_rows"] == cell["counts"]["records"]
        assert cell["counts"]["lines_read"] == cell["lines"]
        assert cell["counts"]["evaluate_calls"] == cell["lines"]
        # file_glob 判定必须至少每个成员一次；具体次数如实反映处理器实现（探针不干预）。
        assert cell["counts"]["glob_checks"] >= cell["files"]
        assert cell["timings"]["stages"]["file_glob_membership"] >= 0.0
        assert cell["timings"]["seconds_per_line"] == pytest.approx(
            cell["timings"]["total_task_seconds"] / cell["lines"]
        )
        assert cell["timings"]["lines_per_second"] == pytest.approx(
            1 / cell["timings"]["seconds_per_line"]
        )
    stages = report["cells"][1]["timings"]["stages"]
    for key in ("guard_stat_and_copy", "archive_open_and_listing", "decompress_and_decode"):
        assert stages[key] > 0.0


def test_derived_block_separates_fixed_from_per_line_cost(tmp_path):
    report = make_report(tmp_path)
    assert len(report["derived"]) == 1
    group = report["derived"][0]
    assert group["rules"] == 1 and group["lines"] == [120, 400]
    first, second = report["cells"]
    total_first = first["timings"]["total_task_seconds"]
    total_second = second["timings"]["total_task_seconds"]
    slope = (total_second - total_first) / (second["lines"] - first["lines"])
    assert group["timings"]["seconds_per_line"] == pytest.approx(slope)
    assert group["timings"]["fixed_seconds_per_task"] == pytest.approx(
        total_second - slope * second["lines"]
    )
    assert group["timings"]["evaluate_seconds_per_line"] > 0.0


def test_report_shape_is_stable_even_when_a_stage_never_fires(tmp_path):
    report = make_report(tmp_path)
    expected = set(probe.RECORDER_COUNTS) | set(probe.REPO_COUNTS)
    for cell in report["cells"]:
        assert set(cell["counts"]) >= expected
        assert set(cell["timings"]["stages"]) == set(probe.STAGES)
        assert all(cell["counts"][key] >= 0 for key in expected)
    assert report["requested_cells"] == [[1, 120], [1, 400]]
    assert report["knobs"]["files"] == 2 and report["knobs"]["fault"] == "none"


def test_writer_discards_rows_and_caps_batch_memory():
    writer = probe.CountingWriter()
    record = MatchRecord(
        job_id=1,
        file_task_id=1,
        rule_id="r",
        rule_set_id=1,
        rule_version=1,
        severity="info",
        tags=[],
        source_path="s",
        member_path="m",
        file_name="m",
        line_no=1,
        raw_line="x",
        raw_truncated=False,
        fields={},
        numeric_value=None,
    )
    for _ in range(3):
        writer.write_batch(tuple([record] * 5))
    assert (writer.rows, writer.batches, writer.max_batch_rows) == (15, 3, 5)
    # 内存有界：写入替身只留计数，绝不留存匹配记录本身。
    assert not [
        value
        for value in vars(writer).values()
        if isinstance(value, (list, tuple)) and any(isinstance(item, MatchRecord) for item in value)
    ]


def test_probe_flushes_at_batch_size_and_keeps_plumbing_visible(tmp_path):
    report = make_report(tmp_path, batch_size=16, heartbeat_every_lines=100)
    for cell in report["cells"]:
        counts = cell["counts"]
        assert counts["max_batch_rows"] <= 16
        assert counts["write_batches"] >= math.ceil(cell["matched_rows"] / 16)
        assert counts["heartbeats"] >= 1
        assert counts["touches"] >= counts["write_batches"]
        assert counts["cancel_checks"] >= counts["heartbeats"]
        assert cell["timings"]["stages"]["writer_write_batch"] >= 0.0
        assert cell["timings"]["stages"]["repo_fencing_and_complete"] >= 0.0


@pytest.mark.parametrize("fault", ["heartbeat", "lease", "cancel"])
def test_broken_fakes_fail_closed_instead_of_reporting_success(tmp_path, fault, capsys):
    # 心跳与取消检查在生产节奏下（2 万行/5 秒）不会命中小任务，所以故障注入用例
    # 显式把心跳频率调到每行一次，否则"替身坏了也报成功"根本不会被触发。
    with pytest.raises(probe.ProbeError):
        make_report(tmp_path, fault=fault, heartbeat_every_lines=1)
    code = probe.main(cli_args(tmp_path, ["--fault", fault, "--heartbeat-every-lines", "1"]))
    assert code == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert captured.err.strip()


@pytest.mark.parametrize(
    "flag,value", [("--files", "0"), ("--line-bytes", "-1"), ("--batch-size", "0")]
)
def test_cli_rejects_non_positive_parameters(tmp_path, capsys, flag, value):
    assert probe.main(cli_args(tmp_path, [flag, value])) == 1
    assert not capsys.readouterr().out


@pytest.mark.parametrize("cells", ["0:120", "1:0", "1:-5", "junk", "1", ""])
def test_cli_rejects_malformed_cells(tmp_path, capsys, cells):
    assert probe.main(cli_args(tmp_path, ["--cells", cells])) == 1
    assert not capsys.readouterr().out


def test_cli_rejects_work_larger_than_the_bound(tmp_path, capsys):
    assert probe.main(cli_args(tmp_path, ["--cells", "1:1000000"])) == 1
    assert not capsys.readouterr().out


def test_cli_reports_json_on_stdout_only(tmp_path, capsys):
    assert probe.main(cli_args(tmp_path)) == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert not captured.err.strip()
    assert report["probe"] == "task_overhead" and report["seed"] == 7
    assert [cell["lines"] for cell in report["cells"]] == [120, 400]
    assert all(key in report for key in ("tolerance", "derived", "limitations", "environment"))
    assert report["environment"]["database"] is False
    assert report["environment"]["hyperscan_required"] is False
    joined = " ".join(report["limitations"]).lower()
    assert "commit" in joined and "not measured" in joined
    assert "data/_bench" in joined


def test_same_seed_produces_the_same_input_fingerprint(tmp_path):
    first = make_report(tmp_path)
    second = make_report(tmp_path / "again")
    assert probe.deterministic_view(first) == probe.deterministic_view(second)
    assert [cell["source_sha256"] for cell in first["cells"]] == [
        cell["source_sha256"] for cell in second["cells"]
    ]
    assert {cell["config_sha256"] for cell in first["cells"]} == {
        cell["config_sha256"] for cell in first["cells"]
    }
    third = make_report(tmp_path / "other", seed=8)
    changed = probe.deterministic_view(third)["cells"][0]["source_sha256"]
    assert changed != first["cells"][0]["source_sha256"]


def test_stage_times_are_repeated_runs_of_the_same_cell(tmp_path):
    """A single cell is noisy; the probe must still keep the same cell shape twice."""
    report = make_report(tmp_path, repeat=2)
    assert len(report["cells"]) == 4
    assert [cell["lines"] for cell in report["cells"]] == [120, 120, 400, 400]
    per_line = [cell["timings"]["seconds_per_line"] for cell in report["cells"]]
    assert all(value > 0.0 for value in per_line)
    assert pstdev(per_line) / max(per_line) < 1.0
    assert (
        probe.deterministic_view(report)["cells"]
        == probe.deterministic_view(make_report(tmp_path, repeat=2))["cells"]
    )
