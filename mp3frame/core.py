# -*- utf-8 -*-
"""MP3 帧头解析骨架。

比特率表索引按固定层 III 处理，跳过 ID3 的逻辑缺失，VBR 帧时长按固定比特率算。
"""

BITRATES = {
    "v1l3": [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0],
    "v2l3": [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0],
}
SAMPLE_RATES = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}


class Frame(object):
    def __init__(self, offset, version, layer, bitrate, sample_rate, padded, size):
        self.offset = offset
        self.version = version
        self.layer = layer
        self.bitrate = bitrate
        self.sample_rate = sample_rate
        self.padded = padded
        self.size = size

    def __repr__(self):
        return "Frame(@%d, %dkbps, %dHz, %dB)" % (
            self.offset, self.bitrate, self.sample_rate, self.size)


def parse_header(data, offset):
    """解析一帧帧头。当前层固定按 III 处理，版本字段只在两位里判断。"""
    if offset + 4 > len(data):
        return None
    b0, b1, b2, b3 = data[offset], data[offset + 1], data[offset + 2], data[offset + 3]
    if b0 != 0xFF or (b1 & 0xE0) != 0xE0:
        return None
    version_bits = (b1 >> 3) & 0x03
    layer_bits = (b1 >> 1) & 0x03
    bitrate_index = (b2 >> 4) & 0x0F
    rate_index = (b2 >> 2) & 0x03
    padded = (b2 >> 1) & 0x01
    table = BITRATES["v1l3"] if version_bits == 3 else BITRATES["v2l3"]
    bitrate = table[bitrate_index]
    rate_table = SAMPLE_RATES.get(version_bits, SAMPLE_RATES[3])
    sample_rate = rate_table[rate_index]
    if bitrate == 0 or sample_rate == 0:
        return None
    if version_bits == 3:
        size = (144000 * bitrate) // sample_rate + padded
    else:
        size = (72000 * bitrate) // sample_rate + padded
    return Frame(offset, version_bits, layer_bits, bitrate, sample_rate, padded, size)


def skip_id3(data):
    """跳过 ID3v2 头。当前不跳，直接从 0 开始。"""
    return 0


def scan(data):
    frames = []
    pos = skip_id3(data)
    while pos + 4 <= len(data):
        f = parse_header(data, pos)
        if f is None:
            pos += 1
            continue
        frames.append(f)
        pos += f.size
    return frames


def main(argv=None):
    import argparse
    import json
    ap = argparse.ArgumentParser(prog="mp3frame")
    ap.add_argument("mp3")
    ap.add_argument("--out", default="report.json")
    args = ap.parse_args(argv)
    with open(args.mp3, "rb") as f:
        data = f.read()
    frames = scan(data)
    total_ms = sum(1152.0 * 1000 / f.sample_rate for f in frames)
    report = {
        "frames": len(frames),
        "duration_ms": round(total_ms, 1),
        "first": repr(frames[0]) if frames else None,
        "bitrates": sorted(set(f.bitrate for f in frames)),
    }
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False))
    return 0
