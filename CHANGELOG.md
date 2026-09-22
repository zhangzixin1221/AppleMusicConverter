# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [1.0.0] - 2026-09-22

### 新增

- 音频格式转换：无损格式（FLAC / WAV / AIFF / APE / WavPack / DSD）转 ALAC 无损，输出 `.m4a`
- 音频直通：已是 ALAC / AAC / MP3 的文件只重写标签，不重新编码
- 容器修正：源文件扩展名与实际编码不符时（如 AAC 藏在 `.mp3` 里）自动重新封装为 `.m4a`
- 大小控制：超出单曲上限时按 ALAC 降位深 → ALAC 降采样 → AAC 的顺序逐级降级，每步实测文件大小
- 歌词内嵌：从内嵌标签或外挂 `.lrc` 提取，自动清理时间戳与 LRC 头部，以纯文本写入 `©lyr` / `USLT`
- 封面内嵌：内嵌封面优先，回退到同目录命名约定的图片；超大封面自动压缩
- 编码安全：全程不经 shell 调用外部程序；外挂歌词编码嗅探；老式 ID3v2.3 标签乱码自动修复
- 图形界面（tkinter）与命令行界面
- 单文件 exe 构建脚本（PyInstaller）
- 单元测试：35 项，覆盖歌词清理、编码嗅探、文件命名、编码方案规划

### 备注

- 本地导入 Apple Music 的文件不支持逐行滚动歌词，歌词以纯文本形式内嵌
- 发布版 exe 不含 ffmpeg，需要单独安装或放在 exe 同目录
