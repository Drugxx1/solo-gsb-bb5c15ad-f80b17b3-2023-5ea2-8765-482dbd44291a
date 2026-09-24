"""字幕时间漂移校正、上下位置规划与阅读负荷热区 Web 服务（FastAPI）。"""
from __future__ import annotations

import math
from pathlib import Path

from fractions import Fraction

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, StrictFloat, StrictInt, field_validator

from .mapping import AnchorError, PiecewiseMapping, build_anchors
from .placement import (
    Block,
    PlacementError,
    count_switches,
    export_vtt,
    parse_blocks,
    plan_positions,
)
from . import terms as terms_lib
from .reading_load import find_reading_zones
from .srt import SrtParseError, parse_document, parse_srt, rewrite_srt
from .timecode import format_signed_timecode, format_timecode
from .wrap import apply_wrap, line_display_width

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(
    title="字幕校正、位置规划、专名统一、智能断行与阅读负荷工具",
    description="粘贴 SRT 后可做时间漂移校正、按画面遮挡时段规划字幕上下位置、"
    "统一专名别名、按列数/行数智能断行，或分析重叠字幕叠加后的阅读负荷热区。",
    version="1.4.0",
)

_FIELD_LABELS = {
    "rate_threshold": "阅读速率阈值（字/秒）",
    "min_duration_ms": "最短持续时长（毫秒）",
    "switch_cost_ms": "相邻字幕上下换位代价（毫秒）",
    "max_columns": "每行最大显示列数",
    "max_lines": "每条字幕最多行数",
}


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """请求体字段校验失败统一返回 422 中文说明。

    非有限浮点（NaN / Infinity）不能原样回显（标准 JSON 无法序列化，默认
    处理器会直接 500），这里按字段给出固定中文提示。
    """
    for err in exc.errors():
        loc = err.get("loc") or ()
        field = loc[-1] if loc else None
        label = _FIELD_LABELS.get(str(field), "请求参数")
        value = err.get("input")
        if isinstance(value, float) and not math.isfinite(value):
            message = f"{label}必须是有限的数字。"
        elif err.get("type") in ("float_type", "int_type"):
            message = f"{label}必须是数字。"
        elif err.get("type") == "greater_than":
            message = f"{label}必须严格大于 0。"
        elif err.get("type") == "greater_than_equal":
            limit = err.get("ctx", {}).get("ge")
            if isinstance(limit, int) and limit == 1:
                message = f"{label}必须为不小于 1 的整数。"
            else:
                message = f"{label}不能小于 {limit if limit is not None else 0}。"
        else:
            message = f"{label}不合法。"
        return JSONResponse(
            status_code=422,
            content={"detail": {"type": "validation", "field": field, "message": message}},
        )
    return JSONResponse(status_code=422, content={"detail": {"type": "validation"}})


class AnchorPair(BaseModel):
    original: str = Field(..., description="字幕上观察到的原时间，HH:MM:SS,mmm")
    corrected: str = Field(..., description="人工确认的正确时间，HH:MM:SS,mmm")


class CorrectRequest(BaseModel):
    srt: str = Field(..., description="原始 SRT 内容")
    anchors: list[AnchorPair] = Field(..., min_length=2)


class CorrectedCue(BaseModel):
    index: str
    original_start: str
    original_end: str
    corrected_start: str
    corrected_end: str
    text: str


class CorrectResponse(BaseModel):
    corrected_srt: str
    cues: list[CorrectedCue]
    warnings: list[str]


class OcclusionBlockIn(BaseModel):
    start: str = Field(..., description="遮挡开始时间，HH:MM:SS,mmm")
    end: str = Field(..., description="遮挡结束时间，HH:MM:SS,mmm，须严格晚于开始")


class PlacementRequest(BaseModel):
    srt: str = Field(..., description="原始 SRT 内容")
    top_blocks: list[OcclusionBlockIn] = Field(
        default_factory=list, description="画面上方遮挡时段"
    )
    bottom_blocks: list[OcclusionBlockIn] = Field(
        default_factory=list, description="画面下方遮挡时段"
    )
    switch_cost_ms: StrictInt = Field(
        ..., ge=0, description="相邻字幕上下换位代价（非负整数毫秒）"
    )


class BlockView(BaseModel):
    start: str
    end: str


class PlacedCueView(BaseModel):
    index: str
    start: str
    end: str
    text: str
    position: str
    top_occluded_ms: int
    bottom_occluded_ms: int
    occluded_ms: int
    still_occluded: bool


