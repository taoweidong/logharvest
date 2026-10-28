import importlib
import importlib.util
import json
from dataclasses import asdict
from decimal import Decimal

import pytest

from scripts.multimode_prototype import ScanStats
from src.domain.rules import evaluator
from src.domain.rules.evaluator import compile_ruleset, evaluate_line
from src.domain.rules.models import KeywordMatch, RegexMatch


def test_module_entrypoint_exists():
    assert importlib.util.find_spec("scripts.bench_multimode"), "benchmark is not implemented"


@pytest.fixture
def bench():
    assert importlib.util.find_spec("scripts.bench_multimode"), "benchmark is not implemented"
    return importlib.import_module("scripts.bench_multimode")


class ControlledPrototype:
    native_available = True
    compile_error = None

    def __init__(self, config, change=lambda rows: rows):
        self.compiled = compile_ruleset(config)
        self.stats = ScanStats()
        self.change = change
        self.databases = {}
        self.route_counts = {
            "native_keywords": 2,
            "screened_regex": 3,
            "compatible_rules": 0,
            "unique_literals": 3,
        }

    @property
    def reference(self):
        raise AssertionError("oracle must not use prototype.reference")

    def evaluate_line(self, line, line_no):
        self.stats.lines += 1
        self.stats.native_scans += 1
        return self.change(evaluate_line(self.compiled, line, line_no))


@pytest.fixture
def controlled(bench, monkeypatch):
    monkeypatch.setattr(bench, "compile_prototype", ControlledPrototype)
    return bench


@pytest.mark.parametrize("scenario", ["sparse", "dense", "fallback", "repeated"])
def test_workload_is_frozen_deterministic_and_reports_real_shape(bench, scenario):
    config, corpus = bench.make_workload(40, 12, scenario, 128, 42)
    again = bench.make_workload(40, 12, scenario, 128, 42)
    assert (config, corpus) == again
    assert isinstance(corpus, tuple)
    assert len(corpus) == 12
    assert len(config.rules) == (64 if scenario == "repeated" else 40)
    assert len({rule.id for rule in config.rules}) == len(config.rules)
    assert {len(line.encode()) for line in corpus} == ({4096} if scenario == "repeated" else {128})
    if scenario == "repeated":
        assert {rule.match.any[0] for rule in config.rules} == {"a" * n for n in range(1, 65)}
        return
    keywords = [rule.match for rule in config.rules if isinstance(rule.match, KeywordMatch)]
    regexes = [rule.match for rule in config.rules if isinstance(rule.match, RegexMatch)]
    assert len(keywords) == 16 and len(regexes) == 24
    assert any(match.any for match in keywords) and any(match.all for match in keywords)
    assert any(not match.case_sensitive for match in keywords)
    assert len({match.model_dump_json() for match in regexes}) < len(regexes)
    if scenario == "fallback":
        assert any("(?<=" in match.pattern for match in regexes)
        assert any("i" in match.flags for match in regexes)
    else:
        assert any(match.pattern == r"\d{1,3}-benchx=(?P<x>\d+)" for match in regexes)
    compiled = compile_ruleset(config)
    rows = sum(len(evaluate_line(compiled, line, n)) for n, line in enumerate(corpus, 1))
    if scenario == "dense":
        assert rows == 40 * 12
    if scenario == "sparse":
        assert 0 < rows < 40 * 12 // 4
    assert corpus != bench.make_workload(40, 12, scenario, 128, 43)[1]


def test_paired_rounds_use_independent_oracle_identical_lines_and_alternate(
    controlled, monkeypatch
):
    calls = []
    oracle_configs = []
    original_compile = evaluator.compile_ruleset

    def oracle_compile(config):
        oracle_configs.append(config)
        return original_compile(config)

    def oracle(compiled, line, line_no):
        calls.append(("reference", line, line_no))
        return evaluate_line(compiled, line, line_no)

    class Recorded(ControlledPrototype):
        def evaluate_line(self, line, line_no):
            calls.append(("prototype", line, line_no))
            assert all(config is not self.compiled.config for config in oracle_configs)
            return super().evaluate_line(line, line_no)

    monkeypatch.setattr(evaluator, "compile_ruleset", oracle_compile)
    monkeypatch.setattr(evaluator, "evaluate_line", oracle)
    monkeypatch.setattr(controlled, "compile_prototype", Recorded)
    report = controlled.run_benchmark(rules=5, lines=2, rounds=2, scenario="dense")
    assert len(oracle_configs) == 1
    assert [item[0] for item in calls] == [
        "reference",
        "reference",
        "prototype",
        "prototype",
        "reference",
        "prototype",
        "reference",
        "prototype",
        "prototype",
        "prototype",
        "reference",
        "reference",
        "reference",
        "prototype",
        "reference",
        "prototype",
    ]
    for label in ("reference", "prototype"):
        observed = [(line, number) for name, line, number in calls if name == label]
        assert observed == observed[:2] * 4
    assert report["rules"] == 5 and report["lines"] == 2
    assert report["output_rows"] == 10 and report["compared_lines"] == 4
    assert report["equality_complete"] is True
    assert report["stats"]["lines"] == 8
    assert report["route_counts"]["native_keywords"] == 2
    assert set(report["compile_seconds"]) == {"reference", "prototype"}
    assert all(len(report[key]) == 64 for key in ("input_sha256", "config_sha256"))
    assert report["versions"]["python"] and report["versions"]["regex"]
    assert "hyperscan" in report["versions"] and report["platform"]
    for label in ("reference", "prototype"):
        timing = report[label]
        assert 0 < timing["min_seconds"] <= timing["median_seconds"] <= timing["max_seconds"]
        assert timing["lines_per_second"] == pytest.approx(2 / timing["median_seconds"])
    assert report["speedup"] > 0 and len(report["rounds"]) == 2
    assert "SLA" in " ".join(report["limitations"])


