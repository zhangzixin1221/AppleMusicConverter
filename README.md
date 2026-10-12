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
- **智能大小控制** —— 超出上限时按「听感损失从小到大」逐级降级，**每一步都真实编码后实测大小**
- **预算包含封面和歌词** —— 内嵌素材也算进上限，不会「音频没超、加完封面就超了」
- **原子写入** —— 先写临时文件再整体替换，中途失败不会留下半个损坏的文件
- **自动内嵌歌词** —— 从内嵌标签或外挂 `.lrc` / `.srt` 提取，自动清理时间轴转成纯文本
- **自动内嵌封面** —— 内嵌封面图优先，找不到则用同目录的 `cover` / `folder` / `AlbumArt*` 等；WebP/BMP/GIF/TIFF 会自动转成 Apple 支持的 JPEG
- **抗乱码** —— 针对中文标签和歌词的编码问题做了多层防护，同时不会误改「Motörhead」这类正常外文名
- **图形界面 + 命令行** —— 双击即用，也可以脚本批量调用，两边都能中途取消
- **界面自适应** —— 窗口按内容量出合适大小（高 DPI 屏幕上也不会挤掉组件），缩放时列表和日志同步伸缩，按钮等控件保持原有比例

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

双击 `启动转换器.bat`（源码）或 `AMC.exe`（发布版）。添加文件 → 设置上限 → 开始转换。转换过程中可以点「取消」中止，完成后可以点「打开输出文件夹」。

界面分四块：**待转换文件**（标题栏显示当前个数，路径过长可横向滚动）、**转换设置**、**进度与日志**、**底部状态栏**。

- 窗口按内容自动量尺寸：打开时所有控件都完整可见，同时也保证不会超出桌面；屏幕放大到 150%/200% 时按钮和字号会跟着放大，窗口尺寸也同步调整。
- 缩放窗口时，中间的**文件列表**和**日志区**按 3:2 同步伸缩，按钮、输入框、标签保持原有大小，不会被拉变形。
- 设置项下方的说明文字会随窗口宽度自动换行，不会被截断。
- 快捷键：`Ctrl+O` 添加文件，`Ctrl+Enter` 开始转换，`Esc` 取消，列表中 `Delete` 移除选中项、`Ctrl+A` 全选。

### 命令行

```bat
python -m amc --gui                                  :: 打开界面
python -m amc "D:\Downloads\某首歌.flac"              :: 转换单个文件
python -m amc D:\Music --recursive -o D:\Out         :: 整个文件夹（含子文件夹）
python -m amc D:\Music --target-mb 50                :: 只扫描第一层
python -m amc D:\Music --target-mb 0                 :: 不限制大小
python -m amc --check                                :: 检查运行环境
```

| 参数 | 说明 |
|---|---|
| `--out`, `-o` | 输出文件夹，默认与源文件同目录 |
| `--target-mb` | 单曲大小上限，默认 `50`；填 `0` 关闭限制 |
| `--keep-name` | 保留原文件名（默认按「歌手 - 标题」重命名） |
| `--force-alac` | 强制输出 ALAC 无损，即使源已是 MP3/AAC |
| `--recursive`, `-r` | 递归扫描子文件夹（默认只处理文件夹第一层） |
| `--lyrics-encoding` | 强制指定外挂歌词编码，如 `gbk` / `big5` / `utf-8` |
| `--no-overwrite` | 输出文件已存在时跳过，而不是原地替换 |
| `--no-tag-repair` | 关闭标签乱码自动修复（修复猜错时使用） |
| `--quiet`, `-q` | 只输出每个文件的结果行 |
| `--version`, `-V` | 显示版本 |
| `--check` | 只检查环境 |

转换过程中按 `Ctrl+C` 会**真的停掉正在跑的 ffmpeg**（再按一次强制退出），退出码 `130`。全部成功的退出码是 `0`，有失败是 `1`，没找到输入文件是 `2`。

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

几个已经处理好的细节：

