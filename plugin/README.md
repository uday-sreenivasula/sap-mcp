# sap: SAP connector for Claude (tables, in-SAP calculations, reports, attachments)

Lets Claude work with your company's SAP S/4HANA directly. Production is the default; Claude switches to the development system only when asked.

- **Any SAP table**, like SE16 (`read_sap_table`, `describe_sap_table`). This is Claude's first choice for reading data.
- **Calculations done inside SAP**, so only the result comes back:
  - totals with GROUP BY and top-N lists (`aggregate_sap_table`)
  - duplicate checks (`find_duplicates`)
  - trial balance by period from ACDOCA (`trial_balance_by_period`)
  - vendor / customer ageing (`open_items_ageing`)
  - GR/IR differences (`grir_differences`)
- **Your SAP GUI reports:** find them, see their selections and variants, and run them. The output is exported to Excel in `Downloads\SAP reports` (`run_sap_report`).
- **Attachments** of invoices, POs, material and FI documents, saved to `Downloads\SAP attachments` (`get_attachments`).
- **The standard OData tools** (GL, AP/AR, sales, purchasing, stock, GST, pricing), used where they're better.

It doesn't post or change anything itself. The one exception is running an SAP report that posts, and Claude asks you before running one. Writing to SAP is a separate, private plugin (`sap-write`).

## Usage controls (so SAP questions don't eat the Claude plan)
These are set centrally in the plugin's settings (`.mcp.json`), so everyone gets them:

| Control | Default | What it does |
|---|---|---|
| Large results go to Excel | over ~3k tokens (`SAP_MCP_INLINE_CHARS=12000`) | The rows are saved to `Downloads\SAP exports`; Claude gets the row count, columns, a 10-row preview, amount totals and the file path |
| Allowance per conversation | ~50k tokens of SAP data (`SAP_MCP_SESSION_BUDGET_CHARS=200000`) and 80 calls (`SAP_MCP_MAX_CALLS`) | Going past either opens a window on **your** screen asking whether to allow more. Claude cannot extend it by itself. |
| Compact tool set | `SAP_MCP_TOOLSET=core` | 22 tools (~7k tokens per conversation instead of ~18k). `full` brings back the 22 older special-purpose OData tools. |
| Usage log | `%USERPROFILE%\.sap-mcp\usage_log.jsonl` | One line per SAP call: tool, size, time, saved to a file or not |

Ask Claude for **"SAP token budget"** to see this conversation's usage per tool.

## Install
1. Install the **sap** plugin from the organisation plugin library.
2. Start a new Claude session. **The first start takes 1–3 minutes.** It checks for Python and installs it for your user if it's missing (Windows: Python 3.12 via winget; Mac: a private copy of Python 3.12 inside the connector's own folder, no admin rights needed), then installs the connector's packages.
3. Ask Claude: **"log in to SAP"**. A small login window opens on your screen. Type your **own** SAP user and password, then click **Log in**. Your login is tested against SAP and stored in Windows Credential Manager (Mac: the Keychain) for your computer user only.
   - **The password never goes through the chat**, and Claude never sees it.
   - SAP applies your own authorizations, so Claude sees exactly what your user can see in SAP.
4. Test it: ask Claude **"whoami in SAP"**.

If your SAP password changes, ask Claude to "log in to SAP" again. For dev, say "log in to SAP dev".

## Attachments
Ask, for example, *"get the attachments of MIRO 5100067620/2026"*. Claude lists the files and saves them to `Downloads\SAP attachments\<document>`. It then reads the saved file if you ask it to. File contents are never pasted into the chat.

## Requirements and troubleshooting
- **Windows or macOS, with network access to your SAP Gateway** (office network or VPN for internal systems).
- **Your SAP systems:** the plugin build reads them from `site.json` (copy `site.example.json`). Without it, the login window asks each user for the server address and client.
- **Configuration tables and attachments** need the custom SAP service `ZCFG_READ_SRV`, which you build once with the guide in `sap_side/`.
- **Setup log:** Windows `%LOCALAPPDATA%\sap-mcp-plugin\setup.log`; Mac `~/Library/Application Support/sap-mcp-plugin/setup.log`.
- **To force setup to run again,** delete the `ready-*` file in that folder. It also re-runs by itself if the Python it was set up with is removed.
- **Logins** live in `%USERPROFILE%\.sap-mcp\config.json` (no password) and Windows Credential Manager or the Mac Keychain (entries `sap-mcp` and `sap-mcp:dev`). They're shared with the older installer version, so existing logins carry over.
