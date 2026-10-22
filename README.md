# AutoDL 空闲 GPU 监控器

按地区、GPU 型号和最少空闲卡数监控 AutoDL 算力商城。满足条件时在终端显示结果、蜂鸣，并可通过邮件、Server酱或 Webhook 通知。程序不会自动下单。

此仓库不包含个人登录 token、邮箱账号、收件地址、邮箱授权码或运行日志。邮件通知默认关闭。北京A区和 RTX 2080 Ti 仅为可修改的示例监控条件。

## 1. 运行环境

- Windows，安装 Python 3.8 或以上版本，安装时勾选“Add Python to PATH”。
- 使用 Python 标准库，无需执行 `pip install`。
- 需要能联网访问 AutoDL；启用邮件时还需要能连接邮箱的 SMTP 服务。

在文件夹空白处右键打开终端，运行：

```powershell
python --version
```

后续所有命令都在本文件夹中运行。也可以先用 `cd "解压后的文件夹完整路径"` 进入目录；请将引号里的提示替换成自己的实际路径。

## 2. 创建本地配置并填写 AutoDL 登录 token

下载并解压项目，或用 Git 克隆本仓库。在项目文件夹中打开 PowerShell，将模板复制为实际配置：

```powershell
Copy-Item .\config.example.json .\config.json
```

也可以在资源管理器中复制 `config.example.json`，将副本重命名为 `config.json`；注意不要变成 `config.json.json`。这一步仅在首次使用时执行，已有配置不要重复覆盖。

用记事本或代码编辑器打开 `config.json`，找到：

```json
"authorization": ""
```

