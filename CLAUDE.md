# sap-mcp: read-only SAP S/4HANA MCP server

Python MCP server (`mcp` SDK 2.x, `MCPServer`) that gives Claude read-only access to SAP S/4HANA over
OData v2. Built for finance workflows: GL, AP/AR, sales, purchasing, materials. **Primary design goal:
token efficiency.** Company-specific settings (SAP addresses, clients, company note) live in a git-ignored
`site.json`; internal notes, if any, in a git-ignored `CLAUDE.local.md`.

## Layout

- `src/sap_mcp/server.py`: all MCP tools (`@mcp.tool()`), response shaping, usage controls, CLI entry (`main`).
- `src/sap_mcp/sap_client.py`: `SapODataClient` (httpx). GET only; POST/PUT/PATCH/DELETE raise `ReadOnlyViolation`. Every request goes through `_send()` (hard wall-clock deadline); `$metadata` is disk-cached.
- `src/sap_mcp/config_tables.py`: table reader and customizing shortcuts (`condition_types`, `pricing_procedure`, `account_determination`) over the custom `ZCFG_READ_SRV`.
- `src/sap_mcp/compute.py`: calculations done IN SAP via `ZCFG_READ_SRV`'s single `Compute(Operation, Params)` function import (AGGREGATE, DUPLICATES, TRIAL_BALANCE, AGEING, GRIR, reports, attachments). Params = JSON with UPPER-CASE keys. **New SAP-side calculations = a new ABAP operation only, no SEGW change.**
- `src/sap_mcp/reports.py`: `find_sap_reports` / `sap_report_info` / `run_sap_report`: SUBMIT AND RETURN; ALV captured via `CL_SALV_BS_RUNTIME_INFO`, saved to Excel in `Downloads/SAP reports`. The tool text tells Claude to ask before running reports that could post.
- `src/sap_mcp/source.py`: `get_sap_source`: ABAP code via Compute `GET_SOURCE` (program / include / class / interface / function module / tcode), saved as `.abap` under `Downloads/SAP source`; inline up to `SAP_MCP_SOURCE_INLINE_CHARS` (10k, below the spill limit). `get_program_parts`: text pool + CUA (RS_CUA_INTERNAL_FETCH tables passed through, keys lower-cased, blanks dropped) + screens, saved as `<prog>.parts.json`.
- `src/sap_mcp/printforms.py`: `get_smartform` (form XML saved to `Downloads/SAP source/smartforms`, tolerant outline parser: any element with `NODETYPE` + `INAME` is a node) and `read_sap_text` (READ_TEXT). Read-only; editing forms belongs to sap-write.
- `src/sap_mcp/attachments.py`: `get_attachments`: GOS files saved to `Downloads/SAP attachments`; content is never returned to the model.
- `src/sap_mcp/login_dialog.py`: `sap_login` tool: Tk login window in a child process; the password never passes through the chat. Asks for the server address and client when the site settings don't name them.
- `src/sap_mcp/environments.py`: the `prod` / `dev` environments, filled from `site.json` (`SAP_MCP_SITE`, the package folder, or `~/.sap-mcp/site.json`), plus `SAP_<ENV>_<FIELD>` overrides. `site.example.json` shows the format.
- `src/sap_mcp/config.py`: per-environment config resolution (env vars > stored per-user login > site settings).
- `src/sap_mcp/credentials.py`: per-user login store: `~/.sap-mcp/config.json` plus the password in the OS keychain (keyring service `sap-mcp`, `sap-mcp:dev` for dev).
- `src/sap_mcp/migrate.py`: on start as the plugin, removes older stand-alone SAP servers from Claude Desktop's config (backup first) and stops their processes.
- `src/sap_mcp/ui.py`: shared window look: DPI awareness on Windows, aqua theme on macOS.
- `sap_side/`: ABAP source and the SEGW / transport guide for `ZCFG_READ_SRV`.
- `plugin/`: Claude plugin launchers and manifest. `tools/build_plugin.py` assembles `../sap-plugin` (the folder to publish) and copies `site.json` into it. `plugin.json`, `launch.cmd` and `launch.sh` VER must equal `__version__`.
- `tests/`: pytest + respx (no live SAP needed). `tests/site.test.json` holds example systems.

## Plugin start line (Windows + macOS from one `.mcp.json`)

