# -*- coding: utf-8 -*-
"""MPEG-1 / MPEG-2 / MPEG-2.5 Layer I-III 帧头解析与扫描。

实现要点：

* ID3v2 头用同步安全整数（syncsafe）读取整段长度后整体跳过；
  连续的多个标签会依次跳过；ID3v1 尾标（最后 128 字节以 ``TAG``
  开头）在扫描时排除。
* 版本、层、比特率索引、采样率索引全部参与帧长与时长计算，
  四类比特率表（MPEG-1 L1/L2/L3、MPEG-2/2.5）分别查表。
* 比特率索引 0（自由格式）与 15（无效）、采样率索引 3（保留）、
  版本 1 / 层 0（保留）都不当作正常帧。
* 帧长按层区分：Layer I 以 4 字节槽计，Layer II 用 144 系数，
  Layer III 在 MPEG-1 用 144、MPEG-2/2.5 用 72 系数。
* 保护位为 0 表示帧头后跟 16 位 CRC；CRC 的 2 字节已经包含在帧长里，
  ``header_size`` 变为 6，``payload_offset`` 相应后移。
* 扫描时如果按帧长跳到的位置不是合法帧头，会先在当前帧内寻找更早的
  真实帧头（帧长字段与实际间隔不一致的情况），再退化为逐字节重新
  同步，不会因为一段损坏字节放弃后续所有帧。
"""

BITRATES = {
    # MPEG-1
    "v1l1": [0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448, 0],
    "v1l2": [0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384, 0],
    "v1l3": [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0],
    # MPEG-2 / MPEG-2.5
    "v2l1": [0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256, 0],
    "v2l2": [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0],
    "v2l3": [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0],
}

SAMPLE_RATES = {
    3: [44100, 48000, 32000],   # MPEG-1
    2: [22050, 24000, 16000],   # MPEG-2
    0: [11025, 12000, 8000],    # MPEG-2.5
}

VERSION_NAMES = {3: "1", 2: "2", 0: "2.5"}
LAYER_NUMBERS = {3: 1, 2: 2, 1: 3}          # 层字段原始两位 -> 实际层号
LAYER_NAMES = {1: "I", 2: "II", 3: "III"}
BITRATE_TABLE_KEYS = {
    3: {1: "v1l1", 2: "v1l2", 3: "v1l3"},
    2: {1: "v2l1", 2: "v2l2", 3: "v2l3"},
    0: {1: "v2l1", 2: "v2l2", 3: "v2l3"},
}

ID3V1_SIZE = 128


class Frame(object):
    """一个已定位的 MP3 帧。

    ``version`` 与 ``layer`` 沿用骨架里的原始位值：
    version 3/2/0 表示 MPEG-1/2/2.5，layer 3/2/1 表示 Layer I/II/III；
    可读的层号与版本名见 ``layer_num`` / ``version_name``。
    """

    def __init__(self, offset, version, layer, bitrate, sample_rate, padded, size):
        self.offset = offset
        self.version = version
        self.layer = layer
        self.bitrate = bitrate
        self.sample_rate = sample_rate
        self.padded = padded
        self.size = size

        # 以下属性由 parse_header 补齐；直接构造 Frame 时也有合理默认值。
        self.bitrate_index = None
        self.sample_rate_index = None
        self.crc = False
        self.header_size = 4
        self.samples_per_frame = samples_per_frame(version, layer)
        self.duration_ms = frame_duration_ms(
            version, layer, sample_rate
        )
        self.version_name = VERSION_NAMES.get(version, "?")
        self.layer_num = LAYER_NUMBERS.get(layer, 0)

        # 由 scan 填写：帧头声明的长度与实际占用的长度不一致时，
        # actual_size 是到下一个帧头（或文件尾）的真实距离。
        self.actual_size = size
        self.size_mismatch = False
        self.truncated = False
        # 帧头声明的比特率与帧长；scan 在帧长与实际间隔不符时会按
        # 实际间隔修正 bitrate，原值保留在这里便于核查。
        self.declared_bitrate = bitrate
        self.declared_size = size

    @property
    def payload_offset(self):
        """帧数据的起始偏移（跳过 4 字节帧头与可选 CRC）。"""
        return self.offset + self.header_size

    def __repr__(self):
        return "Frame(@%d, MPEG%s Layer %s, %dkbps, %dHz, %dB)" % (
            self.offset,
            self.version_name,
            LAYER_NAMES.get(self.layer_num, "?"),
            self.bitrate,
            self.sample_rate,
            self.size,
        )


