# -*- coding: utf-8 -*-
"""生成一个带 ID3v2 头、25 帧假 MP3 的文件：前 15 帧 128kbps，后 10 帧 192kbps。"""
import os
import struct

HERE = os.path.dirname(os.path.abspath(__file__))


def frame(bitrate_idx, rate_idx=0, padded=0, payload=0):
    b1 = 0xFB
    b2 = (bitrate_idx << 4) | (rate_idx << 2) | (padded << 1)
    b3 = 0x44
    if bitrate_idx == 9:
        size = 144000 * 128 // 44100 + padded
    else:
        size = 144000 * 192 // 44100 + padded
    body = bytes([0xFF, b1, b2, b3]) + b"\x00" * (size - 4 - payload)
    return body


def id3():
    payload = b"TIT2" + struct.pack(">I", 10) + b"\x00\x00" + b"test title"
    size = len(payload)
    syncsafe = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F,
                      (size >> 7) & 0x7F, size & 0x7F])
    return b"ID3\x03\x00\x00" + syncsafe + payload


if __name__ == "__main__":
    os.makedirs(os.path.join(HERE, "data"), exist_ok=True)
    data = id3()
    for _ in range(15):
        data += frame(9)
    for _ in range(10):
        data += frame(13)
    path = os.path.join(HERE, "data", "sample.mp3")
    with open(path, "wb") as f:
        f.write(data)
    print("已生成", path, len(data), "字节")