class PlacementResponse(BaseModel):
    vtt: str
    cues: list[PlacedCueView]
    still_occluded_cues: list[PlacedCueView]
    merged_top_blocks: list[BlockView]
    merged_bottom_blocks: list[BlockView]
    total_occluded_ms: int
    switch_count: int


class ReadingLoadRequest(BaseModel):
    srt: str = Field(..., description="原始 SRT 内容")
    rate_threshold: StrictFloat = Field(
        ..., gt=0, description="阅读速率阈值（字/秒），必须为正数"
    )
    min_duration_ms: StrictInt = Field(
        ..., ge=0, description="热区最短连续时长（非负整数毫秒）"
    )

    @field_validator("rate_threshold")
    @classmethod
    def _threshold_must_be_finite(cls, value: float) -> float:
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("阅读速率阈值必须是有限的正数，不能为 NaN 或 Infinity。")
        return value


class CueLoadView(BaseModel):
    order: int
    index: str
    start: str
    end: str
    char_count: int
    duration_ms: int
    rate: float
    text: str


class ReadingZoneView(BaseModel):
    start: str
    end: str
    duration_ms: int
    peak_rate: float
    cue_orders: list[int]


class ReadingLoadResponse(BaseModel):
    rate_threshold: float
    min_duration_ms: int
    cue_count: int
    zone_count: int
    cues: list[CueLoadView]
    zones: list[ReadingZoneView]


class TermRuleIn(BaseModel):
    standard: str = Field(..., description="标准写法（替换目标）")
    aliases: list[str] = Field(..., description="该标准写法的别名列表，逐个替换")


class TermsRequest(BaseModel):
    srt: str = Field(..., description="原始 SRT 内容")
    rules: list[TermRuleIn] = Field(
        default_factory=list, description="专名规则，按输入顺序参与等长同分裁决"
    )


class TermHitView(BaseModel):
    cue_order: int
    line_no: int
    column: int
    source: str
    standard: str


class TermsResponse(BaseModel):
    revised_srt: str
    hits: list[TermHitView]
    hit_count: int


class WrapRequest(BaseModel):
    srt: str = Field(..., description="原始 SRT 内容")
    max_columns: StrictInt = Field(
        ..., ge=1, le=1000, description="每行最大显示列数（正整数）"
    )
    max_lines: StrictInt = Field(
        ..., ge=1, le=100, description="每条字幕最多行数（正整数）"
    )


class WrappedLineView(BaseModel):
    text: str
    columns: int


class WrappedCueView(BaseModel):
    cue_order: int
    index: str
    start: str
    end: str
    original_text: str
    lines: list[WrappedLineView]
    line_count: int
    changed: bool


class SkippedCueView(BaseModel):
    cue_order: int
    index: str
    start: str
    end: str
    reason_code: str
    reason: str
    text: str


class WrapResponse(BaseModel):
    wrapped_srt: str
    max_columns: int
    max_lines: int
    cue_count: int
    wrapped_count: int
    changed_count: int
    skipped_count: int
    cues: list[WrappedCueView]
    skipped: list[SkippedCueView]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/placement")
def placement_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "placement.html")


@app.get("/reading-load")
def reading_load_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "reading_load.html")


@app.get("/terms")
def terms_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "terms.html")


@app.get("/wrap")
def wrap_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "wrap.html")


