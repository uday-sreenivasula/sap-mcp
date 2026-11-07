"""SAP S/4HANA OData HTTP client.

Strictly read-only: only GET requests are issued. Any caller that tries to
mutate state will get a :class:`ReadOnlyViolation`.

This client targets the SAP Gateway OData v2 surface that ships with S/4HANA.
It handles:

* Basic authentication
* sap-client (mandt) query parameter on every request
* OData v2 JSON format (?$format=json)
* CSRF tokens (not strictly needed for GETs, but fetched on demand so the
  same session can be used for future writes if you ever extend this)
* Cookie persistence inside an httpx.Client session
* A hard wall-clock deadline on every request (``timeout_seconds``, default
  30s). httpx's own timeouts are per network phase — a Gateway that trickles a
  large response a few bytes at a time never trips them — so we also stop
  reading once the deadline passes and raise :class:`SapTimeout`.
* A disk cache of ``$metadata`` documents, so big services are downloaded once
  rather than on every schema question.
"""
from __future__ import annotations

import json
import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

import httpx

from . import credentials
from .config import SapConfig


class ReadOnlyViolation(RuntimeError):
    """Raised when something tries to issue a non-GET request."""


class SapODataError(RuntimeError):
    """Raised when SAP Gateway returns a non-success response."""

    def __init__(self, status_code: int, message: str, body: str = ""):
        super().__init__(f"SAP returned {status_code}: {message}")
        self.status_code = status_code
        self.body = body


class SapTimeout(SapODataError):
    """Raised when SAP does not finish answering within the deadline."""

    def __init__(self, message: str):
        RuntimeError.__init__(self, message)
        self.status_code = 0
        self.body = ""


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


#: Wall-clock ceiling for a multi-page scan (query_all). Claude Desktop gives
#: up on a tool call after about a minute and shows nothing, so a scan that
#: would run longer stops just short of that and says so instead.
SCAN_SECONDS = _env_float("SAP_MCP_SCAN_SECONDS", 55.0)

#: How long a cached $metadata document stays valid. Service structure only
#: changes when someone transports a new version of the service.
METADATA_TTL_DAYS = _env_float("SAP_MCP_METADATA_TTL_DAYS", 7.0)

#: Headers describing the wire encoding; they must not be copied onto a
#: response whose body has already been decoded.
_WIRE_HEADERS = {"content-encoding", "content-length", "transfer-encoding"}


def sap_error_text(body: str) -> str:
    """Pull the human-readable text out of a Gateway error body, if any."""
    try:
        err = json.loads(body).get("error", {})
        msg = err.get("message")
        return (msg.get("value") if isinstance(msg, dict) else msg) or ""
    except (ValueError, AttributeError):
        pass
    m = re.search(r"<message[^>]*>(.*?)</message>", body or "", re.S)
    return m.group(1).strip() if m else ""