- **DSD 不会被降成 CD 音质**。DSD64 按惯例解码成 88.2kHz/24bit 再进 ALAC（DSD128 → 176.4kHz，DSD256 → 352.8kHz），而不是先塌到 44.1kHz。
- **32/64bit 浮点 PCM 会保留到 32bit**，不会因为位深读数超范围就一路掉到 16bit。
- **AAC 码率跟着预算和源文件走**。给一个 96kbps 的 Opus 转 512kbps 的 AAC 只会让文件变大，不会变好听，所以有损源的上限是它自己码率的 1.5 倍。
- **采样率换算尽量走整数倍**（88.2k → 44.1k，而不是硬塞到 48k）。

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

三条规则保证上限是真的：

1. **封面和歌词先占预算。** 内嵌封面最大可有 6MB，所以最终大小要按 `音频 + 封面 + 歌词` 算。规划编码前先把素材大小从预算里扣掉，否则会出现「单曲上限 50MB，成品 53MB」。
2. **每一步都真实编码后测量。** 估算只用来决定「这个方案值不值得试」，不值得信任到能决定成败——超出预算就删掉换下一级。实测才是最终依据。
3. **只有绝对不可能的方案才会被跳过。** 估算公式是经验值（ALAC 约为原始 PCM 的 0.62），对特别好压的素材会低估、对噪声类素材会高估。所以只有「连最好的压缩率都放不下」的方案才会直接排除，其余一律实测。

推导示例（247 秒的曲子，50MB 预算）：

```
50MB × 1024 × 1024 × 8 ÷ 247 秒 ≈ 1698 kbps 可用
ALAC 16bit/48kHz 实测约 950 kbps  →  放得下，用 ALAC，无损
```

把 `--target-mb` 设为 `0` 可完全关闭限制。码率下限为 64kbps：极端情况（比如 2 小时的音频压到 5MB）宁可放弃达标也不会把码率压到听不下去——这时会在日志里明确写出「仍超出目标」。

---

## 覆盖与安全

- **默认覆盖同名输出**（`--no-overwrite` 可改成「已存在就跳过并报告」）。注意这里说的是*输出文件*：重新转换同一首歌会原地替换，而不是堆积出 `歌名 (2).m4a`、`歌名 (3).m4a`。
- **绝不为了重新编码而毁掉源文件**。如果输出路径和源文件同名、而这个方案需要重新编码（降采样或转 AAC），程序会改存成 `歌名 (2).m4a` 并在日志里说明。只重写标签的直通方案不受此限制（它不会损失音质）。
- **先写临时文件，再整体替换**。编码和写标签都在目标目录旁的 `.amc-tmp-…` 上完成，最后一步用 `os.replace` 一步换上去。所以中途崩溃、断电、被取消都不会留下半个文件，也不会破坏上一版输出。
- 临时文件名不会被当作输入文件，正常结束时也不会残留。

---

## 歌词与封面

**歌词来源**（按优先级）：内嵌标签（`LYRICS` / `UNSYNCEDLYRICS` / `USLT` / `©lyr`）→ 同目录同名 `.lrc` → 同目录同名 `.srt`。

带时间轴的歌词会自动转成纯文本：去掉 `[mm:ss.xx]` 时间戳、`[ti:]/[ar:]/[al:]` 头部、SRT 的序号行和 `-->` 时间行，以及只有时间戳的空行。同一句歌词在不同时间点重复出现（副歌）会保留，只有同一时间戳下的重复行才会被合并。

> **为什么是纯文本而不是滚动歌词？**
> Apple Music 对**本地导入**的文件只把 `©lyr` 内容当纯文本渲染。把 Apple 自家曲目用的 TTML 格式写进本地文件，界面会直接把 XML 源码显示出来。纯文本是本地文件能达到的最好效果。

外挂歌词编码会自动嗅探：UTF-8（含 BOM）、UTF-16（含无 BOM 的「记事本 Unicode」）、UTF-32、GB18030、Big5、Shift-JIS。识别错了可以用 `--lyrics-encoding gbk` 强制指定。

**封面来源**：内嵌封面图 → 同目录 `cover` / `folder` / `front` / `album` / `albumart` / `thumb` / `artwork` 等命名 → 同名图片 → `AlbumArt_*`。

- 按**文件内容**（magic number）判断图片类型，不看扩展名。
- WebP / BMP / GIF / TIFF 会自动转成 JPEG，因为 Apple Music 只认 JPEG 和 PNG。
- 超过 6MB 或大于 2000px 会自动压缩。
- 认不出来的「图片」（比如一个改了名的文本文件）会被忽略，而不是塞进文件里。

