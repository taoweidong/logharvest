"""Per-stage wall-time breakdown of one real Worker task; no database, no network.

Drives the production :class:`~src.application.worker.processor.TaskProcessor` against
in-memory fakes for ``TaskRepository``/``MatchWriter`` and a real archive in a local temp
directory, so fixed per-task plumbing can be separated from evaluation cost. Output is one
JSON document on stdout; anything that invalidates the numbers exits 1 with stderr only.

    python -m scripts.probe_task_overhead --cells 1:2000,1:20000,5000:1000,5000:2000
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
import zipfile
from contextlib import contextmanager, redirect_stdout
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from shutil import rmtree
from statistics import median
from tempfile import mkdtemp
from time import perf_counter

import scripts.bench_multimode as bench
import src.application.worker.processor as processor
from src.application.worker.ports import ClaimedTask
from src.application.worker.processor import TaskProcessor
from src.domain.rules.evaluator import compile_ruleset
from src.domain.source_guard import POLICY_IGNORE

STAGES = (
    "guard_stat_and_copy",  # 1. read-only guard + realpath/size/mtime stat + local copy
    "archive_open_and_listing",  # 2. container open + member listing (first line pull)
    "decompress_and_decode",  # 3. inflate + decode into LineRecords
    "file_glob_membership",  # 4. pathspec membership checks
    "evaluate_line",  # 5. rule evaluation
    "match_record_and_buffer",  # 6. result object construction (+ buffer appends)
    "writer_write_batch",  # 7. write_batch plumbing only (fake writer: no DB at all)
    "repo_fencing_and_complete",  # 8. touch/heartbeat/cancel/complete plumbing only
)
LIMITS = {
    "rules": 100_000,
    "lines": 200_000,
    "files": 256,
    "line_bytes": 4096,
    "batch_size": 100_000,
}
# 阶段之和最多只能比总耗时多出 overlap_fraction（防重复计时），未被阶段解释的剩余最多占总耗
# 时的 unaccounted_fraction（防漏计时）；absolute_seconds 是 perf_counter 的粒度余量。
TOLERANCE = {"overlap_fraction": 0.02, "unaccounted_fraction": 0.25, "absolute_seconds": 0.005}
HYPOTHESIS = {"task_duration_p50_seconds": 39.9, "evaluator_lines_per_second": 255.0, "lines": 2000}
FAULTS = ("none", "heartbeat", "lease", "cancel")
DEFAULT_CELLS = "1:2000,1:20000,5000:1000,5000:2000"
BASE_CORPUS_LINES = 2_000
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MEMBER_SPEC_ATTR = "_member_spec"  # 处理器缓存的 file_glob PathSpec：探针只加计时包装


class ProbeError(RuntimeError):
    """The probe cannot defend its own numbers, so no report is emitted."""


@dataclass(frozen=True)
class Shape:
    """One CLI invocation: which (rules x lines) cells to run and with which knobs."""

    cells: tuple[tuple[int, int], ...] = ()
    files: int = 3
    line_bytes: int = 128
    seed: int = 42
    scenario: str = "sparse"
    batch_size: int = 1000
    heartbeat_every_lines: int = 20_000
    repeat: int = 1
    fault: str = "none"
    tmp_root: str | Path | None = None
    deadline_seconds: float = 85.0


@contextmanager
def _timed(target: dict[str, float], key: str):
    started = perf_counter()
    try:
        yield
    finally:
        target[key] = target.get(key, 0.0) + perf_counter() - started


RECORDER_COUNTS = ("lines_read", "evaluate_calls", "records", "glob_checks", "refreshes")
REPO_COUNTS = ("touches", "heartbeats", "completes", "cancel_checks")


@dataclass
class _Recorder:
    """Accumulates the stage windows the production loop cannot report itself."""

    seconds: dict[str, float] = field(default_factory=lambda: dict.fromkeys(STAGES, 0.0))
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(RECORDER_COUNTS, 0))
    extra: dict[str, float] = field(default_factory=dict)
    decoded_chars: int = 0
    enter_scan_at: float = 0.0
    fenced_before_scan: float = 0.0

    def add(self, key: str, value: float) -> None:
        self.seconds[key] += value

    def bump(self, key: str) -> None:
        self.counts[key] = self.counts.get(key, 0) + 1


class CountingWriter:
    """MatchWriter fake: counts rows and discards them, so no DB commit is measured."""

    def __init__(self) -> None:
        self.rows = 0
        self.batches = 0
        self.max_batch_rows = 0
        self.deleted = 0
        self.fenced_seconds: dict[str, float] = {}

    def delete_for_task(self, task_id: int) -> None:
        with _timed(self.fenced_seconds, "delete"):
            self.deleted += 1

    def write_batch(self, matches) -> None:
        with _timed(self.fenced_seconds, "write"):
            self.batches += 1
            self.rows += len(matches)
            self.max_batch_rows = max(self.max_batch_rows, len(matches))


class FencingRepo:
    """TaskRepository fake with self-timed lease/heartbeat/cancel/complete plumbing."""

    def __init__(self, fault: str = "none") -> None:
        self.fault = fault
        self.completed_status: str | None = None
        self.fenced_seconds: dict[str, float] = {}
        self.calls: dict[str, int] = dict.fromkeys(REPO_COUNTS, 0)

    def _bump(self, key: str) -> None:
        self.calls[key] = self.calls.get(key, 0) + 1

    def touch(self, task_id: int, attempts: int) -> bool:
        with _timed(self.fenced_seconds, "touch"):
            self._bump("touches")
            return self.fault != "lease"

    def heartbeat(
        self, task_id: int, attempts: int, processed_lines: int, matched_rows: int
    ) -> bool:
        with _timed(self.fenced_seconds, "heartbeat"):
            self._bump("heartbeats")
            return self.fault != "heartbeat"

    def complete(
        self,
        task_id: int,
        attempts: int,
        status: str,
        processed_lines: int,
        matched_rows: int,
        error: str | None = None,
        retryable: bool = True,
    ) -> None:
        with _timed(self.fenced_seconds, "complete"):
            self._bump("completes")
            self.completed_status = status

    def is_job_cancelled(self, job_id: int) -> bool:
        with _timed(self.fenced_seconds, "cancel"):
            self._bump("cancel_checks")
            return self.fault == "cancel"


class _TimingSpec:
    """Duck-typed pathspec wrapper: times the ``file_glob`` membership check only."""

    def __init__(self, spec, recorder: _Recorder) -> None:
        self._spec = spec
        self._recorder = recorder

    def match_file(self, value):
        with _timed(self._recorder.seconds, STAGES[3]):
            self._recorder.bump("glob_checks")
            return self._spec.match_file(value)

    def __getattr__(self, name):
        return getattr(self._spec, name)


class _ProbingProcessor(TaskProcessor):
    """Adds one timing hook around the local copy; every behaviour is ``super``'s."""

    recorder: _Recorder

    def _copy_to_local(self, task: ClaimedTask, task_dir: Path) -> Path:
        started = perf_counter()
        target = super()._copy_to_local(task, task_dir)
        self.recorder.extra["copy_to_local_seconds"] = perf_counter() - started
        return target


