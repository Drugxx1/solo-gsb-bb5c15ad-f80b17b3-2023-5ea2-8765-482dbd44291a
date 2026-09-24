"""核心算法与 FastAPI 接口测试。"""
from fractions import Fraction

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.linewrap import (
    WrapFailure,
    WrapPlan,
    build_atoms,
    char_display_width,
    is_cjk_char,
    is_han,
    normalize_text,
    plan_wrap,
    wrap_document,
)
from app.mapping import AnchorError, PiecewiseMapping, build_anchors
from app.placement import (
    Block,
    PlacementError,
    count_switches,
    export_vtt,
    intersection_ms,
    merge_blocks,
    parse_blocks,
    plan_positions,
)
from app.reading_load import count_display_chars, find_reading_zones
from app.srt import BOM, SrtParseError, format_srt, parse_document, parse_srt, rewrite_srt
from app.terms import TermError, TermRule, apply_terms, build_rules, scan_line
from app.timecode import format_timecode, parse_timecode

client = TestClient(app)


# ---------- 时间码 ----------

def test_parse_and_format_timecode():
    assert parse_timecode("00:01:23,456") == 83_456
    assert parse_timecode("1:2:3.4") == 3_723_400  # 毫秒补 3 位 -> 400
    assert format_timecode(83_456) == "00:01:23,456"
    assert format_timecode(-5) == "00:00:00,000"


def test_invalid_timecode():
    with pytest.raises(ValueError):
        parse_timecode("00:60:00,000")
    with pytest.raises(ValueError):
        parse_timecode("not-a-time")
    with pytest.raises(ValueError):
        parse_timecode("")


# ---------- SRT ----------

SAMPLE = (
    "1\r\n"
    "00:00:05,000 --> 00:00:08,000\r\n"
    "你好\n\n"
    "2\n"
    "00:00:10,000 --> 00:00:12,500\n"
    "第二行上\n第二行下\n"
)


def test_parse_preserves_index_and_text():
    cues = parse_srt(SAMPLE)
    assert len(cues) == 2
    assert cues[0].index == "1"
    assert cues[0].text == "你好"
    assert cues[1].text == "第二行上\n第二行下"
    assert cues[1].end - cues[1].start == 2500


def test_parse_missing_index_auto_numbers():
    cues = parse_srt("00:00:01,000 --> 00:00:02,000\n无序号块\n")
    assert cues[0].index == "1"


def test_parse_errors():
    with pytest.raises(SrtParseError):
        parse_srt("")
    with pytest.raises(SrtParseError):
        parse_srt("1\n00:00:05,000 --> 00:00:05,000\n等长\n")
    with pytest.raises(SrtParseError):
        parse_srt("1\n00:00:05,000 --> 00:00:03,000\n倒退\n")


def test_roundtrip_srt():
    out = format_srt(parse_srt(SAMPLE))
    assert "00:00:05,000 --> 00:00:08,000" in out
    assert "第二行上\n第二行下" in out


# ---------- 无损重写 ----------

POS_SAMPLE = (
    BOM
    + "1\r\n"
    + "  00:00:05,000 --> 00:00:08,000  X1:10 X2:630 Y1:20 Y2:60\r\n"
    + "你好\r\n"
    + "\r\n"
    + "2\r\n"
    + "00:00:10,000\t-->\t00:00:12,500  Position:50%,30%,0\r\n"
    + "第二行上\r\n第二行下\r\n"
)


def test_rewrite_identity_is_byte_identical():
    # 恒等锚点：除时间码按规范重写外，其余内容（BOM/CRLF/参数/箭头空格）逐字节不动
    cues = parse_srt(POS_SAMPLE)
    out = rewrite_srt(POS_SAMPLE, [(c.start, c.end) for c in cues])
    assert out == POS_SAMPLE


def test_rewrite_only_touches_timing_timecodes():
    cues = parse_srt(POS_SAMPLE)
    out = rewrite_srt(POS_SAMPLE, [(1_000, 4_000), (6_000, 8_500)])

    # 开头 BOM 唯一保留
    assert out.startswith(BOM)
    assert BOM not in out[1:]  # 正文内没有额外 BOM

    # 原有 CRLF 换行、定位参数、箭头前后空格全部保留
    assert (
        "  00:00:01,000 --> 00:00:04,000  X1:10 X2:630 Y1:20 Y2:60\r\n"
    ) in out
    assert (
        "00:00:06,000\t-->\t00:00:08,500  Position:50%,30%,0\r\n"
    ) in out

    # 序号与正文原样
    assert "1\r\n" in out
    assert "你好\r\n" in out
    assert "第二行上\r\n第二行下\r\n" in out

    # 旧时间码不再残留
    assert "00:00:05,000" not in out
    assert "00:00:12,500" not in out


def test_rewrite_preserves_blank_lines_and_lf():
    src = (
        "1\n00:00:05,000 --> 00:00:08,000\n第一句\n"
        "\n"
        "\n"  # 多余空行也保留
        "2\n00:00:10,000 --> 00:00:12,500 X1:1\r\n第二句\n"
    )
    cues = parse_srt(src)
    out = rewrite_srt(src, [(0, 1_000), (2_000, 3_000)])
    assert out.count("\r") == 1  # 仅第二块原有的那个 CR 保留，其余仍是 LF
    assert out.startswith("1\n00:00:00,000 --> 00:00:01,000\n第一句\n\n\n2\n")
    assert out.endswith("00:00:02,000 --> 00:00:03,000 X1:1\r\n第二句\n")


def test_rewrite_without_bom_stays_bomless():
    src = "1\n00:00:05,000 --> 00:00:08,000\n你好\n"
    out = rewrite_srt(src, [(1_000, 2_000)])
    assert not out.startswith(BOM)
    assert out == "1\n00:00:01,000 --> 00:00:02,000\n你好\n"


def test_rewrite_collapses_multiple_leading_boms_to_one():
    src = BOM + BOM + "1\n00:00:05,000 --> 00:00:08,000\n你好\n"
    cues = parse_srt(src)
    assert len(cues) == 1
    out = rewrite_srt(src, [(1_000, 2_000)])
    assert out == BOM + "1\n00:00:01,000 --> 00:00:02,000\n你好\n"


def test_rewrite_invalid_srt_raises():
    with pytest.raises(SrtParseError):
        rewrite_srt("garbage without timing", [(1_000, 2_000)])


# ---------- 锚点与映射 ----------

def test_build_anchors_requires_two():
    with pytest.raises(AnchorError, match="至少需要 2 组"):
        build_anchors([("00:00:01,000", "00:00:00,500")])


def test_original_not_strictly_increasing_rejected():
    with pytest.raises(AnchorError, match="原时间未严格递增"):
        build_anchors([
            ("00:00:10,000", "00:00:08,000"),
            ("00:00:10,000", "00:00:09,000"),
        ])
    with pytest.raises(AnchorError, match="原时间未严格递增"):
        build_anchors([
            ("00:00:20,000", "00:00:08,000"),
            ("00:00:10,000", "00:00:09,000"),
        ])


