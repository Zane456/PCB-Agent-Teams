---
name: setup
description: >-
  First-run setup of a freshly cloned workspace — locale, toolchain, USER.md. Invoke ONLY when root `USER.md` is missing, or the user asks to set up / configure / 初始化 the workspace. A filled `USER.md` means setup is done: never invoke.
---

# Setup — first run only

## Preconditions (check all three before Step 1)

**1. `USER.md` must be absent.** If it exists, `diff USER.md USER.md.example` first. Identical, or `§0 所属地`
still `[待填]`, means someone ran the manual `cp` and never filled it in — the gate reads "configured"
while nothing is. Report that, then run this guide normally and overwrite the stub at Step 4 (no `rm`
needed — it holds nothing). Never guess a country from it.
A genuinely filled `USER.md` → stop, say so in one line, do nothing else.

**2. Runtime is Claude Code.** If you are any other agent (Codex, Copilot, Cursor, Gemini …), say so
before Step 1: nothing here is wired for you — routing table and skill directory use Claude Code
conventions, and the skills shell out to `kicad-cli`, a project venv and distributor APIs that other
agents sandbox differently. Point at `README.md` → *Using another agent* and let them decide. Never
proceed as if the runtime were supported.

**3. Language.** Step 1 is the only English message. Step 2 onward, reply in the user's language.

## Step 1 — one question, nothing else

> Which country are you in? (Reply in your own language.)

Alone. No preamble, no setup instructions — the user may not read English yet. One line buys both the
**locale** (Phase 2 routing) and the **language** (whatever they answer in).

If their opening message already showed their language, that half is answered: ask the same country
question *in their language*. Still one question, still nothing else.

## Step 2 — locale verdict

Switch to their language. Lead with the constraint so this never reads as a limit on the whole workspace:

> Locale binds **exactly one phase** — Phase 2, part selection, because it decides which distributor
> warehouse is actually reachable for you. Phases 0, 1, 2.5, 3, 3.5, 4, 4.5, 5 are locale-neutral.

| Answer | Skill | Say |
| --- | --- | --- |
| Japan | `component-selecting-JP` | Ready today. Needs one free DigiKey key (2 env vars); Mouser/element14 optional. |
| China mainland | `component-selecting-CN` | Ready today. **Zero API keys** — LCSC public endpoints. |
| anything else | none | No skill for that country yet → the three options below. |

### Unimplemented locale — three honest options

Never silently reuse another locale's skill. Present all three, recommend by effort:

🅰  **Hand-pick parts, skip the skill.** Write the MPN list yourself, hand it to `component-preparing`.
Zero setup; every other phase (schematic, ERC, SPICE, PCB, DRC, EMC, thermal, Gerber) runs untouched.
Fastest, costs nothing.

🅱  **Build `component-selecting-<XX>`.** The engine is already locale-parameterised, so a port is a thin
shell — **but only if the region has a distributor with a public API** (DigiKey / Mouser / LCSC are the
only ones the engine speaks). No API distributor → say so, go 🅰. Only if they pick 🅱, read
`references/new_locale.md`.

🅲  **Use the nearest implemented locale, verify stock yourself.** Cheap, but stock/price/lifecycle come
from the wrong warehouse — feasibility passes only, never ordering.

## Step 3 — toolchain check

Report a pass/fail checklist. Install nothing without asking.

```bash
uname -s                                                  # Darwin expected
python3.12 --version                                      # 3.12 only; 3.13 / 3.14 unsupported
command -v kicad-cli || ls /Applications/KiCad/KiCad.app  # KiCad 10
command -v ngspice                                        # brew install ngspice
ls .venv/bin/python 2>/dev/null                           # venv already built?
```

**OS — say it before they invest time.** Developed and tested on **macOS** only. Linux untested but
plausible (scripts probe `PATH` / Homebrew / Snap / Flatpak for `kicad-cli`). Windows will hit path
assumptions; WSL is the realistic route.

Missing pieces → give the exact command from `README.md` → *Prerequisites*. Show these, do not run them
(slow, network-bound):

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium   # JP pipeline only (DigiKey probing)
```

## Step 4 — write the config files

Reversible only; refuse to overwrite a target that already exists — **except a confirmed stub**. A
`USER.md` byte-identical to `USER.md.example` holds nothing to lose, so overwrite it in place rather than
demanding `rm` first. Never overwrite a `USER.md` that has any real content, and never touch an existing
`.env`.

**Always reach this step, whatever the locale.** `USER.md` is what retires this guide — finish without
writing it and the gate fires again next session, trapping the user in setup. An unimplemented locale is
still a locale: record it truthfully.

1. `cp USER.md.example USER.md`, then fill from this conversation:
   - **§0 所属地** — the country as the user said it, even with no skill covering it. Never substitute a
     neighbour to make routing "work". If unimplemented, append the option they picked, e.g.
     `德国（无对应 skill — 走手工选品 → component-preparing）`.
   - **§0 沟通语言 + §5 语言** — both, same value. Filling only §5 leaves the summary table mismatched.
   - **§3 焊接能力** — ask now, one question: *smallest package you can hand-solder, and do you have hot
     air / reflow?* Gates part selection directly (a QFN pick is useless to an iron-only bench).
   - Everything else stays `[待填]` — skills ask when relevant.
2. Keys, by locale: CN → none, skip `.env` entirely. JP (or any DigiKey-based locale) →
   `cp .env.example .env`, name the two variables, link the free registration. Never write or echo a key.

## Step 5 — close out, and retire

> Setup is done. This guide is gated on `USER.md` being absent, so it will not run again.
> To delete it outright: `rm -rf .claude/skills/setup`

Then hand off: `project-init` for a new board, `circuit-design` if they already know the topology.

## Do not

- ❌ Send anything but the single question in Step 1
- ❌ Keep speaking English after they answered in another language
- ❌ Silently fall back to JP or CN for an unlisted country
- ❌ Promise a `component-selecting-<XX>` skill without passing the feasibility gate
- ❌ Run `pip install` / venv builds / `playwright install` without asking
- ❌ Overwrite a `USER.md` holding real content, or any existing `.env` (a stub `USER.md` is the one exception)
- ❌ Finish the conversation without writing `USER.md`
