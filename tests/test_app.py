"""核心算法与 FastAPI 接口测试。"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
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
from app.reading_load import count_chars, cue_loads, find_hot_zones
from app.srt import BOM, SrtParseError, format_srt, parse_srt, rewrite_srt
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

# cue1: 6 字 / 2s = 3 字/秒；cue2: 9 字 / 3s = 3 字/秒；重叠 [1s, 2s] 合计 6
LOAD_SRT = (
    "1\n00:00:00,000 --> 00:00:02,000\n一二三四五六\n\n"
    "2\n00:00:01,000 --> 00:00:04,000\nABCDEFGHI\n"
)


def test_count_chars_strips_all_unicode_whitespace():
    assert count_chars("一　二\n三\t四") == 4  # 全角空格、换行、制表符全部去掉
    assert count_chars("a b") == 2  # 不间断空格 U+00A0
    assert count_chars("  \n\t　") == 0


def test_cue_loads_rate_and_order():
    loads = cue_loads(parse_srt(LOAD_SRT))
    assert [l.order for l in loads] == [1, 2]
    assert loads[0].chars == 6 and loads[0].duration_ms == 2000
    assert loads[0].rate == 3.0
    assert loads[1].chars == 9 and loads[1].rate == 3.0


def test_hot_zone_overlap_sums_rates():
    zones = find_hot_zones(parse_srt(LOAD_SRT), 5.0, 0)
    assert len(zones) == 1
    zone = zones[0]
    assert (zone.start, zone.end) == (1000, 2000)
    assert zone.peak_rate == 6.0
    assert zone.cue_orders == [1, 2]


def test_threshold_is_strict_greater_than():
    # 阈值恰好等于合计速率时不算热区（严格超过）
    assert find_hot_zones(parse_srt(LOAD_SRT), 6.0, 0) == []
    # 单条速率恰好 3 == 阈值也不算；重叠处 6 > 3 才算
    zones = find_hot_zones(parse_srt(LOAD_SRT), 3.0, 0)
    assert len(zones) == 1
    assert (zones[0].start, zones[0].end) == (1000, 2000)


def test_min_duration_filters_short_zones():
    # 重叠段只有 1000ms：下限 1001ms 被过滤，下限 1000ms 保留
    assert find_hot_zones(parse_srt(LOAD_SRT), 5.0, 1001) == []
    zones = find_hot_zones(parse_srt(LOAD_SRT), 5.0, 1000)
    assert len(zones) == 1
    assert zones[0].end - zones[0].start == 1000


def test_touching_zones_merge_but_gapped_zones_stay_separate():
    # 两条相接字幕各自速率都为 10 字/秒，端点相接合并为一段
    touching = (
        "1\n00:00:00,000 --> 00:00:01,000\n0123456789\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\nabcdefghij\n"
    )
    zones = find_hot_zones(parse_srt(touching), 5.0, 0)
    assert len(zones) == 1
    assert (zones[0].start, zones[0].end) == (0, 2000)
    assert zones[0].peak_rate == 10.0
    assert zones[0].cue_orders == [1, 2]

    # 中间夹一段非超阈区间则保持两段独立
    gapped = (
        "1\n00:00:00,000 --> 00:00:01,000\n0123456789\n\n"
        "2\n00:00:02,000 --> 00:00:03,000\nabcdefghij\n"
    )
    zones = find_hot_zones(parse_srt(gapped), 5.0, 0)
    assert [(z.start, z.end) for z in zones] == [(0, 1000), (2000, 3000)]


def test_sub_millisecond_boundaries_are_not_second_sampled():
    # 热区只有 400ms（非整秒边界），整秒采样会漏掉，事件扫描必须识别
    short = (
        "1\n00:00:00,000 --> 00:00:01,000\n0123456789\n\n"  # 10 字/秒
        "2\n00:00:00,200 --> 00:00:00,600\nABCDE\n"  # 12.5 字/秒
    )
    zones = find_hot_zones(parse_srt(short), 15.0, 0)
    assert len(zones) == 1
    assert (zones[0].start, zones[0].end) == (200, 600)
    assert zones[0].peak_rate == 22.5
    assert zones[0].cue_orders == [1, 2]
    # 下限超过 400ms 时过滤
    assert find_hot_zones(parse_srt(short), 15.0, 401) == []


def test_peak_tracks_highest_segment_and_participants():
    # 三条叠加：[2s,3s) 三条同时在，峰值在此时段
    srt = (
        "1\n00:00:00,000 --> 00:00:03,000\n" + "甲" * 30 + "\n\n"      # 10 字/秒
        "2\n00:00:01,000 --> 00:00:03,000\n" + "乙" * 20 + "\n\n"      # 10 字/秒
        "3\n00:00:02,000 --> 00:00:04,000\n" + "丙" * 20 + "\n"        # 10 字/秒
    )
    zones = find_hot_zones(parse_srt(srt), 15.0, 0)
    assert len(zones) == 1
    zone = zones[0]
    # [1s,2s) 两条合计 20；[2s,3s) 三条合计 30（峰值）；3s 后仅剩 cue3 = 10 落出热区
    assert (zone.start, zone.end) == (1000, 3000)
    assert zone.peak_rate == 30.0
    assert zone.cue_orders == [1, 2, 3]


def test_no_zones_when_all_rates_below_threshold():
    assert find_hot_zones(parse_srt(LOAD_SRT), 100.0, 0) == []


def test_reading_load_page_served():
    resp = client.get("/reading-load")
    assert resp.status_code == 200
    assert "阅读负荷热区" in resp.text


def reading_load_body(**overrides):
    body = {"srt": LOAD_SRT, "threshold": 5.0, "min_duration_ms": 0}
    body.update(overrides)
    return body


def test_api_reading_load_success():
    resp = client.post("/api/reading-load", json=reading_load_body())
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["threshold"] == 5.0
    assert data["min_duration_ms"] == 0
    assert len(data["zones"]) == 1
    zone = data["zones"][0]
    assert zone["start"] == "00:00:01,000"
    assert zone["end"] == "00:00:02,000"
    assert zone["start_ms"] == 1000 and zone["end_ms"] == 2000
    assert zone["duration_ms"] == 1000
    assert zone["peak_rate"] == 6.0
    assert zone["cue_orders"] == [1, 2]
    assert [c["order"] for c in data["cues"]] == [1, 2]
    assert data["cues"][0]["chars"] == 6
    assert data["cues"][0]["rate"] == 3.0
    assert data["cues"][0]["text"] == "一二三四五六"


def test_api_reading_load_min_duration_applied():
    resp = client.post(
        "/api/reading-load", json=reading_load_body(min_duration_ms=1001)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["zones"] == []


@pytest.mark.parametrize("bad_threshold", [0, -1, -0.5, True, False, "5", "abc", None])
def test_api_reading_load_rejects_non_positive_threshold(bad_threshold):
    resp = client.post(
        "/api/reading-load", json=reading_load_body(threshold=bad_threshold)
    )
    assert resp.status_code == 422


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_api_reading_load_rejects_non_finite_threshold_json(literal):
    # NaN / Infinity 不是合法 JSON 数值，服务端仍应稳定返回 422 而非 500
    resp = client.post(
        "/api/reading-load",
        content='{"srt":"x","threshold":%s,"min_duration_ms":0}' % literal,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422


@pytest.mark.parametrize("bad_ms", [-1, 1.5, True, False, "5", "abc"])
def test_api_reading_load_rejects_non_nonneg_int_duration(bad_ms):
    resp = client.post(
        "/api/reading-load", json=reading_load_body(min_duration_ms=bad_ms)
    )
    assert resp.status_code == 422


def test_api_reading_load_requires_fields():
    resp = client.post("/api/reading-load", json={"srt": LOAD_SRT})
    assert resp.status_code == 422


def test_api_reading_load_rejects_bad_srt():
    resp = client.post(
        "/api/reading-load", json=reading_load_body(srt="garbage without timing")
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["type"] == "srt"


def test_api_reading_load_whitespace_chars_example():
    # 正文含全角空格与换行：计字时全部去掉
    srt = "1\n00:00:00,000 --> 00:00:01,000\n一　二\n三\t四\n"
    resp = client.post(
        "/api/reading-load",
        json={"srt": srt, "threshold": 3.0, "min_duration_ms": 0},
    )
    assert resp.status_code == 200, resp.text
    cue = resp.json()["cues"][0]
    assert cue["chars"] == 4
    assert cue["rate"] == 4.0
