# -*- coding: utf-8 -*-
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mp3frame.core import (  # noqa: E402
    Frame,
    build_report,
    frame_length,
    parse_header,
    samples_per_frame,
    scan,
    skip_id3,
)

SAMPLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "data", "sample.mp3")


# --- 构造辅助 -------------------------------------------------------------

def header(version_bits, layer_bits, bitrate_idx, rate_idx, padded=0,
           protection=1, byte3=0x00):
    b1 = 0xE0 | (version_bits << 3) | (layer_bits << 1) | (protection & 1)
    b2 = (bitrate_idx << 4) | (rate_idx << 2) | (padded << 1)
    return bytes([0xFF, b1, b2, byte3])


def make_frame(version_bits=3, layer_bits=1, bitrate_idx=9, rate_idx=0,
               padded=0, protection=1, fill=0x00):
    # 由帧头反推帧长，避免测试里复写公式。
    h = header(version_bits, layer_bits, bitrate_idx, rate_idx, padded, protection)
    probe = parse_header(h, 0)
    assert probe is not None, (version_bits, layer_bits)
    body = bytes([fill]) * (probe.size - len(h))
    return h + body


def id3v2(body, flags=0, footer=b""):
    size = len(body)
    syncsafe = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F,
                      (size >> 7) & 0x7F, size & 0x7F])
    return b"ID3\x03\x00" + bytes([flags]) + syncsafe + body + footer


# --- 原有测试 -------------------------------------------------------------

def test_frame_repr():
    f = Frame(0, 3, 1, 128, 44100, 0, 417)
    assert "128" in repr(f)


def test_parse_silence_returns_none():
    assert parse_header(b"\x00\x00\x00\x00", 0) is None


def test_parse_short_data():
    assert parse_header(b"\xff\xfb", 0) is None


# --- 1. ID3 ---------------------------------------------------------------

def test_skip_id3v2_uses_syncsafe_length():
    # 0x7F 在同步安全整数里按 7 位解释：body = 0x00_00_02_01 -> 257，
    # 若被当成普通大端 32 位则会是 513。用尾部标记确认跳到了正确位置。
    body = b"\x00" * 257
    data = id3v2(body) + b"AUDIO"
    assert skip_id3(data) == 10 + 257
    assert data[skip_id3(data):] == b"AUDIO"


def test_skip_id3v2_multiple_tags_and_footer():
    first = id3v2(b"\x11" * 40, flags=0x10, footer=b"3DI" + b"\x00" * 7)
    second = id3v2(b"\x22" * 300)
    data = first + second + b"AUDIO"
    assert skip_id3(data) == len(first) + len(second)


def test_scan_skips_id3v2_without_byte_scanning():
    data = id3v2(b"\xff\xfb\x90\x44" + b"\x00" * 30) + make_frame() * 3
    frames = scan(data)
    assert len(frames) == 3
    # 假同步字在标签体内，若逐字节扫描会被误认；正确跳过时首帧在标签之后。
    assert frames[0].offset == skip_id3(data)


