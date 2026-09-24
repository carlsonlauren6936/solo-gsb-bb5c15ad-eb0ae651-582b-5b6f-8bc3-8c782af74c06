# 字幕校正、位置规划与阅读负荷工具

基于 **Python + FastAPI** 的轻量 Web 工具，包含三个功能页面：

1. **字幕时间漂移校正**：粘贴 SRT 字幕，填入多组人工确认的「原时间 / 正确时间」
   锚点，按分段线性映射校正字幕时间，在网页预览并下载校正后的 SRT。
2. **字幕位置规划**（<http://localhost:8000/placement>）：粘贴 SRT，填写画面上方 /
   下方被遮挡（台标、弹幕条、滚动新闻等）的时间段与相邻字幕上下换位代价，自动为
   每条字幕选择总遮挡与换位代价最小的位置，并导出带 `line` 位置设置的 WebVTT。
3. **阅读负荷热区**（<http://localhost:8000/reading-load>）：粘贴 SRT，按
   「字数 ÷ 显示秒数」计算每条字幕的阅读速率（字/秒），重叠字幕速率相加，扫描出
   合计速率超阈且持续足够久的热区并在页面高亮、下载同一结果的 JSON；
   对应接口 `POST /api/reading-load`（<http://localhost:8000/api/reading-load>）。

## 功能与校正规则（时间漂移）

- 粘贴标准 SRT，字幕**序号与文本原样保留**，仅校正开始/结束时间。
- **无损导出**：直接在原始 SRT 文本上逐条替换时间轴行的起止时间码，
  序号、字幕正文、箭头前后空格、`X1:`/`X2:`/`Y1:`/`Y2:`/`Position:` 等行尾
  定位参数、空行、原有换行（CRLF/LF 原样保留，不统一改写）以及文件开头的
  UTF-8 BOM 均保持不变；原文件无 BOM 时结果也不添加 BOM。
- 至少 **2 组**锚点（建议在片头、片中、片尾各取一组，越多越精确）。
- **相邻锚点之间做分段线性插值**；早于第一个锚点或晚于最后一个锚点的字幕，
  沿用最近一个区段（首段 / 末段）的斜率外推。
- 锚点**不自动排序**：原时间列必须严格递增，正确时间列也必须严格递增；
  任一侧出现相等或倒退时，拒绝生成并返回具体原因（指出是第几组锚点）。
- 时间格式 `HH:MM:SS,mmm`（毫秒分隔符逗号、句点均可，毫秒允许 1–3 位）。
- 校正后早于 `00:00:00,000` 的时间会截断为 0，并给出警告。
- 结果可在页面预览、逐条对比，并一键下载 `corrected.srt`：页面预览与下载
  使用接口返回的同一份 `corrected_srt`，下载文件开头**最多一个 BOM**
  （仅当原 SRT 带 BOM 时保留），为 UTF-8 编码。
- SRT 无法解析时接口仍返回 `422` 错误，并在 `detail.message` 中说明原因。

## 功能与规划规则（字幕位置）

- 输入 SRT（复用同一套字幕解析器）、画面**上方 / 下方各自的遮挡时间段**
  （`start` / `end`，`HH:MM:SS,mmm`，结束必须严格晚于开始）以及
  **相邻字幕上下换位代价** `switch_cost_ms`（非负整数，单位毫秒）。
- **同侧重叠遮挡时段先求并集**（首尾相接也合并），再按并集时段与每条字幕
  时间区间的交集累计该侧遮挡时长（毫秒）。
- 以 **总遮挡时长 + 相邻字幕上下换位代价最小** 为目标做动态规划；
  总代价完全相同时**按字幕顺序逐条优先选择下方**（避免字幕无必要地出现在上方）。
- 换位代价越大，规划越倾向让整段字幕保持同侧、减少上下跳动；填 `0` 表示
  换位零成本，每条字幕都选遮挡更短的一侧（同分时下方）。
- 导出 **WebVTT**（`WEBVTT` 文件头、时间码用句点 `.`），并在每条 cue 的
  设置中写入行位置：上方 `line:10%`、下方 `line:90%`，可直接用于支持 WebVTT
  `line` 设置的播放器。
- 返回逐条的上 / 下选择、两侧遮挡时长、所选位置的遮挡时长，以及
  **所选位置仍被遮挡的字幕列表** `still_occluded_cues`（两侧同时遮挡时出现），
  页面中高亮提示并可一键下载 `placement.vtt`。
- 遮挡时间段起止无效（格式错误或结束不晚于开始）、换位代价不是非负整数、
  SRT 无法解析时，接口均返回 `422`，并在 `detail.message` 中给出中文原因。

