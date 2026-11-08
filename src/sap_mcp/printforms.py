"""Print-form development aids: Smart Forms and SAPscript texts, read-only.

* ``get_smartform``: a Smart Form's definition as XML (what SMARTFORMS ->
  Utilities -> Download Form produces), saved to a file, with an outline of
  its pages, windows and nodes so the form can be understood without the
  graphical Form Builder. The generated function module's code is one
  ``get_sap_source`` call away.
* ``read_sap_text``: SO10 / SAPscript texts (READ_TEXT), or a list of them.

Editing or creating forms is a write into SAP and lives in the separate
sap-write plugin, not here.
"""
from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from .compute import compute
from .config_tables import MAX_ROWS
from .sap_client import SCAN_SECONDS, SapODataClient, SapODataError
from .source import INLINE_CHARS, _SAFE, default_folder

OUTLINE_CHARS = 8000
#: Smart Forms node types as the Form Builder shows them.
NODE_TYPES = {
    "PA": "PAGE", "WI": "WINDOW", "TE": "TEXT", "TA": "TABLE", "TM": "TEMPLATE", "CO": "CODE",
    "LO": "LOOP", "CM": "COMMAND", "GR": "GRAPHIC", "AD": "ADDRESS", "FL": "FOLDER",
    "AL": "ALTERNATIVE", "CP": "COMPLEX_SECTION", "EV": "EVENT", "TX": "TEXT_MODULE",
}
_FORM = re.compile(r"^[A-Z0-9_/*%]{1,30}$")


def _older(e: SapODataError, op: str) -> SapODataError:
    return SapODataError(
        e.status_code,
        f"The ZCFG_READ_SRV service on this SAP system is an older version without {op}. "
        "The updated ZCL_ZCFG_READ_DPC_EXT has to be activated in the development system and "
        "transported (sap_side/README.md, section 7).",
        e.body,
    )


def _compute(client: SapODataClient, op: str, params: dict) -> list[dict]:
    try:
        return compute(client, op, params, deadline=SCAN_SECONDS)
    except SapODataError as e:
        if "Unknown Operation" in str(e):
            raise _older(e, op) from e
        raise


# ------------------------------------------------------------- Smart Forms

def get_smartform(client: SapODataClient, name: str, *, folder: str = "", top: int = 200) -> dict:
    form = (name or "").strip().upper()
    if not _FORM.match(form):
        raise ValueError(f"{name!r} is not a valid Smart Form name or pattern (e.g. ZMR_INVOICE, ZMR*).")
    rows = _compute(client, "GET_SMARTFORM", {"REPORT": form, "MAX_ROWS": max(1, min(int(top), MAX_ROWS))})

    if "*" in form or "%" in form:
        forms = [{"form": r["NAME"], "title": r.get("TITLE") or None, "language": r.get("LANGUAGE"),
                  "changed_by": r.get("CHANGED_BY"), "changed_on": r.get("CHANGED_ON")}
                 for r in rows if r.get("KIND") == "FORM"]
        return {"pattern": form, "forms": forms, "count": len(forms),
                "hint": "Call get_smartform with one form name for its outline and XML."}

    sf = next((r for r in rows if r.get("KIND") == "SMARTFORM"), None)
    if not sf:
        raise SapODataError(404, f"SAP returned nothing for Smart Form {form}.", "")
    xml_text = sf.get("XML") or ""
    where = (Path(folder) if folder else default_folder() / "smartforms")
    where.mkdir(parents=True, exist_ok=True)
    target = where / f"{_SAFE.sub('_', sf['NAME'])}.xml"
    target.write_text(xml_text, encoding="utf-8")

    outline, stats = _outline(xml_text)
    text = "\n".join(outline)
    out = {
        "form": sf["NAME"], "title": sf.get("TITLE") or None, "language": sf.get("LANGUAGE"),
        "changed_by": sf.get("CHANGED_BY"), "changed_on": sf.get("CHANGED_ON"),
        "function_module": sf.get("FUNCTION") or None,
        "saved_to": str(target), "xml_chars": len(xml_text),
        "nodes": stats,
        "outline": text if len(text) <= OUTLINE_CHARS else text[:OUTLINE_CHARS] + "\n... (outline cut; read the XML file)",
        "hint": ("The XML file holds everything: window positions, text lines, program lines, conditions. "
                 "get_sap_source(function_module) reads the generated code; read_sap_table TNAPR shows "
                 "which output type / print program uses this form."),
    }
    return out