def _timed_lines(inner, recorder: _Recorder):
    """Pull the real reader line by line, charging only the pull to stages 2/3."""
    started = perf_counter()
    key = STAGES[1]
    try:
        for record in inner:
            recorder.add(key, perf_counter() - started)
            recorder.bump("lines_read")
            recorder.decoded_chars += len(record.text)
            key = STAGES[2]
            yield record
            started = perf_counter()
    finally:
        close = getattr(inner, "close", None)
        if close is not None:
            close()


def _fenced(repo: FencingRepo, writer: CountingWriter) -> float:
    return sum(repo.fenced_seconds.values()) + sum(writer.fenced_seconds.values())


@contextmanager
def _instrument(recorder: _Recorder, proc, repo: FencingRepo, writer: CountingWriter):
    real_evaluate = processor.evaluate_line
    real_record = processor.MatchRecord
    real_scan = processor.scan_archive
    spec = getattr(proc, MEMBER_SPEC_ATTR, None)
    if spec is None:
        raise ProbeError(f"{MEMBER_SPEC_ATTR} missing: processor internals changed")
    setattr(proc, MEMBER_SPEC_ATTR, _TimingSpec(spec, recorder))

    def timed_evaluate(rules, text, line_no):
        with _timed(recorder.seconds, STAGES[4]):
            recorder.bump("evaluate_calls")
            return real_evaluate(rules, text, line_no)

    def timed_record(**kwargs):
        with _timed(recorder.seconds, STAGES[5]):
            recorder.bump("records")
            return real_record(**kwargs)

    def timed_scan(path, **kwargs):
        recorder.enter_scan_at = perf_counter()
        recorder.fenced_before_scan = _fenced(repo, writer)
        scan = real_scan(path, **kwargs)
        scan.lines = _timed_lines(scan.lines, recorder)
        return scan

    processor.evaluate_line = timed_evaluate
    processor.MatchRecord = timed_record
    processor.scan_archive = timed_scan
    try:
        yield
    finally:
        processor.evaluate_line = real_evaluate
        processor.MatchRecord = real_record
        processor.scan_archive = real_scan