@pytest.mark.parametrize(
    "change",
    [
        lambda rows: rows[:-1],
        lambda rows: list(reversed(rows)),
        *[
            lambda rows, field=field, value=value: [
                rows[0].model_copy(update={field: value}),
                *rows[1:],
            ]
            for field, value in [
                ("rule_id", "wrong"),
                ("severity", "wrong"),
                ("tags", ["wrong"]),
                ("fields", {"x": "wrong"}),
                ("numeric_value", Decimal("999")),
                ("matched_text", "wrong"),
            ]
        ],
    ],
    ids=["dropped", "reordered", "id", "severity", "tags", "fields", "numeric", "text"],
)
def test_rejects_complete_match_changes(controlled, monkeypatch, change):
    monkeypatch.setattr(
        controlled, "compile_prototype", lambda config: ControlledPrototype(config, change)
    )
    with pytest.raises(ValueError, match="mismatch"):
        controlled.run_benchmark(rules=5, lines=1, rounds=1, scenario="dense")


@pytest.mark.parametrize("during_validation", [False, True])
def test_timeout_invalidates_even_equal_empty_results(controlled, monkeypatch, during_validation):
    calls = 0
    previous_handlers = list(evaluator.logger.handlers)

    def timeout(*args):
        nonlocal calls
        calls += 1
        if not during_validation or calls > 2:
            evaluator.logger.warning("rule x: regex timed out after 1 ms on line 1")
        return []

    monkeypatch.setattr(evaluator, "evaluate_line", timeout)
    monkeypatch.setattr(ControlledPrototype, "evaluate_line", timeout)
    with pytest.raises(ValueError, match="timeout"):
        controlled.run_benchmark(rules=5, lines=1, rounds=1, scenario="dense")
    assert evaluator.logger.handlers == previous_handlers


def test_timeout_counter_is_authoritative_without_exception_propagation(bench):
    import contextlib
    import logging

    guard = bench.RejectTimeout()
    record = logging.LogRecord(
        "t",
        logging.WARNING,
        __file__,
        1,
        "rule r1: regex timed out after 50 ms on line 3",
        (),
        None,
    )
    # 即使日志实现不再向外传播异常，计数门禁也必须判无效
    with contextlib.suppress(ValueError):
        guard.emit(record)
    assert guard.count == 1
    with pytest.raises(ValueError, match="1 regex timeout"):
        guard.verify()
    clean = bench.RejectTimeout()
    clean.verify()


@pytest.mark.parametrize("failure", ["unavailable", "fallback"])
def test_cli_fails_closed_without_json_on_native_failure(controlled, monkeypatch, capsys, failure):
    class Broken(ControlledPrototype):
        native_available = failure != "unavailable"
        compile_error = "not installed" if failure == "unavailable" else None

        def evaluate_line(self, line, line_no):
            self.stats.fallback_lines += 1
            return super().evaluate_line(line, line_no)

    monkeypatch.setattr(controlled, "compile_prototype", Broken)
    assert controlled.main(["--rules", "5", "--lines", "1", "--rounds", "1"]) != 0
    captured = capsys.readouterr()
    assert not captured.out and captured.err


def test_rejects_correct_results_when_native_path_never_runs(controlled, monkeypatch):
    class NeverScans(ControlledPrototype):
        def evaluate_line(self, line, line_no):
            self.stats.lines += 1
            return evaluate_line(self.compiled, line, line_no)

    monkeypatch.setattr(controlled, "compile_prototype", NeverScans)
    with pytest.raises(ValueError, match="native scans"):
        controlled.run_benchmark(rules=5, lines=1, rounds=1, scenario="dense")


@pytest.mark.parametrize("flag", ["--rules", "--lines", "--rounds", "--line-bytes"])
@pytest.mark.parametrize("value", ["0", "-1", "1000001"])
def test_cli_rejects_nonpositive_or_unbounded_parameters(bench, capsys, flag, value):
    with pytest.raises(SystemExit) as error:
        bench.main([flag, value])
    assert error.value.code != 0
    assert not capsys.readouterr().out


def test_cli_outputs_json_with_reproducible_fingerprints(controlled, capsys):
    args = ["--rules", "5", "--lines", "2", "--rounds", "1", "--scenario", "dense"]
    assert controlled.main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert controlled.main(args) == 0
    second = json.loads(capsys.readouterr().out)
    assert first["seed"] == 42
    assert first["input_sha256"] == second["input_sha256"]
    assert first["config_sha256"] == second["config_sha256"]
    assert controlled.main([*args, "--seed", "43"]) == 0
    assert first["input_sha256"] != json.loads(capsys.readouterr().out)["input_sha256"]
    assert controlled.main(["--lines", "10000", "--line-bytes", "65536"]) != 0
    assert not capsys.readouterr().out


@pytest.mark.parametrize("scenario", ["sparse", "dense", "fallback", "repeated"])
def test_small_native_integration(bench, scenario):
    pytest.importorskip("hyperscan", reason="optional native prototype dependency")
    report = bench.run_benchmark(rules=40, lines=12, rounds=2, scenario=scenario)
    assert report["equality_complete"] and report["stats"]["fallback_lines"] == 0
    assert report["stats"]["native_scans"] > 0
    assert report["native_databases"]
    assert report["rules"] == (64 if scenario == "repeated" else 40)
    assert report["output_rows"] > 0
    assert set(report["stats"]) == set(asdict(ScanStats()))
    if scenario == "fallback":
        assert report["route_counts"]["compatible_rules"] == 24
    if scenario == "repeated":
        assert report["line_bytes"] == 4096
        assert report["stats"]["literal_events"] == 64 * 12 * 4
