# ZCFG_READ_SRV — SAP-side setup (DEV → PRD)

A read-only Gateway service that lets the sap-mcp tools `read_config_table`,
`describe_config_table`, `condition_types`, `pricing_procedure` and
`account_determination` read customizing tables. Build it once in **DEV**,
test it, then transport it to **PRD**. All objects go into the existing
package **`<your package>`** (any existing customer package, e.g. `ZFI`).

What it does:
- Reads **any** table or database view, with the caller's own SAP user.
- Runs `AUTHORITY-CHECK S_TABU_NAM` (activity 03, table name) on every read.
- Rejects WHERE clauses that try to reach other tables (sub-queries, JOIN,
  UNION, host variables). Always refuses USR02, USH02, USRPWDHISTORY,
  RFCDES, SSF_PSE_D, RSECTAB and SECSTORE.
- Returns at most 5000 rows per call (the MCP asks for at most 1000).

**Authorizations:** every call runs under the caller's own SAP user.

- **Calling the service at all** needs S_SERVICE for `ZCFG_READ_SRV`. Gateway checks this
  before any code runs, so it cannot be waived in the class. SAP_ALL users have it. Everyone
  else needs one small role (see "Reader role for users without SAP_ALL" below). The role
  opens the door only; it grants no data.
- **Table reads** check S_TABU_NAM (activity 03, table name) for each table.
- **Reports** (`REPORT_INFO`, `RUN_REPORT`) run only for a user who could start them in SAP
  GUI: S_TCODE for the transaction named, or for any transaction that starts the program,
  or SA38 / SE38. The report's own checks (company code, account type, ...) then run as usual.
  So a purchase user with FBL1N in their role can run FBL1N here, and nothing more.
- **Source code** needs S_DEVELOP display.

### Reader role for users without SAP_ALL

One role for all Claude users, created once in PFCG:

1. PFCG, role `Z_CLAUDE_READ`, description "Claude SAP connector: call ZCFG_READ_SRV", Save.
2. Tab *Menu*: the arrow next to *Transaction*, choose *Authorization Default*. In the popup:
   *TADIR Service*, Program ID `R3TR`, Object Type `IWSG`, name `ZCFG_READ_SRV_0001`. Repeat
   with Object Type `IWSV`, name `ZCFG_READ_SRV` (the entry ending in `0001`).
3. Tab *Authorizations*: *Change Authorization Data*. Only S_SERVICE should appear, green.
   Save, *Generate* the profile.
4. Tab *User*: add the users, *User Comparison*.

Do this in the system where roles are maintained (transport it if roles travel from DEV).

---

## 1. Transport request (DEV)

Create a **workbench** request in SE09/SE10, or let the first save below
prompt for one. Use package `<your package>` and this same request for every object
in steps 2–4, so everything travels to PRD together.

## 2. Gateway project (SEGW, DEV)

1. **SEGW** → Create Project: `ZCFG_READ`, description "Config table reader
   for Claude", package `<your package>`, *Service with SAP Annotations*.
2. **Data Model → Entity Types**: create two entity types. For each one,
   tick *Create related Entity Set*. Set the **ABAP Field Name** exactly as
   shown, because the class code relies on these names.

   **TableRow** (entity set `TableRows`)

   | Property | Key | Edm type   | ABAP field name |
   |----------|-----|------------|-----------------|
   | RowNo    | ✔   | Edm.Int32  | ROW_NO          |
   | Data     |     | Edm.String | DATA            |

   **TableField** (entity set `TableFields`)

   | Property    | Key | Edm type   | Max length | ABAP field name |
   |-------------|-----|------------|------------|-----------------|
   | FieldName   | ✔   | Edm.String | 30         | FIELD_NAME      |
   | Position    |     | Edm.Int32  |            | POSITION        |
   | KeyFlag     |     | Edm.String | 1          | KEY_FLAG        |
   | DataType    |     | Edm.String | 4          | DATA_TYPE       |
   | Length      |     | Edm.Int32  |            | LENGTH          |
   | Decimals    |     | Edm.Int32  |            | DECIMALS        |
   | CheckTable  |     | Edm.String | 30         | CHECK_TABLE     |
   | DataElement |     | Edm.String | 30         | DATA_ELEMENT    |
   | Description |     | Edm.String | 60         | DESCRIPTION     |

   Leave `Data` **without** a max length (it holds a whole row as JSON).
   Untick *Creatable / Updatable / Deletable* on both entity sets.

3. **Data Model → Function Imports**: create two.

   **ReadTable**: Return Type Kind *Entity Type*, Return Type `TableRow`,
   Return Cardinality `0..n`, Return Entity Set `TableRows`,
   **HTTP Method Type `GET`**. Parameters:

   | Name      | Edm type   | Max length | Nullable |
   |-----------|------------|------------|----------|
   | TableName | Edm.String | 30         | no       |
   | Where     | Edm.String | (none)     | yes      |
   | Fields    | Edm.String | (none)     | yes      |
   | MaxRows   | Edm.Int32  |            | yes      |
   | Skip      | Edm.Int32  |            | yes      |

   **GetTableFields**: Return Type Kind *Entity Type*, Return Type
   `TableField`, Cardinality `0..n`, Entity Set `TableFields`, **HTTP Method
   Type `GET`**. One parameter: `TableName`, Edm.String, length 30, not
   nullable.