def build_archive(path: Path, files: int, lines: int, corpus: tuple[str, ...]) -> Path:
    """Deterministic zip (fixed DOS stamp) whose bytes are the seeded workload."""
    per, extra = divmod(lines, files)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for index in range(files):
            count = per + (1 if index < extra else 0)
            body = "\n".join(corpus[(index * per + step) % len(corpus)] for step in range(count))
            info = zipfile.ZipInfo(f"members/log-{index:03d}.log", date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, body)
    return path


def _config_sha(config) -> str:
    canonical = json.dumps(config.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _parse_cells(text: str) -> tuple[tuple[int, int], ...]:
    cells = []
    for item in text.split(","):
        parts = item.strip().split(":")
        if len(parts) != 2:
            raise ProbeError(f"cell {item!r} must be RULES:LINES")
        try:
            cells.append((int(parts[0]), int(parts[1])))
        except ValueError as exc:
            raise ProbeError(f"cell {item!r} must be integers: {exc}") from exc
    return tuple(cells)


def _validate(shape: Shape) -> None:
    if not shape.cells or shape.repeat < 1 or shape.deadline_seconds <= 0:
        raise ProbeError("cells must hold RULES:LINES pairs, repeat >= 1, deadline > 0")
    if shape.fault not in FAULTS or shape.scenario not in bench.SCENARIOS:
        raise ProbeError(f"fault must be in {FAULTS} and scenario in {bench.SCENARIOS}")
    if shape.heartbeat_every_lines < 1:
        raise ProbeError("heartbeat-every-lines must be >= 1")
    bounded = {"files": shape.files, "line_bytes": shape.line_bytes, "batch_size": shape.batch_size}
    for rules, lines in shape.cells:
        bounded.update({"rules": rules, "lines": lines})
        for name, value in bounded.items():
            if not 1 <= value <= LIMITS[name]:
                raise ProbeError(f"{name} must be in 1..{LIMITS[name]}, found {value}")
        if lines * shape.line_bytes > MAX_SOURCE_BYTES:
            raise ProbeError(f"source would exceed {MAX_SOURCE_BYTES} bytes: reduce --cells")


def _check_accounting(index: int, total: float, stages: dict) -> float:
    measured = sum(stages.values())
    slack = TOLERANCE["absolute_seconds"]
    if measured > total * (1 + TOLERANCE["overlap_fraction"]) + slack:
        raise ProbeError(f"cell {index}: stages double count: {measured:.6f}s > {total:.6f}s")
    unaccounted = total - measured
    if unaccounted > total * TOLERANCE["unaccounted_fraction"] + slack:
        raise ProbeError(
            f"cell {index}: only {measured / total:.1%} of the task is attributed; tolerance "
            f"allows {TOLERANCE['unaccounted_fraction']:.0%} unexplained"
        )
    return unaccounted


def _cell_report(
    index, rules, lines, shape, config, path, stat, result, repo, writer, recorder, total, stages
) -> dict:
    unaccounted = _check_accounting(index, total, stages)
    return {
        "index": index,
        "rules": len(config.rules),
        "files": shape.files,
        "lines": lines,
        "line_bytes": shape.line_bytes,
        "bytes": stat.st_size,
        "status": result.status,
        "processed_lines": result.processed_lines,
        "matched_rows": result.matched_rows,
        "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "config_sha256": _config_sha(config),
        "counts": {
            **recorder.counts,
            **repo.calls,
            "decoded_chars": recorder.decoded_chars,
            "rows": writer.rows,
            "write_batches": writer.batches,
            "max_batch_rows": writer.max_batch_rows,
            "deletes": writer.deleted,
        },
        "timings": {
            "total_task_seconds": total,
            "stages": {key: stages[key] for key in STAGES},
            "measured_stage_seconds": sum(stages.values()),
            "unaccounted_seconds": unaccounted,
            "unaccounted_fraction": unaccounted / total,
            "seconds_per_line": total / lines,
            "lines_per_second": lines / total,
            "copy_to_local_seconds": recorder.extra.get("copy_to_local_seconds", 0.0),
            "decoded_bytes_per_second": recorder.decoded_chars / max(stages[STAGES[2]], 1e-9),
        },
    }


def _run_cell(
    index: int, rules: int, lines: int, shape: Shape, source_dir: Path, work_dir: Path
) -> dict:
    config, corpus = bench.make_workload(
        rules, min(lines, BASE_CORPUS_LINES), shape.scenario, shape.line_bytes, shape.seed
    )
    path = build_archive(source_dir / f"task-{index:03d}.zip", shape.files, lines, corpus)
    stat = path.stat()
    task = ClaimedTask(
        id=1000 + index,
        job_id=2000 + index,
        source_path=path,
        file_name=path.name,
        attempts=1,
        rule_set_id=3000 + index,
        rule_version=1,
        root_path=source_dir,
        expected_size_bytes=stat.st_size,
        expected_mtime=datetime.fromtimestamp(stat.st_mtime, UTC),
    )
    repo, writer, recorder = FencingRepo(shape.fault), CountingWriter(), _Recorder()
    proc = _ProbingProcessor(
        repo=repo,
        writer=writer,
        rules=compile_ruleset(config),
        work_dir=work_dir,
        batch_size=shape.batch_size,
        heartbeat_every_lines=shape.heartbeat_every_lines,
        nas_read_only_policy=POLICY_IGNORE,
        on_lease_refresh=lambda: recorder.bump("refreshes"),
        cancel_check=lambda: repo.is_job_cancelled(task.job_id),
    )
    proc.recorder = recorder
    with _instrument(recorder, proc, repo, writer):
        started = perf_counter()
        try:
            result = proc.process(task)
        except processor.LeaseLost as exc:
            raise ProbeError(f"cell {index} ({rules} rules x {lines} lines): {exc}") from exc
        total = perf_counter() - started
    if result.status != "succeeded":
        raise ProbeError(f"cell {index} finished {result.status}: {result.error}")
    stages = dict(recorder.seconds)
    stages[STAGES[0]] = recorder.enter_scan_at - started - recorder.fenced_before_scan
    stages[STAGES[6]] = writer.fenced_seconds.get("write", 0.0)
    stages[STAGES[7]] = _fenced(repo, writer) - recorder.fenced_before_scan - stages[STAGES[6]]
    return _cell_report(
        index,
        rules,
        lines,
        shape,
        config,
        path,
        stat,
        result,
        repo,
        writer,
        recorder,
        total,
        stages,
    )


def _timing(cell: dict, key: str) -> float:
    timings = cell["timings"]
    return timings[key] if key in timings else timings["stages"][key]


def _sampled(cells: list[dict], size: int, key: str) -> float:
    return median([_timing(cell, key) for cell in cells if cell["lines"] == size])


def _derive(cells: list[dict]) -> list[dict]:
    """Difference the smallest and largest file per ruleset: fixed cost versus per-line cost."""
    derived = []
    for rules in sorted({cell["rules"] for cell in cells}):
        owned = [cell for cell in cells if cell["rules"] == rules]
        sizes = sorted({cell["lines"] for cell in owned})
        if len(sizes) < 2:
            continue
        small, large = sizes[0], sizes[-1]
        totals = [_sampled(owned, size, "total_task_seconds") for size in (small, large)]
        evaluated = [_sampled(owned, size, "evaluate_line") for size in (small, large)]
        slope = (totals[1] - totals[0]) / (large - small)
        eval_slope = (evaluated[1] - evaluated[0]) / (large - small)
        derived.append(
            {
                "rules": rules,
                "lines": [small, large],
                "timings": {
                    "total_task_seconds": totals,
                    "seconds_per_line": slope,
                    "fixed_seconds_per_task": totals[1] - slope * large,
                    "evaluate_seconds_per_line": eval_slope,
                    "plumbing_seconds_per_line": slope - eval_slope,
                },
            }
        )
    return derived


LIMITATIONS = [
    "writer_write_batch is a no-op fake: real PostgreSQL INSERT and commit wait are NOT measured.",
    "repo_fencing_and_complete times fake calls only: real SELECT/UPDATE round-trips NOT measured.",
    "Source is a local temp directory, not a NAS mount: no network read latency of the copy.",
    "nas_read_only_policy=ignore: the read-only guard runs but can never fail the task here.",
    "Stages wrap evaluate_line/MatchRecord/scan_archive and the member spec, so their sums include "
    "the timer overhead, while the unaccounted remainder excludes the per-line clock, the loop "
    "bookkeeping and the finally-block work-dir removal.",
    "archive_open_and_listing is the first line pull: it also holds the first member's decode.",
    "file_glob membership cost tracks whatever the processor does today (no fixed count assumed).",
    "Rule compile is excluded from the task window; each cell is one task with attempts=1.",
    "GC pauses provoked by evaluator allocation land inside whichever stage is running, so "
    "decompress_and_decode is inflated by allocation in the large-ruleset cells.",
    "Corpora are the frozen bench_multimode synthetic workload tiled over members; not real logs.",
    "Times are single-process warm-cache wall-clock on one machine; no DB, no data/_bench writes.",
]


def run_probe(shape: Shape | None = None, **overrides) -> dict:
    settings = replace(Shape(cells=_parse_cells(DEFAULT_CELLS)), **overrides)
    if shape is not None:
        settings = replace(shape, **overrides)
    _validate(settings)
    owns_root = settings.tmp_root is None
    root = Path(mkdtemp(prefix="task-overhead-")) if owns_root else Path(settings.tmp_root)
    source_dir, work_dir = root / "source", root / "work"
    source_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    planned = [pair for pair in settings.cells for _ in range(settings.repeat)]
    deadline = perf_counter() + settings.deadline_seconds
    measured: list[dict] = []
    try:
        for index, (rules, lines) in enumerate(planned):
            if perf_counter() > deadline:
                raise ProbeError(
                    f"deadline of {settings.deadline_seconds}s exceeded before cell {index}; "
                    "reduce --cells (5000 rules costs about 4ms per line)"
                )
            measured.append(_run_cell(index, rules, lines, settings, source_dir, work_dir))
    finally:
        rmtree(root if owns_root else work_dir, ignore_errors=True)
    return {
        "probe": "task_overhead",
        "schema": 1,
        "seed": settings.seed,
        "scenario": settings.scenario,
        "knobs": {
            "files": settings.files,
            "line_bytes": settings.line_bytes,
            "batch_size": settings.batch_size,
            "repeat": settings.repeat,
            "heartbeat_every_lines": settings.heartbeat_every_lines,
            "deadline_seconds": settings.deadline_seconds,
            "fault": settings.fault,
        },
        "requested_cells": [[rules, lines] for rules, lines in planned],
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpu_count": os.cpu_count(),
            "database": False,
            "hyperscan_required": False,
            "workers": 1,
        },
        "tolerance": TOLERANCE,
        "reference_hypothesis": HYPOTHESIS,
        "cells": measured,
        "derived": _derive(measured),
        "limitations": LIMITATIONS,
    }


