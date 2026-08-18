# kimi-lastcall

[English](README.md) | [简体中文](README.zh-CN.md)

**不许无声退场。留下一封可核验的交接信——再按明确的本机策略自动换窗，或由人类确认换窗。**

kimi-lastcall 是一层用于受管 [Kimi Code](https://www.kimi.com/) TUI 会话的本地连续性机制。它刻意拆成两半：

- **Relay 落笔闸：**上下文快耗尽的窗口在停止前，必须由它自己亲笔留下交接信。
- **Last-call 换窗控制器：**亲笔交接机械就绪后，按配置自动发送、或经人类确认兜底发送一次固定的 `/new`；新窗口必须通过 `SessionStart`、cwd、座位、`state.json` 与 `wire.jsonl` 的机械验证，才能接管。

单凭达到阈值不会换窗：当前窗口还必须写好必需文件，并落下与本 session 绑定的 done 标记。hook 永远不碰终端输入；控制器也不会替模型撰写或改写交接信。

这里的“亲笔”是指由当前 Kimi agent 在正常回合中写入项目文件，而不是由控制器代写。它不表示“人类亲写”“没有使用 LLM”，也不表示天然比 Kimi 自带的压缩摘要更准确。

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
自动策略：鉴权排队（先完成 HTTP 回包）
或人工策略/兜底：预览 → 精确短语 → 确认
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

## 常见问题

### 它会减少 token 消耗吗？

不一定。换窗前写交接信、新窗口再读取交接信，都会在切换时增加一些 token 开销。之后的新窗口不再携带整段旧上下文，后续每轮请求可能更轻；但总量仍取决于任务长度、交接信大小，以及服务商的缓存与计费方式。我们尚未做出证明净节省的基准测试，也不把 kimi-lastcall 宣传成 token 优化工具。它的目标是让跨 session 的连续性变得明确、可查看、可核验。

### 它和 Kimi Code 自带的上下文压缩有什么区别？

先说共同点：两种机制都会让模型判断什么值得留下，并把旧上下文浓缩。这个过程是共同机制，不是差异。“亲笔交接”不能被理解成“模型没有做摘要”，也不能被理解成“结果自然更保真”。

在本次对照所检查的 Kimi Code 实现中，压缩会把较早的文本消息连同结构化总结提示词交给 LLM，再用生成的摘要替换 session 内的旧上下文，同时保留最近的消息。可直接查看当时的[压缩实现](https://github.com/MoonshotAI/kimi-cli/blob/cbc15c076d17f70fec9f89c90c0502e68657f505/src/kimi_cli/soul/compaction.py)与[总结提示词](https://github.com/MoonshotAI/kimi-cli/blob/cbc15c076d17f70fec9f89c90c0502e68657f505/src/kimi_cli/prompts/compact.md)。

| | Kimi Code 上下文压缩 | kimi-lastcall |
| --- | --- | --- |
| 连续性的单位 | 原 session 带着浓缩后的上下文继续 | 明确执行 `/new`，由新 session 接管 |
| 交接载体 | session 上下文中的生成摘要 | 换窗前写入项目目录的可读文件 |
| 人类控制 | 手动 `/compact` 或自动压缩 | done 前可检查、修改交接文件；可选自动或人工确认换窗 |
| 接管过程 | 没有新 session 接管步骤 | 核验 `SessionStart`、cwd、tmux 座位、`state.json`、`wire.jsonl`，并留下收据 |
| 主要目的 | 缓解上下文压力，让原 session 继续 | 外置交接，并核验一次实际的 session 轮换 |

kimi-lastcall **不宣称**摘要能力更强、事实保真度更高或 token 更省。这些结论都需要目前尚未完成的对照基准。

两者可以共存。kimi-lastcall 不会关闭上下文压缩；automatic 模式只要求关闭 Kimi 的“缓存过期、下一条消息将重新发送完整历史”弹窗，因为它会截住固定的 `/new` 输入。上下文压缩仍可作为极限兜底。

### 我需要 kimi-lastcall 吗？

如果 Kimi Code 原生压缩已经足够维持你的任务连续性，大概率不需要。原生机制更简单，活动部件也更少。

kimi-lastcall 面向的是明确需要这些能力的人：把交接持久保存在项目旁边、以新 session 作为边界、在换窗前检查或修改交接，以及机械证明预期的新 session 确实完成了接管。

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
  --handoff-file HANDOVER.md \
  --switch-mode automatic
```

cwd 必须已存在且属于当前用户。`--switch-mode automatic` 必须显式选择；省略它（或写 `manual`）则只允许人类确认后换窗。配置写入 `~/.local/state/kimi-lastcall/`：目录权限精确为 0700，权威文件为 0600。控制器只监听 `127.0.0.1`。

无人值守的自动座位还应在 Kimi Code 的 `tui.toml`（通常为 `~/.kimi-code/tui.toml`）加入顶层设置：

```toml
cache_expiry_hint = false
```

它只关闭 Kimi 的“缓存过期、下一条消息将重新发送完整历史”弹窗；该弹窗会截住下一条输入，直到有人作出选择。它不会关闭上下文压缩。automatic 模式下，`kimi-lastcall` 会只读检查这一设置；无法证明弹窗已关闭时会明确报警，但绝不会替用户静默改写 Kimi 的全局 UI 配置。

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

### 5. 写信、落标，然后换窗

Relay 触发后，由当前窗口亲笔写交接信。五节模板可以这样查看：

```sh
kimi-lastcall template
```

必需的交接文件写好后，为当前绑定 session 落下完成标记：

```sh
kimi-lastcall done
```

在 `automatic` 模式下，再停止一次。Stop hook 会证明自己属于受管座位、持有一把内核文件锁，并向带鉴权的回环控制器发送一份不含正文的请求，收到 `202 Accepted` 后退出。hook 进程退出、内核释放锁以后，后台 worker 才能继续；它会重新核验 binding、阈值、交接文件、done 标记与 tmux 身份，并且只发送一次字面量 `/new`。

在 `manual` 模式下——或自动模式故障后的人工兜底——使用面板：

1. 查看上下文用量、落笔阈值、交接文件状态与剩余写信空间；
2. 点击“预览”；
3. 精确输入面板展示的 `NEW <digest>` 短语；
4. 点击“确认并换窗”。

只有新 Kimi session 通过机械验证并完成绑定，这次操作才算成功。超时会保持故障关闭并显示出来，不会偷偷报成功，也不会自动重发。

无需重装 hooks 就能切换策略：

```sh
kimi-lastcall set-mode automatic   # 或：manual
```

切换模式后请重启 `kimi-lastcall serve`；运行中的控制器刻意不会热加载权威配置。

## Relay 落笔闸

每次 `Stop` 时，Relay 都会读取当前 session 的本地 `wire.jsonl`，取最后一条 `usage.record`。

- 未达到阈值（默认上下文容量的 70%）：正常停止。
- 达到或超过阈值、且没有 done 标记：阻止停止，并显示操作步骤和剩余写信空间。
- 达到或超过阈值、且已有 done 标记：manual 模式正常放行；automatic 模式向已验证的控制器排队换窗。
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
- 自动请求与当前原始 session、cwd、tmux socket/session/pane、阈值、必需文件及 done 标记全部绑定；公开状态只显示摘要。
- 控制器先持久化请求并回包，flush 完 HTTP 响应后才启动 worker；worker 还必须取得 Stop hook 的内核锁，因此 `/new` 不会重入发起请求的进程。
- 一旦存在 switch-in-progress，就表示 `/new` 可能已经发出。重启后绝不自动重发，面板会明确要求人工恢复。
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
kimi-lastcall set-mode MODE         选择 manual 或 automatic 换窗
```

## 开发与验证

```sh
python3 -m pytest -q
```

测试只使用临时目录和合成身份。测试套件为人工与自动两条路径各准备了一条可执行假 tmux 端到端；自动路径真实走 Stop hook → HTTP 排队 → 后台 worker → 固定 `/new` → `SessionStart` 接管，并核对最终 binding 与收据。

## 卸载

```sh
kimi-lastcall uninstall
```

这只移除 Kimi 配置中的托管 hook 块。控制器状态不会自动删除，因为那里可能保存着一次中断换窗的唯一诊断。停止受管座位与控制器后，请自行检查并删除 `~/.local/state/kimi-lastcall`。

## 作者与致谢

本项目由山山、阿衡、阿问、阿朔共同完成，并由山山主导；阿衡、阿问、阿朔分别通过 Codex、Claude、Kimi Code 作出实质性共同作者贡献，详见 [AUTHORS.md](AUTHORS.md)。Forge — 离落、cmh-lite — 咲咲、lmc5-session-carryover — 蛋、anticipation — 里奈所分享的构思在 [ACKNOWLEDGEMENTS.md](ACKNOWLEDGEMENTS.md) 中致谢；本项目未复制这些来源的源代码。

## 许可证

MIT，见 [LICENSE](LICENSE)。
