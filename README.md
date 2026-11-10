# 鲸鲸bot

一只住在 QQ 群里的鱼。会聊天、会怼人、会发表情包、会唱歌，心情会自己变。

**下载安装包 → [Releases](https://github.com/banzhuanxueying114-coder/jingjing-bot/releases/latest)**（安装包 3.23 GB，切成两段，页面里有拼接方法）

这个仓库是它的 **AstrBot 插件与打包工具**源码。框架本体、NapCat、音色权重不在仓库里 —— 安装包里都有。

> ⚠️ **请先读 [DISCLAIMER.md](DISCLAIMER.md)。** 这是一份个人自用的实现，公开只是为了让别人能搭一套自己的，不提供任何担保。用主号跑机器人有封号风险。

---

## 它是什么

几个插件叠在一个群聊机器人上，目标不是「助手」，是「像群里一个真人」：

| 插件 | 干什么 |
| --- | --- |
| `mood` | 心情 / 劲头 / 火气三个数，六档语气（脏 / 损 / 平 / 和 / 冷 / 嗨）。**每轮由模型自己在回复末尾报** `[心情-4][火气+6]`，不额外调模型。带记仇账本，按天淡出。 |
| `memorize` | 记得人和事。认人（谁是谁、跟你什么关系）、群级记忆（这个群是什么地方）、自动提炼。 |
| `voice` | 翻唱 + 开口。音乐平台 / B 站搜歌 → 分离人声 → RVC 换声 → 混音。说话走 Edge TTS 打底再换声。 |
| `stickers` | 表情包，按概率附带或关键词触发。 |
| `illust` | 搬图。关键词触发，从图库按 tag 找，配一句话。 |
| `crowd_guard` | 群越大说话越少（>400 人减半，每多 400 再减半）。 |
| `clause_split` | 长句按逗号 / 换行拆成多条发，像真人在群里打字。 |
| `quote_reply` | 只在「答的不是最新那条」时才引用。 |
| `liang_time` | 上班时段主动回复概率砍半，走包月网关时翻倍。 |
| `mute_guard` | 被禁言期间不回复（不花 token）。 |
| `repeat_follow` | 跟读。 |
| `poke_reply` | 拍一拍回一句。 |
| `owner_boost` | 管理员说话时更愿意接。 |
| `censor_fallback` | 模型拒答时换成一句短的。 |
| `websearch` | 联网搜索。 |
| `no_period` / `persona_switch` | 去句号 / 换人设。 |

`面板.py` 是配套的桌面配置面板（tkinter，标准库，无第三方依赖）：群白名单、回复概率、token 用量与花费、所有插件参数、心情与记仇账本、语音状态。

## 目录

```
plugins/                       插件源码（复制到 AstrBot/data/plugins/ 下即可）
build/
  build.py                     从一份能跑的工作副本拼出安装包
  secret_patterns.example.py   脱敏扫描规则模板（复制一份自己填）
  tools/                       出厂配置生成、建库、分卷
  assets/                      安装向导、人设模板、说明文档、许可证
面板.py                        桌面配置面板
```

## 自己搭一套

完整流程见 [build/assets/使用说明.md](build/assets/使用说明.md)。最省事的是下载 Release 里的安装包，双击 `安装.bat` 跟着问一遍。

从源码手装：

1. 装 AstrBot 4.29.x，起得来
2. 把 `plugins/` 整个复制到 `AstrBot/data/plugins/`
3. 需要翻唱的话，按说明书的语音一节准备 `voice-env`（插件按 `qqbot/voice-env/Scripts/python.exe` 这个相对位置找它）
4. 面板里填模型、群号、管理员
5. 重启

`build/build.py` 带 4 个框架补丁（群聊窗口改配置项、注入块不写历史、STT 兼容非标准字段），见 `build/assets/astrbot-4.29.1-mods.patch`。

## 打包

```bat
python build\build.py --stage          :: 只拼目录
python build\build.py --pack           :: 拼 + 压缩 + 出自解压 exe
python build\build.py --scan-only      :: 只做脱敏扫描
```

`build.py` 会扫描产出树，命中任何密钥、QQ 号、群号、机器路径就拒绝出包。规则在 `build/secret_patterns.py`（不进 git），模板见 `secret_patterns.example.py`。

## 许可证

本仓库代码以 **AGPL-3.0** 发布 —— 它和 AGPL 的 AstrBot 深度耦合，保持同协议最省事。

依赖组件见 [LICENSES/](LICENSES/README.md)：

- AstrBot：AGPL-3.0
- NapCat：Limited Redistribution License —— **禁止商用**，再分发必须附原文

**因为 NapCat 的限制，本项目整体不得用于商业用途。**

## 免责

见 [DISCLAIMER.md](DISCLAIMER.md)。简言之：自用玩具，按现状提供，出任何事自己负责，不许商用，别拿去骚扰人。
