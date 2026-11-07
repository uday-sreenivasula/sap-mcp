# SAP connector for Claude

A read-only MCP server that lets Claude work with SAP S/4HANA directly: read any table the way SE16 does,
let SAP do the arithmetic (totals, trial balance, ageing, GR/IR, duplicate checks), run your SAP GUI reports
and export them to Excel, and fetch document attachments. Built for finance teams, with token efficiency as
the main design goal. It runs as a Claude plugin on Windows and macOS.

## What it can do

| Area | Tools |
|---|---|
| Table reads | `read_sap_table`, `describe_sap_table`, `aggregate_sap_table` (SUM / COUNT / MIN / MAX / AVG, GROUP BY, top N) |
| Calculations inside SAP | `trial_balance_by_period` (ACDOCA), `open_items_ageing`, `grir_differences`, `find_duplicates` |
| SAP GUI reports | `find_sap_reports`, `sap_report_info`, `run_sap_report` (ALV output saved to Excel) |
| Attachments | `get_attachments` (GOS files saved to Downloads, never pasted into the chat) |
| Customizing shortcuts | `condition_types`, `pricing_procedure`, `account_determination` |
| OData | `list_services`, `get_service_metadata`, `query_entity` |
| Session | `sap_login`, `whoami`, `list_environments`, `switch_environment`, `token_budget` |

## How it works

- **Python MCP server** (`src/sap_mcp`, `mcp` SDK 2.x) talking to SAP Gateway over OData v2, GET only.
- **One small custom Gateway service, `ZCFG_READ_SRV`**, does table reads and calculations inside SAP
  (`ReadTable`, `GetTableFields`, `Compute`). ABAP source and a step-by-step SEGW / transport guide are in
  [`sap_side/`](sap_side/README.md). New calculations are new ABAP operations only; no SEGW change.
- **Claude plugin** (`plugin/`): `launch.cmd` / `launch.sh` install a private Python environment on first
  start; `tools/build_plugin.py` assembles the folder you publish to your Claude organisation.

## Security model

- **Read-only.** The client refuses POST / PUT / PATCH / DELETE. (`run_sap_report` runs real reports; the tool
  text tells Claude to ask before running anything that could post.)
- **Everyone logs in as themselves.** `sap_login` opens a small window; the password never passes through the
  chat and is stored in the OS keychain (Windows Credential Manager or the macOS Keychain). SAP authorizations
  decide what each user sees.
- **No credentials or server addresses in the code.** Your systems live in a git-ignored `site.json`.
- **Usage controls:** large results are saved to Excel instead of being sent to Claude; each conversation has a
  data allowance that only the user can extend (a click in a window); every call is logged locally.

## Set-up

1. **SAP side:** build `ZCFG_READ_SRV` with the guide in [`sap_side/README.md`](sap_side/README.md) and
   activate it in your systems.
2. **Your systems:** copy `site.example.json` to `site.json` and fill in your Gateway addresses, clients and a
   one-line company note (company codes, chart of accounts, currency). Without it, the login window asks each
   user for the server address.
3. **Build the plugin:** `python tools/build_plugin.py` creates `../sap-plugin`, with your `site.json` inside.
4. **Publish** that folder to your Claude organisation's plugin library, or add it as a local plugin.
5. **Use it:** in Claude, say "log in to SAP", then ask your question.

Users need network access to your SAP Gateway (office network or VPN for internal systems).

## Development

```
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"      # macOS: .venv/bin/pip
.venv\Scripts\python -m pytest              # offline, SAP calls are mocked
```

Design rules and lessons learned are in [`CLAUDE.md`](CLAUDE.md).