def test_scan_ignores_id3v1_tail():
    frames = make_frame() * 2
    # 尾标内容故意包含一个看似合法的帧头，不能被扫描进来。
    tail = b"TAG" + b"\x00" * 122 + b"\x00" * 3
    # 把合法帧头塞进尾标末尾（同时保持 128 字节长度）。
    tail = b"TAG" + b"\x00" * 121 + b"\xff\xfb\x90\x44"
    assert len(tail) == 128
    data = frames + tail
    got = scan(data)
    assert len(got) == 2
    assert [f.offset for f in got] == [0, len(frames) // 2]


def test_skip_id3_truncated_tag():
    data = b"ID3\x03\x00\x00\x00\x7f\x7f\x7f"   # 声称很大但被截断
    assert skip_id3(data) == len(data)


# --- 2. 帧头与比特率表 ----------------------------------------------------

def test_four_bitrate_tables_are_distinct():
    # 同一个比特率索引 2 在四类表里取值不同。
    l1 = parse_header(header(3, 3, 2, 0), 0)
    l2 = parse_header(header(3, 2, 2, 0), 0)
    l3 = parse_header(header(3, 1, 2, 0), 0)
    l2_v2 = parse_header(header(2, 2, 2, 0), 0)
    assert (l1.bitrate, l2.bitrate, l3.bitrate, l2_v2.bitrate) == (64, 48, 40, 16)


def test_layer_and_version_are_recorded():
    f = parse_header(header(2, 1, 9, 0), 0)
    assert f.version == 2 and f.version_name == "2"
    assert f.layer == 1 and f.layer_num == 3
    assert f.sample_rate == 22050


def test_free_format_and_invalid_indices_rejected():
    assert parse_header(header(3, 1, 0, 0), 0) is None     # 自由格式
    assert parse_header(header(3, 1, 15, 0), 0) is None    # 无效比特率
    assert parse_header(header(3, 1, 9, 3), 0) is None     # 保留采样率
    assert parse_header(header(1, 1, 9, 0), 0) is None     # 保留版本
    assert parse_header(header(3, 0, 9, 0), 0) is None     # 保留层


def test_version_and_samples_per_frame():
    # 参数沿用帧头原始位值：layer 3/2/1 = Layer I/II/III。
    assert samples_per_frame(3, 3) == 384
    assert samples_per_frame(3, 2) == 1152
    assert samples_per_frame(3, 1) == 1152
    assert samples_per_frame(2, 1) == 576
    assert samples_per_frame(0, 1) == 576


# --- 3. 帧长与时长 --------------------------------------------------------

def test_frame_length_formulas_by_layer():
    # MPEG-1 Layer I 以 4 字节槽计。
    assert frame_length(3, 3, 128, 44100, 0) == (12 * 128000 // 44100) * 4 == 136
    # MPEG-1 Layer II / III 用 144 系数。
    assert frame_length(3, 2, 128, 44100, 0) == 144 * 128000 // 44100 == 417
    assert frame_length(3, 1, 128, 44100, 0) == 417
    # MPEG-2 Layer III 用 72 系数。
    assert frame_length(2, 1, 64, 22050, 0) == 72 * 64000 // 22050 == 208
    # MPEG-2 Layer II 仍用 144。
    assert frame_length(2, 2, 64, 22050, 0) == 144 * 64000 // 22050 == 417
    # padding 影响：Layer I 加一个槽，其他层加一个字节。
    assert frame_length(3, 1, 128, 44100, 1) == 418
    assert frame_length(3, 3, 128, 44100, 1) == (12 * 128000 // 44100 + 1) * 4 == 140


def test_frame_size_matches_header_when_scanning():
    for version_bits, layer_bits in [(3, 1), (3, 2), (3, 3), (2, 1), (0, 1)]:
        data = make_frame(version_bits, layer_bits)
        f = parse_header(data, 0)
        assert f.size == len(data)


def test_duration_per_frame_by_layer_and_rate():
    assert parse_header(header(3, 1, 9, 0), 0).duration_ms == 1152 * 1000 / 44100
    assert parse_header(header(3, 3, 2, 0), 0).duration_ms == 384 * 1000 / 44100
    assert parse_header(header(3, 2, 9, 0), 0).duration_ms == 1152 * 1000 / 44100
    assert parse_header(header(2, 1, 9, 0), 0).duration_ms == 576 * 1000 / 22050

def test_frame_size_matches_header_when_scanning_more_layers():
    for version_bits, layer_bits in [(3, 2), (3, 3), (2, 2), (2, 1), (0, 1)]:
        assert parse_header(header(version_bits, layer_bits, 9, 0), 0).size > 4


def test_scan_25_frames_total_duration_within_5ms():
    data = make_frame() * 25
    frames = scan(data)
    assert len(frames) == 25
    expected = 25 * 1152 * 1000.0 / 44100
    assert abs(sum(f.duration_ms for f in frames) - expected) <= 5.0


def test_scan_offsets_do_not_drift():
    data = b"".join(make_frame(protection=1) for _ in range(15))
    data += b"".join(make_frame(protection=0) for _ in range(10))
    frames = scan(data)
    assert len(frames) == 25
    for prev, cur in zip(frames, frames[1:]):
        assert cur.offset == prev.offset + prev.size


# --- 4. CRC ---------------------------------------------------------------

def test_crc_flag_and_header_size():
    plain = parse_header(header(3, 1, 9, 0, protection=1), 0)
    protected = parse_header(header(3, 1, 9, 0, protection=0), 0)
    assert plain.crc is False and plain.header_size == 4
    assert plain.payload_offset == 4
    assert protected.crc is True and protected.header_size == 6
    assert protected.payload_offset == 6
    # CRC 的 2 字节已经包含在帧长内，帧长不因保护位改变。
    assert plain.size == protected.size


def test_crc_frames_payload_offset_and_sizes():
    # protection=0 表示有 CRC：payload 从第 6 字节开始。
    data = make_frame(protection=0) + make_frame(protection=0) + make_frame(protection=1)
    frames = scan(data)
    assert frames[0].payload_offset == 6
    assert frames[2].payload_offset == frames[2].offset + 4
    # 保护位不改变帧长，下帧偏移仍然间隔 417。
    assert [f.offset for f in frames] == [0, 417, 834]


def test_scan_handles_crc_frames():
    data = make_frame(protection=0) * 3 + make_frame(protection=1) * 2
    frames = scan(data)
    assert len(frames) == 5
    assert [f.crc for f in frames] == [True, True, True, False, False]


# --- 5. 重同步与统计 ------------------------------------------------------

def test_resync_after_corrupted_bytes():
    a = make_frame()
    b = make_frame()
    c = make_frame()
    garbage = b"\x00\x01\xff\x00\x7f"     # 不含完整同步字
    data = a + garbage + b + c
    frames = scan(data)
    assert len(frames) == 3
    assert [f.offset for f in frames] == [0, len(a) + len(garbage),
                                          len(a) + len(garbage) + len(b)]


def test_fake_sync_word_with_invalid_header_is_not_a_frame():
    # 0xFF 0xE0 之后比特率索引 15 非法，不能当成帧；后续真帧仍要找回。
    fake = bytes([0xFF, 0xE0, 0xF0, 0x00])
    data = bytes([0x00, 0x00]) + fake + make_frame() * 2
    frames = scan(data)
    assert len(frames) == 2
    assert frames[0].offset == 2 + len(fake)


def test_build_report_counts_and_offsets():
    first = make_frame(bitrate_idx=9) * 15      # 128 kbps
    second = make_frame(bitrate_idx=11) * 10    # 192 kbps
    frames = scan(first + second)
    report = build_report(frames)
    assert report["frames"] == 25
    assert report["bitrate_distribution"] == {"128": 15, "192": 10}
    assert report["first_frame_offset"] == 0
    assert report["frame_offsets"] == [f.offset for f in frames]
    assert report["frame_offsets"][15] == 15 * 417


def test_sample_file():
    with open(SAMPLE, "rb") as f:
        data = f.read()
    frames = scan(data)
    report = build_report(frames)
    assert report["frames"] == 25
    assert report["first_frame_offset"] == 30
    # sample.mp3 后 10 帧帧头写的是索引 13（256kbps），但实际间隔 626 字节
    # 是 192kbps 的帧长；扫描器按实际长度修正，并保留声明值。
    assert report["bitrate_distribution"] == {"128": 15, "192": 10}
    assert report["declared_bitrate_distribution"] == {"128": 15, "256": 10}
    assert report["resynced_frames"] == 10
    assert report["truncated_frames"] == 0
    assert abs(report["duration_ms"] - 25 * 1152 * 1000.0 / 44100) <= 5.0


def test_sample_offsets_are_exact():
    with open(SAMPLE, "rb") as f:
        data = f.read()
    offsets = [f.offset for f in scan(data)]
    expected = [30 + 417 * i for i in range(15)]
    expected += [30 + 417 * 15 + 626 * i for i in range(10)]
    assert offsets == expected


def test_resync_ignores_fake_sync_inside_declared_frame():
    # 帧载数据里埋一个合法帧头，但不在任何合法帧长位置上；不能误切。
    embedded = make_frame()
    payload = bytes([0x00]) * 30 + embedded[:4] + bytes([0x00]) * 379
    a_full = header(3, 1, 9, 0) + payload
    assert len(a_full) == 417
    frames = scan(a_full + make_frame() * 2)
    assert [f.offset for f in frames] == [0, 417, 834]