## 功能与扫描规则（阅读负荷热区）

- 输入 SRT（复用同一套字幕解析器）、**阅读速率阈值** `threshold`（字/秒，
  **正数**，可填小数）与**热区最短持续时间** `min_duration_ms`（毫秒，
  **非负整数**，填 `0` 表示不设下限）。
- 每条字幕的速率 = **去掉全部 Unicode 空白后的字符数**（普通空格、制表符、
  换行、全角空格 `　`、不间断空格 ` ` 等均不计字）÷ **显示秒数**
  （结束 − 开始）。
- **重叠字幕的速率相加**：同一时刻有多条字幕显示时，合计速率为各条速率之和；
  每条字幕在自身显示区间内速率恒定。
- 按全部字幕的**开始 / 结束事件**把时间轴切成半开区间 `[t, t+1)` 逐段扫描，
  **不做整秒采样**，毫秒及更细粒度的速率变化都能识别。
- 找出合计速率**严格超过**阈值（等于不算）的连续区间：相接的超阈区间
  （前一段结束时间 = 下一段开始时间）**自动合并**为一个热区，被任意长度的
  非超阈区间隔开则保持独立；仅当连续时长达到 `min_duration_ms` 才保留。
- 逐段（逐热区）返回**起止时间**（`HH:MM:SS,mmm` 及毫秒整数）、
  **峰值速率** `peak_rate`（字/秒，区间内合计速率的最大值）以及
  **参与字幕的输入顺序号** `cue_orders`（1 起步，按 SRT 解析顺序；即按
  字幕块在粘贴文本中的先后，而非 SRT 文件内写的序号），同时返回逐条字幕的
  字数、时长与速率供核对。
- 页面中用橙色高亮参与热区的字幕、按真实时间比例绘制热区时间轴，点击热区可
  定位参与字幕；**下载的 `reading-load.json` 与页面展示、接口返回为同一份
  JSON 结果**。
- 阈值不是正数（含 `0`、负数、布尔值、字符串、`NaN` / `Infinity`）、
  最短持续不是非负整数或 SRT 无法解析时，接口均返回 `422`。

## 一条命令启动（Docker Compose）

```bash
docker compose up -d --build
```

启动后访问：

- 时间漂移校正界面（默认首页）：<http://localhost:8000/>
- 字幕位置规划界面：<http://localhost:8000/placement>
- 阅读负荷热区界面：<http://localhost:8000/reading-load>
- 阅读负荷热区接口：`POST` <http://localhost:8000/api/reading-load>
- 健康检查：<http://localhost:8000/health>
- 接口文档（FastAPI 自带）：<http://localhost:8000/docs>

停止：`docker compose down`

### 配置

| 配置项 | 说明 | 默认值 |
| --- | --- | --- |
| 端口 | 通过环境变量 `HOST_PORT` 修改宿主机映射端口；容器内固定监听 8000 | `8000` |
| 重启策略 | `restart: unless-stopped`（见 `docker-compose.yml`） | — |

例如改用宿主机 9000 端口：

```bash
HOST_PORT=9000 docker compose up -d --build
# 访问 http://localhost:9000/ 、http://localhost:9000/placement
# 与 http://localhost:9000/reading-load（接口 /api/reading-load）
```

## 不用 Docker，直接运行（Python 3.10+）

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

同样访问 <http://localhost:8000/>、<http://localhost:8000/placement>
与 <http://localhost:8000/reading-load>（接口 `POST /api/reading-load`）。

## HTTP 接口

### `POST /api/correct`

```json
{
  "srt": "1\n00:00:05,000 --> 00:00:08,000\n你好\n",
  "anchors": [
    {"original": "00:00:10,000", "corrected": "00:00:08,000"},
    {"original": "00:02:40,000", "corrected": "00:02:35,000"}
  ]
}
```

成功返回 `corrected_srt`（与输入同构的完整 SRT 文本：只替换各时间轴行的起止
时间码，序号、正文、定位参数、空行、CRLF/LF 换行及 BOM 原样保留）、`cues`
（逐条前后时间对照）与 `warnings`（如时间截断等提示）。SRT 解析失败或锚点不
满足严格递增时返回 `422`，并在 `detail.message` 中给出中文原因。

### `POST /api/placement`