4. **Generate Runtime Objects** (red/white wheel). Keep the default names:
   `ZCL_ZCFG_READ_MPC(_EXT)`, `ZCL_ZCFG_READ_DPC(_EXT)`, model
   `ZCFG_READ_MDL`, service `ZCFG_READ_SRV`. Use package `<your package>` and the
   same request.

## 3. The ABAP code (DEV)

1. Open **`ZCL_ZCFG_READ_DPC_EXT`** in SE24 (switch to *Source Code-Based*)
   or in Eclipse ADT.
2. Replace the whole source with [`ZCL_ZCFG_READ_DPC_EXT.abap`](ZCL_ZCFG_READ_DPC_EXT.abap)
   from this folder. Activate it.
3. If activation reports that `TS_TABLEROW` or `TS_TABLEFIELD` is unknown,
   open `ZCL_ZCFG_READ_MPC` and use the structure names generated there (`TS_...`). They
   follow the entity type names from step 2.2.

The code uses `pcre =` in `replace( )` / `find( )`, which needs ABAP 7.55 or
later (S/4HANA 2020+). On an older release, change `pcre` to `regex` in
`check_where`.

## 4. Register and activate the service (DEV)

1. **/IWFND/MAINT_SERVICE** → *Add Service* → System Alias `LOCAL` →
   technical service `ZCFG_READ_SRV` → *Add Selected Services*. Use package
   `<your package>` and the **same transport request**. That puts the service
   registration (IWSG/IWSV/IWOM) and the ICF node into the transport, so PRD
   needs no manual registration.
2. Check that the ICF node `/sap/opu/odata/sap/ZCFG_READ_SRV` is active
   (green) in the service's *ICF Node* button, or in SICF.
3. **The system alias is client-specific customizing.** The `LOCAL`
   assignment (table `/IWFND/C_MGDEAM`, view `/IWFND/V_MGDEAM`) is **not** in
   the workbench request. It did not carry over from the golden client to
   the dev client and had to be added there again in /IWFND/MAINT_SERVICE.
   the production client is not modifiable, so the alias has to reach it by
   **customizing request**:
   - In the development system's customizing client, check SE10 (tick *Customizing*) for a request
     that already holds `/IWFND/C_MGDEAM` / `ZCFG_READ_SRV_0001`.
   - If there is none: create a customizing request, then **SM30** →
     `/IWFND/V_MGDEAM` → select the `ZCFG_READ_SRV_0001` / `LOCAL` line →
     *Table View → Transport* → *Include in Request*.
   - Fallback: in SE10, add `R3TR TABU /IWFND/C_MGDEAM` to the object list
     with key `<client>ZCFG_READ_SRV_0001*`.

## 5. Test in DEV (/IWFND/GW_CLIENT)

Use HTTP method GET and log on to client **200**, the client sap-mcp uses for
dev:

```
/sap/opu/odata/sap/ZCFG_READ_SRV/GetTableFields?TableName='T030'&$format=json
/sap/opu/odata/sap/ZCFG_READ_SRV/ReadTable?TableName='T001'&Where=''&Fields='BUTXT,WAERS'&MaxRows=5&Skip=0&$format=json
/sap/opu/odata/sap/ZCFG_READ_SRV/ReadTable?TableName='T685T'&Where='SPRAS = ''E'' AND KAPPL = ''M'''&Fields='VTEXT'&MaxRows=10&Skip=0&$format=json
```

Each row comes back as `{"RowNo":1,"Data":"{\"BUKRS\":\"1000\",...}"}`.
Then check that the guards work. Each of these must return an error, even
for a SAP_ALL user:

```
ReadTable?TableName='USR02'&Where=''&Fields=''&MaxRows=1&Skip=0
ReadTable?TableName='T001'&Where='BUKRS IN ( SELECT BNAME FROM USR02 )'&Fields=''&MaxRows=1&Skip=0
ReadTable?TableName='T001'&Where=''&Fields='NOSUCHFIELD'&MaxRows=1&Skip=0
```

Then test from Claude. Restart Claude Desktop (the MCP side is already
installed on your PC) and ask it to *switch to dev and show company codes
from T001 using read_config_table*. Also try *condition_types for
application M* and *account_determination WRX*.

## 6. Transport to PRD

1. **SE09/SE10**: check that the request contains:
   - the classes `ZCL_ZCFG_READ_*`
   - the SEGW project (`IWPR ZCFG_READ`) and the model (`IWMO`)
   - the service (`IWSV`, `IWSG`, `IWOM`)
   - the ICF node (`SICF`)

   Release it.