@app.post("/api/correct", response_model=CorrectResponse)
def correct(req: CorrectRequest) -> CorrectResponse | JSONResponse:
    # 1. 解析 SRT（保留原始序号与文本；无法解析仍返回 422）
    try:
        cues = parse_srt(req.srt)
    except SrtParseError as exc:
        raise HTTPException(status_code=422, detail={"type": "srt", "message": str(exc)})

    # 2. 校验锚点：数量、时间格式、两侧时间严格递增
    pairs = [(pair.original, pair.corrected) for pair in req.anchors]
    try:
        anchors = build_anchors(pairs)
    except AnchorError as exc:
        raise HTTPException(
            status_code=422, detail={"type": "anchors", "message": str(exc)}
        )

    mapping = PiecewiseMapping(anchors)
    warnings: list[str] = []
    new_ranges: list[tuple[int, int]] = []

    # 3. 逐条字幕做分段线性映射
    for cue in cues:
        new_start_raw = mapping.map_ms(cue.start)
        new_end_raw = mapping.map_ms(cue.end)
        new_start = max(new_start_raw, 0)
        new_end = max(new_end_raw, 0)

        if new_start_raw < 0 or new_end_raw < 0:
            warnings.append(
                f"序号 {cue.index}：校正后早于 00:00:00,000，"
                f"时间已截断到 0（计算值 {format_signed_timecode(new_start_raw)}"
                f" → {format_signed_timecode(new_end_raw)}），"
                "建议在更早位置补充锚点。"
            )
        if new_end <= new_start:
            warnings.append(
                f"序号 {cue.index}：校正后字幕时长不为正"
                f"（{format_timecode(new_start)} → {format_timecode(new_end)}），"
                "该结果已保留但可能异常，请检查锚点。"
            )

        new_ranges.append((new_start, new_end))

    # 4. 无损重写：只替换各时间轴行的起止时间码；序号、正文、箭头空格、
    #    X1:/X2: 等定位参数、空行、原有换行及开头 BOM 全部保持原样
    try:
        result_srt = rewrite_srt(req.srt, new_ranges)
    except SrtParseError as exc:
        raise HTTPException(status_code=422, detail={"type": "srt", "message": str(exc)})

    cue_views = [
        CorrectedCue(
            index=cue.index,
            original_start=format_timecode(cue.start),
            original_end=format_timecode(cue.end),
            corrected_start=format_timecode(new_start),
            corrected_end=format_timecode(new_end),
            text=cue.text,
        )
        for cue, (new_start, new_end) in zip(cues, new_ranges)
    ]

    return CorrectResponse(corrected_srt=result_srt, cues=cue_views, warnings=warnings)


@app.post("/api/placement", response_model=PlacementResponse)
def placement(req: PlacementRequest) -> PlacementResponse | JSONResponse:
    # 1. 复用现有 SRT 解析器
    try:
        cues = parse_srt(req.srt)
    except SrtParseError as exc:
        raise HTTPException(status_code=422, detail={"type": "srt", "message": str(exc)})

    # 2. 校验遮挡时段并各自求并集（换位代价由 StrictInt(ge=0) 保证为非负整数）
    try:
        top_blocks: list[Block] = parse_blocks(
            [(b.start, b.end) for b in req.top_blocks], "画面上方"
        )
        bottom_blocks: list[Block] = parse_blocks(
            [(b.start, b.end) for b in req.bottom_blocks], "画面下方"
        )
    except PlacementError as exc:
        raise HTTPException(
            status_code=422, detail={"type": "blocks", "message": str(exc)}
        )

    # 3. 动态规划选择每条字幕的位置（同分优先下方）
    placements = plan_positions(
        cues, top_blocks, bottom_blocks, req.switch_cost_ms
    )

    cue_views = [
        PlacedCueView(
            index=cue.index,
            start=format_timecode(cue.start),
            end=format_timecode(cue.end),
            text=cue.text,
            position=p.position,
            top_occluded_ms=p.top_occluded_ms,
            bottom_occluded_ms=p.bottom_occluded_ms,
            occluded_ms=p.occluded_ms,
            still_occluded=p.still_occluded,
        )
        for cue, p in zip(cues, placements)
    ]

    def block_views(blocks: list[Block]) -> list[BlockView]:
        return [
            BlockView(start=format_timecode(b.start), end=format_timecode(b.end))
            for b in blocks
        ]

    return PlacementResponse(
        vtt=export_vtt(cues, placements),
        cues=cue_views,
        still_occluded_cues=[v for v in cue_views if v.still_occluded],
        merged_top_blocks=block_views(top_blocks),
        merged_bottom_blocks=block_views(bottom_blocks),
        total_occluded_ms=sum(v.occluded_ms for v in cue_views),
        switch_count=count_switches(placements),
    )


@app.post("/api/reading-load", response_model=ReadingLoadResponse)
def reading_load(req: ReadingLoadRequest) -> ReadingLoadResponse | JSONResponse:
    # 1. 复用现有 SRT 解析器
    try:
        cues = parse_srt(req.srt)
    except SrtParseError as exc:
        raise HTTPException(status_code=422, detail={"type": "srt", "message": str(exc)})

    # 2. 按起止事件扫描半开时间轴（阈值经十进制短表示转 Fraction 精确比较：
    #    输入 0.3 即数学意义的 0.3，杜绝二进制浮点误差影响严格大于判定）
    loads, zones = find_reading_zones(
        cues, Fraction(str(req.rate_threshold)), req.min_duration_ms
    )

    cue_views = [
        CueLoadView(
            order=load.order,
            index=cue.index,
            start=format_timecode(cue.start),
            end=format_timecode(cue.end),
            char_count=load.char_count,
            duration_ms=load.duration_ms,
            rate=round(float(load.rate), 3),
            text=cue.text,
        )
        for cue, load in zip(cues, loads)
    ]
    zone_views = [
        ReadingZoneView(
            start=format_timecode(zone.start),
            end=format_timecode(zone.end),
            duration_ms=zone.end - zone.start,
            peak_rate=round(float(zone.peak_rate), 3),
            cue_orders=list(zone.cue_orders),
        )
        for zone in zones
    ]

    return ReadingLoadResponse(
        rate_threshold=req.rate_threshold,
        min_duration_ms=req.min_duration_ms,
        cue_count=len(cues),
        zone_count=len(zones),
        cues=cue_views,
        zones=zone_views,
    )


