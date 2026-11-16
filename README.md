# MP3 帧头解析

用纯 Python 标准库实现的 MPEG-1 / MPEG-2 / MPEG-2.5 Layer I-III 帧扫描器：
跳过标签、解析帧头、按层计算帧长与时长，并在遇到损坏字节时重新同步。

## 能力

- **标签**：ID3v2 头按同步安全整数（每字节 7 位）读取整段长度后整体跳过，
  支持连续多个标签与 footer 标志；ID3v1 尾标（末尾 128 字节的 `TAG`）不计入扫描。
- **帧头**：版本、层、比特率索引、采样率索引全部参与计算，四类比特率表
  （MPEG-1 L1/L2/L3、MPEG-2/2.5 L1/L2/L3）分别查表。比特率索引 0（自由格式）、
  15（无效）、采样率索引 3（保留）、版本 1 / 层 0（保留）都不算正常帧。
- **帧长**：Layer I 按 4 字节槽计；Layer II 用 144 系数；Layer III 在 MPEG-1
  用 144、MPEG-2/2.5 用 72。扫描按帧长前进，偏移不会累积跑飞。
- **时长**：Layer I 384、Layer II 1152、Layer III MPEG-1 1152 / MPEG-2 576 采样，
  按实际采样率换算。
- **CRC**：保护位为 0 时帧头后有 16 位 CRC，`header_size` 为 6、
  `payload_offset` 相应后移；2 字节已包含在帧长里。
- **重同步**：帧长落点不是合法帧头时，先在声明范围内按合法帧长找真实下一帧；
  找不到再逐字节前进。损坏字节不会导致后续整段放弃。
- **统计**：帧数、总时长、比特率分布、每帧偏移、重同步与 CRC 帧数。

## 运行

```
python -m mp3frame.cli data/sample.mp3 --out report.json
python -m pytest tests -q
```

## sample.mp3 扫描结果

`python -m mp3frame.cli data/sample.mp3 --out report.json` 的输出：

| 项目 | 值 |
| --- | --- |
| 帧数 | **25** |
| 总时长 | **653.061 ms** |
| 128 kbps 帧数 | **15** |
| 192 kbps 帧数 | **10** |
| 第一帧偏移 | **30**（即 ID3v2 头之后） |

帧偏移：`30, 447, 864, …`（前 15 帧间隔 417 字节），随后
`6285, 6911, …`（后 10 帧间隔 626 字节）。CRC 帧 0 个，重同步帧 10 个。

> 注意：`sample.mp3` 后 10 帧的帧头比特率索引写的是 13，按 MPEG-1 Layer III
> 表应为 256 kbps（帧长 835），但文件里的实际帧间隔是 626 字节，对应 192 kbps。
> 也就是说帧头声明与帧长不一致。扫描器按实际间隔校正比特率，因此统计为
> 192 kbps × 10 帧；原始声明值保留在报告的 `declared_bitrate_distribution`
> （`128: 15, 256: 10`）中便于核查。样本本身不修改。

## 接口

```python
from mp3frame.core import Frame, parse_header, skip_id3, scan, build_report

data = open("data/sample.mp3", "rb").read()
first = skip_id3(data)          # -> 30
frame = parse_header(data, 30)  # -> Frame 或 None
frames = scan(data)             # -> [Frame, ...]
report = build_report(frames)   # -> dict（即 CLI 的 JSON 结构）
```

`Frame` 的字段：`offset`、`version`（3/2/0 = MPEG-1/2/2.5）、`layer`
（3/2/1 = Layer I/II/III）、`bitrate`、`sample_rate`、`padded`、`size`，
以及 `crc`、`header_size`、`payload_offset`、`samples_per_frame`、`duration_ms`、
`actual_size`、`size_mismatch`、`truncated`、`declared_bitrate`。