def test_corrected_not_strictly_increasing_rejected():
    with pytest.raises(AnchorError, match="正确时间未严格递增"):
        build_anchors([
            ("00:00:10,000", "00:00:09,000"),
            ("00:00:20,000", "00:00:09,000"),
        ])


def test_invalid_anchor_time_rejected():
    with pytest.raises(AnchorError, match="原时间无效"):
        build_anchors([("bad", "00:00:09,000"), ("00:00:20,000", "00:00:19,000")])


def _mapping():
    # 10s->8s（-2s），100s->92s（-8s），斜率 56/60
    anchors = build_anchors([
        ("00:00:10,000", "00:00:08,000"),
        ("00:01:40,000", "00:01:32,000"),
    ])
    return PiecewiseMapping(anchors)


def test_mapping_hits_anchor_exactly():
    m = _mapping()
    assert m.map_ms(10_000) == 8_000
    assert m.map_ms(100_000) == 92_000


def test_mapping_interpolation_between_anchors():
    m = _mapping()
    # 中间点 55s -> 50s（线性中点）
    assert m.map_ms(55_000) == 50_000


def test_mapping_extrapolates_with_nearest_segment_slope():
    m = _mapping()
    # 右端外推：160s + 60s -> 92s + 56s = 148s
    assert m.map_ms(160_000) == 148_000
    # 左端外推：0 -> 8000 + (0-10000)*56/60 = 8000 - 9333.33 = -1333
    assert m.map_ms(0) == -1333


def test_three_anchor_piecewise_slopes():
    anchors = build_anchors([
        ("00:00:00,000", "00:00:00,000"),
        ("00:01:00,000", "00:00:54,000"),   # 斜率 0.9
        ("00:02:00,000", "00:02:00,000"),   # 斜率 1.1
    ])
    m = PiecewiseMapping(anchors)
    # 第一区段：30s -> 27s
    assert m.map_ms(30_000) == 27_000
    # 第二区段：90s -> 54s + (30s)*1.1 = 87s
    assert m.map_ms(90_000) == 87_000
    # 末段斜率外推：150s -> 120s + 30s*1.1 = 153s
    assert m.map_ms(150_000) == 153_000


# ---------- HTTP 接口 ----------

def request_body():
    return {
        "srt": SAMPLE,
        "anchors": [
            {"original": "00:00:10,000", "corrected": "00:00:08,000"},
            {"original": "00:01:40,000", "corrected": "00:01:32,000"},
        ],
    }


def test_health_and_index():
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/").status_code == 200


def test_api_correct_success():
    resp = client.post("/api/correct", json=request_body())
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["cues"][0]["index"] == "1"
    assert data["cues"][0]["text"] == "你好"
    assert data["cues"][0]["corrected_start"] == format_timecode(
        _mapping().map_ms(5000)
    )
    assert "你好" in data["corrected_srt"]


def test_api_lossless_preserves_bom_crlf_and_positioning():
    src = (
        BOM
        + "1\r\n"
        + "00:00:05,000 --> 00:00:08,000 X1:10 X2:630\r\n"
        + "你好\r\n\r\n"
        + "2\r\n"
        + "00:00:10,000 --> 00:00:12,500\r\n"
        + "第二行\r\n"
    )
    body = {
        "srt": src,
        "anchors": [
            {"original": "00:00:05,000", "corrected": "00:00:05,000"},
            {"original": "00:00:10,000", "corrected": "00:00:10,000"},
        ],
    }
    resp = client.post("/api/correct", json=body)
    assert resp.status_code == 200, resp.text
    # 恒等锚点下，返回体应与输入逐字符一致（BOM/CRLF/定位参数均无损）
    assert resp.json()["corrected_srt"] == src


def test_api_bomless_input_returns_bomless_srt():
    src = "1\n00:00:05,000 --> 00:00:08,000 X1:10\n你好\n"
    body = {
        "srt": src,
        "anchors": [
            {"original": "00:00:05,000", "corrected": "00:00:06,000"},
            {"original": "00:00:10,000", "corrected": "00:00:11,000"},
        ],
    }
    resp = client.post("/api/correct", json=body)
    assert resp.status_code == 200, resp.text
    out = resp.json()["corrected_srt"]
    assert not out.startswith(BOM)
    assert out == "1\n00:00:06,000 --> 00:00:09,000 X1:10\n你好\n"


def test_api_rejects_bad_anchors_with_reason():
    body = request_body()
    body["anchors"][1]["original"] = "00:00:10,000"  # 与第一组相等
    resp = client.post("/api/correct", json=body)
    assert resp.status_code == 422
    assert resp.json()["detail"]["type"] == "anchors"
    assert "原时间未严格递增" in resp.json()["detail"]["message"]


def test_api_rejects_bad_srt():
    body = request_body()
    body["srt"] = "garbage without timing"
    resp = client.post("/api/correct", json=body)
    assert resp.status_code == 422
    assert resp.json()["detail"]["type"] == "srt"


# ---------- 字幕位置规划 ----------

PLACEMENT_SRT = (
    "1\n00:00:00,000 --> 00:00:10,000\n第一句\n\n"
    "2\n00:00:10,000 --> 00:00:20,000\n第二句\n\n"
    "3\n00:00:20,000 --> 00:00:30,000\n第三句\n"
)


def _placement_cues():
    return parse_srt(PLACEMENT_SRT)


def test_merge_blocks_unions_overlapping_and_adjacent():
    blocks = [Block(2_000, 8_000), Block(6_000, 12_000), Block(12_000, 15_000)]
    assert merge_blocks(blocks) == [Block(2_000, 15_000)]
    # 不重叠不相接的保持两段，乱序输入按开始时间排序
    assert merge_blocks([Block(20_000, 22_000), Block(0, 5_000)]) == [
        Block(0, 5_000),
        Block(20_000, 22_000),
    ]


def test_intersection_sums_overlap_with_cue():
    blocks = [Block(2_000, 8_000), Block(100_000, 110_000)]
    # 字幕 [0,10s] 与第一段交集 6s，与第二段无交集
    assert intersection_ms(0, 10_000, blocks) == 6_000
    # 仅端点相接不算遮挡（右开区间语义，毫秒整数）
    assert intersection_ms(8_000, 9_000, blocks) == 0


def test_parse_blocks_merges_and_validates():
    blocks = parse_blocks(
        [("00:00:02,000", "00:00:08,000"), ("00:00:06,000", "00:00:12,000")],
        "画面上方",
    )
    assert blocks == [Block(2_000, 12_000)]

    with pytest.raises(PlacementError, match="结束时间"):
        parse_blocks([("00:00:10,000", "00:00:10,000")], "画面下方")
    with pytest.raises(PlacementError, match="画面上方第 2 个"):
        parse_blocks(
            [("00:00:01,000", "00:00:02,000"), ("bad", "00:00:03,000")],
            "画面上方",
        )


