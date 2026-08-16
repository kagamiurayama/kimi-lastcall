# kimi-lastcall

[English](README.md) | [简体中文](README.zh-CN.md)

**No silent exits. Leave a verifiable handoff — then switch automatically or with human confirmation, by explicit local policy.**

kimi-lastcall is a local continuity layer for a managed [Kimi Code](https://www.kimi.com/) TUI session. It has two deliberately separate halves:

- **Relay gate:** before a context-heavy window stops, ask that same window to write a handwritten handoff.
- **Last-call controller:** after the handwritten handoff is mechanically ready, send one fixed `/new` command either automatically or through a human-confirmed fallback, then accept the new session only after `SessionStart`, cwd, seat, `state.json`, and `wire.jsonl` all verify.

The threshold alone never switches sessions: the same window must write the required files and create its session-bound done marker. The hook never sends terminal input. The controller never writes or rewrites the handoff.

## What the complete flow looks like

```text
Kimi Stop hook
    │
    ├─ below threshold ───────────────────────────────→ stop normally
    │
    └─ at threshold → ask this window to write Relay
                         │
                         ├─ LETTER/HANDOFF files
                         └─ session-bound done marker
                                      │
Automatic policy: authenticated queue (HTTP response completes first)
or manual policy/fallback: preview → exact phrase → confirm
                                      │
Managed tmux receives: C-u → literal /new → Enter
                                      │
Kimi SessionStart freezes identity with O_EXCL
                                      │
Controller verifies seat + cwd + state.json + wire.jsonl
                                      │
New local binding + optional integration callback + receipt
```

The full controller drives Kimi's interactive `/new` command through a managed tmux TUI. It is not a hidden Kimi remote API and it does not claim that Kimi's `SessionStart` hook can initiate a switch by itself. See Kimi's official documentation for [hooks](https://www.kimi.com/code/docs/en/kimi-code-cli/customization/hooks.html), [sessions](https://www.kimi.com/code/docs/en/kimi-code-cli/guides/sessions.html), and [slash commands](https://www.kimi.com/code/docs/en/kimi-code-cli/reference/slash-commands.html).

## Requirements

- Python 3.8+
- Kimi Code CLI
- Linux or macOS; WSL is best effort
- `tmux` only for the full switch controller (the Relay gate works without it)
- one dedicated tmux socket/session for the managed Kimi seat

The Python package itself uses only the standard library: no runtime dependencies, telemetry, hosted service, or transcript upload.

## Quick start: full product

The order matters: configure and start the loopback controller before launching the managed Kimi seat, so its first `SessionStart` can be adopted.

### 1. Install the package and hooks

```sh
python3 -m pip install .
kimi-lastcall install --dry-run
kimi-lastcall install
```

`install` appends one marker-delimited block containing the `Stop` and `SessionStart` hooks to `~/.kimi-code/config.toml`. It writes `config.toml.kimi-lastcall.bak` before the first modification and is idempotent.

### 2. Configure one managed seat

Choose a real project directory and names for a dedicated tmux socket/session:

```sh
kimi-lastcall configure \
  --cwd "$HOME/my-kimi-resident" \
  --tmux-socket kimi-resident \
  --tmux-session kimi-resident \
  --handoff-file LETTER.md \
  --handoff-file HANDOVER.md \
  --switch-mode automatic
```

The cwd must already exist and belong to the current user. `--switch-mode automatic` is explicit; omit it (or use `manual`) to keep human confirmation as the only switch trigger. Configuration is stored under `~/.local/state/kimi-lastcall/` with a 0700 directory and 0600 authority files. The controller listens only on `127.0.0.1`.

For an unattended automatic seat, set the following top-level preference in Kimi Code's `tui.toml` (normally `~/.kimi-code/tui.toml`):

```toml
cache_expiry_hint = false
```

This disables only Kimi's idle cache-cost dialog, which otherwise intercepts the next submitted message until someone chooses an option. It does not disable context compaction. `kimi-lastcall` checks this preference in automatic mode and reports a compatibility warning when it cannot prove the dialog is disabled; it never rewrites Kimi's global UI settings.

### 3. Start the controller

```sh
kimi-lastcall serve
```

It prints a one-time local login URL such as:

```text
http://127.0.0.1:8765/?token=...
```

Opening it stores the token in an HttpOnly, SameSite cookie and immediately removes it from the visible URL. Keep `serve` running under your normal process supervisor.

For a VPS, do not expose the port publicly. Use an SSH tunnel from your computer:

```sh
ssh -L 8765:127.0.0.1:8765 your-vps
```

Then open the URL on your own computer.

### 4. Start Kimi in the managed tmux seat

```sh
mkdir -p "$HOME/my-kimi-resident"
tmux -L kimi-resident new-session \
  -s kimi-resident \
  -c "$HOME/my-kimi-resident" \
  kimi
```

The `SessionStart` hook proves all three tmux identities (inherited socket, queried session, and pane), waits briefly for Kimi's local artifacts, and binds that session. A Kimi process started in the same cwd but outside this seat cannot retarget the controller.

Run `kimi-lastcall status`; the panel becomes usable after the first binding.

### 5. Write, mark, then switch

When Relay fires, write the handoff in the current window. The five-section template is available with:

```sh
kimi-lastcall template
```

After the required handoff files are present, mark this exact session done:

```sh
kimi-lastcall done
```

In `automatic` mode, stop again. The Stop hook proves that it belongs to the managed seat, holds a kernel file lease, posts a content-free request to the authenticated loopback controller, receives `202 Accepted`, and exits. The worker cannot proceed until the kernel releases that lease at hook-process exit; it then rechecks the binding, threshold, handoff files, marker, and tmux identity before sending literal `/new` once.

In `manual` mode—or as an automatic-mode recovery fallback—use the panel:

1. inspect context usage, threshold, handoff readiness, and remaining writing room;
2. choose **Preview**;
3. type the displayed `NEW <digest>` phrase exactly;
4. choose **Confirm and switch**.

The operation is complete only when the new Kimi session is mechanically verified and bound. A timeout stays fail-closed and remains visible; it is not silently declared successful or automatically retried.

You can change policy without reinstalling hooks:

```sh
kimi-lastcall set-mode automatic   # or: manual
```

Restart `kimi-lastcall serve` after changing mode; the running controller deliberately does not hot-reload authority configuration.

## The Relay gate

On every `Stop`, Relay reads the last local `usage.record` from that session's `wire.jsonl`.

- Below the trigger (default 70% of the context limit): stop normally.
- At or above it, without a done marker: block the stop with instructions and remaining writing room.
- At or above it with a done marker: manual mode allows the stop; automatic mode queues the verified controller switch.
- At most three blocked stops per session. The fourth stop is allowed with a loud `handoff_missing` record, so the hook cannot trap you forever.
- A corrupt counter or unrecognized model capacity fails open with a non-content audit diagnostic.
- Done and skip-once markers are session-bound.

Core-only use is supported: install the hooks but do not run `configure`/`serve`. In that mode Relay still works and `SessionStart` only delivers a prior `handoff_missing` notice; there is no `/new` controller.

### Thresholds and model capacity

The gate first reads the active model alias's `max_context_size` from Kimi's local config. Its fallback table covers the currently documented `k3`, `k3-256k`, `kimi-for-coding`, and `kimi-for-coding-highspeed` identifiers. Unknown future models are not guessed; Relay fails open and records `model_context_unknown`.

The panel stores an explicit threshold in 50,000-token steps. The maximum is the lower of 950,000 and the last 50k step strictly below the observed model capacity. While capacity is unknown, the UI and server use a conservative 250k ceiling. The panel also shows the writing room left above the selected threshold.

CLI equivalent:

```sh
kimi-lastcall set-trigger 450000
```

## Optional external-surface callback

kimi-lastcall does not contain private Chat, Telegram, or harness code. Instead, an installation may register one argv-only callback that runs after the new session's local artifacts and seat have verified:

```sh
kimi-lastcall configure \
  --cwd "$HOME/my-kimi-resident" \
  --tmux-socket kimi-resident \
  --tmux-session kimi-resident \
  --on-adopt-json '["/absolute/path/to/rebind-my-surfaces"]'
```

No shell is used. The callback receives only local environment bindings:

- `KIMI_LASTCALL_SESSION_ID`
- `KIMI_LASTCALL_SESSION_DIR`
- `KIMI_LASTCALL_WIRE_PATH`
- `KIMI_LASTCALL_MANAGED_CWD`

Make the callback idempotent. A non-zero exit leaves the pending marker in place and keeps the operation fail-closed.

## Security and failure semantics

The two halves intentionally fail differently:

| Area | Failure rule | Why |
| --- | --- | --- |
| Stop/Relay gate | fail open, audit the error | a broken helper must not imprison a live TUI |
| `/new` controller and adoption | fail closed, preserve pending state | an unverified new session must not inherit write surfaces |

Additional boundaries:

- HTTP binds only `127.0.0.1`; Host and browser Origin are checked.
- The control token is an owner-only 0600 file; bearer auth is reserved for the local hook, browser writes use a same-origin HttpOnly cookie.
- Authority files reject symlinks, wrong owners, wrong modes, unknown fields, and retargeting.
- The pending SessionStart identity is created with `O_EXCL`; a second session cannot overwrite it.
- tmux is invoked with argv, never a shell. The only terminal payload is fixed `/new`.
- Automatic requests are bound to the current raw session, cwd, tmux socket/session/pane, threshold, required files, and done marker; public status exposes only the digest.
- The controller persists the request before replying and starts the worker only after flushing the HTTP response. The worker must then acquire the Stop hook's kernel lease, so `/new` cannot re-enter the process that queued it.
- A persisted switch-in-progress marker means `/new` may already have been sent. After restart it is never sent again automatically; the panel reports that manual recovery is required.
- Public status includes file readiness and session digests, never handoff bodies or raw session ids.
- Audit entries contain decisions/error classes only, not transcript or handoff content.
- If the controller was offline when SessionStart occurred, restarting it revalidates the frozen pending marker before adopting; it never clears the marker merely because it restarted.

## Commands

```text
kimi-lastcall install [--dry-run]   install Stop + SessionStart hooks
kimi-lastcall uninstall             remove only the managed hook block
kimi-lastcall configure ...         configure the managed seat/controller
kimi-lastcall serve                 run the authenticated local Web panel
kimi-lastcall status                show gate + controller state
kimi-lastcall template              print the handwritten Relay template
kimi-lastcall done                  mark the bound session handoff complete
kimi-lastcall set-trigger TOKENS    set an exact 50k-step threshold
kimi-lastcall set-mode MODE         choose manual or automatic switching
```

## Development

```sh
python3 -m pytest -q
```

Tests use temporary directories and synthetic identities only. The suite includes fake executable tmux end-to-end tests for both paths: one human-confirmed and one real Stop hook → HTTP queue → background worker → fixed `/new` → `SessionStart` adoption chain. Both verify the final binding and receipt.

## Uninstall

```sh
kimi-lastcall uninstall
```

This removes exactly the managed Kimi hook block. Controller state is deliberately not auto-deleted; it may contain the only diagnosis of an interrupted switch. Review and remove `~/.local/state/kimi-lastcall` yourself after the managed seat and controller are stopped.

## Authorship and acknowledgements

This project was built by 山山 (Shan), 阿衡 (Aheng), 阿问 (Awen), and 阿朔 (Ashuo) through human-led collaboration. The latter three contributed through Codex, Claude, and Kimi Code respectively as substantive AI co-authors; see [AUTHORS.md](AUTHORS.md). Conceptual influences shared by Forge — 离落, cmh-lite — 咲咲, lmc5-session-carryover — 蛋, and anticipation — 里奈 are credited in [ACKNOWLEDGEMENTS.md](ACKNOWLEDGEMENTS.md); no source code from those projects was copied.

## License

MIT. See [LICENSE](LICENSE).
