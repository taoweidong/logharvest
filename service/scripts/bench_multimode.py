"""Deterministic, no-DB evaluator benchmark; opt-in native prototype only."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import platform
import random
import sys
from contextlib import redirect_stdout
from dataclasses import asdict
from importlib.metadata import PackageNotFoundError, version
from statistics import median
from time import perf_counter

from scripts.multimode_prototype import compile_prototype
from src.domain.rules import evaluator
from src.domain.rules.models import KeywordMatch, RegexMatch, Rule, RuleSetConfig, Threshold

SCENARIOS = ("sparse", "dense", "fallback", "repeated")
LIMITS = {"rules": 100_000, "lines": 10_000, "rounds": 100, "line_bytes": 65_536}


def make_workload(rules: int, lines: int, scenario: str, line_bytes: int, seed: int):
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario: {scenario}")
    for name, value in (("rules", rules), ("lines", lines), ("line_bytes", line_bytes)):
        if not 1 <= value <= LIMITS[name]:
            raise ValueError(f"{name} must be in 1..{LIMITS[name]}")
    width = 4096 if scenario == "repeated" else line_bytes
    if lines * width > 16 * 1024 * 1024:
        raise ValueError("frozen corpus exceeds 16 MiB; reduce --lines or --line-bytes")
    if scenario == "repeated":
        config = RuleSetConfig(
            name=scenario,
            rules=[
                Rule(id=f"r{i}", match=KeywordMatch(any=["a" * i], case_sensitive=True))
                for i in range(1, 65)
            ],
        )
        return config, ("a" * width,) * lines

    def tokens(group):
        return (
            "KEY" + (f"{group:05d}" if group else ""),
            "benchx" + (f"{group:05d}" if group else ""),
        )

    configured = []
    for i in range(rules):
        group = i // 10 if scenario == "sparse" else 0
        key, literal = tokens(group)
        mode = i % 5
        threshold = None
        if mode < 2:
            words = [key, "MISSING"] if mode == 0 else [key, "READY"]
            match = KeywordMatch(
                **{"any" if mode == 0 else "all": words}, case_sensitive=bool(group % 2)
            )
        else:
            pattern = (
                rf"\d{{1,3}}-{literal}=(?P<x>\d+)"
                if mode == 2
                else rf"{literal}=(?P<x>\d+)"
                if mode == 3
                else f"signal{literal}"
            )
            flags = ""
            if scenario == "fallback":
                if mode == 2:
                    pattern = rf"(?<={literal}=)(?P<x>\d+)"
                else:
                    flags = "i"
            match = RegexMatch(pattern=pattern, flags=flags)
            if mode == 2:
                threshold = Threshold(field="x", op="gte", value="10")
        configured.append(
            Rule(
                id=f"r{i}",
                match=match,
                threshold=threshold,
                severity="warning" if i % 2 else "info",
                tags=["bench", str(i % 3)],
            )
        )
    rng = random.Random(seed)
    corpus = []
    for i in range(lines):
        group = rng.randrange((rules + 9) // 10) if scenario == "sparse" else 0
        key, literal = tokens(group)
        value = rng.randint(10, 999)
        text = f"{key.lower()} {key} READY 12-{literal}={value} signal{literal} seq={i} "
        if scenario == "sparse" and i % 10:
            text = f"ordinary unrelated log seq={i} value={value} "
        corpus.append(text[:width].ljust(width, "."))
    return RuleSetConfig(name=scenario, rules=configured), tuple(corpus)


class RejectTimeout(logging.Handler):
    """Counts evaluator timeout warnings; the caller must assert `count == 0`.

    Raising from ``emit`` happens to propagate in CPython today, but that is an
    implementation detail, so the authoritative signal is the counter.
    """

    def __init__(self, level=logging.NOTSET) -> None:
        super().__init__(level)
        self.count = 0
        self.last = ""

    def emit(self, record):
        message = record.getMessage()
        if "timed out" in message or "timeout" in message.lower():
            self.count += 1
            self.last = message
            raise ValueError(f"invalid benchmark: regex timeout: {message}")

    def verify(self):
        if self.count:
            raise ValueError(f"invalid benchmark: {self.count} regex timeout(s): {self.last}")


def _timed(call):
    start = perf_counter()
    result = call()
    return result, perf_counter() - start


def _summary(times, lines, rows):
    middle = median(times)
    return {
        "seconds": times,
        "median_seconds": middle,
        "min_seconds": min(times),
        "max_seconds": max(times),
        "lines_per_second": lines / middle,
        "rows_per_second": rows / middle,
    }


def _version(package):
    try:
        return version(package)
    except PackageNotFoundError:
        return None


def run_benchmark(*, rules=5000, lines=500, rounds=5, scenario="sparse", line_bytes=128, seed=42):
    if not 1 <= rounds <= LIMITS["rounds"]:
        raise ValueError(f"rounds must be in 1..{LIMITS['rounds']}")
    config, corpus = make_workload(rules, lines, scenario, line_bytes, seed)
    config_bytes = json.dumps(
        config.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    input_hash = hashlib.sha256()
    for line in corpus:
        data = line.encode("utf-8")
        input_hash.update(len(data).to_bytes(8, "big"))
        input_hash.update(data)

    guard = RejectTimeout(logging.WARNING)
    logger = evaluator.logger
    old_level, old_disabled = logger.level, logger.disabled
    logger.disabled = False
    logger.setLevel(logging.WARNING)
    logger.addHandler(guard)
    try:
        # Compile independent deep copies: never take the oracle from prototype.reference.
        reference, reference_compile = _timed(
            lambda: evaluator.compile_ruleset(config.model_copy(deep=True))
        )
        prototype, prototype_compile = _timed(
            lambda: compile_prototype(config.model_copy(deep=True))
        )
        if not prototype.native_available or prototype.compile_error:
            raise ValueError(f"native unavailable: {prototype.compile_error}")
        runners = {
            "reference": lambda line, n: evaluator.evaluate_line(reference, line, n),
            "prototype": prototype.evaluate_line,
        }
        paired = []
        rows = 0
        for index in range(rounds):
            order = ["reference", "prototype"] if index % 2 == 0 else ["prototype", "reference"]
            measured, counts = {}, {}
            for name in order:
                run = runners[name]
                # Discard each list immediately: output memory is O(rules), not O(lines*rules).
                counts[name], measured[name] = _timed(
                    lambda run=run: sum(len(run(line, n)) for n, line in enumerate(corpus, 1))
                )
            rows = 0
            for n, line in enumerate(corpus, 1):
                expected = runners["reference"](line, n)
                actual = runners["prototype"](line, n)
                if expected != actual:
                    raise ValueError(f"result mismatch in round {index + 1}, line {n}")
                rows += len(expected)
            if any(count != rows for count in counts.values()):
                raise ValueError(f"timed output count mismatch in round {index + 1}")
            if prototype.stats.fallback_lines:
                raise ValueError(
                    f"unexpected native fallback_lines={prototype.stats.fallback_lines}"
                )
            if not prototype.stats.native_scans:
                raise ValueError("no native scans; the fast path was never exercised")
            paired.append(
                {
                    "order": order,
                    "seconds": measured,
                    "output_rows": rows,
                    "speedup": measured["reference"] / measured["prototype"],
                }
            )
        guard.verify()
    finally:
        logger.removeHandler(guard)
        logger.setLevel(old_level)
        logger.disabled = old_disabled

    summaries = {
        name: _summary([item["seconds"][name] for item in paired], lines, rows)
        for name in ("reference", "prototype")
    }
    databases = []
    for sensitive, database in getattr(prototype, "databases", {}).items():
        info = database.info()
        databases.append(
            {
                "case_sensitive": sensitive,
                "size_bytes": database.size(),
                "info": info.decode(errors="replace") if isinstance(info, bytes) else info,
            }
        )
    return {
        "scenario": scenario,
        "seed": seed,
        "requested_rules": rules,
        "rules": len(config.rules),
        "lines": len(corpus),
        "requested_line_bytes": line_bytes,
        "line_bytes": len(corpus[0].encode()),
        "input_sha256": input_hash.hexdigest(),
        "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
        "fingerprint_format": "config: sorted compact JSON; input: uint64be length + UTF-8/line",
        "versions": {
            "python": platform.python_version(),
            "regex": _version("regex"),
            "hyperscan": _version("hyperscan"),
        },
        "platform": platform.platform(),
        "native_databases": databases,
        "native_available": prototype.native_available,
        "compile_seconds": {"reference": reference_compile, "prototype": prototype_compile},
        "output_rows": rows,
        "compared_lines": lines * rounds,
        "equality_complete": True,
        "rounds": paired,
        **summaries,
        "speedup": summaries["reference"]["median_seconds"]
        / summaries["prototype"]["median_seconds"],
        "route_counts": prototype.route_counts,
        "stats": asdict(prototype.stats),
        "stats_scope": "all timed and untimed validation passes; two passes per round",
        "limitations": [
            "Synthetic evaluator-only, not an SLA; excludes DB, IO and decompression.",
            "Full ordered equality checked in separate untimed passes after each AB/BA pair.",
            "Warm-process timings include allocation/counting; caches are not cleared.",
            "Separate compilation includes deep copy; prototype also compiles its reference.",
            "Frozen corpus capped at 16 MiB; only one line's results per evaluator retained.",
            "Repeated overrides requested rules/line bytes: 64 nested literals, 4096 bytes.",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)

    def bounded(name):
        def parse(value):
            number = int(value)
            if not 1 <= number <= LIMITS[name]:
                raise argparse.ArgumentTypeError(f"{name} must be in 1..{LIMITS[name]}")
            return number

        return parse

    for name, default in (("rules", 5000), ("lines", 500), ("rounds", 5), ("line_bytes", 128)):
        parser.add_argument("--" + name.replace("_", "-"), type=bounded(name), default=default)
    parser.add_argument("--scenario", choices=SCENARIOS, default="sparse")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)
    try:
        with redirect_stdout(sys.stderr):
            report = run_benchmark(**vars(args))
        encoded = json.dumps(report, allow_nan=False)
    except Exception as exc:  # noqa: BLE001 -- CLI fails closed, never emits a partial success report.
        print(f"invalid benchmark: {exc}", file=sys.stderr)
        return 1
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