2. Import into PRD (via QAS if your landscape has one) with STMS.
3. Import the **customizing request with the system alias** (step 4.3) right
   after the workbench request. Client 100 is not modifiable, so the alias
   cannot be added there by hand. Then, in the production client, run
   **/IWFND/MAINT_SERVICE**, find `ZCFG_READ_SRV` and check that the ICF
   node is active and the system alias is `LOCAL`. If the service is
   missing, it was not in the transport. Add it there with *Add Service* (it
   needs a client open for changes, or a Basis user).
4. Test with one GW_CLIENT URL from step 5, in client 100.

Until the service is active in a system, the MCP tools return a clear
message: "ZCFG_READ_SRV is not available on this SAP system". The other
tools keep working.

---

## 7. Update: the Compute function (calculations, reports, attachments)

**One SEGW change, one class paste, one workbench transport.** This adds
everything new in a single function import on the existing service. The ICF
node and system alias in PRD stay as they are, so there's **no customizing
transport and no ICF activation** this time. Future calculations need only a new
version of the class, with no SEGW step at all.

What `Compute(Operation, Params)` does inside SAP:

| Operation | Computed in SAP |
|---|---|
| `AGGREGATE` | SUM / AVG / MIN / MAX / COUNT / COUNT DISTINCT with GROUP BY, ORDER BY, top N, on any table |
| `DUPLICATES` | Groups of rows sharing the same values on chosen fields (e.g. RBKP vendor + reference + amount) |
| `TRIAL_BALANCE` | From ACDOCA per G/L account: opening, period debit, credit, closing, with account names |
| `AGEING` | Vendor / customer open items at a key date, per partner, in due-date buckets |
| `GRIR` | EKBE goods receipt vs invoice receipt per PO item (items that differ) |
| `LIST_REPORTS` / `REPORT_INFO` / `RUN_REPORT` | Find SAP GUI reports, show their selections and variants, run them (ALV output captured as data) |
| `LIST_ATTACHMENTS` / `GET_ATTACHMENT` | GOS attachments of a document |
| `GET_SOURCE` | ABAP source of a program, include, class, interface or function module (or the program behind a tcode), active version, after an S_DEVELOP display check; customer includes on request |
| `GET_SMARTFORM` | A Smart Form's definition as XML (`CL_SSF_FB_SMART_FORM->XML_DOWNLOAD`, the Form Builder's download) plus its generated function module; a pattern lists forms |
| `READ_TEXT` | SAPscript / SO10 texts via `READ_TEXT`, with the format column; a pattern lists text headers |
| `PROGRAM_PARTS` | A program's text pool (`READ TEXTPOOL`), GUI statuses / function texts / menus / titles (`RS_CUA_INTERNAL_FETCH`) and screens (D020S / D020T) |

**SEGW (DEV), project `ZCFG_READ`:**

1. **Data Model → Function Imports** → create **`Compute`**:
   - Return Type Kind *Entity Type*
   - Return Type `TableRow`
   - Cardinality `0..n`
   - Entity Set `TableRows`
   - **HTTP Method Type GET**

   Parameters:

   | Name      | Edm type   | Max length | Nullable |
   |-----------|------------|------------|----------|
   | Operation | Edm.String | 30         | no       |
   | Params    | Edm.String | (none)     | yes      |

   No new entity type is needed: results come back in the existing `TableRows`.
2. **Generate Runtime Objects** on a **new** workbench request, package `<your package>`.
3. **SE24** → `ZCL_ZCFG_READ_DPC_EXT` → **Source Code-Based** → replace the whole
   source with the updated [`ZCL_ZCFG_READ_DPC_EXT.abap`](ZCL_ZCFG_READ_DPC_EXT.abap)
   → **Ctrl+F2** (syntax check) → **Ctrl+F3** (activate). If the check shows
   errors, send a screenshot and Claude will fix the code.
4. **Clear the metadata cache:** **/IWFND/CACHE_CLEANUP** and **/IWBEP/CACHE_CLEANUP**
   (tick *all models*, F8).
5. **Test in /IWFND/GW_CLIENT** (client 200):
   ```
   /sap/opu/odata/sap/ZCFG_READ_SRV/Compute?Operation='AGGREGATE'&Params='{"TABLE_NAME":"T001","GROUP_BY":"LAND1"}'&$format=json
   ```
   Then tell Claude, and it will test every operation end to end on dev.
6. **Transport to PRD.** Workbench request only. After the import, run the two
   cache clean-ups in PRD as well.

**Adding an operation later** (as `GET_SOURCE`, `GET_SMARTFORM` and `READ_TEXT` were): paste the new
class source in SE24, activate, run the two cache clean-ups, transport the class. No SEGW step.

Reports run with `SUBMIT ... AND RETURN` under the user's own SAP user. Any
executable report can be run, including ones that post. Claude is instructed
to ask before running a report that may change data. Reports that open
popups can't run this way, and a run has to finish within about 55 seconds.

GOS object keys, for reference:

| Document | GOS object type | Key |
|---|---|---|
| MIRO invoice | `BUS2081` | invoice no. (10) + fiscal year |
| MIGO material document | `BUS2017` | material doc no. (10) + year |
| Purchase order | `BUS2012` | PO number (10) |
| FI document | `BKPF` | company code (4) + doc no. (10) + fiscal year |
