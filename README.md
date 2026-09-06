# Apple Music 桌面悬浮歌词

监听本机 Apple Music 播放状态，在桌面悬浮显示同步歌词（KTV 扫光效果）。

> ⚠️ 本项目为个人业余娱乐开源工具，仅供学习交流，**与 Apple 公司无任何关联，并非官方软件**。

## 功能

- 监听本地 Apple Music / iTunes 播放状态（Windows 系统媒体会话 SMTC）
- 桌面悬浮歌词：纵向列表面板 / 单行模式，KTV 式逐句扫光高亮
- 长句自动换行、可拖动、可调透明度/字号/行数、任务栏吸附模式
- 歌词获取：Apple Music 本地官方缓存（有则优先）＋ LRCLib ＋ 网易云 ＋ QQ 音乐，五路并行、先到先显示
- 「换下一份歌词」手动切换候选来源
- 自定义图标：程序目录放 `icon.ico` 即生效（无需重新编译）

## 运行

```bash
pip install -r requirements.txt
python main.py
```

歌词内容版权归原作者/平台所有，本项目仅作个人学习使用。

## 说明

- 程序图标为原创设计，无第三方商标内容。
- 依赖 PyQt5（GPL v3）、requests（Apache-2.0）等开源组件。