Plugin MCP config has no per-OS keys, and Windows can't spawn a `.cmd` without cmd. `command` =
`${ComSpec:-/bin/sh}` (spelling `ComSpec`, Windows' real casing); args =
`["-c", "IFS=;exec sh $SAP_MCP_LAUNCH", "/c", "<root>\\launch.cmd"]`, env `SAP_MCP_LAUNCH=<root>/launch.sh`.
Windows: cmd ignores the tokens before `/c` and runs `launch.cmd`. macOS: sh runs the string, and `IFS=` keeps a
path with spaces whole. **Nothing before `/c` may contain a slash or the plugin path:** Claude passes the root
with forward slashes (`C:/Users/.../Claude-Data/...`) and cmd scans the whole line for switches, so
`/Claude-Data` would be read as `/C`. `tests/test_plugin_launch.py` guards this.

- `launch.sh` / `setup.sh` must stay LF (the build normalises them).
- Packages are installed per version (`lib-<ver>`); setup never deletes other versions' `ready-*` markers, so a
  Claude that hasn't restarted yet never re-runs setup over the package folder it is using.
- macOS setup: Python 3.10+ **with tkinter** from python.org or Homebrew, else a private python-build-standalone
  3.12 in `~/Library/Application Support/sap-mcp-plugin/python`; never `/usr/bin/python3`.
- Never build on a uv-managed Python: uv renames its folders on update.

## Design rules (keep these when changing tools)

1. **Read-only.** No write verbs or CSRF calls. Exception: `run_sap_report` can run any report (the tool text asks Claude to confirm with the user first).
   **Prefer table reads and SAP-side computation** (`read_sap_table`, `aggregate_sap_table`, `Compute` operations) over OData services and over adding rows up in Python; the server `instructions` say the same.
2. **No credentials or server addresses in the repo or MCP config.** Each user logs in as themselves; SAP authorizations decide what they see. Addresses come from `site.json` or the login window.
3. **Usage controls** (`src/sap_mcp/usage.py`, applied in `_safe`), kept for every new tool:
   - Results over `SAP_MCP_INLINE_CHARS` (12k) are **saved to Excel** (`Downloads/SAP exports`) with row count, columns, preview, amount totals and path.
   - Per-conversation allowance: `SAP_MCP_SESSION_BUDGET_CHARS` (200k) and `SAP_MCP_MAX_CALLS` (80). Extending it needs the user's click in a window (`confirm_dialog.py`, separate process). Tests must never open windows (conftest patches `usage.ask_to_continue`).
   - `SAP_MCP_TOOLSET=core` (default) hides the legacy special-purpose OData tools (`_LEGACY_TOOLS`): ~7k instead of ~18k tokens of tool definitions per conversation. New tools: short docstrings, small defaults.
   - Every call is logged to `~/.sap-mcp/usage_log.jsonl`. Limits are set in `plugin/.mcp.json`.
4. **Token efficiency:** every tool returns through `_safe()`, which strips `__metadata` / `__deferred` noise and enforces a per-call cap. Oversized responses are **refused, never truncated** (broken JSON makes the model re-fetch); the error says how to narrow the query.
5. Error payloads are JSON with `error` = `config` | `sap` | `timeout` | `invalid_argument` | `internal` | `too_large` | `budget_exhausted`. `sap` errors carry the SAP message text and a `hint`.
6. **Never wait on SAP unbounded.** Per request: 30 s wall-clock, enforced while streaming the body. Multi-page scans: `SAP_MCP_SCAN_SECONDS` (55 s, just under Claude Desktop's ~60 s tool timeout); they raise `SapTimeout` rather than return a partial sum.
7. `$metadata` and table field lists are cached in `~/.sap-mcp/cache/<kind>/<host>_<client>/` for `SAP_MCP_METADATA_TTL_DAYS` (7; 0 = off).
8. **Tool calls can run in parallel** (mcp 2.x runs sync tools in worker threads). Module-level state in `server.py` must be read and written under `_state_lock`.
9. Table-read `where` clauses are **ABAP Open SQL**, not OData. Shortcuts intersect wanted fields with the live DDIC field list, so a field missing on a release never breaks them. `account_determination` defaults to the chart(s) in `SAP_MCP_CHARTS` or the site settings; `"*"` = all charts.

## Deploying a custom OData service (DEV → PRD)

See `sap_side/README.md`. Lessons that apply to every SEGW service:

- Register the service in **/IWFND/MAINT_SERVICE** on the same workbench request as the classes; that carries the registration and the ICF node.
- **The system alias is client-specific customizing** (`/IWFND/C_MGDEAM`) and is NOT in the workbench request. If production is not modifiable, send it in a customizing request (SM30 → `/IWFND/V_MGDEAM` → Transport).
- After every import that changes the service model, run **/IWBEP/CACHE_CLEANUP** and **/IWFND/CACHE_CLEANUP** in the target system.
- The ICF node may arrive **inactive**; activate it in SICF or /IWFND/MAINT_SERVICE.
- Strict Open SQL on S/4: SEGW's `TT_*` table types are generic (no key) and can't be RETURNING types; dynamic `SELECT ... INTO CORRESPONDING FIELDS` fails if a selected column (e.g. `MANDT`) has no target field.

## Commands

```
python -m venv .venv && .venv\Scripts\pip install -e ".[dev]"   # dev setup (macOS: .venv/bin/pip)
.venv\Scripts\python -m pytest                                   # tests (offline, respx-mocked)
.venv\Scripts\python tools\build_plugin.py                       # build ../sap-plugin
```

- Bump `version` in `pyproject.toml`, `src/sap_mcp/__init__.py`, `plugin/.claude-plugin/plugin.json`, `plugin/launch.cmd` and `plugin/launch.sh` together.
- `mcp` SDK 2.x (`from mcp.server.mcpserver import MCPServer`) needs Python 3.10+.