def test_plan_prefers_unoccluded_side_and_counts_switch():
    cues = _placement_cues()
    top = [Block(2_000, 12_000)]   # 字幕1 遮挡8s、字幕2 遮挡2s
    bottom = [Block(22_000, 28_000)]  # 字幕3 下方遮挡6s

    # 代价 0：字幕1/2 下方无遮挡，字幕3 上方无遮挡 → 下/下/上，1 次换位
    p = plan_positions(cues, top, bottom, 0)
    assert [x.position for x in p] == ["bottom", "bottom", "top"]
    assert count_switches(p) == 1
    assert sum(x.occluded_ms for x in p) == 0
    # 逐条遮挡时长独立于最终选择
    assert p[0].top_occluded_ms == 8_000 and p[0].bottom_occluded_ms == 0
    assert p[2].top_occluded_ms == 0 and p[2].bottom_occluded_ms == 6_000


def test_plan_switch_cost_keeps_side_when_cheaper():
    cues = _placement_cues()
    top = [Block(2_000, 12_000)]
    bottom = [Block(22_000, 28_000)]
    # 换位 5000 < 留在下方的遮挡 6000：仍翻到上方
    assert [x.position for x in plan_positions(cues, top, bottom, 5_000)] == [
        "bottom",
        "bottom",
        "top",
    ]
    # 换位 7000 > 6000：全留下方，字幕3 承受 6000ms 遮挡
    p = plan_positions(cues, top, bottom, 7_000)
    assert [x.position for x in p] == ["bottom", "bottom", "bottom"]
    assert p[2].occluded_ms == 6_000 and p[2].still_occluded


def test_plan_ties_prefer_bottom_per_cue():
    cues = _placement_cues()
    # 无遮挡、零代价：所有组合同分，逐条优先下方
    p = plan_positions(cues, [], [], 0)
    assert [x.position for x in p] == ["bottom"] * 3

    # 词典序验证：字幕1 双侧无遮挡时，无论后续如何选择，字幕1 都应取下方
    # 字幕2 下方遮挡 1000 必须翻上，字幕3 双侧无遮挡（下方与字幕2 换位代价为 0）
    bottom = [Block(10_000, 11_000)]
    p2 = plan_positions(cues, [], bottom, 0)
    assert [x.position for x in p2] == ["bottom", "top", "bottom"]
    assert p2[0].position == "bottom"


def test_export_vtt_uses_line_positions():
    from app.placement import CuePlacement

    cues = _placement_cues()
    placements = [
        CuePlacement("bottom", 8_000, 0),
        CuePlacement("bottom", 2_000, 0),
        CuePlacement("top", 0, 6_000),
    ]
    vtt = export_vtt(cues, placements)
    assert vtt.startswith("WEBVTT\n\n")
    assert "00:00:00.000 --> 00:00:10.000 line:90%" in vtt
    assert "00:00:20.000 --> 00:00:30.000 line:10%" in vtt
    assert "第一句" in vtt


def test_placement_page_served():
    resp = client.get("/placement")
    assert resp.status_code == 200
    assert "字幕位置规划" in resp.text


def placement_body(**overrides):
    body = {
        "srt": PLACEMENT_SRT,
        "top_blocks": [
            {"start": "00:00:02,000", "end": "00:00:08,000"},
            {"start": "00:00:06,000", "end": "00:00:12,000"},
        ],
        "bottom_blocks": [{"start": "00:00:22,000", "end": "00:00:28,000"}],
        "switch_cost_ms": 5000,
    }
    body.update(overrides)
    return body


def test_api_placement_success():
    resp = client.post("/api/placement", json=placement_body())
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert [c["position"] for c in data["cues"]] == ["bottom", "bottom", "top"]
    # 同侧重叠时段先求并集再回传
    assert data["merged_top_blocks"] == [
        {"start": "00:00:02,000", "end": "00:00:12,000"}
    ]
    assert data["merged_bottom_blocks"] == [
        {"start": "00:00:22,000", "end": "00:00:28,000"}
    ]
    assert data["switch_count"] == 1
    assert data["total_occluded_ms"] == 0
    assert data["still_occluded_cues"] == []
    assert data["cues"][0]["top_occluded_ms"] == 8000
    assert data["vtt"].startswith("WEBVTT\n\n")
    assert "line:90%" in data["vtt"] and "line:10%" in data["vtt"]


