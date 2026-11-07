"""GOS attachments ("Services for Object" files) via ZCFG_READ_SRV.

SAP keeps attachments of business documents in SAPoffice, linked to the
document through table SRGBTBREL. ZCFG_READ_SRV's Compute function has two
operations for them:

* ``LIST_ATTACHMENTS`` — what is attached (no content).
* ``GET_ATTACHMENT`` — one file, base64. SAP only returns it if it really is
  linked to the object named in the call.

File content never goes into the conversation: a 75 KB PDF is ~100 KB of
base64, past the per-call cap, and useless to read as text anyway. Files are
written to a folder on this PC and the answer lists their paths.
"""
from __future__ import annotations

import base64
import os
import re
from pathlib import Path
from typing import Any, Optional

from .compute import compute
from .sap_client import SapODataClient

#: Friendly document kinds -> (GOS object type, key builder).
#: Keys are the SAP object keys as stored in SRGBTBREL-INSTID_A.
KINDS: dict[str, tuple[str, str]] = {
    "invoice":     ("BUS2081", "MIRO supplier invoice: number + fiscal year"),
    "material_doc": ("BUS2017", "MIGO material document: number + year"),
    "po":          ("BUS2012", "Purchase order: number"),
    "fi":          ("BKPF", "Accounting document: company code + number + fiscal year"),
}

_SAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def _num(value: str, width: int = 10) -> str:
    """Document numbers are stored with leading zeros when numeric."""
    v = (value or "").strip()
    return v.zfill(width) if v.isdigit() else v.upper()


def gos_key(kind: str, number: str, year: str = "", company_code: str = "") -> tuple[str, str]:
    """GOS (object type, key) for a friendly document kind."""
    kind = kind.strip().lower()
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)} (or pass object_type + object_key).")
    otype = KINDS[kind][0]
    if kind == "po":
        return otype, _num(number)
    if not year.strip():
        raise ValueError(f"{kind} needs year (the fiscal / material document year).")
    if kind == "fi":
        if not company_code.strip():
            raise ValueError("fi needs company_code, e.g. 1000.")
        return otype, f"{company_code.strip().upper():<4}{_num(number)}{year.strip()}"
    return otype, f"{_num(number)}{year.strip()}"


def default_folder() -> Path:
    override = os.environ.get("SAP_MCP_ATTACHMENTS_DIR")
    return Path(override) if override else Path.home() / "Downloads" / "SAP attachments"


def _shape(row: dict) -> dict:
    out = {
        "doc_id": row.get("DOC_ID", ""),
        "file_name": row.get("FILE_NAME", ""),
        "extension": row.get("EXTENSION", ""),
        "size_bytes": int(row.get("SIZE_BYTES") or 0),
        "kind": row.get("RELATION", ""),
        "created_by": row.get("CREATED_BY", ""),
    }
    when = str(row.get("CREATED_ON") or "")
    if len(when) >= 8 and when[:8].isdigit():
        out["created_on"] = f"{when[6:8]}.{when[4:6]}.{when[0:4]}"
    return out


def list_attachments(client: SapODataClient, object_type: str, key: str) -> list[dict]:
    rows = compute(client, "LIST_ATTACHMENTS", {"OBJECT_TYPE": object_type.strip().upper(), "OBJECT_KEY": key})
    return [_shape(r) for r in rows]


def download(
    client: SapODataClient,
    object_type: str,
    key: str,
    doc_id: str,
    folder: Path,
) -> dict:
    rows = compute(client, "GET_ATTACHMENT", {
        "OBJECT_TYPE": object_type.strip().upper(), "OBJECT_KEY": key, "DOC_ID": doc_id,
    })
    if not rows:
        raise ValueError(f"SAP returned no content for {doc_id}.")
    row = rows[0]
    info = _shape(row)
    data = base64.b64decode(row.get("CONTENT") or "")
    name = _SAFE.sub("_", info["file_name"] or doc_id).strip(" .") or doc_id
    if info["extension"] and not name.lower().endswith("." + info["extension"].lower()):
        name = f"{name}.{info['extension']}"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    n = 2
    while target.exists() and target.read_bytes() != data:
        target = folder / f"{Path(name).stem} ({n}){Path(name).suffix}"
        n += 1
    target.write_bytes(data)
    info.update(saved_to=str(target), bytes_written=len(data))
    return info


def get_attachments(
    client: SapODataClient,
    *,
    kind: str = "",
    number: str = "",
    year: str = "",
    company_code: str = "",
    object_type: str = "",
    object_key: str = "",
    download_files: bool = True,
    folder: Optional[str] = None,
) -> dict[str, Any]:
    if object_type and object_key:
        otype, key = object_type.strip().upper(), object_key.strip()
    else:
        otype, key = gos_key(kind, number, year, company_code)
    files = list_attachments(client, otype, key)
    out: dict[str, Any] = {
        "object_type": otype,
        "object_key": key,
        "count": len(files),
        "attachments": files,
    }
    if not files:
        out["note"] = f"No GOS attachments on {otype} {key}."
        return out
    if download_files:
        where = Path(folder) if folder else default_folder() / f"{otype}_{key}"
        out["attachments"] = [
            download(client, otype, key, f["doc_id"], where) if f["kind"] == "ATTA" else f
            for f in files
        ]
        out["folder"] = str(where)
    return out

