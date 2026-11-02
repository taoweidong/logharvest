"""Aho–Corasick 自动机的边界分支：未 build、空自动机、空针与重叠针。"""

from src.domain.rules.aho import AhoCorasick


def test_find_all_before_build_returns_empty_set() -> None:
    # 未调用 build（失败链/输出链未构建）时必须返回空集而不是崩溃或漏配。
    automaton = AhoCorasick(["error", "timeout"])
    assert automaton.find_all("an error happened") == set()


def test_empty_automaton_returns_empty_set_even_after_build() -> None:
    automaton = AhoCorasick([])
    automaton.build()
    assert automaton.needle_count == 0
    assert automaton.find_all("anything") == set()


def test_find_all_reports_every_needle_present_including_overlaps() -> None:
    # 经典 she/his/hers 样本：`ushers` 同时包含 he/she/hers。
    automaton = AhoCorasick(["he", "she", "his", "hers"])
    automaton.build()
    assert automaton.find_all("ushers") == {0, 1, 3}
    assert automaton.find_all("his") == {2}
    assert automaton.find_all("x") == set()


def test_duplicate_needles_merge_to_one_id() -> None:
    automaton = AhoCorasick(["err", "err", "ok"])
    automaton.build()
    assert automaton.needle_count == 2
    assert automaton.find_all("error ok") == {0, 1}


def test_empty_needle_is_constant_true_like_substring_semantics() -> None:
    # `"" in text` 恒真：空针存在于根状态输出，任何文本（含空文本）都报告它，
    # 与朴素 substring 预筛语义保持一致（零假阴性）。
    automaton = AhoCorasick(["", "error"])
    automaton.build()
    assert automaton.find_all("") == {0}
    assert automaton.find_all("an error") == {0, 1}
