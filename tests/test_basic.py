# -*- coding: utf-8 -*-
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mp3frame.core import Frame, parse_header  # noqa: E402


def test_frame_repr():
    f = Frame(0, 3, 1, 128, 44100, 0, 417)
    assert "128" in repr(f)


def test_parse_silence_returns_none():
    assert parse_header(b"\x00\x00\x00\x00", 0) is None


def test_parse_short_data():
    assert parse_header(b"\xff\xfb", 0) is None
