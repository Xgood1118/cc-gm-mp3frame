# MP3 帧头解析

骨架能认出帧同步字、能读比特率与采样率、能按帧长跳跃扫描，但版本与层没处理对。

## 已知缺口

- `parse_header` 只判断版本两位，层字段读出来后没有参与比特率表选择，
  Layer I 与 Layer II 的比特率表完全不同。
- `skip_id3` 直接返回 0，文件开头的 ID3v2 头会被当成垃圾字节逐字节扫，
  这段时间里可能撞上假的同步字。
- 帧长公式固定按 Layer III，其他层算出来偏移错位，扫描会跑飞。
- 帧时长固定按 1152 采样，Layer I 是 384、Layer II 是 1152、Layer III 是 1152，
  且 MPEG-2 的 Layer III 是 576。
- 比特率表里索引 0 表示自由格式，索引 15 表示无效，当前把 0 当无效但 15 会读到 0。
- 没有 CRC 校验位判断，带 CRC 的帧要在帧头后多跳 2 字节。

## 运行

```
python gen_samples.py
python -m mp3frame.cli data/sample.mp3 --out report.json
python -m pytest tests -q
```

`sample.mp3` 是 25 个假帧：开头一个 ID3v2 头，前 15 帧 128kbps，后 10 帧 192kbps，
采样率都是 44.1k，全部按 Layer III 帧长构造。