@app.post("/api/terms", response_model=TermsResponse)
def unify_terms(req: TermsRequest) -> TermsResponse | JSONResponse:
    # 1. 先校验专名规则：空别名、同一别名指向不同标准写法均返回 422
    try:
        rules = terms_lib.build_rules([(r.standard, r.aliases) for r in req.rules])
    except terms_lib.TermError as exc:
        raise HTTPException(status_code=422, detail={"type": "terms", "message": str(exc)})

    # 2. 复用现有 SRT 解析，并取每条字幕正文在物理行序列中的区间
    try:
        doc = parse_document(req.srt)
    except SrtParseError as exc:
        raise HTTPException(status_code=422, detail={"type": "srt", "message": str(exc)})

    # 3. 仅扫描正文各物理行：从左到右一次匹配原文，同位置最长别名优先，
    #    等长按规则输入顺序；命中的替换文本不再参与匹配。只改写正文命中片段，
    #    序号、时间轴、排版参数、空行、BOM 与换行风格保持原样
    revised, hits = terms_lib.apply_terms(
        req.srt, rules, [(r.start_line, r.end_line) for r in doc.body_ranges]
    )

    return TermsResponse(
        revised_srt=revised,
        hits=[
            TermHitView(
                cue_order=hit.cue_order,
                line_no=hit.line_no,
                column=hit.column,
                source=hit.source,
                standard=hit.standard,
            )
            for hit in hits
        ],
        hit_count=len(hits),
    )


@app.post("/api/wrap", response_model=WrapResponse)
def wrap_subtitles(req: WrapRequest) -> WrapResponse | JSONResponse:
    # 1. 复用现有 SRT 解析，并取每条字幕正文在物理行序列中的区间
    try:
        doc = parse_document(req.srt)
    except SrtParseError as exc:
        raise HTTPException(status_code=422, detail={"type": "srt", "message": str(exc)})

    # 2. 逐条做 DP 最优断行；排不下的保留原正文并记录原因。只改写成功字幕
    #    的正文，序号、时间轴、定位参数、空行、BOM 与原换行风格保持原样
    wrapped_srt, results, skipped_results = apply_wrap(
        req.srt,
        [cue.index for cue in doc.cues],
        [(r.start_line, r.end_line) for r in doc.body_ranges],
        req.max_columns,
        req.max_lines,
    )

    result_by_order = {r.cue_order: r for r in results}
    skipped_by_order = {s.cue_order: s for s in skipped_results}

    cue_views: list[WrappedCueView] = []
    skipped_views: list[SkippedCueView] = []
    for cue_order, cue in enumerate(doc.cues, start=1):
        if cue_order in skipped_by_order:
            skip = skipped_by_order[cue_order]
            skipped_views.append(
                SkippedCueView(
                    cue_order=cue_order,
                    index=cue.index,
                    start=format_timecode(cue.start),
                    end=format_timecode(cue.end),
                    reason_code=skip.reason_code,
                    reason=skip.reason,
                    text=cue.text,
                )
            )
            continue
        result = result_by_order[cue_order]
        cue_views.append(
            WrappedCueView(
                cue_order=cue_order,
                index=cue.index,
                start=format_timecode(cue.start),
                end=format_timecode(cue.end),
                original_text=result.original_text,
                lines=[
                    WrappedLineView(text=line, columns=line_display_width(line))
                    for line in result.lines
                ],
                line_count=result.line_count,
                changed=result.changed,
            )
        )

    return WrapResponse(
        wrapped_srt=wrapped_srt,
        max_columns=req.max_columns,
        max_lines=req.max_lines,
        cue_count=len(doc.cues),
        wrapped_count=len(results),
        changed_count=sum(1 for r in results if r.changed),
        skipped_count=len(skipped_results),
        cues=cue_views,
        skipped=skipped_views,
    )