def _outline(xml_text: str) -> tuple[list[str], dict]:
    """Pages > windows > nodes from the form XML. Tolerant: any element with
    NODETYPE + INAME children is a node; nesting gives the hierarchy."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        return [f"(XML could not be parsed: {e})"], {}
    parent = {c: p for p in root.iter() for c in p}

    def child_text(el, tag):
        c = el.find(tag)
        return (c.text or "").strip() if c is not None else ""

    nodes = [el for el in root.iter() if el.find("NODETYPE") is not None and el.find("INAME") is not None]
    node_set = set(nodes)
    lines: list[str] = []
    stats: dict[str, int] = {}
    for el in nodes:
        depth, p = 0, parent.get(el)
        while p is not None:
            if p in node_set:
                depth += 1
            p = parent.get(p)
        ntype = child_text(el, "NODETYPE")
        label = NODE_TYPES.get(ntype, ntype)
        stats[label] = stats.get(label, 0) + 1
        extra = []
        texts = _own(el, node_set, lambda t: t.endswith("TDLINE"))
        code = _own(el, node_set, lambda t: t.endswith("ITEM") or t == "CODE_LINE")
        if texts:
            extra.append(f"{len(texts)} text lines: {texts[0][:60]!r}")
        if code and ntype == "CO":
            extra.append(f"{len(code)} program lines")
        cond = child_text(el, "CONDITION") or child_text(el, "CONDITIONS")
        if cond:
            extra.append("has condition")
        caption = child_text(el, "CAPTION")
        lines.append("  " * depth + f"{label} {child_text(el, 'INAME')}"
                     + (f": {caption}" if caption else "") + (f"  [{'; '.join(extra)}]" if extra else ""))
    return lines or ["(no nodes found; see the XML file)"], stats


def _own(el, node_set, match) -> list[str]:
    """Texts of matching descendants that belong to this node, not to a nested node."""
    out: list[str] = []
    for child in el:
        if child in node_set:
            continue
        if match(child.tag.upper()) and (child.text or "").strip():
            out.append(child.text.strip())
        out.extend(_own(child, node_set, match))
    return out


# ------------------------------------------------------------ SAPscript texts

def read_text(
    client: SapODataClient, name: str = "", *, object: str = "TEXT", id: str = "", language: str = "",
    top: int = 200,
) -> dict:
    rows = _compute(client, "READ_TEXT", {
        "TEXT_OBJECT": (object or "TEXT").strip().upper(), "TEXT_NAME": (name or "").strip(),
        "TEXT_ID": (id or "").strip().upper(), "LANGUAGE": (language or "").strip().upper(),
        "MAX_ROWS": max(1, min(int(top), MAX_ROWS)),
    })
    if not rows:
        return {"object": (object or "TEXT").upper(), "pattern": name, "texts": [], "count": 0}
    if rows[0].get("KIND") == "TEXT_HEADER":
        texts = [{"name": r["NAME"], "id": r.get("ID"), "language": r.get("LANGUAGE"),
                  "title": r.get("TITLE") or None, "changed_by": r.get("CHANGED_BY"),
                  "changed_on": r.get("CHANGED_ON")} for r in rows]
        return {"object": rows[0].get("OBJECT"), "pattern": name or "*", "texts": texts, "count": len(texts),
                "hint": "Call read_sap_text with one name (and id / language) for its lines."}
    t = rows[0]
    body = t.get("TEXT") or ""
    out = {"object": t.get("OBJECT"), "name": t.get("NAME"), "id": t.get("ID"), "language": t.get("LANGUAGE"),
           "title": t.get("TITLE") or None, "lines": int(t.get("LINE_COUNT") or 0),
           "format_note": "Each line starts with its SAPscript format: '*' paragraph, '/' new line, '=' continuation."}
    if len(body) <= INLINE_CHARS:
        out["text"] = body
    else:
        where = default_folder() / "texts"
        where.mkdir(parents=True, exist_ok=True)
        target = where / _SAFE.sub("_", f"{t.get('OBJECT')}_{t.get('NAME')}_{t.get('ID')}_{t.get('LANGUAGE')}.txt")
        target.write_text(body, encoding="utf-8")
        out.update(saved_to=str(target), preview="\n".join(body.splitlines()[:60]),
                   note="Long text saved to the file; preview shows the first 60 lines.")
    return out