```json
{
  "srt": "1\n00:00:05,000 --> 00:00:09,000\n第一句\n\n2\n00:00:14,000 --> 00:00:18,000\n第二句\n",
  "top_blocks": [
    {"start": "00:00:12,000", "end": "00:00:20,000"}
  ],
  "bottom_blocks": [
    {"start": "00:00:24,000", "end": "00:00:32,000"}
  ],
  "switch_cost_ms": 2000
}
```

`top_blocks` / `bottom_blocks` 均可省略或传空数组（表示该侧全程无遮挡）。
成功返回：

- `vtt`：带 `line:10%` / `line:90%` 行位置设置的完整 WebVTT 文本；
- `cues`：逐条字幕的 `position`（`top` / `bottom`）、`top_occluded_ms`、
  `bottom_occluded_ms`、所选位置遮挡 `occluded_ms` 及 `still_occluded` 标记；
- `still_occluded_cues`：所选位置仍被遮挡的字幕（两侧同时遮挡时才可能出现）；
- `merged_top_blocks` / `merged_bottom_blocks`：两侧遮挡求并集后的时间段
  （`HH:MM:SS,mmm`），便于页面核对；
- `total_occluded_ms`：所选位置的遮挡总毫秒数；`switch_count`：上下换位次数。

时间段起止无效或 `switch_cost_ms` 不是非负整数时返回 `422`
（后者为请求体校验错误，前者与 SRT 解析错误在 `detail.message` 中给出中文原因）。

### `POST /api/reading-load`

```json
{
  "srt": "1\n00:00:00,000 --> 00:00:02,000\n一二三四五六\n\n2\n00:00:01,000 --> 00:00:04,000\nABCDEFGHI\n",
  "threshold": 5,
  "min_duration_ms": 0
}
```

成功返回：

- `threshold` / `min_duration_ms`：回显本次使用的阈值与最短持续毫秒；
- `zones`：热区列表，每项含 `start` / `end`（`HH:MM:SS,mmm`）、
  `start_ms` / `end_ms` / `duration_ms`（毫秒整数）、`peak_rate`
  （峰值合计速率，字/秒）与 `cue_orders`（参与字幕的输入顺序号，1 起步）；
- `cues`：逐条字幕的 `order`（输入顺序号）、`index`（SRT 中书写的序号）、
  起止时间、`chars`（去 Unicode 空白后的字数）、`duration_ms`、`rate`
  （字/秒）与文本。

上例中两条字幕速率均为 3 字/秒，重叠段 `00:00:01,000 → 00:00:02,000`
合计 6 字/秒，严格超过阈值 5，返回 1 个热区：

```json
{
  "threshold": 5.0,
  "min_duration_ms": 0,
  "zones": [
    {
      "start": "00:00:01,000",
      "end": "00:00:02,000",
      "start_ms": 1000,
      "end_ms": 2000,
      "duration_ms": 1000,
      "peak_rate": 6.0,
      "cue_orders": [1, 2]
    }
  ],
  "cues": [
    {"order": 1, "index": "1", "start": "00:00:00,000", "end": "00:00:02,000",
     "chars": 6, "duration_ms": 2000, "rate": 3.0, "text": "一二三四五六"},
    {"order": 2, "index": "2", "start": "00:00:01,000", "end": "00:00:04,000",
     "chars": 9, "duration_ms": 3000, "rate": 3.0, "text": "ABCDEFGHI"}
  ]
}
```

`threshold` 必须为正数（整数或小数；布尔值、字符串、`NaN` 等拒绝），
`min_duration_ms` 必须为非负整数，SRT 解析失败时同样返回 `422`
（后两者为请求体校验错误，SRT 错误在 `detail.message` 中给出中文原因）。
页面下载的 `reading-load.json` 即该接口的完整响应体。

## 目录结构

```
.
├── app/
│   ├── main.py           # FastAPI 应用、/api/correct、/api/placement 与 /api/reading-load 接口
│   ├── mapping.py        # 锚点校验与分段线性映射
│   ├── placement.py      # 遮挡时段并集、遮挡时长统计、位置规划 DP、WebVTT 导出
│   ├── reading_load.py   # 字/秒速率计算、起止事件半开时间轴扫描、热区合并
│   ├── srt.py            # SRT 解析与无损重写（仅替换时间码，保留原格式）
│   ├── timecode.py       # 时间码解析与格式化（SRT / WebVTT）
│   └── static/
│       ├── index.html        # 时间漂移校正页面（原生 HTML/CSS/JS）
│       ├── placement.html    # 字幕位置规划页面（原生 HTML/CSS/JS）
│       └── reading_load.html # 阅读负荷热区页面（原生 HTML/CSS/JS）
├── tests/               # 核心算法与接口测试
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```