def samples_per_frame(version, layer):
    """每帧采样数（version/layer 为帧头里的原始位值）。

    Layer I 384；Layer II 1152；Layer III MPEG-1 1152、MPEG-2/2.5 576。
    """
    layer_num = LAYER_NUMBERS.get(layer)
    if layer_num == 1:
        return 384
    if layer_num == 2:
        return 1152
    if layer_num == 3:
        return 1152 if version == 3 else 576
    return 0


def frame_duration_ms(version, layer, sample_rate):
    """一帧的时长（毫秒），按层与实际采样数计算。"""
    samples = samples_per_frame(version, layer)
    if not samples or not sample_rate:
        return 0.0
    return samples * 1000.0 / sample_rate


def frame_length(version, layer, bitrate, sample_rate, padded):
    """按层计算整帧字节数（含 4 字节帧头与可选 CRC）。"""
    layer_num = LAYER_NUMBERS[layer]
    if layer_num == 1:
        # Layer I 以 4 字节为一个槽。
        return (12 * bitrate * 1000 // sample_rate + padded) * 4
    if layer_num == 2:
        return 144 * bitrate * 1000 // sample_rate + padded
    coefficient = 144 if version == 3 else 72
    return coefficient * bitrate * 1000 // sample_rate + padded


def _bitrate_table_key(version, layer):
    """返回比特率表键；保留的版本/层组合返回 None。"""
    try:
        return BITRATE_TABLE_KEYS[version][LAYER_NUMBERS[layer]]
    except KeyError:
        return None


def parse_header(data, offset):
    """解析 ``data[offset:]`` 处的帧头，失败返回 None。

    只有帧同步字并不够：版本、层、比特率索引、采样率索引都必须合法。
    比特率索引 0 是自由格式（无法确定帧长），15 为无效，二者都返回 None。
    """
    if offset < 0 or offset + 4 > len(data):
        return None
    b0, b1, b2, b3 = data[offset], data[offset + 1], data[offset + 2], data[offset + 3]
    if b0 != 0xFF or (b1 & 0xE0) != 0xE0:
        return None

    version_bits = (b1 >> 3) & 0x03
    layer_bits = (b1 >> 1) & 0x03
    if version_bits == 1:          # 保留版本
        return None
    if layer_bits == 0:            # 保留层
        return None

    bitrate_index = (b2 >> 4) & 0x0F
    rate_index = (b2 >> 2) & 0x03
    padded = (b2 >> 1) & 0x01

    if bitrate_index in (0, 15):   # 0 = 自由格式，15 = 无效
        return None
    if rate_index == 3:            # 保留采样率索引
        return None

    table = BITRATES[_bitrate_table_key(version_bits, layer_bits)]
    bitrate = table[bitrate_index]
    sample_rate = SAMPLE_RATES[version_bits][rate_index]
    if bitrate == 0 or sample_rate == 0:
        return None

    size = frame_length(version_bits, layer_bits, bitrate, sample_rate, padded)
    if size < 4:
        return None

    frame = Frame(
        offset, version_bits, layer_bits, bitrate, sample_rate, padded, size
    )
    frame.bitrate_index = bitrate_index
    frame.sample_rate_index = rate_index
    # 保护位为 0 表示帧头后跟 16 位 CRC；这 2 字节已经包含在 size 里。
    frame.crc = not (b1 & 0x01)
    frame.header_size = 6 if frame.crc else 4
    frame.samples_per_frame = samples_per_frame(version_bits, layer_bits)
    frame.duration_ms = frame_duration_ms(version_bits, layer_bits, sample_rate)
    frame.version_name = VERSION_NAMES[version_bits]
    frame.layer_num = LAYER_NUMBERS[layer_bits]
    return frame


def _syncsafe_to_int(raw, start):
    """读取 4 字节同步安全整数（每字节只用低 7 位）。"""
    return (
        ((raw[start] & 0x7F) << 21)
        | ((raw[start + 1] & 0x7F) << 14)
        | ((raw[start + 2] & 0x7F) << 7)
        | (raw[start + 3] & 0x7F)
    )


def skip_id3(data):
    """返回第一个音频帧的偏移，整体跳过开头连续的 ID3v2 标签。

    ID3v2 头 10 字节：``ID3`` + 版本 2 + 标志 1 + 同步安全长度 4。
    长度字段只表示标签体（含 padding），不含 10 字节头；若设置了
    footer 标志（0x10），尾部还有额外 10 字节。
    """
    pos = 0
    total_len = len(data)
    while pos + 10 <= total_len and data[pos:pos + 3] == b"ID3":
        flags = data[pos + 5]
        body_size = _syncsafe_to_int(data, pos + 6)
        tag_size = 10 + body_size + (10 if flags & 0x10 else 0)
        new_pos = pos + tag_size
        if new_pos > total_len:
            # 标签被截断，后面不可能再有完整帧。
            return total_len
        pos = new_pos
    if pos == 0 and total_len < 10 and data[:3] == b"ID3":
        # 只剩一个不完整的 ID3 头，按没有音频处理。
        return total_len
    return pos


def _id3v1_start(data):
    """若文件以 ID3v1 尾标结束，返回尾标起点，否则返回文件长度。"""
    if len(data) >= ID3V1_SIZE and data[-ID3V1_SIZE:-ID3V1_SIZE + 3] == b"TAG":
        return len(data) - ID3V1_SIZE
    return len(data)


def _valid_frame_lengths(version, layer, sample_rate, padded):
    """该版本/层/采样率下所有合法比特率对应的帧长集合。"""
    key = _bitrate_table_key(version, layer)
    if key is None:
        return set()
    return {
        frame_length(version, layer, bitrate, sample_rate, padded)
        for bitrate in BITRATES[key]
        if bitrate
    }


def _bitrate_for_length(version, layer, sample_rate, padded, length):
    """若 ``length`` 恰好等于某合法比特率的帧长，返回该比特率，否则 None。"""
    key = _bitrate_table_key(version, layer)
    if key is None:
        return None
    for bitrate in BITRATES[key]:
        if bitrate and frame_length(
            version, layer, bitrate, sample_rate, padded
        ) == length:
            return bitrate
    return None


def _apply_actual_size(frame, actual_size):
    """记录实际长度；若它与声明帧长不符但对应合法比特率，修正比特率。"""
    frame.actual_size = actual_size
    frame.size_mismatch = actual_size != frame.size
    if not frame.size_mismatch:
        return
    for padded in (frame.padded, 0, 1):
        bitrate = _bitrate_for_length(
            frame.version, frame.layer, frame.sample_rate, padded, actual_size
        )
        if bitrate is not None:
            frame.bitrate = bitrate
            return


def _recover_within_frame(data, frame, end):
    """在帧头声明的长度范围内寻找真正的下一帧头。

    只在“合法帧长”对应的偏移上试同步，避免把帧载荷里偶然出现的
    0xFF 同步字误当成新帧。找不到返回 None。
    """
    lengths = set()
    for padded in (0, 1):
        lengths |= _valid_frame_lengths(
            frame.version, frame.layer, frame.sample_rate, padded
        )
    for length in sorted(lengths):
        if length < frame.header_size:
            continue
        candidate = frame.offset + length
        if candidate >= end:
            continue
        if parse_header(data, candidate) is not None:
            return candidate
    return None


def scan(data):
    """扫描整个缓冲区并返回按出现顺序排列的 Frame 列表。

    正常路径直接按帧长前进。若按帧长跳到的位置不是合法帧头，会先看
    当前帧声明的范围内是否有更早的真实帧头（帧长字段与实际间隔不符），
    否则从声明位置继续逐字节寻找同步字；损坏字节不会导致后面整段丢失。
    """
    frames = []
    pos = skip_id3(data)
    end = _id3v1_start(data)
    while pos + 4 <= end:
        frame = parse_header(data, pos)
        if frame is None:
            pos += 1
            continue
        frames.append(frame)
        declared_next = pos + frame.size

        # 1) 帧长正好落在下一帧头上：常规路径。
        if declared_next <= end and parse_header(data, declared_next) is not None:
            frame.actual_size = frame.size
            pos = declared_next
            continue

        # 2) 正好到可扫描区域末尾：正常收尾。
        if declared_next == end:
            frame.actual_size = frame.size
            pos = end
            continue

        # 3) 帧长没有落在帧头上：在声明范围内按合法帧长找更早的真实帧头。
        #    不搜索到 ID3v1 尾标里，避免把尾标里的假同步字当成帧。
        nearer = _recover_within_frame(data, frame, min(declared_next, end))
        if nearer is not None:
            _apply_actual_size(frame, nearer - pos)
            pos = nearer
            continue

        # 4) 声明帧尾越过可扫描区域末尾：作为最后一帧接受。
        #    若剩余字节恰好等于某个合法帧长，说明帧是完整的、只是帧头
        #    声明的比特率与实际长度不符，否则视为被截断。
        if declared_next >= end:
            remaining = end - pos
            _apply_actual_size(frame, remaining)
            frame.truncated = remaining < 4 or remaining not in _valid_frame_lengths(
                frame.version, frame.layer, frame.sample_rate, frame.padded
            )
            pos = end
            continue

        # 5) 帧尾之外才有下一个帧头：本帧正常，跳到帧尾继续找。
        frame.actual_size = frame.size
        pos = declared_next
    return frames


def total_duration_ms(frames):
    """帧列表的总时长（毫秒）。"""
    return sum(frame.duration_ms for frame in frames)


def build_report(frames):
    """把帧列表汇总成 CLI / JSON 报告。"""
    distribution = {}
    declared = {}
    for frame in frames:
        distribution[frame.bitrate] = distribution.get(frame.bitrate, 0) + 1
        declared[frame.declared_bitrate] = declared.get(frame.declared_bitrate, 0) + 1
    return {
        "frames": len(frames),
        "duration_ms": round(total_duration_ms(frames), 3),
        "bitrate_distribution": {
            str(kbps): distribution[kbps] for kbps in sorted(distribution)
        },
        "bitrates": sorted(distribution),
        "declared_bitrate_distribution": {
            str(kbps): declared[kbps] for kbps in sorted(declared)
        },
        "frame_offsets": [frame.offset for frame in frames],
        "first_frame_offset": frames[0].offset if frames else None,
        "first": repr(frames[0]) if frames else None,
        "resynced_frames": sum(1 for frame in frames if frame.size_mismatch),
        "truncated_frames": sum(1 for frame in frames if frame.truncated),
        "crc_frames": sum(1 for frame in frames if frame.crc),
    }


def main(argv=None):
    import argparse
    import json

    ap = argparse.ArgumentParser(prog="mp3frame")
    ap.add_argument("mp3")
    ap.add_argument("--out", default="report.json")
    args = ap.parse_args(argv)
    with open(args.mp3, "rb") as f:
        data = f.read()
    report = build_report(scan(data))
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(json.dumps(report, ensure_ascii=False))
    return 0