浏览器登录 [AutoDL](https://www.autodl.com)，按 `F12`，打开 **Application（应用）→ Local Storage（本地存储）→ https://www.autodl.com**，选中名为 **token** 的条目，复制其 **Value（值）**。将完整值放入上面的英文双引号中：

```json
"authorization": "替换成你自己的完整token"
```

上面是单个字段示例，请修改原文件中的对应字段，保留原有逗号；不要把这段单独替换整个配置文件。复制的是 token 对应的值，不是 `token` 这几个字。不要额外添加 `Bearer ` 前缀。若复制结果包含包裹整个值的双引号，不要把那层引号重复粘贴到 JSON 中。

token 相当于登录凭证，请勿发布或发给别人。填写后检查：

```powershell
python autodl_monitor.py --check
```

以“机器列表接口可用”为校验成功的依据；只看到“地区列表返回”不够，因为地区列表是公开接口。若提示登录过期或 `AuthorizeFailed`，重新登录网页并更新 token。

可选替代方式：在同目录新建 `token.txt`，只放完整 token 一行；或设置环境变量 `AUTODL_TOKEN`。读取顺序为 `--token` 参数 > 环境变量 `AUTODL_TOKEN` > `config.json` 的 `authorization` > `token.txt`。选一种方式即可；旧环境变量会覆盖文件中的值。

也支持手机号、密码交互登录：

```powershell
python autodl_monitor.py --login --phone 你的手机号
```

将“你的手机号”替换成真实号码。密码会在终端交互输入，成功后 token 写入 `config.json`。若登录接口不可用，使用上面的浏览器方式。

## 3. 填写监控条件

以下是 `config.json` 中已有的示例字段：

```json
"regions": ["北京A区"],
"gpus": ["RTX 2080 Ti"],
"min_idle_gpu": 1,
"gpu_match": "loose",
"interval_seconds": 30
```

| 字段 | 填什么 |
| --- | --- |
| `regions` | 地区名称数组，例如 `["北京A区", "北京B区"]`；也可填写内部机房码；`[]` 表示不限地区。 |
| `gpus` | GPU 型号数组，例如 `["RTX 2080 Ti"]`；`[]` 表示不限型号。 |
| `min_idle_gpu` | 每台机器至少需要几张可用空闲显卡，填正整数，默认 `1`。 |
| `gpu_match` | `exact` 为规范化后完整名称匹配；`loose` 为包含匹配，会纳入名称包含目标的变体，例如 `RTX 2080 Ti *2`。 |
| `interval_seconds` | 每轮结束后的等待秒数，默认 `30`；一轮查询自身也需要时间。 |

同名地区会展开为多个机房，启动时会打印具体范围。用以下命令查看当前名称：

```powershell
python autodl_monitor.py --list-regions
python autodl_monitor.py --list-gpus
```

`--list-regions` 不需要 token，`--list-gpus` 需要有效 token。

其他配置一般保持默认：

| 字段 | 作用及默认值 |
| --- | --- |
| `api_base` | API 地址，默认 `https://api.autodl.com`。 |
| `timeout_seconds` | 单次请求超时秒数，默认 `20`。 |
| `page_size` | 每页请求的机器数量，默认 `100`。 |
| `max_pages` | 每轮最多查询的页数，默认 `5`；较大地区可能需要提高，以覆盖更多机器。 |
| `notify_on_change_only` | 默认 `true`；有符合条件的机器，且匹配机器或空闲卡数发生变化时才尝试通知，首轮有货也会通知。 |
| `cooldown_seconds` | 整体通知的最小间隔，默认 `180` 秒；冷却期内的变化可能被跳过。 |
| `open_browser_on_new` | 默认 `true`，通知时打开商城；不需要时设为 `false`。 |
| `market_url` | 打开的商城地址，默认 `https://www.autodl.com/market/list/`。 |

配置必须是合法 JSON：英文双引号，不写注释，最后一个字段后不加逗号。布尔值使用小写 `true` / `false`。配置损坏时程序会警告并使用默认值，请留意启动日志。

## 4. 启动与停止

填好 token 和监控条件后，双击 `start.bat`，或执行：

```powershell
python autodl_monitor.py
```

窗口要保持运行，电脑不能关机或睡眠。按 `Ctrl+C` 停止监控。

常用命令：

```powershell
# 只查一轮；有货时仍会发通知、蜂鸣或打开浏览器
python autodl_monitor.py --once

# 临时覆盖地区、型号、最少空闲卡数和等待时间，不改配置文件
python autodl_monitor.py --regions 北京A区 --gpus "RTX 4090" --min-idle 2 --interval 30

# 查看完整参数
python autodl_monitor.py --help
```

PowerShell 中调用批处理要使用 `.\start.bat`，例如 `.\start.bat --once`。批处理已使用 Windows CRLF 换行；编辑时请保留此格式。

日志中的“扫描 N 台”是筛选目标 GPU 型号后的**服务器台数**，不是显卡张数。例如 40 台服务器、每台 8 张显卡，总共是 320 张；显卡总数也不等于当前空闲可租数。“该地区在售 X 种型号”来自本轮已拉取的数据，受查询分页上限影响。

## 5. 可选：邮件通知

不需要邮件时，保持 `notification.email.enabled` 为 `false`，账号和地址留空即可，终端提示和蜂鸣仍可使用。

需要 SMTP 邮件通知时，修改 `config.json` → `notification` → `email` 中的以下字段：

| 字段 | 应填写的内容 |
| --- | --- |
| `enabled` | 改为 `true`。 |
| `smtp_host` | 发件邮箱服务商的 SMTP 服务器。QQ/Foxmail 通常为 `smtp.qq.com`。 |
| `smtp_port` | SSL 通常为 `465`；STARTTLS 通常为 `587`，以服务商说明为准。 |
| `use_ssl` | 465 通常填 `true`；使用 STARTTLS 时填 `false`。 |
| `smtp_user` | 你自己的完整发件邮箱地址。 |
| `from` | 发件人邮箱地址，通常与 `smtp_user` 相同。 |
| `to` | 收件邮箱数组，可填一个或多个真实邮箱，例如 `["recipient@example.com"]`；示例地址须替换。 |
| `subject_prefix` | 邮件标题前缀，可保持 `[AutoDL监控] `。 |
| `password` | 建议留空，并按下一步把邮箱授权码放入 `smtp_password.txt`。 |
| `cooldown_seconds` | 单个收件地址被拒后暂停重试的秒数，默认 `900`，不同于外层通知冷却。 |

在发件邮箱中开启 SMTP 服务，获取 **SMTP 授权码或应用专用密码**。QQ/Foxmail 不应使用网页登录密码。在项目同目录新建 `smtp_password.txt`，将授权码写进去，只放一行内容，不加引号或说明文字。该文件不会包含在 GitHub 仓库中。

授权码读取顺序：`notification.email.password` > 环境变量 `SMTP_QQ_AUTH_CODE` > `smtp_password.txt`。

验证邮件通道：

```powershell
python autodl_monitor.py --test-email
```

这条命令会实际向 `to` 中的地址发送测试邮件，不需要 AutoDL token。收件人逐个发送，检查输出中的每个地址；整体成功可能只代表其中一个地址发送成功。持续出现 `550` 时先核对收件地址，再检查对方邮箱限制。

## 6. 可选：其他通知方式

这些字段都在 `notification` 内；不使用就保持空字符串。

| 字段 | 填什么 |
| --- | --- |
| `sendkey` | 自己在 [Server酱](https://sct.ftqq.com/) 获取的 SendKey，用于微信等推送。 |
| `webhook_url` | 自己的 Webhook 接收地址；程序 POST JSON，包含 `title`、`content`、`time`。 |
| `resend_api_key` | 自己的 [Resend](https://resend.com) API Key。填写后邮件优先走 Resend，不走 SMTP。 |

使用 Resend 还需要在 `notification.email` 中添加 `resend_from`，填写经 Resend 验证的发件地址，并填写 `to` 收件人数组。Resend 通道由 `resend_api_key` 是否非空决定；`email.enabled=false` 不能阻止已配置的 Resend 通道发送。如要关闭 Resend，清空其 API Key。

Server酱、Webhook 可与邮件同时启用。

## 7. 文件说明与再次分享

```text
autodl/
├── autodl_monitor.py    主程序
├── config.json         首次使用时复制模板生成；不提交到 Git
├── config.example.json 无个人信息的配置模板
├── smtp_password.txt   使用邮件时自行创建；不提交到 Git
├── start.bat           Windows 启动脚本
├── README.md           本说明
├── .gitignore          避免 Git 跟踪常见凭证和日志文件
└── logs/
    └── monitor.log     运行后自动产生的日志；不提交到 Git
```

恢复空白配置：先保留自己仍需使用的配置，再用 `config.example.json` 的内容覆盖 `config.json`。

自己填写信息并运行后，再次分享时请：

1. 将 `config.json` 恢复成空白模板，移除 token、邮箱、API Key 和私人 Webhook 地址。
2. 清空 `smtp_password.txt`，如创建过 `token.txt` 或 `.env` 也应移除其中凭证。
3. 清空运行日志，不夹带截图、备份配置、编辑器备份或缓存。

`.gitignore` 只影响 Git 对未跟踪文件的处理，不会自动清理已跟踪文件、历史提交或 ZIP 压缩包；手动打包前仍需检查。若凭证已公开泄露，应在对应服务中撤销或更换。
