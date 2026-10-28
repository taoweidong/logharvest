"""纯 Python Aho–Corasick 多模式字面量匹配自动机。

为什么不用组合正则：5000 个字面量的 ``(?=(a|b|…))`` lookahead 交替在 ``regex``
模块上没有按 trie 优化（实测 5000 针 × 200 字符行 ≈ 1.9 ms/行，反而比逐针
substring 慢 2.5 倍）；AC 自动机单次扫描 O(行长 + 命中数)，同场景实测 16 倍。

语义：:meth:`find_all` 返回**在文本中出现过的**针编号集合——每个针至少有一个
出现位置就会被报告（含前后缀重叠，如针 ``ab`` 与 ``abc`` 同时出现在 ``abc`` 中），
因此作为规则求值的候选预筛时**零假阴性**；重叠针靠输出链（output links）保证。
"""

from __future__ import annotations

from collections import deque


class AhoCorasick:
    """按构造顺序为每个针分配编号；重复针会合并到同一编号。"""

    __slots__ = ("_built", "_children", "_fail", "_outputs", "needle_count")

    def __init__(self, needles: list[str]) -> None:
        # 状态 0 是根；children/fail/outputs 三个列表按下标平行增长。
        self._children: list[dict[str, int]] = [{}]
        self._fail: list[int] = [0]
        self._outputs: list[list[int]] = [[]]
        self._built = False
        ids: dict[str, int] = {}
        for needle in needles:
            if needle in ids:
                continue  # 重复针合并：find_all 返回集合语义，编号唯一即可
            state = 0
            for ch in needle:
                nxt = self._children[state].get(ch)
                if nxt is None:
                    self._children.append({})
                    self._fail.append(0)
                    self._outputs.append([])
                    nxt = len(self._children) - 1
                    self._children[state][ch] = nxt
                state = nxt
            ids[needle] = state
            self._outputs[state].append(len(ids) - 1)
        self.needle_count = len(ids)

    def build(self) -> None:
        """构建失败链与输出链；必须在 find_all 之前调用一次。"""
        queue: deque[int] = deque()
        for child in self._children[0].values():
            self._fail[child] = 0
            queue.append(child)
        while queue:
            state = queue.popleft()
            for ch, child in self._children[state].items():
                queue.append(child)
                follow = self._fail[state]
                while follow and ch not in self._children[follow]:
                    follow = self._fail[follow]
                fail_child = self._children[follow].get(ch, 0)
                self._fail[child] = 0 if fail_child == child else fail_child
                # 输出链：本状态的针 + 失败链目标的针（合并到同列表，扫描时零跳转）
                self._outputs[child] = self._outputs[child] + self._outputs[self._fail[child]]
        self._built = True

    def find_all(self, text: str) -> set[int]:
        """返回在 text 中出现过的针编号集合；未 build 或空自动机返回空集。"""
        if not self._built or not self.needle_count:
            return set()
        found: set[int] = set()
        children = self._children
        outputs = self._outputs
        fail = self._fail
        state = 0
        # 空针（""）存在于根状态输出：它在任何文本中都"出现"（`"" in text` 恒真），
        # 与朴素 substring 语义保持一致。
        if outputs[0]:
            found.update(outputs[0])
        for ch in text:
            while state and ch not in children[state]:
                state = fail[state]
            state = children[state].get(ch, 0)
            if outputs[state]:
                found.update(outputs[state])
        return found
