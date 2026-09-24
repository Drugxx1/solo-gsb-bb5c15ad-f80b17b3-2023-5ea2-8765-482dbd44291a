"""字幕智能断行：按显示列数 / 行数限制对每条字幕正文做最优折行。

处理流程（每条正文相互独立）：

1. **归一化**：把原有换行与所有连续 Unicode 空白（含全角空格、制表符）折成
   单个半角空格，并去掉正文首尾空白；
2. **可断位置**：空格处（折行后空格丢弃）以及汉字 / 中文标点与任意字符的
   相邻处；连续的非汉字非空白字符（英文单词、数字串等）整体不可拆；
3. **显示列宽**：东亚宽字符 / 全角字符（Unicode East Asian Width 为 ``W``
   或 ``F``）计 2 列，组合字符（通用类别 ``Mn`` / ``Me``）计 0 列，
   其余可见字符计 1 列；
4. **避头尾**：闭标点不允许出现在行首，开标点不允许出现在行尾；
5. **动态规划**：在每行不超过 ``max_columns`` 列、总行数不超过
   ``max_lines`` 的可行方案中，最小化各行剩余列数（列宽上限 − 实际列宽）
   的平方和；同分时按断点序列（断点位置元组）取字典序靠前者。

排不下（单个不可拆片段超宽，或行数限制内无满足避头尾的方案）的字幕保留
原正文不动，并给出输入顺序号与原因。

重写通过直接替换原始物理行实现：序号、时间轴、``X1:``/``Position`` 等
定位参数、空行、原有换行（CRLF/LF/CR）以及开头 BOM 均保持原样。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .srt import BOM, split_line_eol, split_physical_lines

# ---------------------------------------------------------------------------
# 字符分类
# ---------------------------------------------------------------------------

# 开标点（禁止出现在行尾）
_OPENING_PUNCT: frozenset[str] = frozenset(
    "（《【「『〈〔〖〘〚｟"  # CJK 左括号 / 左引号
    "“‘〝"
    "([{"
)

# 闭标点（禁止出现在行首）
_CLOSING_PUNCT: frozenset[str] = frozenset(
    "）》】」』〉〕〗〙〛｠"  # CJK 右括号 / 右引号
    "”’〞〟"
    ")]}"
    "，。！？；：、…—·～"
    ",.;:!?%"
    "％℃°"
    "・"
)

_WHITESPACE_RE = re.compile(r"\s+")


def _is_han(ch: str) -> bool:
    """是否为汉字（CJK 统一表意文字各区块；假名不在断行相邻判定范围内）。"""
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF
        or 0x3400 <= code <= 0x4DBF
        or 0x20000 <= code <= 0x2A6DF
        or 0x2A700 <= code <= 0x2B73F
        or 0x2B740 <= code <= 0x2B81F
        or 0x2B820 <= code <= 0x2CEAF
        or 0xF900 <= code <= 0xFAFF
        or 0x2F800 <= code <= 0x2FA1F
    )


def _is_cjk_punct(ch: str) -> bool:
    """是否为中文标点 / CJK 符号（参与“汉字/中文标点相邻处”的断行判定）。"""
    code = ord(ch)
    return (
        0x3000 <= code <= 0x303F  # CJK 符号和标点
        or ch in _OPENING_PUNCT
        or ch in _CLOSING_PUNCT
        or 0xFF00 <= code <= 0xFF60  # 全角 ASCII（含全角标点）
    )


def _is_cjk_adjacent(ch: str) -> bool:
    """汉字 / 中文标点：与任意字符的相邻处均可断。"""
    return _is_han(ch) or _is_cjk_punct(ch)


def display_width(ch: str) -> int:
    """单个字符的显示列数。

    - 东亚宽字符 / 全角字符（East Asian Width 为 ``W`` / ``F``）：2 列；
    - 组合字符（通用类别 ``Mn`` / ``Me``，如重音组合记号）：0 列；
    - 格式控制等不可见字符（``Cf``）：0 列；
    - 其他可见字符：1 列。
    """
    category = unicodedata.category(ch)
    if category in ("Mn", "Me", "Cf"):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


# ---------------------------------------------------------------------------
# 归一化与切分
# ---------------------------------------------------------------------------


def normalize_body(text: str) -> str:
    """原有换行及连续空白折成单个半角空格，并去掉首尾空白。"""
    return _WHITESPACE_RE.sub(" ", text).strip()


def line_display_width(text: str) -> int:
    """一行成品文本的显示列宽（宽/全角 2 列、组合字符 0 列、其余 1 列）。"""
    return sum(display_width(ch) for ch in text)


@dataclass(frozen=True)
class Unit:
    """归一化文本中的一个可排印单位。

    ``ch`` 为单位文本：普通单位是单个非空白字符（含附着在其后的组合字符
    序列，见 :func:`build_units`），英文单词则是连续的非汉字非空白字符串；
    ``space_before`` 表示该单位前是否有一个空格断点。
    """

    ch: str
    space_before: bool
    width: int


def build_units(text: str) -> list[Unit]:
    """把归一化后的正文切成单位序列。

    汉字 / 中文标点各自成单位；连续的其他非空白可见字符（英文单词、数字、
    URL 等）合并为一个不可拆单位。组合字符（``Mn`` / ``Me``）附着在紧邻的
    前一个字符上（即便它属于英文单词或位于空格之后），不单独成单位。
    """
    units: list[Unit] = []
    i = 0
    pending_space = False
    length = len(text)
    while i < length:
        ch = text[i]
        if ch == " ":
            pending_space = True
            i += 1
            continue

        start = i
        if _is_cjk_adjacent(ch):
            i += 1
        else:
            while i < length and text[i] != " " and not _is_cjk_adjacent(text[i]):
                i += 1
        # 组合记号附着在刚切出的基字符（或英文单词）之后，跨越基字符吸收
        while i < length and unicodedata.category(text[i]) in ("Mn", "Me"):
            i += 1
        token = text[start:i]
        width = sum(display_width(c) for c in token)
        units.append(
            Unit(ch=token, space_before=pending_space, width=max(width, 0))
        )
        pending_space = False
    return units


# ---------------------------------------------------------------------------
# 动态规划
# ---------------------------------------------------------------------------

# 排不下的原因分类
REASON_ATOM_TOO_WIDE = "atom_too_wide"
REASON_TOO_MANY_LINES = "too_many_lines"


class WrapError(ValueError):
    """单条字幕在给定行数 / 列数限制内无法排下。"""

    reason = ""


class AtomTooWideError(WrapError):
    """存在宽度超过单行列数上限的不可拆片段（英文单词等）。"""

    reason = REASON_ATOM_TOO_WIDE


class TooManyLinesError(WrapError):
    """满足列数与避头尾约束所需的最少行数仍超过每条最多行数。"""

    reason = REASON_TOO_MANY_LINES


@dataclass(frozen=True)
class Gap:
    """单位 ``u-1`` 与单位 ``u`` 之间的可断位置（``u`` 从 1 开始）。

    两个单位之间若有空格，存在两种合法断法：``at_space=True`` 在空格处折
    （空格丢弃）；``at_space=False`` 仅在汉字 / 中文标点相邻处折（空格保留
    在后行行首并计入列宽，实际上只会出现在两侧都是汉字类字符的场合）。
    """

    u: int
    at_space: bool


def find_gaps(units: list[Unit]) -> list[Gap]:
    """枚举所有可断位置：空格处，以及汉字 / 中文标点与任意字符的相邻处。

    空格与汉字相邻规则同时成立（如“你好 世界”）时给出两种断法，由动态
    规划按代价选择；开标点与前导字符、闭标点与后继字符之间即使可断也列出，
    避头尾约束在动态规划时按断点两侧字符裁决。
    """
    gaps: list[Gap] = []
    for u in range(1, len(units)):
        prev_ch = units[u - 1].ch[-1]
        cur_ch = units[u].ch[0]
        if units[u].space_before:
            gaps.append(Gap(u=u, at_space=True))
        if _is_cjk_adjacent(prev_ch) and _is_cjk_adjacent(cur_ch):
            gaps.append(Gap(u=u, at_space=False))
    return gaps


def _cut_allowed(left: Unit, right: Unit) -> bool:
    """断点是否满足避头尾：行尾非开标点、行首非闭标点。

    空格断点本身会被丢弃，实际行尾 / 行首仍是空格两侧的可见单位；非空格
    断点两侧单位各自成线。两种情形都按两侧首末字符检查即可。
    """
    if left.ch[-1] in _OPENING_PUNCT:
        return False
    if right.ch[0] in _CLOSING_PUNCT:
        return False
    return True


def wrap_cue(text: str, max_columns: int, max_lines: int) -> list[str]:
    """对一条字幕正文求最优断行，返回各行文本（空格断点处分行则丢弃空格）。

    无法排下时抛出 :class:`AtomTooWideError` 或 :class:`TooManyLinesError`。
    """
    normalized = normalize_body(text)
    units = build_units(normalized)
    if not units:
        # 解析器已拒绝纯空白正文，这里仅作兜底
        raise AtomTooWideError("正文仅含空白字符。")

    # 不可拆片段超宽：英文单词等单位自身超过单行列数上限
    for unit in units:
        if unit.width > max_columns:
            raise AtomTooWideError(
                f"不可拆片段“{unit.ch}”宽 {unit.width} 列，超过每行最大 "
                f"{max_columns} 列。"
            )

    gaps = find_gaps(units)
    n = len(units)

    # 前缀宽度：prefix_w[i] 为单位 0..i-1 的宽度之和；prefix_sp[i] 为
    # 0..i-1 中“前面带空格”的单位数（单位 0 的前导空格已被 strip）。
    prefix_w = [0] * (n + 1)
    prefix_sp = [0] * (n + 1)
    for i, unit in enumerate(units):
        prefix_w[i + 1] = prefix_w[i] + unit.width
        prefix_sp[i + 1] = prefix_sp[i] + (1 if unit.space_before else 0)

    def line_width(lo: int, hi: int, cut_at_space: bool) -> int:
        """单位 [lo, hi) 排在同一行的显示列宽。

        行 [lo, hi) 内部单位间的空格计 1 列；但当该行的进入断点 ``lo`` 是
        “在空格处折行”时，单位 ``lo`` 前的空格随折行丢弃，不计入行宽。
        """
        spaces = prefix_sp[hi] - prefix_sp[lo + 1] if hi > lo else 0
        if lo > 0 and units[lo].space_before and not cut_at_space:
            spaces += 1  # 空格保留在本行行首
        return prefix_w[hi] - prefix_w[lo] + spaces

    # 同一边界可能有两种断法（在空格处折 / 在汉字相邻处折、保留空格）
    gaps_by_u: dict[int, list[Gap]] = {}
    for gap in gaps:
        gaps_by_u.setdefault(gap.u, []).append(gap)

    # dp[j]：一条“当前行从单位 j 开始”的开放路径，键为
    # (j 之前已完成的行数, 进入该行的断法)；进入断法决定单位 j 前的空格
    # 是否保留在行首（"space" 已随折行丢弃；"keep" 保留并计 1 列；
    # "start" 为首行）。值为 (前序行累计代价, 断点序列)。
    #
    # 断点序列元素为 (断点位置 u, 断法)，断法 0=保留空格、1=空格处折；
    # 代价相同时直接比较该元组，即“断点序列优先靠前”（位置先比，位置相同
    # 时优先保留空格的汉字相邻断法）。
    Cut = tuple[int, int]  # (位置 u, 0=keep / 1=space)
    StateVal = tuple[int, tuple[Cut, ...]]
    dp: list[dict[tuple[int, str], StateVal]] = [dict() for _ in range(n + 1)]
    dp[0][(0, "start")] = (0, ())

    for j in range(0, n):
        if not dp[j]:
            continue
        if j == 0:
            boundaries: tuple[str, ...] = ("start",)
        else:
            # 每个 gap 是一种合法断法；kind 即新一行的进入断法
            boundaries = tuple(
                "space" if gap.at_space else "keep"
                for gap in gaps_by_u.get(j, ())
                if _cut_allowed(units[j - 1], units[j])
            )
        for kind in boundaries:
            for (completed, entry), (prev_cost, prev_cuts) in tuple(dp[j].items()):
                if entry != kind:
                    continue
                line_no = completed + 1  # 当前开放行是第几行
                if line_no > max_lines:
                    continue
                new_cuts = (
                    prev_cuts
                    if j == 0
                    else prev_cuts + ((j, 1 if kind == "space" else 0),)
                )
                for i in range(j + 1, n + 1):
                    width = line_width(j, i, kind == "space")
                    if width > max_columns:
                        # 单位宽度均非负：继续延长只会更宽
                        break
                    # 行 [j,i) 至此闭合：其代价入账，已完成行数 +1。
                    # 下一行从单位 i 开始，其“进入断法”由边界 i 上实际
                    # 选用的 gap 决定；因此要为该边界的每种合法 gap 各存
                    # 一份状态（j'=i 时只读取匹配自己进入断法的那份）。
                    candidate = (
                        prev_cost + (max_columns - width) ** 2,
                        new_cuts,
                    )
                    if i == n:
                        keys = ((line_no, "end"),)
                    else:
                        keys = tuple(
                            (line_no, "space" if ng.at_space else "keep")
                            for ng in gaps_by_u.get(i, ())
                            if _cut_allowed(units[i - 1], units[i])
                        )
                    for key in keys:
                        old = dp[i].get(key)
                        if old is None or candidate < old:
                            dp[i][key] = candidate

    end_states = dp[n]
    if not end_states:
        raise TooManyLinesError(
            f"在每行 {max_columns} 列的限制内，满足避头尾规则所需的最少行数"
            f"超过每条最多 {max_lines} 行。"
        )

    # 在所有可行（行数 <= max_lines）方案中取最优；代价同分由断点序列裁决
    _, cuts = min(end_states.values())
    # 每行的进入断法：首行无前导空格；之后由该行起点处断点的标记决定
    enter_space_dropped = [False] + [cut[1] == 1 for cut in cuts]
    bounds = (0,) + tuple(cut[0] for cut in cuts) + (n,)
    lines: list[str] = []
    for idx, (lo, hi) in enumerate(zip(bounds[:-1], bounds[1:])):
        pieces: list[str] = []
        for k in range(lo, hi):
            if units[k].space_before and (pieces or not enter_space_dropped[idx]):
                # 行内空格保留；行首空格仅在“保留空格”断法（标记 0）下保留
                pieces.append(" ")
            pieces.append(units[k].ch)
        lines.append("".join(pieces))
    return lines


# ---------------------------------------------------------------------------
# SRT 无损重写
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WrapResult:
    """一条成功断行字幕的结果。"""

    cue_order: int
    index: str
    original_text: str
    lines: tuple[str, ...]
    line_count: int
    changed: bool


@dataclass(frozen=True)
class SkippedCue:
    """排不下、保留原正文的字幕。"""

    cue_order: int
    index: str
    reason_code: str
    reason: str


def _choose_eol(lines: list[str], start_line: int, end_line: int) -> str:
    """选择该字幕块新行的行尾：沿用原正文第一行行尾；原正文是文件末行而
    无行尾时，退回时间轴行行尾；再退回 ``\\n``。"""
    if start_line < end_line:
        _, eol = split_line_eol(lines[start_line])
        if eol:
            return eol
    _, eol = split_line_eol(lines[start_line - 1])
    return eol or "\n"


def apply_wrap(
    content: str,
    cue_indices: list[str],
    body_ranges: list[tuple[int, int]],
    max_columns: int,
    max_lines: int,
) -> tuple[str, list[WrapResult], list[SkippedCue]]:
    """在原始 SRT 上只替换成功断行字幕的正文，其余内容保持原样。

    ``cue_indices`` / ``body_ranges`` 与 :func:`app.srt.parse_document`
    解析出的字幕一一对应（按输入顺序）。全部排不下或正文无需改动时，
    返回 SRT 与输入逐字符一致（含开头 BOM 数量与换行风格）。
    """
    bom_count = 0
    while bom_count < len(content) and content[bom_count] == BOM:
        bom_count += 1
    lines = split_physical_lines(content[bom_count:])

    results: list[WrapResult] = []
    skipped: list[SkippedCue] = []
    replacements: list[tuple[int, int, list[str]]] = []

    for cue_order, (index, (start_line, end_line)) in enumerate(
        zip(cue_indices, body_ranges), start=1
    ):
        original_parts = [
            split_line_eol(lines[k])[0] for k in range(start_line, end_line)
        ]
        original_text = "\n".join(original_parts)
        try:
            wrapped = wrap_cue(original_text, max_columns, max_lines)
        except WrapError as exc:
            skipped.append(
                SkippedCue(
                    cue_order=cue_order,
                    index=index,
                    reason_code=type(exc).reason,
                    reason=str(exc),
                )
            )
            continue

        new_text = "\n".join(wrapped)
        result = WrapResult(
            cue_order=cue_order,
            index=index,
            original_text=original_text,
            lines=tuple(wrapped),
            line_count=len(wrapped),
            changed=new_text != original_text,
        )
        results.append(result)

        if not result.changed:
            continue
        eol = _choose_eol(lines, start_line, end_line)
        last_eol = split_line_eol(lines[end_line - 1])[1]
        new_lines = [part + eol for part in wrapped[:-1]]
        new_lines.append(wrapped[-1] + last_eol)
        replacements.append((start_line, end_line, new_lines))

    if not replacements:
        return content, results, skipped

    out: list[str] = []
    cursor = 0
    # body_ranges 互不相交且按输入顺序递增，顺序拼接即可
    for start_line, end_line, new_lines in replacements:
        out.extend(lines[cursor:start_line])
        out.extend(new_lines)
        cursor = end_line
    out.extend(lines[cursor:])

    # 与 srt.rewrite_srt / terms.apply_terms 一致：原文件开头无论几个 BOM，
    # 重写输出最多保留一个
    return (BOM if bom_count else "") + "".join(out), results, skipped
