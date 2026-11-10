# 鲸鲸bot 打包

从工作区里的「能跑的版本」产出一个别人能装的安装包。

## 产物

`build\dist\` 下：

| 文件 | 大小 | 用途 |
| --- | --- | --- |
| `鲸鲸bot_v1.0_安装包.exe` | 3.2 GB | 自解压安装包，双击解到任意空目录，再双击里面的 `安装.bat` |
| `更新包_v1.0.zip` | 31 MB | 只含代码，给已经装过的人更新 |
| `SHA256SUMS.txt` | — | 校验值 |

## 怎么跑

```bat
cd /d <你的工作区>
qqbot\AstrBot\.venv\Scripts\python.exe build\build.py --stage    :: 只拼目录，不压缩
qqbot\AstrBot\.venv\Scripts\python.exe build\build.py --pack     :: 拼 + 压 + 出 exe
qqbot\AstrBot\.venv\Scripts\python.exe build\build.py --pack-only :: 复用已有目录，只压
qqbot\AstrBot\.venv\Scripts\python.exe build\build.py --scan-only :: 只查泄露
```

`--pack` 里压缩那步 7.48 GB → 3.23 GB 大约 15 分钟，其余几十秒。中途改了源码就用
`--pack-only`，省掉重新拷贝。

## 目录在干什么

```
build\
  build.py              构建脚本：拷贝、脱敏扫描、压缩、出更新包
  assets\
    factory\            出厂配置（16 个，全部清空 Key/群号/人设名）
    installer\          进包的东西：安装.bat / 启动*.bat / 更新.bat / install.py / update.py
    persona.txt         人设模板，占位符 {name} {owner} {owner_qq} {owner_nick} {second_keeper}
    persona_meta.json   人设 id 与开场白
    napcat_*.json       NapCat 干净模板（token、登录账号已清空）
    LICENSES\           NapCat 的协议原文，必须随包发
    使用说明.md         给买家的说明书
    README.txt          解压后第一眼看到的那张
  tools\                构建期的辅助脚本，不进包
  out\                  拼好的目录（7.5 GB，可删）
  dist\                 最终产物
```

## 出厂配置怎么来的

`build\tools\make_factory_config.py` 读**当前正在跑的那份**配置，然后清掉：所有
`provider_sources` / `provider` / `platform`、Key、管理员 QQ、群白名单、dashboard 的
密码与 jwt、插件里的 `fast_source_id` / `llm_source_id` / `keepers` / `censor_fallback`
的群号。改完工作区配置后重跑一次它，再重新打包，才能把新默认值带进包。

`make_factory_config.py` 生成的 `cmd_config.json` 不带密码。框架首次启动时
（`astrbot\core\config\astrbot_config.py:130-140`）会自己生成随机密码打印在控制台，
所以「出厂不带密码」是它支持的路径，别手动塞一个默认密码。

## 脱敏是怎么保证的

`build.py` 里的 `SECRETS` 有九条正则，覆盖 API Key、三个 QQ 号、五个群号、两个昵称、
工作区路径、网关地址。`--pack` 前一定先 `scan`，命中任何一条就直接退出不打包。

已经踩过的坑（改动时留神）：

- `.pth` 文件里写死了构建机的 AstrBot 绝对路径，直接复制 `site-packages` 会把路径带进去。
- `astrbot-4.29.1.dist-info\direct_url.json` 记着 `file:///` 开头的构建机源码路径。
- `blend.py` / `speak_server.py` 原先硬编码绝对路径，已改成相对 `parents[4]`。
- 插件里的群号（`censor_fallback`）要清，`metadata.yaml` 里的 `repo:` 也要去。

## 验证过一次的

在一个空目录下解压出安装包，实跑过：向导七步 → 配置与插件补丁正确 → 人设
写库无残留占位符 → 面板能开（7 页、语音状态绿）→ `main.py` 能起（dashboard 200、
框架自动补密码、18 个插件全加载）→ `更新.bat` 换掉 1301 个文件且 cmd_config / db /
白名单 / 人设 / 安装记录全部保留。

自动化测试：`.tmp\test_install.py`（31 项）、`.tmp\test_update.py`（13 项）、
`.tmp\test_wizard.py`（7 项）、`.tmp\test_panel.py`，全 `bad: 0`。

## 还没做的

- 没在真正的另一台电脑上装过（同一台机器的另一个目录不算）。真机第一次装时最可能的
  问题是 QQ 版本、UAC 权限、显卡驱动。
- 包内 `napcat` 是照搬本机那份，它上游版本更新后要重新拷一次。
- 更新包不做增量，是全量覆盖代码（31 MB，忍了）。