class SapODataClient:
    """Minimal OData v2 client. Synchronous on purpose — MCP tool handlers
    are simple request/response and the SAP Gateway is the bottleneck."""

    def __init__(self, config: SapConfig, http_client: Optional[httpx.Client] = None):
        self.config = config
        self._client = http_client or httpx.Client(
            auth=(config.username, config.password),
            verify=config.verify_tls,
            timeout=httpx.Timeout(
                config.timeout_seconds, connect=min(10.0, config.timeout_seconds)
            ),
            headers={
                "Accept": "application/json",
                "User-Agent": "sap-mcp/0.3 (+claude-mcp)",
            },
        )
        self._csrf_token: Optional[str] = None
        #: service -> raw $metadata XML, for this session.
        self._metadata_mem: dict[str, str] = {}

    # ------------------------------------------------------------------ utils

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SapODataClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _build_url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if path.startswith("/"):
            return self.config.base_url.rstrip("/") + path
        return self.config.root_url + path.lstrip("/")

    def _merge_params(self, params: Optional[Mapping[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {
            "sap-client": self.config.client,
            "$format": "json",
        }
        if params:
            for k, v in params.items():
                if v is None:
                    continue
                out[k] = v
        return out

    def cache_dir(self, kind: str) -> Path:
        """Per-system cache folder, e.g. ``~/.sap-mcp/cache/metadata/sap-prod.example.com_44300_100``.

        Keyed on host + client rather than the env name, so a re-pointed env
        never serves another system's cache.
        """
        host = re.sub(r"[^A-Za-z0-9.]+", "_", urlparse(self.config.base_url).netloc)
        return credentials.config_dir() / "cache" / kind / f"{host}_{self.config.client}"

    # ------------------------------------------------------------- transport

    def _send(
        self,
        url: str,
        params: Mapping[str, Any],
        headers: Optional[Mapping[str, str]] = None,
        deadline: Optional[float] = None,
    ) -> httpx.Response:
        """GET ``url`` and read the whole body, giving up after ``deadline`` seconds.

        Every SAP call goes through here, so nothing can wait on SAP forever.
        """
        limit = deadline or self.config.timeout_seconds
        started = time.monotonic()
        try:
            with self._client.stream("GET", url, params=params, headers=headers) as resp:
                body = bytearray()
                for chunk in resp.iter_bytes():
                    body += chunk
                    if time.monotonic() - started > limit:
                        raise SapTimeout(self._timeout_text(url, limit))
                kept = [
                    (k, v) for k, v in resp.headers.multi_items()
                    if k.lower() not in _WIRE_HEADERS
                ]
                return httpx.Response(
                    resp.status_code, headers=kept, content=bytes(body), request=resp.request
                )
        except httpx.TimeoutException as e:
            raise SapTimeout(self._timeout_text(url, limit)) from e
        except httpx.HTTPError as e:
            raise SapODataError(
                0,
                f"Network error reaching {self.config.base_url}: {e}. "
                "Check the VPN / office network connection.",
            ) from e

    @staticmethod
    def _timeout_text(url: str, limit: float) -> str:
        what = url.split("/sap/opu/odata/", 1)[-1].split("?", 1)[0]
        return (
            f"SAP did not finish answering within {limit:g}s ({what}). The request "
            "was abandoned. Narrow the query (date range, filter, fewer fields) or "
            "try again — the Gateway may be busy."
        )

    @staticmethod
    def _check_status(resp: httpx.Response, what: str = "OData request failed") -> None:
        if resp.status_code < 400:
            return
        detail = sap_error_text(resp.text)
        suffix = f": {detail}" if detail else ""
        if resp.status_code == 401:
            raise SapODataError(401, "Authentication failed (check user/password/client)", resp.text[:2000])
        if resp.status_code == 403:
            raise SapODataError(
                403, "Authorization denied (user lacks scope for this service)" + suffix, resp.text[:2000]
            )
        raise SapODataError(resp.status_code, what + suffix, resp.text[:2000])

    # ----------------------------------------------------------- public API

    def get(
        self, path: str, params: Optional[Mapping[str, Any]] = None, deadline: Optional[float] = None,
    ) -> dict[str, Any]:
        """Issue a GET to the SAP Gateway and return parsed JSON.

        ``deadline`` overrides the per-request time limit (report runs).
        """
        url = self._build_url(path)
        resp = self._send(url, self._merge_params(params), deadline=deadline)
        self._check_status(resp)

        ctype = resp.headers.get("content-type", "")
        if "json" not in ctype.lower():
            # Some metadata endpoints return XML; surface the raw body for those.
            return {"_raw": resp.text, "_content_type": ctype}
        return resp.json()

    def fetch_csrf(self) -> str:
        """Fetch a CSRF token from the service root. Not needed for reads,
        but exposed so callers can pre-warm one if desired."""
        url = self.config.root_url
        resp = self._send(
            url,
            {"sap-client": self.config.client},
            headers={"x-csrf-token": "fetch"},
        )
        token = resp.headers.get("x-csrf-token")
        if not token:
            raise SapODataError(resp.status_code, "Could not fetch CSRF token", resp.text[:500])
        self._csrf_token = token
        return token

    # Guard rails for the read-only contract --------------------------------

    def _blocked(self, verb: str):
        raise ReadOnlyViolation(
            f"sap-mcp is read-only by design. Refusing to issue {verb}. "
            "If you need write access, fork the project and remove this guard."
        )

    def post(self, *a, **k):    self._blocked("POST")     # noqa: E704
    def put(self, *a, **k):     self._blocked("PUT")      # noqa: E704
    def patch(self, *a, **k):   self._blocked("PATCH")    # noqa: E704
    def delete(self, *a, **k):  self._blocked("DELETE")   # noqa: E704

    # ---------------------------------------------- high-level convenience

    #: Catalog fields worth returning. The full ServiceCollection entity also
    #: carries MetadataUrl, ServiceUrl, ImageUrl, Author, Description and
    #: UpdatedDate — none of which help a caller decide which service to use,
    #: and which together account for most of the payload.
    CATALOG_SELECT = "TechnicalServiceName,Title,TechnicalServiceVersion"

    def list_services(
        self,
        *,
        select: Optional[str] = CATALOG_SELECT,
        top: int = 500,
        skip: Optional[int] = None,
    ) -> dict[str, Any]:
        """List OData services exposed by the Gateway catalog.

        ``select`` defaults to the three fields a caller actually needs.
        Pass ``select=None`` to get every field back (the pre-0.2 behaviour) —
        be aware that is roughly 25x the payload.
        """
        path = "/sap/opu/odata/IWFND/CATALOGSERVICE;v=2/ServiceCollection"
        params: dict[str, Any] = {"$top": top}
        if skip is not None:
            params["$skip"] = skip
        if not select:
            return self.get(path, params=params)

        try:
            return self.get(path, params={**params, "$select": select})
        except SapODataError as e:
            # Not every Gateway release accepts $select on the catalog
            # collection. Falling back costs bandwidth but keeps the tool
            # working — the caller trims the fields either way.
            if e.status_code not in (400, 501):
                raise
            return self.get(path, params=params)

    def _metadata_file(self, service: str) -> Path:
        name = re.sub(r"[^A-Za-z0-9_.-]+", "_", service.strip("/"))
        return self.cache_dir("metadata") / f"{name}.xml"

    def get_metadata(self, service: str, refresh: bool = False) -> dict[str, Any]:
        """Return the $metadata document for a service (XML).

        Looked up in memory, then in the disk cache (valid for
        ``SAP_MCP_METADATA_TTL_DAYS``, default 7), and only downloaded when
        neither has it. ``refresh=True`` forces a fresh download. Set
        ``SAP_MCP_METADATA_TTL_DAYS=0`` to switch the disk cache off.

        Note: $metadata endpoints reject ALL OData system query options
        (including ``$format``). We deliberately bypass the standard
        :py:meth:`get` here because it injects ``$format=json``, which
        SAP Gateway rejects with HTTP 400 / ``/IWFND/MED/...`` on
        ``$metadata`` URLs. We only pass ``sap-client`` (the mandt query
        parameter is allowed and required for client-specific systems).
        """
        key = service.strip("/")
        if not refresh and key in self._metadata_mem:
            return {"_raw": self._metadata_mem[key], "_content_type": "application/xml"}

        path = self._metadata_file(key)
        ttl = METADATA_TTL_DAYS * 86_400
        if not refresh and ttl > 0:
            try:
                if time.time() - path.stat().st_mtime < ttl:
                    text = path.read_text(encoding="utf-8")
                    self._metadata_mem[key] = text
                    return {"_raw": text, "_content_type": "application/xml"}
            except OSError:
                pass  # no cache yet, or unreadable: download instead

        url = self._build_url(f"{key}/$metadata")
        # $metadata only produces XML — override the session's
        # Accept: application/json so SAP doesn't 406. It is a one-off
        # download per service, so it gets twice the normal deadline.
        resp = self._send(
            url,
            {"sap-client": self.config.client},
            headers={"Accept": "application/xml,*/*;q=0.5"},
            deadline=2 * self.config.timeout_seconds,
        )
        self._check_status(resp, "OData metadata request failed")
        text = resp.text
        self._metadata_mem[key] = text
        if ttl > 0:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(text, encoding="utf-8")
                tmp.replace(path)
            except OSError:
                pass  # caching is an optimisation; never fail the call over it
        # $metadata always returns XML
        return {"_raw": text, "_content_type": resp.headers.get("content-type", "")}

    # ------------------------------------------------ metadata introspection
    #
    # A raw $metadata document for a real S/4HANA service runs to tens of
    # thousands of tokens, the bulk of it sap:* annotations that describe
    # Fiori UI behaviour. Callers almost always want one of two much smaller
    # things: "what entities are in here?" or "what fields does this one
    # entity have?". These two helpers answer exactly those.

    @staticmethod
    def _localname(tag: str) -> str:
        """Strip the ``{namespace}`` prefix from an ElementTree tag."""
        return tag.rsplit("}", 1)[-1]

    @classmethod
    def _parse_edmx(cls, xml_text: str) -> tuple[dict[str, str], dict[str, dict]]:
        """Parse an EDMX document.

        Returns ``(entity_sets, entity_types)`` where ``entity_sets`` maps an
        EntitySet name to its short EntityType name, and ``entity_types`` maps
        an EntityType name to its keys and properties.

        Deliberately namespace-agnostic — SAP emits several EDM namespace
        versions across releases and we only care about local element names.
        """
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            raise SapODataError(0, f"Could not parse $metadata XML: {e}") from e

        entity_sets: dict[str, str] = {}
        entity_types: dict[str, dict] = {}

        for el in root.iter():
            tag = cls._localname(el.tag)

            if tag == "EntitySet":
                set_name = el.get("Name") or ""
                type_name = (el.get("EntityType") or "").rsplit(".", 1)[-1]
                if set_name:
                    entity_sets[set_name] = type_name

            elif tag == "EntityType":
                type_name = el.get("Name") or ""
                if not type_name:
                    continue
                keys = [
                    ref.get("Name")
                    for child in el
                    if cls._localname(child.tag) == "Key"
                    for ref in child
                    if cls._localname(ref.tag) == "PropertyRef" and ref.get("Name")
                ]
                props = []
                for child in el:
                    if cls._localname(child.tag) != "Property":
                        continue
                    prop: dict[str, Any] = {
                        "name": child.get("Name"),
                        "type": (child.get("Type") or "").replace("Edm.", ""),
                    }
                    if child.get("Nullable") == "false":
                        prop["required"] = True
                    if child.get("MaxLength"):
                        prop["max_length"] = child.get("MaxLength")
                    props.append(prop)
                entity_types[type_name] = {"keys": keys, "properties": props}

        return entity_sets, entity_types

    def list_entity_sets(self, service: str, refresh: bool = False) -> dict[str, Any]:
        """Return just the entity set names for a service."""
        entity_sets, _ = self._parse_edmx(self.get_metadata(service, refresh).get("_raw", ""))
        return {
            "service": service,
            "entity_set_count": len(entity_sets),
            "entity_sets": sorted(entity_sets),
            "hint": "Call get_service_metadata(service, entity=<name>) for one entity's fields.",
        }

    def get_entity_schema(self, service: str, entity: str, refresh: bool = False) -> dict[str, Any]:
        """Return keys and properties for a single entity in a service.

        ``entity`` may be either the EntitySet name or the EntityType name;
        matching is case-insensitive.
        """
        entity_sets, entity_types = self._parse_edmx(
            self.get_metadata(service, refresh).get("_raw", "")
        )

        target: Optional[str] = None
        for set_name, type_name in entity_sets.items():
            if set_name.lower() == entity.lower():
                target = type_name
                break
        if target is None:
            for type_name in entity_types:
                if type_name.lower() == entity.lower():
                    target = type_name
                    break

        if target is None or target not in entity_types:
            near = sorted(s for s in entity_sets if entity.lower() in s.lower())
            return {
                "service": service,
                "error": f"Entity {entity!r} not found in {service}.",
                "did_you_mean": near[:10],
                "entity_set_count": len(entity_sets),
            }

        info = entity_types[target]
        return {
            "service": service,
            "entity": entity,
            "entity_type": target,
            "keys": info["keys"],
            "property_count": len(info["properties"]),
            "properties": info["properties"],
        }

    def query(
        self,
        service: str,
        entity_set: str,
        *,
        select: Optional[str] = None,
        filter: Optional[str] = None,  # noqa: A002 — OData option name
        orderby: Optional[str] = None,
        top: Optional[int] = None,
        skip: Optional[int] = None,
        expand: Optional[str] = None,
    ) -> dict[str, Any]:
        """Issue a generic OData query against ``<service>/<entity_set>``."""
        params: dict[str, Any] = {}
        if select:  params["$select"] = select
        if filter:  params["$filter"] = filter
        if orderby: params["$orderby"] = orderby
        if top is not None:    params["$top"] = top
        if skip is not None:   params["$skip"] = skip
        if expand:  params["$expand"] = expand
        path = f"{service.strip('/')}/{entity_set.lstrip('/')}"

        if not select:
            return self.get(path, params=params)

        try:
            return self.get(path, params=params)
        except SapODataError as e:
            # Field lists are our optimisation, not the caller's intent. If a
            # tenant rejects one — a renamed field, or an older Gateway that
            # dislikes $select across $expand — fall back to the unselected
            # query rather than failing the request outright. A genuinely bad
            # $filter will still fail on the retry and surface normally.
            if e.status_code != 400:
                raise
            retry = {k: v for k, v in params.items() if k != "$select"}
            return self.get(path, params=retry)

    def query_all(
        self,
        service: str,
        entity_set: str,
        *,
        select: Optional[str] = None,
        filter: Optional[str] = None,  # noqa: A002
        orderby: Optional[str] = None,
        expand: Optional[str] = None,
        page_size: int = 1000,
        max_records: int = 200_000,
        max_seconds: Optional[float] = None,
    ):
        """Iterate all results across $skip-paged calls.

        Yields rows from the underlying entity set one at a time. Stops when
        a page returns fewer than ``page_size`` rows OR ``max_records`` has
        been yielded (a soft cap to protect against runaway scans on huge GL
        accounts). The caller is responsible for telling the user when the
        cap was hit; this method just stops.

        The whole scan is also bounded in time (``max_seconds``, default
        ``SAP_MCP_SCAN_SECONDS`` = 55s). Running out of time raises
        :class:`SapTimeout` rather than stopping quietly: callers sum these
        rows, and a partial sum presented as a balance would be wrong.

        ``expand`` is passed through to OData ``$expand``; useful for header
        + nested-item queries (e.g. ``A_BillingDocument`` with
        ``expand="to_Item"``). Note that expand payloads are large — drop
        ``page_size`` when using it (200 is a sensible default) so single
        responses don't exceed Gateway response-size limits.

        SAP S/4HANA does not support OData v4 ``$apply=aggregate`` on
        ``A_OperationalAcctgDocItemCube`` (confirmed via live probe — the
        server reports "Property aggregate not found in type ..."). So when
        you need server-side aggregation for balances, the only portable
        option is to page through the rows and sum in the client. This
        helper makes that loop reusable.
        """
        budget = SCAN_SECONDS if max_seconds is None else max_seconds
        started = time.monotonic()
        skip = 0
        yielded = 0
        last_page = 0.0
        while yielded < max_records:
            elapsed = time.monotonic() - started
            # Stop if the next page, judged by the last one, would overrun.
            if yielded and elapsed + last_page >= budget:
                raise SapTimeout(
                    f"Stopped after {elapsed:.0f}s having read {yielded:,} rows of "
                    f"{entity_set} and more remain. No partial result is returned "
                    "because a partial total would be misleading. Narrow the query "
                    "(shorter date range, one company code / account range) and "
                    "re-run, or raise SAP_MCP_SCAN_SECONDS."
                )
            page_started = time.monotonic()
            page = self.query(
                service, entity_set,
                select=select, filter=filter, orderby=orderby,
                top=page_size, skip=skip, expand=expand,
            )
            last_page = time.monotonic() - page_started
            rows = page.get("d", {}).get("results", []) or []
            if not rows:
                return
            for r in rows:
                yield r
                yielded += 1
                if yielded >= max_records:
                    return
            if len(rows) < page_size:
                return
            skip += page_size
