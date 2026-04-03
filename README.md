# 邮件提醒插件

AstrBot 邮箱长时间未读邮件提醒插件。通过 IMAP 协议定时检查邮箱长时间未读的邮件，支持白名单过滤规则和多会话推送，目前只测试了napcat，其他平台接入器没有做测试，不保证可用性。默认的情况下，一小时最多通知一次。

~~这下再也不会错过导师的邮件了~~

## 功能

插件以 `mail_alert` 指令组的形式提供所有功能，支持添加多个邮箱、为每个邮箱配置独立的白名单过滤规则（按发件人域名、发件人地址、邮件主题过滤），并可将通知绑定到多个会话（如不同的 QQ 群或私聊）。定时任务自动检查未读邮件，发现新邮件后向所有绑定会话推送提醒，同一封邮件在冷却时间内不会重复通知。

常见邮箱（QQ、163、126、Gmail、Outlook 等）的 IMAP 服务器地址可自动检测，添加邮箱时会验证 IMAP 连接是否成功。

## 安装

将本插件仓库地址添加到 AstrBot 插件管理中即可安装，或手动克隆到 `data/plugins/` 目录下。

## 配置

插件提供两个可在 WebUI 管理面板上配置的参数：

- `check_interval`：邮件检查间隔，单位为分钟，默认 5 分钟，最小 1 分钟。
- `cooldown`：同一邮件的提醒冷却时间，单位为小时，默认 24 小时。

## Changelog

### v1.0.2

增强了 IMAP 连接失败和手动检查失败时的用户可见错误提示；为包含授权码的添加邮箱指令增加了撤回提醒；启动时会自动测试已配置代理的连通性并输出日志；同时补充了插件配置 schema，用于在 AstrBot WebUI 中直接配置插件参数。

### v1.0.1

新增 IMAP 代理支持，可为 Gmail、Outlook 等国外邮箱服务器配置代理连接；过滤规则中的 `domain` 匹配改为支持子域名；`mail_alert add` 对 Gmail 授权码中的空格问题增加提示，并修正了帮助文本中的相关说明。

### v1.0.0

首个版本提供基于 IMAP 的未读邮件定时检查能力，支持添加和移除邮箱、绑定通知会话、手动检查未读邮件，以及按发件人域名、发件人地址、邮件主题关键词配置白名单过滤规则。

## 命令列表

| 命令                                       | 说明         |
| ---------------------------------------- | ---------- |
| `mail_alert add <邮箱> <授权码> [IMAP服务器]`    | 添加邮箱监控     |
| `mail_alert remove <邮箱>`                 | 移除邮箱（仅添加者） |
| `mail_alert list`                        | 列出已添加的邮箱   |
| `mail_alert check [邮箱]`                  | 手动检查未读邮件   |
| `mail_alert bind <邮箱>`                   | 绑定当前会话接收通知 |
| `mail_alert unbind <邮箱>`                 | 解绑当前会话     |
| `mail_alert filter add <邮箱> <类型> <值>`    | 添加白名单规则    |
| `mail_alert filter remove <邮箱> <类型> <值>` | 移除白名单规则    |
| `mail_alert filter list <邮箱>`            | 查看过滤规则     |
| `mail_alert status`                      | 查看监控状态     |
| `mail_alert help`                        | 显示帮助信息     |

过滤类型支持 `domain`（发件人域名）、`address`（发件人地址）、`subject`（邮件主题关键词）。设置过滤规则后，只有匹配至少一条规则的邮件才会触发通知。未设置规则时所有未读邮件都会通知。

## 使用示例

```
/mail_alert add user@qq.com myauthcode
/mail_alert filter add user@qq.com domain edu.cn
/mail_alert filter add user@qq.com address teacher@school.edu.cn
/mail_alert bind user@qq.com
/mail_alert check
/mail_alert status
```
