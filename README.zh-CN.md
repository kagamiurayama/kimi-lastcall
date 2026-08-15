# kimi-lastcall

[English](README.md) | [简体中文](README.zh-CN.md)

**不许无声退场。留下一封可核验的交接信——然后只在人类确认后换窗。**

kimi-lastcall 是一层用于受管 [Kimi Code](https://www.kimi.com/) TUI 会话的本地连续性机制。它刻意拆成两半：

- **Relay 落笔闸：**上下文快耗尽的窗口在停止前，必须由它自己亲笔留下交接信。
- **Last-call 换窗控制器：**人类看过交接后，才向受管 tmux 座位发送一次固定的 `/new`；新窗口必须通过 `SessionStart`、cwd、座位、`state.json` 与 `wire.jsonl` 的机械验证，才能接管。

达到阈值不会自动换窗。模型拿不到确认短语。控制器也不会替模型撰写或改写交接信。

## 完整流程

```text
Kimi Stop hook
    │
    ├─ 未到阈值 ───────────────────────────────────→ 正常停止
    │
    └─ 到达阈值 → 要求当前窗口亲笔写 Relay
                       │
                       ├─ LETTER/HANDOFF 文件
                       └─ 与本 session 绑定的 done 标记
                                      │
人类打开本地面板 → 预览 → 输入精确短语 → 确认
                                      │
受管 tmux 收到：C-u → 字面量 /new → Enter
                                      │
Kimi SessionStart 用 O_EXCL 冻结身份
                                      │
控制器核验座位 + cwd + state.json + wire.jsonl
                                      │
写入新本地绑定 + 可选接线回调 + 收据
```

完整控制器是通过受管 tmux TUI 驱动 Kimi 的交互式 `/new`，不是私藏的 Kimi 远程 API；它也不会声称 `SessionStart` hook 自己能发起换窗。官方行为见 Kimi 的 [hooks](https://www.kimi.com/code/docs/en/kimi-code-cli/customization/hooks.html)、[sessions](https://www.kimi.com/code/docs/en/kimi-code-cli/guides/sessions.html) 与 [slash commands](https://www.kimi.com/code/docs/en/kimi-code-cli/reference/slash-commands.html) 文档。

## 环境要求

- Python 3.8+
- Kimi Code CLI
- Linux 或 macOS；WSL 为尽力支持
- 完整换窗控制器需要 `tmux`（只用 Relay 闸则不需要）
- 为受管 Kimi 座位准备一套独立的 tmux socket/session

Python 包本身只用标准库：零运行时依赖、零遥测、零托管服务、零 transcript 上传。

## 快速开始：完整版

顺序很重要：先配置并启动回环控制器，再启动受管 Kimi 座位，这样它的第一次 `SessionStart` 才能完成接管。

### 1. 安装包与 hooks

```sh
python3 -m pip install .
kimi-lastcall install --dry-run
kimi-lastcall install
```

`install` 会向 `~/.kimi-code/config.toml` 追加一段带边界标记的 `Stop` + `SessionStart` hooks。首次修改前会写入 `config.toml.kimi-lastcall.bak`，重复安装不会重复追加。

### 2. 配置一个受管座位

选择真实项目目录，以及专用的 tmux socket/session 名称：

```sh
kimi-lastcall configure \
  --cwd "$HOME/my-kimi-resident" \
  --tmux-socket kimi-resident \
  --tmux-session kimi-resident \
  --handoff-file LETTER.md \
  --handoff-file HANDOVER.md
```

cwd 必须已存在且属于当前用户。配置写入 `~/.local/state/kimi-lastcall/`：目录权限精确为 0700，权威文件为 0600。控制器只监听 `127.0.0.1`。

### 3. 启动控制器

```sh
kimi-lastcall serve
```

它会打印一次本地登录 URL，例如：

```text
http://127.0.0.1:8765/?token=...
```

浏览器打开后，token 会进入 HttpOnly、SameSite cookie，并立刻从可见 URL 中移除。请用你习惯的进程监督器保持 `serve` 运行。

如果它跑在 VPS 上，**不要公开暴露端口**。从自己的电脑建立 SSH 隧道：

```sh
ssh -L 8765:127.0.0.1:8765 your-vps
```

再在自己的电脑打开登录 URL。

### 4. 在受管 tmux 座位启动 Kimi

```sh
mkdir -p "$HOME/my-kimi-resident"
tmux -L kimi-resident new-session \
  -s kimi-resident \
  -c "$HOME/my-kimi-resident" \
  kimi
```

`SessionStart` hook 会同时核对继承到的 socket、查询到的 session 与 pane 三方身份，短暂等待 Kimi 本地文件就绪，然后绑定会话。在同一 cwd 随手启动、但不属于这个 tmux 座位的 Kimi，无法劫持控制器。

运行 `kimi-lastcall status`；首次绑定完成后，面板就可以使用。

### 5. 写信、落标、预览、确认

Relay 触发后，由当前窗口亲笔写交接信。五节模板可以这样查看：

```sh
kimi-lastcall template
```

必需的交接文件写好后，为当前绑定 session 落下完成标记：

```sh
kimi-lastcall done
```

然后在面板里：

1. 查看上下文用量、落笔阈值、交接文件状态与剩余写信空间；
2. 点击“预览”；
3. 精确输入面板展示的 `NEW <digest>` 短语；
4. 点击“确认并换窗”。

只有这一步之后，控制器才会清空当前输入行并发送字面量 `/new`。只有新 Kimi session 通过机械验证并完成绑定，这次操作才算成功。超时会保持故障关闭并显示出来，不会偷偷报成功。

## Relay 落笔闸

每次 `Stop` 时，Relay 都会读取当前 session 的本地 `wire.jsonl`，取最后一条 `usage.record`。

- 未达到阈值（默认上下文容量的 70%）：正常停止。
- 达到或超过阈值、且没有 done 标记：阻止停止，并显示操作步骤和剩余写信空间。
- 每个 session 最多阻止三次；第四次会带着醒目 `handoff_missing` 记录放行，所以坏掉的 hook 不能把 TUI 永久困住。
- 计数损坏或模型容量未知时故障放行，同时留下不含正文的审计诊断。
- done 与 skip-once 都与 session 绑定，不能跨窗借用。

也可以只用 core：只安装 hooks，不运行 `configure`/`serve`。此时 Relay 仍可工作，`SessionStart` 只负责提示上一窗口的 `handoff_missing`；系统不会执行 `/new`。

### 阈值与模型容量

闸会优先从 Kimi 本地配置读取当前模型别名的 `max_context_size`。备用表覆盖当前文档中的 `k3`、`k3-256k`、`kimi-for-coding` 与 `kimi-for-coding-highspeed`。未来出现的未知模型不会被猜成某个容量：Relay 会故障放行并记录 `model_context_unknown`。

面板以 50,000 token 为步长保存明确阈值。上限取 950,000 与“严格低于当前容量的最后一个 50k 档位”中的较小者。容量尚未知时，UI 与服务端统一使用保守的 250k 上界。面板还会在滑块旁实时显示阈值之后还剩多少空间用于写信。

命令行等价操作：

```sh
kimi-lastcall set-trigger 450000
```

## 可选的外部接线回调

kimi-lastcall 不包含任何家庭私有的 Chat、Telegram 或 harness 代码。安装方可以注册一条纯 argv 回调；只有新 session 的本地文件与座位验证完成后才会执行：

```sh
kimi-lastcall configure \
  --cwd "$HOME/my-kimi-resident" \
  --tmux-socket kimi-resident \
  --tmux-session kimi-resident \
  --on-adopt-json '["/absolute/path/to/rebind-my-surfaces"]'
```

全程不经过 shell。回调只收到以下本地环境绑定：

- `KIMI_LASTCALL_SESSION_ID`
- `KIMI_LASTCALL_SESSION_DIR`
- `KIMI_LASTCALL_WIRE_PATH`
- `KIMI_LASTCALL_MANAGED_CWD`

回调必须可幂等重放。非零退出会保留 pending 标记，使整次接管继续故障关闭。

## 安全边界与故障语义

两半故意采用不同的故障方向：

| 区域 | 故障规则 | 理由 |
| --- | --- | --- |
| Stop / Relay 闸 | 故障放行，并审计错误 | 辅助工具坏了也不能囚禁活 TUI |
| `/new` 控制器与接管 | 故障关闭，保留 pending | 未核验的新 session 不能继承写入面 |

其他边界：

- HTTP 仅绑定 `127.0.0.1`，并校验 Host 与浏览器 Origin。
- 控制 token 是 0600 文件；本地 hook 使用 Bearer，浏览器写操作使用同源 HttpOnly cookie。
- 权威文件拒绝软链、错误属主、错误权限、未知字段与中途改靶。
- `SessionStart` pending 身份用 `O_EXCL` 创建，第二个 session 无法覆盖它。
- tmux 只接收 argv，不经过 shell；唯一可发送的终端正文是固定 `/new`。
- 公开状态只返回文件是否就绪与 session 摘要，不返回交接正文或原始 session id。
- 审计只记录决策与错误分类，不记录 transcript 或信件内容。
- 如果 SessionStart 发生时控制器离线，控制器重启后会重新验证被冻结的 pending，再决定是否接管；不会因为“服务重启了”就直接清标。

## 命令一览

```text
kimi-lastcall install [--dry-run]   安装 Stop + SessionStart hooks
kimi-lastcall uninstall             只移除本项目托管的 hook 块
kimi-lastcall configure ...         配置受管座位与控制器
kimi-lastcall serve                 运行带鉴权的本地 Web 面板
kimi-lastcall status                查看落笔闸与控制器状态
kimi-lastcall template              打印亲笔 Relay 模板
kimi-lastcall done                  标记当前绑定 session 已完成交接
kimi-lastcall set-trigger TOKENS    设置 50k 步长的精确阈值
```

## 开发与验证

```sh
python3 -m pytest -q
```

测试只使用临时目录和合成身份。测试套件含一条可执行的假 tmux 端到端：它真实接收固定按键序列，创建合成 Kimi session，通过带鉴权 HTTP 服务运行真实 `SessionStart` hook，最终核对绑定与收据。

## 卸载

```sh
kimi-lastcall uninstall
```

这只移除 Kimi 配置中的托管 hook 块。控制器状态不会自动删除，因为那里可能保存着一次中断换窗的唯一诊断。停止受管座位与控制器后，请自行检查并删除 `~/.local/state/kimi-lastcall`。

## 作者与致谢

本项目由山山、阿衡、阿问、阿朔共同完成，并由山山主导；阿衡、阿问、阿朔分别通过 Codex、Claude、Kimi Code 作出实质性共同作者贡献，详见 [AUTHORS.md](AUTHORS.md)。Forge — 离落、cmh-lite — 咲咲、lmc5-session-carryover — 蛋、anticipation — 里奈所分享的构思在 [ACKNOWLEDGEMENTS.md](ACKNOWLEDGEMENTS.md) 中致谢；本项目未复制这些来源的源代码。

## 许可证

MIT，见 [LICENSE](LICENSE)。