歌词和封面缺失时会在日志里明确告警（`⚠ 未找到歌词; 未找到封面`），不会静默跳过。

---

## 中文乱码的处理

这是 Windows 上最容易出问题的地方，程序做了多层防护：

1. **全程不经 shell** —— 所有外部命令用 argv 列表传给 `subprocess`，不拼接命令行字符串，中文路径和标签不会经过代码页转换
2. **输出显式按 UTF-8 解码** —— 不依赖系统区域设置
3. **外挂歌词编码嗅探** —— 先看 BOM（UTF-32 的 BOM 以 UTF-16 的 BOM 开头，所以必须先判断更长的那个），再按 UTF-8 → GB18030 → Big5 → Shift-JIS 依次试；识别错了可以用 `--lyrics-encoding gbk` 强制指定
4. **标签乱码自动修复** —— 老式 ID3v2.3 标签常把 GBK 字节按 Latin-1 存，程序检测到这种情况时重新解码，取中文最多的结果

第 4 条是**保守**的：只有当一个字符串看起来「就是」乱码（Latin-1 补充区字符占了很大比例、且重新解码后能得到至少两个字）才会动手。因为「歌手 - 稻香」的乱码和「Motörhead」的正常名字长得很像，早期版本会把后者改成「Mot鰎head」，还会连累输出文件名。如果这个判断在你的标签上猜错了，用 `--no-tag-repair` 完全关掉即可。

UTF-8 优先于单字节编码：UTF-8 自带校验，而 GBK 字节几乎不可能碰巧是合法 UTF-8，反过来却经常成立。

MP3 输出统一写 ID3v2.3（iTunes 兼容性最好，中文自动用 UTF-16 存储），MP4 输出由 mutagen 直接写 UTF-8 原子。

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

用 `python -m amc --check` 可以一次性看清 Python、mutagen、ffmpeg、ffprobe 和临时目录是否都正常。

---

## 开发

```bat
pip install -e ".[dev]"
pytest                      :: 全部测试
pytest -m "not integration" :: 只跑纯逻辑测试（不需要 ffmpeg）
```

测试分三层：

- **纯逻辑测试**（`tests/test_core.py`，103 项）覆盖歌词清理、编码嗅探、乱码修复、文件命名、编码方案规划、封面类型识别。不碰外部程序。
- **界面布局测试**（`tests/test_gui_layout.py`，20 项）真的把窗口建出来，然后验证用户提的那几个要求：开窗时没有任何控件被裁掉、每个标签和按钮都拿到了它需要的宽度（文字不会被压掉）、放大缩小窗口时列表和日志同步伸缩、按钮尺寸始终等于它自己的自然尺寸。没有图形环境时自动跳过。
- **端到端测试**（`tests/test_integration.py`，24 项）用真实 ffmpeg 生成素材，验证「跑完的结果究竟对不对」：封面和歌词是否真的写进文件、上限是否真的守住（含封面）、重复运行是否原地替换、重新编码是否会毁掉源文件、编码失败是否破坏旧输出。**没有 ffmpeg 时这一层会自动跳过**；CI 里用 `AMC_REQUIRE_FFMPEG=1` 把它变成硬性要求，避免「一个都没跑也算通过」。

### 项目结构

```
AppleMusicConverter/
├── .github/workflows/ci.yml   # CI：Windows/Linux × Python 3.9–3.13
├── src/amc/
│   ├── __init__.py
│   ├── __main__.py      # 入口 + 环境检查（--check）
│   ├── cli.py           # 命令行界面
│   ├── gui.py           # tkinter 图形界面
│   └── core.py          # 核心引擎
├── tests/
│   ├── conftest.py      # ffmpeg 探测 + 素材工厂
│   ├── test_core.py     # 纯逻辑
│   ├── test_gui_layout.py  # 界面布局与自适应
│   └── test_integration.py
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
- **DSD 需要 ffmpeg 支持对应的解码器**；如果这个 ffmpeg 构建不支持，会自动退回 AAC 并在日志里体现。
- **批量转换是逐个串行的**。ffmpeg 本身已经多线程，串行不会浪费 CPU；想并行可以起多个进程，各处理不同的文件夹。

---

## License

[MIT](LICENSE)