def deterministic_view(report: dict):
    """Everything except measured durations: identical for the same seed."""

    def strip(value):
        if isinstance(value, dict):
            return {key: strip(item) for key, item in value.items() if key != "timings"}
        if isinstance(value, list):
            return [strip(item) for item in value]
        return value

    return strip(report)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cells", default=DEFAULT_CELLS, help="comma separated RULES:LINES pairs")
    parser.add_argument("--fault", default="none", help=f"inject a broken fake: {FAULTS}")
    parser.add_argument("--tmp-dir", default=None, help="keep artifacts under this directory")
    kinds = {"deadline_seconds": float, "scenario": str}
    for name, help_text in (
        ("files", "members per archive"),
        ("line-bytes", "padded width of every corpus line"),
        ("seed", "make_workload seed"),
        ("scenario", "workload shape from bench_multimode"),
        ("batch-size", "MatchWriter flush threshold"),
        ("heartbeat-every-lines", "production heartbeat cadence"),
        ("repeat", "runs per cell"),
        ("deadline-seconds", "abort instead of overrunning"),
    ):
        field_name = name.replace("-", "_")
        parser.add_argument(
            f"--{name}",
            dest=field_name,
            type=kinds.get(field_name, int),
            default=None,
            help=help_text,
        )
    args = parser.parse_args(argv)
    explicit = ("cells", "fault", "tmp_root")
    overrides = {
        name: value
        for name, value in vars(args).items()
        if name in Shape.__dataclass_fields__ and name not in explicit and value is not None
    }
    try:
        with redirect_stdout(sys.stderr):
            report = run_probe(
                None,
                cells=_parse_cells(args.cells),
                fault=args.fault,
                tmp_root=args.tmp_dir,
                **overrides,
            )
        encoded = json.dumps(report, allow_nan=False)
    except Exception as exc:  # noqa: BLE001 -- fail closed: never emit a partial success report.
        print(f"invalid probe: {exc}", file=sys.stderr)
        return 1
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
