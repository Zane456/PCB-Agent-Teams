# Building `component-selecting-<XX>` for a new locale

Read this only when the user picks **option 🅱** in Step 2. Judge feasibility *before* promising anything.

## 1. Feasibility gate

The selection engine speaks **only DigiKey / Mouser / LCSC REST APIs**.

- Region covered by DigiKey or Mouser (US, most of EU, and many others) → **feasible**.
- No API distributor, catalogue is HTML-only → **say so plainly** and route the user back to option 🅰
  (hand-pick parts → `component-preparing`). Do not generate a shell skill that cannot query anything;
  a skill that returns nothing is worse than no skill, because it looks like it worked.

## 2. What the port actually costs

The engine is already locale-parameterised — one script plus one YAML, both under
`.claude/skills/component-selecting-JP/scripts/`:

| File | Role |
| --- | --- |
| `component_select.py` | shared engine; locale-driven, not JP-specific |
| `locale_mapping.yaml` | per-locale vendor priority, URLs, currency, stock thresholds, gate policy |

`locale_mapping.yaml` already ships `美国` / `欧盟` / `unknown` blocks (vendor URLs, DigiKey site + currency,
thresholds). For those regions most of the work is already done — what is missing is the thin shell skill.

`component-selecting-CN` is that thin shell: `SKILL.md` + a wrapper script injecting `--locale` +
`references/`. Copy its shape rather than the JP skill's (JP carries extra DigiKey-browser machinery).

## 3. Order of work

1. Read `.claude/skills/component-selecting-CN/SKILL.md` — the shell to mirror.
2. Read the header comments at the top of `locale_mapping.yaml` — they document every tunable key
   (`gate_policy`, `local_vendor_ids`, `lanes`, `fx_display`, `discover_sources`, `display`, …) plus the
   loader's constraints (no inline comments, no booleans, no list-of-dicts).
3. Add or amend the locale block for the country, including `aliases` covering how the user writes it.
4. Create `.claude/skills/component-selecting-<XX>/` mirroring the CN shell, pointing at that locale.
5. Update the Locale routing table in the workspace `CLAUDE.md` and in `README.md`.

## 4. Honesty rules

- Never claim a locale is supported before its shell skill exists and returns real candidates.
- `lifecycle_policy: unverified` is the correct, honest setting for vendors with no NRND data — do not
  fake a lifecycle field.
- If the new locale's only source is one vendor, set `gate_policy.min_local_sources: 1` and say so; do not
  quietly relax the JP two-source rule for everyone.