def test_api_placement_reports_still_occluded():
    resp = client.post(
        "/api/placement",
        json=placement_body(
            top_blocks=[{"start": "00:00:00,000", "end": "00:00:30,000"}],
            bottom_blocks=[{"start": "00:00:00,000", "end": "00:00:30,000"}],
            switch_cost_ms=0,
        ),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # 两侧全遮挡：仍能选出位置，且全部进入 still_occluded_cues；同分全部下方
    assert len(data["still_occluded_cues"]) == 3
    assert all(c["still_occluded"] for c in data["cues"])
    assert all(c["position"] == "bottom" for c in data["cues"])


@pytest.mark.parametrize("bad_cost", [-1, 1.5, True, False, "5", "abc"])
def test_api_placement_rejects_non_nonneg_int_cost(bad_cost):
    resp = client.post("/api/placement", json=placement_body(switch_cost_ms=bad_cost))
    assert resp.status_code == 422


def test_api_placement_requires_cost_field():
    body = placement_body()
    del body["switch_cost_ms"]
    assert client.post("/api/placement", json=body).status_code == 422


def test_api_placement_rejects_invalid_blocks():
    resp = client.post(
        "/api/placement",
        json=placement_body(
            top_blocks=[{"start": "00:00:10,000", "end": "00:00:10,000"}]
        ),
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["type"] == "blocks"
    assert "结束时间" in resp.json()["detail"]["message"]


def test_api_placement_rejects_bad_timecode_and_srt():
    resp = client.post(
        "/api/placement",
        json=placement_body(
            bottom_blocks=[{"start": "not-a-time", "end": "00:00:10,000"}]
        ),
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["type"] == "blocks"

    resp = client.post("/api/placement", json=placement_body(srt="garbage"))
    assert resp.status_code == 422
    assert resp.json()["detail"]["type"] == "srt"


def test_api_placement_accepts_empty_blocks():
    resp = client.post(
        "/api/placement",
        json={"srt": PLACEMENT_SRT, "switch_cost_ms": 0},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert all(c["position"] == "bottom" for c in data["cues"])
    assert data["merged_top_blocks"] == []
    assert data["merged_bottom_blocks"] == []


# ---------- 阅读负荷热区 ----------

LOAD_SRT = (
    "1\n00:00:00,000 --> 00:00:02,000\n一二三四五六\n\n"            # 3 字/秒
    "2\n00:00:01,000 --> 00:00:03,000\n四五六\n\n"                # 1.5 字/秒
    "3\n00:00:03,000 --> 00:00:04,000\n七八九十\n"                # 4 字/秒
)


def _load_cues():
    return parse_srt(LOAD_SRT)


def test_count_display_chars_strips_all_unicode_whitespace():
    assert count_display_chars("你好 世界\n第二行\t末　了") == 9
    assert count_display_chars("\n\t 　") == 0


def test_cue_loads_rate_and_duration():
    loads, _ = find_reading_zones(_load_cues(), Fraction(100), 0)
    assert [l.char_count for l in loads] == [6, 3, 4]
    assert [l.duration_ms for l in loads] == [2000, 2000, 1000]
    assert [float(l.rate) for l in loads] == [3.0, 1.5, 4.0]
    assert [l.order for l in loads] == [1, 2, 3]


def test_overlapping_rates_add_on_half_open_timeline():
    # 阈值 4：仅 [1s, 2s) 内 3 + 1.5 = 4.5 严格超过；[2s,3s) 仅 1.5 不超
    _, zones = find_reading_zones(_load_cues(), Fraction(4), 0)
    assert len(zones) == 1
    assert (zones[0].start, zones[0].end) == (1000, 2000)
    assert zones[0].peak_rate == Fraction(9, 2)
    assert zones[0].cue_orders == (1, 2)


def test_threshold_is_strict_greater_than():
    # 阈值 4.5：合计恰好 4.5，严格超过不成立；字幕3 速率 4 也不超 -> 无热区
    _, zones = find_reading_zones(_load_cues(), Fraction(45, 10), 0)
    assert zones == []


def test_touching_cues_do_not_overlap():
    # 字幕1 结束 == 字幕2 开始（3s）：半开时间轴上首尾相接不算重叠
    cues = parse_srt(
        "1\n00:00:00,000 --> 00:00:03,000\n一二三四五六七八\n\n"
        "2\n00:00:03,000 --> 00:00:06,000\n一二三四五六七八\n"
    )
    # 每条 8 字 / 3 秒；阈值 8/3：单条不严格超过，相接点也不叠加 -> 无热区
    _, zones = find_reading_zones(cues, Fraction(8, 3), 0)
    assert zones == []
    # 阈值严格小于单条速率时，相接的两段各自超阈后合并为 [0,6s)
    _, merged = find_reading_zones(cues, Fraction("2.6"), 0)
    assert len(merged) == 1
    assert (merged[0].start, merged[0].end) == (0, 6000)
    assert merged[0].cue_orders == (1, 2)
    assert merged[0].peak_rate == Fraction(8, 3)


def test_adjacent_over_segments_merge_into_one_zone():
    # 字幕1：8字/4s=2；字幕2：10字/2s=5；字幕3：9字/3s=3
    cues = parse_srt(
        "1\n00:00:00,000 --> 00:00:04,000\n一二三四五六七八\n\n"
        "2\n00:00:02,000 --> 00:00:04,000\n一二三四五六七八九十\n\n"
        "3\n00:00:04,000 --> 00:00:07,000\n一二三四五六七八九\n"
    )
    # 分段：[0,2)=2；[2,4)=7（字幕1+2）；[4,7)=3（字幕3，与上一段相接）
    # 阈值 2：[0,2) 不严格超，[2,4) 与 [4,7) 在 4s 处相接 -> 合并为 [2,7)
    _, zones = find_reading_zones(cues, Fraction(2), 0)
    assert len(zones) == 1
    assert (zones[0].start, zones[0].end) == (2000, 7000)
    assert zones[0].peak_rate == 7
    assert zones[0].cue_orders == (1, 2, 3)


def test_min_duration_filters_after_merge():
    # 同一热区 1000ms：下限 1000 保留，1001 丢弃
    _, keep = find_reading_zones(_load_cues(), Fraction(4), 1000)
    assert len(keep) == 1 and keep[0].end - keep[0].start == 1000
    _, drop = find_reading_zones(_load_cues(), Fraction(4), 1001)
    assert drop == []
    # 下限 0 不影响
    _, zero = find_reading_zones(_load_cues(), Fraction(4), 0)
    assert len(zero) == 1


def test_ms_precision_not_whole_second_sampling():
    # 仅 150ms 的毫秒级峰值也能检出（整秒采样会漏掉）
    cues = parse_srt(
        "1\n00:00:00,000 --> 00:00:10,000\n慢\n\n"
        "2\n00:00:05,000 --> 00:00:05,150\n甲乙丙丁戊己庚\n"
    )
    _, zones = find_reading_zones(cues, Fraction(40), 0)
    assert [(z.start, z.end) for z in zones] == [(5000, 5150)]
    # 峰值为 7 字 / 0.15 秒（慢速字幕的 0.1 字/秒可忽略）
    assert zones[0].peak_rate > 46
    assert zones[0].cue_orders == (1, 2)


def test_float_threshold_compared_exactly():
    # 0.1+0.2 在二进制浮点下 != 0.3；合计速率恰为 0.3（3字/10秒）时，
    # 以十进制短表示构造的阈值 0.3 不应误判为严格超过
    cues = parse_srt("1\n00:00:00,000 --> 00:00:10,000\n三个字\n")
    _, zones = find_reading_zones(cues, Fraction("0.3"), 0)
    assert zones == []
    _, zones_over = find_reading_zones(cues, Fraction("0.299999"), 0)
    assert len(zones_over) == 1


def test_cue_orders_are_input_positions_sorted():
    # 时间乱序输入：参与序号仍按输入顺序返回，跨分段合并后升序去重
    cues = parse_srt(
        "9\n00:00:02,000 --> 00:00:05,000\n高速重叠字幕内容在这里\n\n"
        "7\n00:00:00,000 --> 00:00:03,000\n另一条更早开始的字幕哦\n"
    )
    _, zones = find_reading_zones(cues, Fraction(3), 0)
    assert zones
    # SRT 序号原样（9、7），但热区引用的是输入顺序号 (1, 2)
    assert zones[0].cue_orders == (1, 2)
    assert [c.index for c in cues] == ["9", "7"]


def test_reading_load_page_served():
    resp = client.get("/reading-load")
    assert resp.status_code == 200
    assert "阅读负荷热区" in resp.text
    assert "/api/reading-load" in resp.text


def load_body(**overrides):
    body = {
        "srt": LOAD_SRT,
        "rate_threshold": 4,
        "min_duration_ms": 1000,
    }
    body.update(overrides)
    return body


def test_api_reading_load_success():
    resp = client.post("/api/reading-load", json=load_body())
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["cue_count"] == 3
    assert data["zone_count"] == 1
    assert data["cues"][0]["order"] == 1
    assert data["cues"][0]["index"] == "1"
    assert data["cues"][0]["char_count"] == 6
    assert data["cues"][0]["rate"] == 3.0
    zone = data["zones"][0]
    assert zone["start"] == "00:00:01,000"
    assert zone["end"] == "00:00:02,000"
    assert zone["duration_ms"] == 1000
    assert zone["peak_rate"] == 4.5
    assert zone["cue_orders"] == [1, 2]


def test_api_reading_load_no_zone():
    resp = client.post(
        "/api/reading-load", json=load_body(rate_threshold=100, min_duration_ms=0)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["zones"] == []


def test_api_reading_load_rejects_bad_srt():
    resp = client.post("/api/reading-load", json=load_body(srt="garbage"))
    assert resp.status_code == 422
    assert resp.json()["detail"]["type"] == "srt"


@pytest.mark.parametrize("bad_threshold", [0, -1, -0.1, True, False, "5", None])
def test_api_reading_load_rejects_bad_threshold(bad_threshold):
    resp = client.post(
        "/api/reading-load", json=load_body(rate_threshold=bad_threshold)
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["field"] == "rate_threshold"


@pytest.mark.parametrize("bad_duration", [-1, 1.5, True, False, "5", None])
def test_api_reading_load_rejects_bad_duration(bad_duration):
    resp = client.post(
        "/api/reading-load", json=load_body(min_duration_ms=bad_duration)
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["field"] == "min_duration_ms"


def test_api_reading_load_accepts_float_threshold():
    resp = client.post(
        "/api/reading-load", json=load_body(rate_threshold=4.5, min_duration_ms=0)
    )
    assert resp.status_code == 200, resp.text
    # 合计恰好 4.5：严格超过不成立
    assert resp.json()["zones"] == []


def test_api_reading_load_nan_token_is_422_not_500():
    resp = client.post(
        "/api/reading-load",
        content=(
            '{"srt":"1\\n00:00:01,000 --> 00:00:02,000\\n好\\n",'
            '"rate_threshold":NaN,"min_duration_ms":0}'
        ),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["field"] == "rate_threshold"


# ---------- 字幕专名统一 ----------

TERMS_SRT = (
    "1\r\n"
    "00:00:01,000 --> 00:00:03,000\r\n"
    "他新买的 IPHONE 到货了\r\n"
    "\r\n"
    "2\n"
    "00:00:04,000 --> 00:00:06,000 X1:10\n"
    "用 iphone 拍视频\n"
    "第二行 iphone15\n"
)


def test_build_rules_rejects_empty_alias_and_standard():

    with pytest.raises(TermError, match="第 2 条规则"):
        build_rules([("北京大学", ["北大"]), ("清华", [""])])
    with pytest.raises(TermError, match="空别名"):
        build_rules([("北京大学", ["  "])])
    with pytest.raises(TermError, match="标准写法不能为空"):
        build_rules([("  ", ["北大"])])


def test_build_rules_rejects_same_alias_different_standard():

    with pytest.raises(TermError, match="同一别名只能对应一个标准写法"):
        build_rules([("北京大学", ["PKU"]), ("北大青鸟", ["PKU"])])
    # 同一别名指向同一标准写法允许（跨规则重复，各自按输入顺序保留）
    rules = build_rules([("北京大学", ["PKU"]), ("北京大学", ["PKU", "北大"])])
    assert rules[0].aliases == ("PKU",)
    assert rules[1].aliases == ("PKU", "北大")


def test_scan_line_longest_wins_and_case_sensitive():

    rules = build_rules([("北京大学", ["Peking University", "Peking Univ", "北大"])])
    out, hits = scan_line("Peking University 与北大", rules)
    assert out == "北京大学 与北京大学"
    # “Peking University”17 字符 + 空格 + “与”，“北大”起点为原文位置 19
    assert [h[0] for h in hits] == [0, 19]
    assert [h[1] for h in hits] == ["Peking University", "北大"]

    # 最长别名优先：同一位置长别名压过其前缀
    out2, hits2 = scan_line("Peking Univ 在此", rules)
    assert out2 == "北京大学 在此"
    assert hits2 == [(0, "Peking Univ", "北京大学")]

    # 区分大小写：小写 peking 不命中
    out3, hits3 = scan_line("peking university", rules)
    assert out3 == "peking university"
    assert hits3 == []


def test_scan_line_equal_length_uses_rule_order():

    # 合法输入下等长别名在同一位置只能是同一字符串（冲突校验已拒绝一对多），
    # 同别名经 build_rules 去重后保留在最早输入的规则中——等长同分自然由
    # scan_line 遍历到的首条规则承担。这里用直接构造的规则覆盖：
    # 1) 别名只在首条规则命中时取首条规则；
    first = TermRule(standard="先", aliases=("AA",), order=1)
    other = TermRule(standard="后", aliases=("AB",), order=2)
    out, hits = scan_line("AA 在此", [first, other])
    assert out == "先 在此"
    assert hits == [(0, "AA", "先")]

    # 2) 等长别名即使出现在后输入的规则，长度不占优也不会抢匹配；
    #    更长别名来自后输入规则时，最长优先仍压过顺序
    longer = TermRule(standard="长", aliases=("AAA",), order=2)
    out2, hits2 = scan_line("AAA", [first, longer])
    assert out2 == "长"
    assert hits2 == [(0, "AAA", "长")]


def test_scan_line_replacement_text_not_rematched():

    # 标准写法自身包含别名：只在原文位置匹配一次。若替换文本被重新扫描，
    # “甲”→“甲甲”会无限增长；正确行为是单次命中后整段跳过
    rules = build_rules([("甲甲", ["甲"])])
    assert scan_line("甲", rules) == ("甲甲", [(0, "甲", "甲甲")])

    # 多字原文：每个原文“甲”各命中一次，替换出来的“甲甲”不回扫
    out, hits = scan_line("甲甲", rules)
    assert out == "甲甲甲甲"
    assert hits == [(0, "甲", "甲甲"), (1, "甲", "甲甲")]

    # 连续不同别名各命中一次，位置基于原文逐个推进
    rules2 = build_rules([("X", ["甲", "乙"])])
    out2, hits2 = scan_line("甲乙甲", rules2)
    assert out2 == "XXX"
    assert [(h[0], h[1]) for h in hits2] == [(0, "甲"), (1, "乙"), (2, "甲")]


def test_apply_terms_only_touches_body_and_preserves_format():

    doc = parse_document(TERMS_SRT)
    rules = build_rules([("iPhone", ["IPHONE", "iphone"])])
    out, hits = apply_terms(
        TERMS_SRT, rules, [(r.start_line, r.end_line) for r in doc.body_ranges]
    )

    # 序号、时间轴（含 X1 排版参数）、CRLF/LF 混排、空行原样
    assert out.startswith("1\r\n00:00:01,000 --> 00:00:03,000\r\n")
    assert "\r\n\r\n2\n00:00:04,000 --> 00:00:06,000 X1:10\n" in out
    assert "他新买的 iPhone 到货了\r\n" in out
    assert "用 iPhone 拍视频\n第二行 iPhone15\n" in out
    # 时间轴与序号不被误改
    assert "00:00:04,000" in out
    # 命中清单：字幕输入序号、正文行号（块内从 1）、0 基列位置、原文、标准
    assert [(h.cue_order, h.line_no, h.column, h.source) for h in hits] == [
        (1, 1, 5, "IPHONE"),
        (2, 1, 2, "iphone"),
        (2, 2, 4, "iphone"),
    ]


def test_apply_terms_preserves_bom_and_cr():

    src = BOM + "1\r00:00:01,000 --> 00:00:02,000\r北大很好\r"
    doc = parse_document(src)
    rules = build_rules([("北京大学", ["北大"])])
    out, hits = apply_terms(
        src, rules, [(r.start_line, r.end_line) for r in doc.body_ranges]
    )
    assert out == BOM + "1\r00:00:01,000 --> 00:00:02,000\r北京大学很好\r"
    assert len(hits) == 1 and hits[0].cue_order == 1 and hits[0].column == 0


def test_apply_terms_no_hit_is_identical_input():

    # 无命中：原文原样返回（多个 BOM 也逐字符保留，不走重写补 BOM 路径）
    src = BOM + BOM + "1\n00:00:01,000 --> 00:00:02,000\n无别名\n"
    doc = parse_document(src)
    rules = build_rules([("X", ["不存在的别名"])])
    out, hits = apply_terms(
        src, rules, [(r.start_line, r.end_line) for r in doc.body_ranges]
    )
    assert out == src
    assert hits == []

    # 空规则列表同样原样返回
    out2, hits2 = apply_terms(
        src, [], [(r.start_line, r.end_line) for r in doc.body_ranges]
    )
    assert out2 == src and hits2 == []


def test_apply_terms_skips_index_and_timing_lines():

    src = "1\n00:00:01,000 --> 00:00:02,000\n正文不含别名\n"
    doc = parse_document(src)
    # 别名恰好等于序号字符与时间轴子串，也只能在正文匹配 -> 无命中
    rules = build_rules([("X", ["1", "00:00:01,000"])])
    out, hits = apply_terms(
        src, rules, [(r.start_line, r.end_line) for r in doc.body_ranges]
    )
    assert out == src and hits == []


def terms_body(**overrides):
    body = {
        "srt": TERMS_SRT,
        "rules": [
            {"standard": "iPhone", "aliases": ["IPHONE", "iphone"]},
            {"standard": "北京大学", "aliases": ["Peking University"]},
        ],
    }
    body.update(overrides)
    return body


def test_terms_page_served():
    resp = client.get("/terms")
    assert resp.status_code == 200
    assert "字幕专名统一" in resp.text
    assert "/api/terms" in resp.text


def test_api_terms_success_and_hits():
    resp = client.post("/api/terms", json=terms_body())
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["hit_count"] == 3
    assert data["hits"][0] == {
        "cue_order": 1,
        "line_no": 1,
        "column": 5,
        "source": "IPHONE",
        "standard": "iPhone",
    }
    assert "他新买的 iPhone 到货了\r\n" in data["revised_srt"]
    # 原文的 CRLF/LF 混排与时间轴排版参数保留
    assert "00:00:04,000 --> 00:00:06,000 X1:10\n" in data["revised_srt"]
    assert data["revised_srt"].count("\r\n") == TERMS_SRT.count("\r\n")


def test_api_terms_no_hit_returns_original():
    body = terms_body(rules=[{"standard": "X", "aliases": ["不存在"]}])
    resp = client.post("/api/terms", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["hit_count"] == 0
    assert data["hits"] == []
    assert data["revised_srt"] == TERMS_SRT


def test_api_terms_empty_rules_is_identity():
    resp = client.post("/api/terms", json={"srt": TERMS_SRT, "rules": []})
    assert resp.status_code == 200, resp.text
    assert resp.json()["revised_srt"] == TERMS_SRT


def test_api_terms_rejects_empty_alias():
    body = terms_body()
    body["rules"][0]["aliases"].append("  ")
    resp = client.post("/api/terms", json=body)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["type"] == "terms"
    assert "空别名" in detail["message"]


def test_api_terms_rejects_conflicting_alias():
    body = terms_body()
    body["rules"].append({"standard": "别的公司", "aliases": ["iphone"]})
    resp = client.post("/api/terms", json=body)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["type"] == "terms"
    assert "iphone" in detail["message"]


def test_api_terms_rejects_bad_srt():
    resp = client.post("/api/terms", json=terms_body(srt="garbage"))
    assert resp.status_code == 422
    assert resp.json()["detail"]["type"] == "srt"


def test_api_terms_bom_and_blank_lines_lossless():
    src = (
        BOM
        + "1\r\n00:00:01,000 --> 00:00:02,000\r\n北大\r\n\r\n\r\n"
        + "2\n00:00:03,000 --> 00:00:04,000\n也是北大\n"
    )
    body = {"srt": src, "rules": [{"standard": "北京大学", "aliases": ["北大"]}]}
    resp = client.post("/api/terms", json=body)
    assert resp.status_code == 200, resp.text
    out = resp.json()["revised_srt"]
    assert out.startswith(BOM + "1\r\n")
    assert "北京大学\r\n\r\n\r\n" in out
    assert out.endswith("2\n00:00:03,000 --> 00:00:04,000\n也是北京大学\n")
    assert resp.json()["hit_count"] == 2


def test_api_terms_multiple_boms_collapse_to_one_on_change():
    # 有命中需要重写时，多个开头 BOM 最多保留一个（与 /api/correct 一致）；
    # 无命中时则逐字符原样返回（见 test_apply_terms_no_hit_is_identical_input）
    src = BOM + BOM + "1\n00:00:01,000 --> 00:00:02,000\n北大\n"
    body = {"srt": src, "rules": [{"standard": "北京大学", "aliases": ["北大"]}]}
    resp = client.post("/api/terms", json=body)
    assert resp.status_code == 200, resp.text
    out = resp.json()["revised_srt"]
    assert out == BOM + "1\n00:00:01,000 --> 00:00:02,000\n北京大学\n"
    assert not out.startswith(BOM + BOM)


def test_api_terms_longer_alias_in_later_rule_wins():
    # 短别名来自先输入的规则、更长别名来自后输入规则：仍按最长选择
    src = "1\n00:00:01,000 --> 00:00:02,000\nABCD 与 AB\n"
    body = {
        "srt": src,
        "rules": [
            {"standard": "短", "aliases": ["AB"]},
            {"standard": "长", "aliases": ["ABCD"]},
        ],
    }
    resp = client.post("/api/terms", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "长 与 短" in data["revised_srt"]
    assert [(h["column"], h["source"], h["standard"]) for h in data["hits"]] == [
        (0, "ABCD", "长"),
        (7, "AB", "短"),
    ]



# ---------- 字幕智能断行 ----------

def test_char_width_east_asian_combining_and_other():
    assert char_display_width("中") == 2          # W
    assert char_display_width("，") == 2          # F（全角）
    assert char_display_width("Ａ") == 2          # 全角字母 F
    assert char_display_width("A") == 1
    assert char_display_width("1") == 1
    assert char_display_width("é") == 1           # 预组合字符本身占 1 列
    assert char_display_width("́") == 0            # U+0301 组合附加符
    assert char_display_width(" ") == 1


def test_han_and_cjk_punct_classification():
    assert is_han("中")
    assert is_han("㐀") and is_han("𠀀")         # 扩展 A / 扩展 B
    assert not is_han("A") and not is_han("，")
    assert is_cjk_char("，") and is_cjk_char("。")
    assert is_cjk_char("（") and is_cjk_char("《")
    assert is_cjk_char("·") and is_cjk_char("—") and is_cjk_char("’")
    assert not is_cjk_char(",") and not is_cjk_char("A")


def test_normalize_collapses_all_whitespace():
    assert normalize_text("一 二\n三\t四　五") == "一 二 三 四 五"
    assert normalize_text("\r\n  你好　世界 \t ") == "你好 世界"


def test_build_atoms_merges_english_but_splits_cjk():
    atoms, gaps = build_atoms("hello world你好")
    assert [a.text for a in atoms] == ["hello", "world", "你", "好"]
    assert gaps == [True, False, False]  # hello/world 之间有空格，其余相邻
    assert [a.width for a in atoms] == [5, 5, 2, 2]
    assert [a.cjk for a in atoms] == [False, False, True, True]
    # 英文单词内部的标点并入单词，不单独成原子
    atoms2, _ = build_atoms("iPhone15, plus")
    assert [a.text for a in atoms2] == ["iPhone15,", "plus"]
    # 组合附加符并入前一原子，不产生额外原子
    atoms3, _ = build_atoms("café 你好")
    assert [a.text for a in atoms3] == ["café", "你", "好"]
    assert atoms3[0].width == 4  # 4 个基字符，组合符 0 列


def test_wrap_balances_lines_by_squared_slack():
    # 6 个汉字共 12 列、8 列 2 行：均衡 3/3（代价 4+4）优于贪心 4/2（0+16）
    plan = plan_wrap("一二三四五六", 8, 2)
    assert isinstance(plan, WrapPlan)
    assert [line.text for line in plan.lines] == ["一二三", "四五六"]
    assert [line.width for line in plan.lines] == [6, 6]
    assert plan.breaks == (3,)


def test_wrap_tie_break_prefers_earlier_break_sequence():
    # 原子宽度 [2,2,1,2,2]（共 9 列，a 两侧是汉字故独立成原子），7 列 2 行：
    # 断在第 2 个原子后为 4/5（代价 3²+2²=13），断在第 3 个原子后为 5/4
    # （代价 2²+3²=13），完全同分且均为全局最优 -> 取断点序列靠前的 (2,)。
    # 更窄的断点（6/3）代价更大，故同分的恰是这两个断法
    plan = plan_wrap("中中a中中", 7, 2)
    assert isinstance(plan, WrapPlan)
    assert [line.text for line in plan.lines] == ["中中", "a中中"]
    assert [line.width for line in plan.lines] == [4, 5]
    assert plan.breaks == (2,)


def test_wrap_spaces_count_one_column_and_breakable():
    plan = plan_wrap("hello world foo bar", 10, 3)
    assert isinstance(plan, WrapPlan)
    assert [line.text for line in plan.lines] == ["hello", "world", "foo bar"]
    assert [line.width for line in plan.lines] == [5, 5, 7]
    # 行内保留空格，空格计 1 列：4 个汉字 8 列 + 3 个空格 3 列 = 11 列
    plan2 = plan_wrap("一 二 三 四", 11, 1)
    assert isinstance(plan2, WrapPlan)
    assert [line.text for line in plan2.lines] == ["一 二 三 四"]
    assert plan2.lines[0].width == 11
    # 10 列放不下 11 列的单行 -> 必须断成 2 行
    plan3 = plan_wrap("一 二 三 四", 10, 2)
    assert isinstance(plan3, WrapPlan)
    assert len(plan3.lines) == 2


def test_wrap_single_line_fits_when_possible():
    plan = plan_wrap("一二三四五", 10, 1)
    assert [line.text for line in plan.lines] == ["一二三四五"]
    assert plan.breaks == ()


def test_wrap_no_break_inside_english_word():
    # 每个单词必须整体成行
    plan = plan_wrap("internationalization test", 20, 2)
    assert [line.text for line in plan.lines] == ["internationalization", "test"]
    failure = plan_wrap("internationalization", 19, 2)
    assert isinstance(failure, WrapFailure)
    assert "不可断开的片段" in failure.reason and "20 列" in failure.reason


def test_wrap_forbids_open_punct_at_line_end():
    # 8 列：原子 你 好 （ 中 文 ） 世 界
    # “好（”（宽 6）与“（中”（宽 4）后断行都会让“（”处行尾/“）”处行首，
    # 可行断点须避开开标点行尾：断在“中”后 -> 行1=你好（中
    plan = plan_wrap("你好（中文）世界", 8, 2)
    assert [line.text for line in plan.lines] == ["你好（中", "文）世界"]
    assert not any(line.text.endswith("（") for line in plan.lines)


def test_wrap_forbids_close_punct_at_line_start():
    # 11 个原子共 22 列、12 列 2 行：恰好对半断会让第 6 个原子“，”成为
    # 第二行行首（闭标点），故只能把逗号留在第一行行尾（12/10）
    plan = plan_wrap("一二三四五，六七八九十", 12, 2)
    assert isinstance(plan, WrapPlan)
    assert [line.text for line in plan.lines] == ["一二三四五，", "六七八九十"]
    assert [line.width for line in plan.lines] == [12, 10]
    assert not any(line.text.startswith("，") for line in plan.lines)


def test_wrap_failure_when_too_many_lines_required():
    failure = plan_wrap("一二三四五六", 4, 1)
    assert isinstance(failure, WrapFailure)
    assert "行数" in failure.reason


def test_wrap_failure_when_only_kinsoku_violating_splits_exist():
    # 12 列恰好只能 6/6 对半，而第 7 个字符是“，” -> 必出现行首闭标点
    failure = plan_wrap("这是中文内容，还有更多字", 12, 2)
    assert isinstance(failure, WrapFailure)
    assert "行首不得为闭标点" in failure.reason


def test_wrap_document_preserves_eol_bom_and_positioning():
    src = (
        BOM
        + "1\r\n00:00:01,000 --> 00:00:03,000\r\n一二三四五六\r\n\r\n"
        + "2\n00:00:04,000 --> 00:00:06,000 X1:10\n你好世界\n"
    )
    out, results = wrap_document(src, 8, 2)
    # BOM 唯一；第一块原 CRLF 风格保留（新增内部换行也用 CRLF）
    assert out.startswith(BOM)
    assert out.startswith(BOM + "1\r\n00:00:01,000 --> 00:00:03,000\r\n")
    assert "一二三\r\n四五六\r\n" in out
    # 第二块 LF 与定位参数原样
    assert "00:00:04,000 --> 00:00:06,000 X1:10\n" in out
    assert results[0].changed and not results[1].changed


def test_wrap_document_identity_when_nothing_changes():
    src = BOM + BOM + "1\n00:00:01,000 --> 00:00:02,000\n一二三四五\n"
    out, results = wrap_document(src, 20, 2)
    assert out == src  # 多个 BOM 也逐字符保留
    assert results[0].success and not results[0].changed


def test_wrap_document_multiple_boms_collapse_to_one_on_change():
    src = BOM + BOM + "1\n00:00:01,000 --> 00:00:02,000\n一二三四五六\n"
    out, _ = wrap_document(src, 8, 2)
    assert out == BOM + "1\n00:00:01,000 --> 00:00:02,000\n一二三\n四五六\n"


def test_wrap_document_cr_only_file_uses_cr_for_new_breaks():
    src = "1\r00:00:01,000 --> 00:00:02,000\r一二三四五六\r"
    out, _ = wrap_document(src, 8, 2)
    assert out == "1\r00:00:01,000 --> 00:00:02,000\r一二三\r四五六\r"


def test_wrap_document_keeps_original_failure_cue_and_blank_lines():
    src = (
        "1\n00:00:01,000 --> 00:00:02,000\n一二三四五六\n\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\nsupercalifragilistic\n\n"
        "3\n00:00:05,000 --> 00:00:06,000\n五六七八九十\n"
    )
    out, results = wrap_document(src, 8, 2)
    status = [(r.cue_order, r.success, r.changed) for r in results]
    assert status == [(1, True, True), (2, False, False), (3, True, True)]
    # 失败字幕原文不动
    assert "2\n00:00:03,000 --> 00:00:04,000\nsupercalifragilistic\n" in out
    # 多余空行保留
    assert "一二三\n四五六\n\n\n2\n" in out
    assert results[1].reason and "不可断开的片段" in results[1].reason


def test_wrap_document_collapses_inner_newlines_to_spaces():
    src = "1\n00:00:01,000 --> 00:00:03,000\n你好\n世界\n"
    out, results = wrap_document(src, 20, 1)
    assert out == "1\n00:00:01,000 --> 00:00:03,000\n你好 世界\n"
    assert [line.text for line in results[0].lines] == ["你好 世界"]


def test_wrap_page_served():
    resp = client.get("/wrap")
    assert resp.status_code == 200
    assert "字幕智能断行" in resp.text
    assert "/api/wrap" in resp.text


WRAP_SRT = (
    "1\r\n00:00:01,000 --> 00:00:03,000\r\n一二三四五六\r\n\r\n"
    "2\n00:00:04,000 --> 00:00:06,000 X1:10\n你好 世界 hello\n第二行内容\n\n"
    "3\n00:00:07,000 --> 00:00:08,000\nsupercalifragilistic\n"
)


def wrap_body(**overrides):
    body = {"srt": WRAP_SRT, "max_columns": 8, "max_lines": 2}
    body.update(overrides)
    return body


def test_api_wrap_success_and_failure_counts():
    resp = client.post("/api/wrap", json=wrap_body())
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["cue_count"] == 3
    # 块1 可断 3/3；块2 空白折叠后宽 19 列、8 列至少 4 行；块3 单词 20 列超宽
    assert data["wrapped_count"] == 1
    assert data["failed_count"] == 2
    assert data["changed_count"] == 1
    cue1 = data["cues"][0]
    assert cue1["cue_order"] == 1 and cue1["index"] == "1" and cue1["changed"]
    assert [[l["text"], l["width"]] for l in cue1["lines"]] == [
        ["一二三", 6], ["四五六", 6]
    ]
    assert [f["cue_order"] for f in data["failures"]] == [2, 3]
    assert "最多行数" in data["failures"][0]["reason"]
    assert "不可断开的片段" in data["failures"][1]["reason"]


def test_api_wrap_srt_is_lossless_except_wrapped_bodies():
    data = client.post("/api/wrap", json=wrap_body()).json()
    out = data["wrapped_srt"]
    assert out.startswith("1\r\n00:00:01,000 --> 00:00:03,000\r\n")
    assert "一二三\r\n四五六\r\n" in out
    # 失败块 2：原文（含 X1 定位参数、LF、两行正文）完全不动
    assert (
        "2\n00:00:04,000 --> 00:00:06,000 X1:10\n"
        "你好 世界 hello\n第二行内容\n"
    ) in out
    # 失败块 3：超长单词原样
    assert "3\n00:00:07,000 --> 00:00:08,000\nsupercalifragilistic\n" in out
    # 块1 原本 1 行 CRLF、断行后变 2 行 CRLF（新增内部换行沿用原 CRLF）；
    # 失败块 2、3 的 LF 风格保持不变
    assert "一二三\r\n四五六\r\n" in out
    assert out.count("\n") - out.count("\r\n") == WRAP_SRT.count("\n") - WRAP_SRT.count("\r\n")


def test_api_wrap_no_change_returns_input_identically():
    # 每条正文均为单行且放得下（含 CRLF / LF 混排）：归一化不改变任何正文，
    # wrapped_srt 与输入逐字符一致
    single_line_srt = (
        "1\r\n00:00:01,000 --> 00:00:03,000\r\n一二三四五六\r\n\r\n"
        "2\n00:00:04,000 --> 00:00:06,000 X1:10\n你好 世界 hello\n\n"
        "3\n00:00:07,000 --> 00:00:08,000\nsupercalifragilistic\n"
    )
    resp = client.post(
        "/api/wrap",
        json={"srt": single_line_srt, "max_columns": 100, "max_lines": 1},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["wrapped_srt"] == single_line_srt
    assert data["changed_count"] == 0
    assert all(c["success"] for c in data["cues"])
    assert data["failures"] == []


def test_api_wrap_multiline_body_newlines_collapse_even_when_wide():
    # 多行正文即使在足够宽、单行放得下时，原有换行也会折成空格（规格要求）
    src = "1\n00:00:01,000 --> 00:00:03,000\n你好\n世界\n"
    resp = client.post(
        "/api/wrap", json={"srt": src, "max_columns": 100, "max_lines": 1}
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()["wrapped_srt"]
    assert out == "1\n00:00:01,000 --> 00:00:03,000\n你好 世界\n"
    assert resp.json()["changed_count"] == 1


@pytest.mark.parametrize("bad_value", [0, -1, 1.5, True, False, "8", None])
def test_api_wrap_rejects_bad_max_columns(bad_value):
    resp = client.post("/api/wrap", json=wrap_body(max_columns=bad_value))
    assert resp.status_code == 422
    assert resp.json()["detail"]["field"] == "max_columns"


@pytest.mark.parametrize("bad_value", [0, -1, 1.5, True, False, "2", None])
def test_api_wrap_rejects_bad_max_lines(bad_value):
    resp = client.post("/api/wrap", json=wrap_body(max_lines=bad_value))
    assert resp.status_code == 422
    assert resp.json()["detail"]["field"] == "max_lines"


def test_api_wrap_requires_fields():
    assert client.post("/api/wrap", json={"srt": WRAP_SRT}).status_code == 422
    assert client.post(
        "/api/wrap", json={"max_columns": 8, "max_lines": 2}
    ).status_code == 422


def test_api_wrap_rejects_bad_srt():
    resp = client.post("/api/wrap", json=wrap_body(srt="garbage without timing"))
    assert resp.status_code == 422
    assert resp.json()["detail"]["type"] == "srt"
