# AMC · Apple Music 格式转换器

把各种格式的音频转换成**可直接导入 Apple Music 的文件**，自动内嵌封面和歌词，并在文件过大时把单曲控制在约 50MB 以内。

A Windows tool that converts audio files into Apple Music importable files with embedded cover art and lyrics, keeping each track under a per-track size budget without re-encoding when it doesn't have to.

[![CI](https://github.com/zhangzixin1221/AppleMusicConverter/actions/workflows/ci.yml/badge.svg)](https://github.com/zhangzixin1221/AppleMusicConverter/actions/workflows/ci.yml)
![Platform](https://img.shields.io/badge/platform-Windows-blue)
![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## 特性

- **优先无损转码** —— FLAC/WAV/APE/DSD 等无损格式转成 ALAC，解码 PCM 与源文件比特级一致
- **能直通就直通** —— 已是 ALAC/AAC/MP3 的文件只重写标签，**完全不重新编码**，零质量损失
- **智能大小控制** —— 超出上限时按「听感损失从小到大」逐级降级，每步真实编码后实测大小
- **自动内嵌歌词** —— 从内嵌标签或外挂 `.lrc` 提取，自动清理时间轴转成纯文本
- **自动内嵌封面** —— 内嵌封面图优先，找不到则用同目录的 `cover.jpg` / `folder.jpg` / `AlbumArt*` 等
- **抗乱码** —— 针对中文标签和歌词的编码问题做了四层防护
- **图形界面 + 命令行** —— 双击即用，也可以脚本批量调用

---

## 快速开始

### 方式一：下载 exe（无需 Python）

到 [Releases](https://github.com/zhangzixin1221/AppleMusicConverter/releases) 下载：

| 文件 | 用途 |
|---|---|
| `AMC.exe` | 图形界面版，双击运行 |
| `AMC-CLI.exe` | 命令行版，适合批处理脚本 |

> **exe 不含 ffmpeg**（体积原因）。请确保 `ffmpeg.exe` / `ffprobe.exe` 在 PATH 中，**或者直接把它们放到 exe 同一目录下**，程序会自动识别。

### 方式二：从源码运行

```bat
git clone https://github.com/zhangzixin1221/AppleMusicConverter.git
cd AppleMusicConverter
pip install -r requirements.txt
启动转换器.bat
```

---

## 使用

### 图形界面

双击 `启动转换器.bat`（源码）或 `AMC.exe`（发布版）。添加文件 → 设置上限 → 开始转换。

### 命令行

```bat
python -m amc --gui                                  :: 打开界面
python -m amc "D:\Downloads\某首歌.flac"              :: 转换单个文件
python -m amc D:\Music --out D:\Out --target-mb 50   :: 整个文件夹
python -m amc D:\Music --recursive --force-alac      :: 递归 + 强制无损
python -m amc --check                                :: 检查运行环境
```

| 参数 | 说明 |
|---|---|
| `--out`, `-o` | 输出文件夹，默认与源文件同目录 |
| `--target-mb` | 单曲大小上限，默认 `50`；填 `0` 关闭限制 |
| `--keep-name` | 保留原文件名（默认按「歌手 - 标题」重命名） |
| `--force-alac` | 强制输出 ALAC 无损，即使源已是 MP3/AAC |
| `--recursive` | 递归扫描子文件夹 |
| `--lyrics-encoding` | 强制指定外挂歌词编码，如 `gbk` / `big5` / `utf-8` |

---

## 输出格式策略

程序**优先选择损失最小的方案**：

| 源格式 | 处理方式 | 质量损失 |
|---|---|---|
| ALAC / AAC / MP3（未超限） | **音频直通**，只重写标签 | **无**，不重新编码 |
| 容器与编码不符（如 AAC 藏在 `.mp3` 里） | 重新封装为 `.m4a`，音频流直通 | **无**（只换外壳） |
| FLAC / WAV / AIFF / APE / WavPack / DSD | 转 **ALAC 无损** | **无**，PCM 比特级一致 |
| OGG / Opus / WMA 等 Apple 不支持的格式 | 转 AAC（预算内最高码率，上限 512kbps） | 有损（原格式本身有损） |

> **为什么不做「统一全转 ALAC」？**
> 把一个 320kbps 的 MP3 转成 ALAC 只会让文件大三倍，音质一点都不会变好——已经丢掉的信息加不回来。直通复用才是对音质最有利的做法。

---

## 大小控制逻辑

只有**超出上限时**才会动手，降级顺序严格按「听感损失从小到大」排列：

```
ALAC 原始位深/采样率
    ↓ 放不下
ALAC 降到 16bit           ← 24/32bit 源才走这步，回放几乎无感
    ↓ 放不下
ALAC 降采样                ← 96k/192k → 48k，88.2k/176.4k → 44.1k
    ↓ 放不下
AAC（预算内最高码率，上限 512kbps）
```

每一步都会**真实编码后测量文件大小**，超出就丢弃重试——是实测保证，不是靠估算。

推导示例（247 秒的曲子，50MB 预算）：

```
50MB × 1024 × 1024 × 8 ÷ 247 秒 ≈ 1698 kbps 可用
ALAC 16bit/48kHz 实测约 950 kbps  →  放得下，用 ALAC，无损
```

把 `--target-mb` 设为 `0` 可完全关闭限制。码率下限为 64kbps：极端情况（比如 2 小时的音频压到 5MB）宁可放弃达标也不会把码率压到听不下去。

---

## 歌词与封面

**歌词来源**（按优先级）：内嵌标签（`LYRICS` / `UNSYNCEDLYRICS` / `USLT` / `©lyr`）→ 同目录同名 `.lrc`。

带时间轴的 LRC 会自动转成纯文本：去掉 `[mm:ss.xx]` 时间戳、`[ti:]/[ar:]/[al:]` 头部，以及只有时间戳的空行。

> **为什么是纯文本而不是滚动歌词？**
> Apple Music 对**本地导入**的文件只把 `©lyr` 内容当纯文本渲染。把 Apple 自家曲目用的 TTML 格式写进本地文件，界面会直接把 XML 源码显示出来。纯文本是本地文件能达到的最好效果。

**封面来源**：内嵌封面图 → 同目录 `cover` / `folder` / `front` / `album` / `AlbumArt*` 等命名的图片 → 同名图片。超过 6MB 会自动压到 2000px 以内。

歌词和封面缺失时会在日志里明确告警（`⚠ 未找到歌词; 未找到封面`），不会静默跳过。

---

## 中文乱码的处理

这是 Windows 上最容易出问题的地方，程序做了四层防护：

1. **全程不经 shell** —— 所有外部命令用 argv 列表传给 `subprocess`，不拼接命令行字符串，中文路径和标签不会经过代码页转换
2. **输出显式按 UTF-8 解码** —— 不依赖系统区域设置
3. **外挂歌词编码嗅探** —— 依次尝试 UTF-8（含 BOM）、UTF-16、GB18030、Big5、Shift-JIS；识别错了可以用 `--lyrics-encoding gbk` 强制指定
4. **标签乱码自动修复** —— 老式 ID3v2.3 标签常把 GBK 字节按 Latin-1 存，程序检测到 Latin-1 补充区字符时会重新解码，取中日韩字符最多的结果

MP3 输出统一写 ID3v2.3（iTunes 兼容性最好），MP4 输出由 mutagen 直接写 UTF-8 原子。

---

## 导入 Apple Music

1. 把输出文件拖进 Apple Music，或「文件 → 将文件添加到资料库」
2. **如果同一首歌之前导入过，先把它从资料库删掉再重新导入**，否则会出现重复曲目

---

## 从源码构建 exe

```bat
build_exe.bat
```

产物在 `dist\`：`AMC.exe`（图形界面）+ `AMC-CLI.exe`（命令行），都是单文件、免安装。

手动构建：

```bat
pip install pyinstaller
pyinstaller --onefile --windowed --name AMC --paths src --collect-all mutagen run_amc.py
```

> 入口必须是根目录的 `run_amc.py` 而不是 `src/amc/__main__.py`——后者使用相对导入，需要保持包上下文。

---

## 环境要求

| 组件 | 说明 |
|---|---|
| 操作系统 | Windows 10 / 11 |
| Python | 3.9+（开发与测试于 3.14） |
| ffmpeg / ffprobe | 需在 PATH 中，或放在 exe 同目录 |
| mutagen | `pip install -r requirements.txt` |

---

## 开发

```bat
pip install -e ".[dev]"
pytest
```

测试只覆盖纯逻辑（歌词清理、编码嗅探、乱码修复、文件命名、编码方案规划），不调用 ffmpeg。

### 项目结构

```
AppleMusicConverter/
├── src/amc/
│   ├── __init__.py
│   ├── __main__.py      # 入口 + 环境检查
│   ├── cli.py           # 命令行界面
│   ├── gui.py           # tkinter 图形界面
│   └── core.py          # 核心引擎
├── tests/
│   └── test_core.py
├── run_amc.py           # 打包用入口
├── build_exe.bat
├── 启动转换器.bat
└── pyproject.toml
```

---

## 已知限制

- **本地导入的文件没有逐行滚动歌词**。这是 Apple Music 客户端的行为，不是文件问题——纯文本歌词已经是本地文件能达到的最好效果。
- **图形界面暂不支持拖拽**（需要额外依赖 `tkinterdnd2`），请用「添加文件 / 添加文件夹」按钮。
- **不联网**。程序不会去网上抓取歌词或封面，只用文件里已有的和同目录的素材。

---

## License

[MIT](LICENSE)
