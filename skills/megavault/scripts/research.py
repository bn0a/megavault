#!/usr/bin/env python3
"""Deterministic mechanics for the /megavault skill.

The orchestrating agent does judgment. This module does everything that can be
decided without a model: identity, deduplication, quote verification against
cached source text, claim status arithmetic, vault projection, and the
publish-time graph gate.

Subagents never write here. They drop result JSON into <run>/inbox/ and the
orchestrator runs `ingest`, the only path from their results into canonical state.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import difflib
import html
import io
import hashlib
import ipaddress
import json
import os
import random
import re
import shlex
import shutil
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

SCHEMA_VERSION = "1.0"


def _root_from_env(var: str, *default_parts: str) -> Path:
    """Machine-portable root. Override with an env var; otherwise fall back to a
    home-relative default."""
    override = os.environ.get(var)
    if override:
        return Path(override).expanduser()
    home = Path.home()
    return home.joinpath(*default_parts)


def _default_vault_root() -> Path:
    """Where fresh scratch vaults land when nothing more specific is given.

    Checked in order: `$RESEARCH_VAULT_ROOT`, then `./vault` relative to the
    current working directory. The CLI flag `--vault-root` overrides this for a
    single invocation. A fresh publish never lands inside a vault root; see the
    `publish` command."""
    for var in ("RESEARCH_VAULT_ROOT",):
        override = os.environ.get(var)
        if override:
            return Path(override).expanduser()
    return Path.cwd() / "vault"


RUNS_ROOT = _root_from_env("RESEARCH_RUNS_ROOT", ".claude", "research-runs")
VAULT_ROOT = _default_vault_root()

RECORD_FILES = {
    "sources": "sources.json",
    "excerpts": "excerpts.json",
    "claims": "claims.json",
    "edges": "edges.json",
    "contradictions": "contradictions.json",
    "gaps": "gaps.json",
    "notes": "notes.json",
    "tables": "tables.json",
}

# Failsafe files. COMMIT_FILE lives in a run and holds one ingest commit until it
# is fully applied; ACTIVE_FILE lives in the runs root and lists open and closed runs.
COMMIT_FILE = "commit.pending.json"
ACTIVE_FILE = "ACTIVE.json"

ID_PREFIX = {
    "sources": "S",
    "excerpts": "X",
    "claims": "CL",
    "edges": "EV",
    "contradictions": "CX",
    "gaps": "GAP",
    "notes": "N",
    "tables": "DT",
}

# Every knob a subagent packet may quote. A run's manifest stores only what its
# mode set; `limits_of` layers these underneath so older runs keep parsing.
DEFAULT_LIMITS = {
    "waves": 2,
    "workers": 3,
    "sources": 40,
    "fanout": 4,
    "max_opus_per_wave": 4,
    "read_tokens_per_agent": 15000,
    "fetches_per_agent": 8,
    "searches_per_agent": 6,
    "max_notes_per_synthesist": 4,
    # Subagent tokens the run may spend, summed from reports/token-ledger.jsonl;
    # 0 means no ceiling. `packet` refuses a new packet once it is reached.
    "max_subagent_tokens": 0,
}

# A prospector packet's search ceiling (`max_searches`) when `packet --searches`
# is not given: the run's `searches_per_agent`, and at least this many for a
# targeted packet (an expand packet, or any packet from wave 2 on).
TARGETED_SEARCHES = 8

# A worker writes its inbox file once. One younger than this many seconds, or
# not yet valid JSON, may still be mid-write: `ingest` leaves it for the next call.
INGEST_MIN_AGE = 3.0

TOKEN_LEDGER = "token-ledger.jsonl"

MODES = {
    #                waves  workers  sources  extractors-per-prospector
    "small": {
        "waves": 1, "workers": 1, "sources": 12, "fanout": 3,
        "max_opus_per_wave": 2, "fetches_per_agent": 5, "searches_per_agent": 4,
        "max_notes_per_synthesist": 2,
    },
    "standard": {
        "waves": 2, "workers": 3, "sources": 40, "fanout": 4,
        "max_opus_per_wave": 4, "fetches_per_agent": 8, "searches_per_agent": 6,
        "max_notes_per_synthesist": 4,
    },
    "vault": {
        "waves": 3, "workers": 5, "sources": 120, "fanout": 6,
        "max_opus_per_wave": 6, "fetches_per_agent": 10, "searches_per_agent": 8,
        "max_notes_per_synthesist": 6,
    },
    "expand": {
        "waves": 2, "workers": 3, "sources": 24, "fanout": 4,
        "max_opus_per_wave": 3, "fetches_per_agent": 8, "searches_per_agent": 6,
        "max_notes_per_synthesist": 3,
    },
}

# Roles that cost an Opus seat. `packet` refuses to emit one past the wave cap.
OPUS_ROLES = {"prospector", "verifier", "synthesist"}

# Discovery arms (the elastic frame). `framed` is the default packet; a `blind`
# or `frame-break` prospector follows the coordinator's own brief and never sees
# the framed skeleton. The brief travels packet → result → every claim (`arm`).
BRIEFS = ("framed", "blind", "frame-break")
OFF_SKELETON_TAG = "off-skeleton"

RUN_KINDS = {"research", "data", "mixed"}

# Claim types that cannot rest on a single source.
CORROBORATION_REQUIRED = {"numerical", "causal", "comparative", "predictive"}

FUZZY_THRESHOLD = 0.92

STATUS_RANK = {"unsupported": 0, "refuted": 0, "disputed": 1, "qualified": 2, "supported": 3}

PROFILES_PATH = Path(__file__).resolve().with_name("source_profiles.json")
# Quality is the default, not an option: insider sources only. `open` stays selectable.
DEFAULT_PROFILE = "practitioner"


def stop(message: str, detail: Any = None) -> "NoReturn":  # type: ignore[valid-type]
    """Exit 3: the script will not guess. A human has to decide and re-run."""
    print(f"STOP: {message}", file=sys.stderr)
    if detail is not None:
        print(json.dumps(detail, indent=2, ensure_ascii=False), file=sys.stderr)
    raise SystemExit(3)


def limits_of(manifest: dict[str, Any]) -> dict[str, Any]:
    return {**DEFAULT_LIMITS, **(manifest.get("limits") or {})}


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


def plus_days(days: int) -> str:
    stamp = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)
    return stamp.strftime("%Y-%m-%d")


def die(message: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(2)


def slugify(value: str, limit: int = 60) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = value.encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return value[:limit] or "untitled"


def sha(value: str, size: int = 16) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:size]


def _retry(action: Any) -> Any:
    """Windows denies a read or a replace while another process holds the file for
    a moment (parallel CLI calls on one run); wait briefly instead of failing."""
    for attempt in range(6):
        try:
            return action()
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.05 * 2 ** attempt)


def read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        if default is None:
            die(f"missing file: {path}")
        return default
    try:
        return json.loads(_retry(lambda: path.read_text(encoding="utf-8")))
    except json.JSONDecodeError as exc:
        die(f"invalid JSON in {path}: {exc}")


def write_json(path: Path, value: Any) -> None:
    """Atomic: a per-process temp file, then replace. No fsync: a power cut can
    still lose the last write (scripts/maintenance/README.md)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    _retry(lambda: tmp.replace(path))


def read_jsonl(path: Path) -> list[Any]:
    """One JSON value per line; a torn last line (a crash mid-append) is skipped."""
    if not path.is_file():
        return []
    rows: list[Any] = []
    for line in _retry(lambda: path.read_text(encoding="utf-8")).splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def write_jsonl(path: Path, rows: list[Any]) -> None:
    """Atomic, like write_json: the whole log is rewritten through a temp file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
        newline="\n",
    )
    _retry(lambda: tmp.replace(path))


def write_text(path: Path, value: str, newline: str = "\n") -> None:
    """Always LF unless the caller says otherwise. A platform-translated write
    would rewrite every line of a vault file and drown one real change in
    thousands of invisible ones."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(value, encoding="utf-8", newline=newline)
    tmp.replace(path)


def read_note(path: Path) -> tuple[str, str]:
    """Text with `\\n` endings, plus the newline style the file actually uses, so
    a rewrite gives it back unchanged."""
    raw = path.read_bytes()
    style = "\r\n" if b"\r\n" in raw else "\n"
    return raw.decode("utf-8", errors="replace").replace("\r\n", "\n"), style


# --------------------------------------------------------------------------
# untrusted text into Markdown
#
# Claim text, titles, publishers, quotes and locators are relayed from web pages
# by a model. Written raw into a table row, a heading or a blockquote, a newline
# or a pipe starts a row or heading that `adopt` and `validate` would read as the
# engine's own (a forged `**supported**` row). So every such value is written on
# one line with its pipes escaped, a tag-like `<` and an inline query code span
# (`$=…`, `=…`) defanged; `_cell_text` undoes exactly that when a table is read.
# --------------------------------------------------------------------------

CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
TAG_START = re.compile(r"<(?=[A-Za-z/!?%])")
TAG_START_ESCAPED = re.compile(r"&lt;(?=[A-Za-z/!?%])")
INLINE_QUERY = re.compile(r"`(\$?)=")
INLINE_QUERY_ESCAPED = re.compile(r"`(\$?)\\=")
UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")
LINK_TARGET_UNSAFE = re.compile(r"[\[\]|#^\\]")
# Executable or remotely embedded content: Dataview/DataviewJS, Templater and
# code-runner blocks, raw HTML that runs or loads something, remote image embeds.
# Core Obsidian only (publishing.md); `validate` fails a note that has any.
EXEC_CONTENT = re.compile(
    r"(?im)^[ \t>]*(?:```|~~~)[ \t{]*(?:dataviewjs|dataview|js-engine|templater|run-[\w-]+|button|tracker)\b"
    r"|<%|(?<!\\)`\$=|<\s*/?\s*(?:script|iframe|object|embed|frame|frameset|form|base|meta|link|style|svg|img"
    r"|video|audio|source)\b|!\[[^\]\n]*\]\(\s*<?\s*(?:https?:)?//"
)


def _exec_kind(match: str) -> str:
    """What an EXEC_CONTENT match is, in words (the match itself must not be echoed)."""
    text = match.lower()
    if "```" in text or "~~~" in text:
        return "a plugin code block"
    if "<%" in text:
        return "a Templater tag"
    if "$=" in text:
        return "an inline DataviewJS query"
    if text.lstrip().startswith("!["):
        return "a remote image embed"
    return "raw HTML " + re.sub(r"[^a-z]", "", text)


def _one_line(value: Any) -> str:
    """No newline, no control character, single spaces."""
    return " ".join(CONTROL_CHARS.sub(" ", str(value)).split())


def _defang(text: str) -> str:
    text = TAG_START.sub("&lt;", text)
    return INLINE_QUERY.sub(lambda m: "`" + m.group(1) + "\\=", text)


def _undefang(text: str) -> str:
    text = INLINE_QUERY_ESCAPED.sub(lambda m: "`" + m.group(1) + "=", text)
    return TAG_START_ESCAPED.sub("<", text)


def _md_text(value: Any) -> str:
    """Untrusted text on one line of prose: a heading, a bullet, a status line."""
    return _defang(_one_line(value))


def _md_cell(value: Any) -> str:
    """One table cell: one line, defanged, every pipe escaped exactly once (an
    engine wikilink's `\\|` stays `\\|`), so no value can end its cell or row."""
    text = re.sub(r"\\+(?=\|)", "", _defang(_one_line(value)))
    return text.replace("|", "\\|")


def _cell_text(cell: str) -> str:
    """A parsed table cell back to the value `_md_cell` wrote."""
    return _undefang(cell.replace("\\|", "|"))


def _md_label(value: Any, nested: bool = False) -> str:
    """Text placed before or inside `[label](url)`. Brackets that could close the
    label early and forge another link become parentheses; with `nested`, one
    balanced level (`[Weekly Digest] Issue 42`) is kept, as MD_LINK reads it."""
    text = _defang(_one_line(value))
    if nested and re.fullmatch(r"(?:[^\[\]]|\[[^\[\]]*\])*", text):
        return text
    return text.replace("[", "(").replace("]", ")")


def _md_url(url: Any) -> str:
    """A URL as a Markdown link target: characters that end the target, the cell
    or the row are percent-encoded."""
    return re.sub(r"[\s()<>|`\"]", lambda m: f"%{ord(m.group()):02X}", str(url).strip())


QUOTE_LEAD = re.compile(r"^(?=[#>\[\-+*=~|])")
QUOTE_LEAD_ESCAPED = re.compile(r"^\\(?=[#>\[\-+*=~|])")


def _md_quote(value: Any) -> str:
    """A verbatim excerpt as the text of one blockquote line: one line (the quote
    check folds whitespace, so this is what was verified), every backtick escaped
    (no fence, no inline query), a tag-like `<` defanged, and a leading character
    that would open a heading, callout, list or nested quote escaped."""
    text = TAG_START.sub("&lt;", _one_line(value)).replace("`", "\\`")
    return QUOTE_LEAD.sub(lambda _: "\\", text)


def _quote_text(fragment: str) -> str:
    """A published blockquote line back to the excerpt `_md_quote` was given."""
    fragment = QUOTE_LEAD_ESCAPED.sub("", fragment)
    return TAG_START_ESCAPED.sub("<", fragment.replace("\\`", "`"))


def _split_row(line: str) -> tuple[list[str], bool]:
    """A table line's raw cells, split on unescaped pipes only, and whether the
    row is closed by a final pipe (the engine always writes one)."""
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    closed = bool(re.search(r"(?<!\\)\|$", body))
    if closed:
        body = body[:-1]
    return [cell.strip() for cell in UNESCAPED_PIPE.split(body)], closed


# --------------------------------------------------------------------------
# URLs: identity, and the hosts a run may record
# --------------------------------------------------------------------------

HOST_LABEL = re.compile(r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?")
TOP_LABEL = re.compile(r"[a-z][a-z0-9-]*")
LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".localdomain", ".home.arpa", ".lan")


def host_problem(host: str) -> str | None:
    """Why a host may not appear in a record, or None. Only a public DNS name or a
    global IP address passes: never loopback, link-local (cloud metadata),
    private or reserved addresses, `localhost`, a single-label name or a name
    with characters no host has (`$`, `;`, a backtick)."""
    host = host.strip().rstrip(".").lower()
    if not host:
        return "no host"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        mapped = getattr(ip, "ipv4_mapped", None)
        if not ip.is_global or ip.is_multicast or (mapped is not None and not mapped.is_global):
            return f"non-public address {host}"
        return None
    try:
        ascii_host = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return "not a valid host name"
    labels = ascii_host.split(".")
    if len(labels) < 2 or not all(HOST_LABEL.fullmatch(label) for label in labels) \
            or not TOP_LABEL.fullmatch(labels[-1]):
        return "not a valid host name"
    if ascii_host == "localhost" or ascii_host.endswith(LOCAL_SUFFIXES):
        return f"non-public host {ascii_host}"
    return None


def _redact_url(url: str) -> str:
    """A URL safe to print or store: any `user:password@` removed."""
    text = _one_line(url)
    try:
        parts = urlsplit(text)
        if parts.username is not None or parts.password is not None:
            netloc = parts.netloc.rpartition("@")[2]
            return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except ValueError:
        return re.sub(r"//[^/@\s]*@", "//", text)
    return text


def _plain_canonical(url: str, keep_query: bool = True) -> str:
    """Scheme, host (lower case, no `www.`, port only when not the default), path
    without a trailing slash, sorted query without tracking keys. Refuses (raises
    ValueError) anything a record must never hold: another scheme, credentials,
    a host that is not public (`host_problem`), a bad port."""
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        raise ValueError(f"not an absolute http(s) URL: {_redact_url(url)!r}")
    if "@" in parts.netloc:
        raise ValueError(f"credentials in URL: {_redact_url(url)!r}")
    host = (parts.hostname or "").rstrip(".")
    problem = host_problem(host)
    if problem:
        raise ValueError(f"{problem} in URL: {_redact_url(url)!r}")
    try:
        port = parts.port
    except ValueError:
        raise ValueError(f"bad port in URL: {_redact_url(url)!r}") from None
    if host.startswith("www."):
        host = host[4:]
    scheme = parts.scheme.lower()
    if ":" in host:
        host = f"[{host}]"
    netloc = host if port in (None, {"http": 80, "https": 443}[scheme]) else f"{host}:{port}"
    query = "&".join(
        item
        for item in sorted(parts.query.split("&"))
        if item and not item.split("=")[0].lower().startswith(("utm_", "fbclid", "gclid"))
    ) if keep_query else ""
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, query, ""))


def _worker_cache_key(url: str) -> str:
    """The form a worker hashes for its cache file name (the one-liner in the
    extractor definition): the netloc as written, so an explicit default port
    stays. Only for a URL that already passed `canonical_url`."""
    canonical_url(url)
    parts = urlsplit(url)
    host = parts.netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    return urlunsplit((parts.scheme.lower(), host, parts.path.rstrip("/") or "/", "", ""))


# One key per arXiv paper whatever the view (abs/html/pdf), version (vN), `.pdf`
# suffix, `www.` or `export.` host.
ARXIV_PATH = re.compile(r"^/(?:abs|html|pdf)/(.+?)(?:v\d+)?(?:\.pdf)?$", re.I)


def canonical_url(url: str) -> str:
    """The identity of a source: lower-case scheme and host without `www.`, no
    trailing slash, fragment or tracking query; an arXiv paper is always
    `https://arxiv.org/abs/<id>`, without its version suffix."""
    plain = _plain_canonical(url)
    parts = urlsplit(plain)
    if parts.netloc in {"arxiv.org", "export.arxiv.org"}:
        match = ARXIV_PATH.match(parts.path)
        if match:
            return f"https://arxiv.org/abs/{match.group(1)}"
    return plain


def publisher_root(url: str) -> str:
    """Registrable-ish host, used as the fallback independence group."""
    bits = _host_of(url).split(".")
    return ".".join(bits[-3:]) if len(bits) > 2 and bits[-2] in {"co", "com", "ac", "gov", "org"} else ".".join(bits[-2:])


# Hosts that publish many independent papers. Their host is never an origin group:
# two arXiv preprints from unrelated labs are not one group.
AGGREGATOR_HOSTS = (
    "arxiv.org", "doi.org", "pubmed.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov", "ssrn.com",
    "semanticscholar.org", "researchgate.net", "osf.io", "biorxiv.org", "medrxiv.org",
)
DOI_IN_URL = re.compile(r"(10\.\d{4,9}/[^\s?#&]+)")


def is_aggregator_group(group: str) -> bool:
    """True when an origin group names an aggregator host rather than a paper."""
    value = str(group or "").strip().lower()
    return bool(value) and "://" not in value and ":" not in value and bool(_suffix_match(value, list(AGGREGATOR_HOSTS)))


def paper_identifier(url: str) -> str | None:
    """`arxiv:<id>`, `doi:<doi>`, `pmid:<n>`, `pmcid:<PMCn>`, `ssrn:<n>` (and the
    Semantic Scholar, ResearchGate and OSF ids) for an aggregator URL, else None."""
    try:
        canonical = canonical_url(url)
    except ValueError:
        return None
    parts = urlsplit(canonical)
    host, path = parts.netloc, unquote(parts.path)
    if host == "arxiv.org":
        match = ARXIV_PATH.match(path)
        return f"arxiv:{match.group(1).lower()}" if match else None
    if _suffix_match(host, ["biorxiv.org", "medrxiv.org"]):
        match = DOI_IN_URL.search(path)
        if match:
            doi = re.sub(r"(v\d+)?(\.full(-text)?)?(\.pdf)?$", "", match.group(1))
            return f"doi:{doi.lower()}"
    if _suffix_match(host, ["doi.org"]):
        match = DOI_IN_URL.search(path)
        return f"doi:{match.group(1).lower()}" if match else None
    if _suffix_match(host, ["ncbi.nlm.nih.gov"]):
        match = re.search(r"/(PMC\d+)", path, re.I)
        if match:
            return f"pmcid:{match.group(1).upper()}"
        match = re.search(r"/(?:pubmed/)?(\d{4,9})(?:/|$)", path)
        return f"pmid:{match.group(1)}" if match else None
    if _suffix_match(host, ["ssrn.com"]):
        match = re.search(r"abstract(?:_id)?=(\d+)", f"{path}?{parts.query}")
        return f"ssrn:{match.group(1)}" if match else None
    if _suffix_match(host, ["semanticscholar.org"]):
        match = re.search(r"/([0-9a-f]{40})(?:/|$)", path)
        return f"s2:{match.group(1)}" if match else None
    if _suffix_match(host, ["researchgate.net"]):
        match = re.search(r"/publication/(\d+)", path)
        return f"researchgate:{match.group(1)}" if match else None
    if _suffix_match(host, ["osf.io"]):
        segments = [s for s in path.split("/") if s and s != "preprints"]
        return f"osf:{segments[-1].lower()}" if segments else None
    match = DOI_IN_URL.search(path)
    return f"doi:{match.group(1).lower()}" if match else None


def fallback_origin_group(url: str) -> str:
    """The independence group of a source that declares none: its registrable
    domain, except on an aggregator, where it is the paper's own identifier (or,
    lacking one, the canonical URL itself), never the aggregator host."""
    host = _host_of(url)
    if _suffix_match(host, list(AGGREGATOR_HOSTS)):
        ident = paper_identifier(url)
        if ident:
            return ident
        try:
            return "url:" + canonical_url(url).split("://", 1)[-1]
        except ValueError:
            return "url:" + url.strip()
    return publisher_root(url)


def effective_origin_group(source: dict[str, Any]) -> str:
    """A stored origin group, unless it is empty or an aggregator host: then the
    group the source's own URL implies (see fallback_origin_group)."""
    group = str(source.get("origin_group") or "").strip()
    url = str(source.get("url") or source.get("canonical_url") or "")
    host = _host_of(url) if url else ""
    on_aggregator = bool(host) and bool(_suffix_match(host, list(AGGREGATOR_HOSTS)))
    # a stored group that is just the old domain fallback of an aggregator URL
    # (pubmed.ncbi.nlm.nih.gov → nih.gov) is no more a group than the host is
    if group and not is_aggregator_group(group) and not (on_aggregator and group == publisher_root(url)):
        return group
    if url:
        try:
            return fallback_origin_group(url)
        except ValueError:
            pass
    return group


def normalize_quote(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = value.replace("’", "'").replace("‘", "'")
    value = value.replace("“", '"').replace("”", '"')
    value = value.replace("–", "-").replace("—", "-").replace("…", "...")
    value = re.sub(r"\s+", " ", value)
    return value.strip().lower()


def claim_key(text: str) -> str:
    text = normalize_quote(text)
    text = re.sub(r"[^a-z0-9 ]+", "", text)
    return sha(text, 24)


# --------------------------------------------------------------------------
# cache views for the quote check
#
# The raw cache file is never changed. An HTML page is also read through a text
# view (tags stripped, entities decoded, whitespace collapsed), and every page
# through a loose form that ignores punctuation, hyphenation and manuscript line
# numbers. A quote found only in the loose form is `fuzzy`, never `exact`.
# --------------------------------------------------------------------------

HTML_START = re.compile(r"^(?:<\?xml[^>]*>\s*)?<(?:!doctype\b|html\b)", re.I)
HTML_TAG = re.compile(r"</?[a-zA-Z][a-zA-Z0-9:-]*(?:\s[^<>]*)?/?>|<!--.*?-->", re.S)
HTML_HIDDEN = re.compile(r"<(script|style|noscript|template)\b[^>]*>.*?</\1\s*>|<!--.*?-->", re.I | re.S)
HTML_BLOCK = re.compile(
    r"</?(?:address|article|aside|blockquote|br|caption|dd|div|dl|dt|figcaption|figure|footer|"
    r"h[1-6]|header|hr|li|main|nav|ol|p|pre|section|table|tbody|td|tfoot|th|thead|title|tr|ul)\b[^>]*>",
    re.I,
)


def looks_like_html(text: str) -> bool:
    """A page that starts like an HTML document, or whose characters are mostly
    markup (twenty tags or more making up a fifth of the text)."""
    if HTML_START.match(text.lstrip("\ufeff \t\r\n")[:512]):
        return True
    sample = text[:200_000]
    tags = HTML_TAG.findall(sample)
    return len(tags) >= 20 and sum(len(tag) for tag in tags) >= 0.2 * len(sample)


def html_text_view(text: str) -> str:
    """What a reader sees: hidden blocks dropped, block tags read as a space,
    inline tags removed, entities decoded, whitespace collapsed."""
    body = HTML_HIDDEN.sub(" ", text)
    body = HTML_BLOCK.sub(" ", body)
    body = re.sub(r"<[^<>]*>", "", body)
    return re.sub(r"\s+", " ", html.unescape(body)).strip()


# Loose form: letters and digits only. Separators that carry a number's meaning
# survive as placeholders, so `1.5` never equals `15`, `-5` never equals `5`,
# `3-5` never equals `35`, and `1 0` never equals `10`.
LOOSE_INVISIBLE = re.compile("[\u00ad\u200b\u200c\u200d\u2060\ufeff]")
LOOSE_DASHES = "\\-\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63"
LOOSE_POINT = re.compile(r"(?<=\d)[.,](?=\d)")
LOOSE_RANGE = re.compile(f"(?<=\\d)[{LOOSE_DASHES}](?=\\d)")
LOOSE_SIGN = re.compile(f"(?<![\\w.,])[{LOOSE_DASHES}](?=\\d)")
LOOSE_GAP = re.compile(r"(?<=\d)\s+(?=\d)")
LOOSE_DROP = re.compile("[^\\w\ue000-\ue003]|_")
# A manuscript or PDF line number: digits at the start of a line, then a space.
LINE_NUMBER = re.compile(r"(?m)^[ \t]*\d{1,4}[ \t]+")
# Shorter quotes get no loose match: too little text to rule out a coincidence.
LOOSE_MIN_CHARS = 16


def loose_text(text: str) -> str:
    value = LOOSE_INVISIBLE.sub("", unicodedata.normalize("NFKC", text)).casefold()
    value = LOOSE_POINT.sub("\ue000", value)
    value = LOOSE_RANGE.sub("\ue001", value)
    value = LOOSE_SIGN.sub("\ue002", value)
    value = LOOSE_GAP.sub("\ue003", value)
    return LOOSE_DROP.sub("", value)


def quote_views(texts: list[str]) -> dict[str, Any]:
    """Everything a quote is checked against, built once per source: the
    normalised raw text, the normalised text view when a file is HTML, and the
    loose forms (raw, raw without line numbers, text view)."""
    raw = "\n".join(texts)
    html_files = [looks_like_html(text) for text in texts]
    view = None
    if any(html_files):
        view = "\n".join(html_text_view(text) if is_html else text
                         for text, is_html in zip(texts, html_files))
    loose = {loose_text(raw), loose_text(LINE_NUMBER.sub("", raw))}
    if view is not None:
        loose.add(loose_text(view))
    return {
        "raw": normalize_quote(raw),
        "text_view": normalize_quote(view) if view is not None else None,
        "loose": [form for form in loose if form],
    }


def check_quote(quote: str, views: dict[str, Any] | None) -> dict[str, Any]:
    """The mechanical quote check. `exact`: the normalised quote is a substring of
    the raw text or of the HTML text view. `fuzzy`: identical once punctuation,
    hyphenation and line numbers are ignored (no score), or a window ratio at or
    above FUZZY_THRESHOLD that passes the meaning guard (`meaning_change`). Else
    `mismatch`, with `meaning_change` naming the guard that refused a high ratio;
    `no_cache` without a cache file. `basis` says which view matched."""
    if views is None:
        return {"status": "no_cache", "score": None, "basis": None}
    needle = normalize_quote(quote)
    if needle and needle in views["raw"]:
        return {"status": "exact", "score": 1.0, "basis": "raw"}
    view = views.get("text_view")
    if needle and view and needle in view:
        return {"status": "exact", "score": 1.0, "basis": "text_view"}
    loose = loose_text(quote)
    if len(loose) >= LOOSE_MIN_CHARS and any(loose in form for form in views["loose"]):
        return {"status": "fuzzy", "score": None, "basis": "loose"}
    (score, start), basis, haystack = _best_window(needle, views["raw"]), "raw", views["raw"]
    if score < FUZZY_THRESHOLD and view:
        alt, alt_start = _best_window(needle, view)
        if alt > score:
            score, start, basis, haystack = alt, alt_start, "text_view", view
    if score < FUZZY_THRESHOLD:
        return {"status": "mismatch", "score": score, "basis": basis}
    changed = meaning_change(needle, haystack, start)
    if changed:
        return {"status": "mismatch", "score": score, "basis": basis, "meaning_change": changed}
    return {"status": "fuzzy", "score": score, "basis": basis}


# --------------------------------------------------------------------------
# run store
# --------------------------------------------------------------------------


class Run:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.records_dir = root / "records"
        self.inbox = root / "inbox"
        self.cache = root / "cache"
        self.reports = root / "reports"
        self.recovered = False  # set by `open` when it finished an interrupted commit

    # -- construction -----------------------------------------------------

    @classmethod
    def create(
        cls,
        question: str,
        mode: str,
        runs_root: Path,
        kind: str = "research",
        overrides: dict[str, Any] | None = None,
        profile: str | None = None,
        run_id: str | None = None,
    ) -> "Run":
        if mode not in MODES:
            die(f"mode must be one of {', '.join(MODES)}")
        if kind not in RUN_KINDS:
            die(f"kind must be one of {', '.join(sorted(RUN_KINDS))}")
        question = question.strip()
        if not question:
            die("question cannot be empty")
        run_id = run_id or f"{slugify(question, 40)}-{sha(question, 8)}"
        run = cls(runs_root / run_id)
        if (run.root / "manifest.json").is_file():
            # finish an interrupted commit first, or a later open replays it over
            # whatever this caller (e.g. `adopt --force`) writes now
            run.recovered = run.roll_forward()
            return run
        for directory in (run.records_dir, run.inbox, run.cache / "raw", run.reports):
            directory.mkdir(parents=True, exist_ok=True)
        limits = {**DEFAULT_LIMITS, **MODES[mode], **(overrides or {})}
        write_json(
            run.root / "manifest.json",
            {
                "schema_version": SCHEMA_VERSION,
                "run_id": run_id,
                "question": question,
                "mode": mode,
                "kind": kind,
                "limits": limits,
                "policy": {
                    "profile": profile or load_profiles().get("default_profile", DEFAULT_PROFILE),
                    "exceptions": [],
                },
                "deviations": [],
                "opus_issued": {},
                "status": "planned",
                "wave": 0,
                "created_at": now(),
                "updated_at": now(),
                "work_units": {"prospector": 0, "extractor": 0, "verifier": 0, "synthesist": 0},
                "counters": {key: 0 for key in ID_PREFIX},
            },
        )
        write_json(run.root / "taxonomy.json", {"domains": []})
        for key, name in RECORD_FILES.items():
            write_json(run.records_dir / name, {})
        return run

    @classmethod
    def open(cls, run_id: str | None, runs_root: Path) -> "Run":
        if run_id:
            candidate = Path(run_id)
            if not candidate.is_absolute():
                candidate = runs_root / run_id
            if not (candidate / "manifest.json").is_file():
                die(f"no run at {candidate}")
            run = cls(candidate)
        else:
            runs = [
                path
                for path in runs_root.glob("*/manifest.json")
            ]
            if not runs:
                die(f"no runs found under {runs_root}")
            latest = max(runs, key=lambda path: path.stat().st_mtime)
            run = cls(latest.parent)
        run.recovered = run.roll_forward()
        return run

    # -- crash safety -----------------------------------------------------

    def commit(self, manifest: dict[str, Any], records: dict[str, Any],
               reports: dict[str, Any] | None = None) -> None:
        """One inbox file's whole effect, all or nothing. It is written once, as a
        single atomic file, and only then applied. A crash before that write leaves
        the store untouched (the inbox file is simply ingested again); a crash after
        it is finished by the next `open`. Records never land without the counters
        that minted their IDs, and a replay never applies half a file."""
        manifest["updated_at"] = now()
        write_json(self.root / COMMIT_FILE,
                   {"manifest": manifest, "records": records, "reports": reports or {}})
        self.roll_forward()

    def roll_forward(self) -> bool:
        pending = self.root / COMMIT_FILE
        if not pending.is_file():
            return False
        data = read_json(pending)
        for kind, value in (data.get("records") or {}).items():
            write_json(self.records_dir / RECORD_FILES[kind], value)
        for name, value in (data.get("reports") or {}).items():
            if name.endswith(".jsonl"):
                write_jsonl(self.reports / name, value)
            else:
                write_json(self.reports / name, value)
        if data.get("manifest"):
            write_json(self.root / "manifest.json", data["manifest"])
        pending.unlink()
        return True

    # -- state ------------------------------------------------------------

    @property
    def manifest(self) -> dict[str, Any]:
        return read_json(self.root / "manifest.json")

    def save_manifest(self, value: dict[str, Any]) -> None:
        value["updated_at"] = now()
        write_json(self.root / "manifest.json", value)

    @property
    def taxonomy(self) -> dict[str, Any]:
        return read_json(self.root / "taxonomy.json", {"domains": []})

    def records(self, kind: str) -> dict[str, Any]:
        return read_json(self.records_dir / RECORD_FILES[kind], {})

    def save_records(self, kind: str, value: dict[str, Any]) -> None:
        write_json(self.records_dir / RECORD_FILES[kind], value)

    def next_id(self, kind: str, manifest: dict[str, Any]) -> str:
        manifest["counters"][kind] = manifest["counters"].get(kind, 0) + 1
        width = 3 if kind != "sources" else 3
        return f"{ID_PREFIX[kind]}-{manifest['counters'][kind]:0{width}d}"

    def cache_path(self, url: str) -> Path:
        return self.cache / "raw" / f"{sha(canonical_url(url), 20)}.txt"

    def cache_files(self, source: dict[str, Any]) -> list[Path]:
        """Every existing cache file of one source. A worker hashes the URL it
        fetched, which may be another form of the same page (an arXiv pdf for the
        abs page, a query string the canonical form drops), so each form it may
        have used is tried: the canonical key, the plain key with and without the
        query, for the stored URL and every other URL seen for this source."""
        urls = [str(source.get("canonical_url") or ""), str(source.get("url") or "")]
        urls += [str(item) for item in source.get("seen_urls") or []]
        found: list[Path] = []
        for url in urls:
            if not url:
                continue
            keys: list[str] = []
            for make in (canonical_url, _plain_canonical,
                         lambda value: _plain_canonical(value, keep_query=False), _worker_cache_key):
                try:
                    keys.append(make(url))
                except ValueError:
                    continue
            for key in keys:
                path = self.cache / "raw" / f"{sha(key, 20)}.txt"
                if path not in found and path.is_file():
                    found.append(path)
        return found

    def cache_texts(self, source: dict[str, Any]) -> list[str]:
        """The text of every cached form of a source, one entry per file."""
        return [path.read_text(encoding="utf-8", errors="replace") for path in self.cache_files(source)]

    def cache_text(self, source: dict[str, Any]) -> str | None:
        """The cached text of a source (all of its cached forms, joined), or None."""
        texts = self.cache_texts(source)
        return "\n".join(texts) if texts else None


# --------------------------------------------------------------------------
# source policy profiles
# --------------------------------------------------------------------------


def load_profiles() -> dict[str, Any]:
    if not PROFILES_PATH.is_file():
        return {"default_profile": "open", "profiles": {"open": {"label": "open"}}}
    return read_json(PROFILES_PATH)


def policy_of(manifest: dict[str, Any]) -> dict[str, Any]:
    policy = manifest.get("policy") or {}
    return {
        "profile": policy.get("profile") or load_profiles().get("default_profile", DEFAULT_PROFILE),
        "exceptions": policy.get("exceptions") or [],
    }


def resolve_profile(profiles: dict[str, Any], name: str) -> dict[str, Any]:
    table = profiles.get("profiles") or {}
    if name not in table:
        die(f"unknown source profile {name!r}; known: {', '.join(sorted(table))}")
    return table[name]


def _host_of(url: str) -> str:
    """The host alone: no credentials, no port, lower case, no `www.`."""
    try:
        host = (urlsplit(url).hostname or "").rstrip(".")
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _suffix_match(host: str, suffixes: list[str]) -> str | None:
    for suffix in suffixes or []:
        item = str(suffix).lower().lstrip(".")
        if host == item or host.endswith("." + item):
            return item
    return None


def evaluate_source(
    profile: dict[str, Any],
    exceptions: list[dict[str, Any]],
    url: str,
    source_type: str,
    tier: int,
    domain_id: str,
) -> dict[str, Any]:
    """Mechanical admission test. Returns {admitted, reason, rule}."""
    host = _host_of(url)
    for exc in exceptions:
        scope = str(exc.get("scope") or "")
        if scope and scope != domain_id:
            continue
        if not _suffix_match(host, [str(exc.get("domain") or "")]):
            continue
        if str(exc.get("action")) == "allow":
            return {
                "admitted": True,
                "rule": "exception:allow",
                "reason": f"explicit allow for {exc.get('domain')}: {exc.get('reason') or 'no reason recorded'}",
            }
        if str(exc.get("action")) == "deny":
            return {
                "admitted": False,
                "rule": "exception:deny",
                "reason": f"explicit deny for {exc.get('domain')}: {exc.get('reason') or 'no reason recorded'}",
            }

    hit = _suffix_match(host, profile.get("allow_domain_suffixes") or [])
    if hit:
        return {"admitted": True, "rule": "profile:allow_domain_suffixes", "reason": f"{hit} is allow-listed by the profile"}

    hit = _suffix_match(host, profile.get("deny_domain_suffixes") or [])
    if hit:
        return {
            "admitted": False,
            "rule": "profile:deny_domain_suffixes",
            "reason": f"{hit} is deny-listed by profile {profile.get('label', '?')}",
        }

    allow_types = profile.get("allow_source_types")
    if allow_types is not None and source_type not in allow_types:
        return {
            "admitted": False,
            "rule": "profile:allow_source_types",
            "reason": (
                f"source_type {source_type!r} is not admissible under profile "
                f"{profile.get('label', '?')} (allowed: {', '.join(sorted(allow_types))})"
            ),
        }
    if source_type in (profile.get("deny_source_types") or []):
        return {
            "admitted": False,
            "rule": "profile:deny_source_types",
            "reason": f"source_type {source_type!r} is deny-listed by profile {profile.get('label', '?')}",
        }

    ceiling = profile.get("max_authority_tier")
    if isinstance(ceiling, int) and tier > ceiling:
        return {
            "admitted": False,
            "rule": "profile:max_authority_tier",
            "reason": f"tier {tier} exceeds the profile ceiling of tier {ceiling}",
        }
    return {"admitted": True, "rule": "profile:default", "reason": "admitted"}


def tier_status_ceiling(profile: dict[str, Any], tier: int | None) -> str | None:
    if tier is None:
        return None
    table = profile.get("tier_status_ceiling") or {}
    return table.get(str(tier))


# --------------------------------------------------------------------------
# vault parsers — the only place this module reads published markdown.
#
# All five are prefix-agnostic: they key off table headers and ID columns, never
# off a file name. Nothing here touches note prose; tables and the Evidence Map's
# fixed bullet shape are the entire contract.
# --------------------------------------------------------------------------

SEPARATOR_CELL = re.compile(r"^:?-{2,}:?$")
ID_CELL = re.compile(r"^(CL|S|EV|GAP|CX|DT)-(\d+)$")
# Titles may contain brackets, so the label part has to allow one level of
# nesting.
MD_LINK = re.compile(r"\[((?:[^\[\]]|\[[^\[\]]*\])*)\]\((https?://[^)\s]+)\)")
EV_BULLET = re.compile(
    r"^- `(EV-\d+)`\s+\*\*(\w+)\*\*\s+\((\w+)\)\s+—\s+(.*)$"
)
MAP_HEADING = re.compile(r"^###\s+(CL-\d+)\s+—\s+(.*)$")
MAP_STATUS = re.compile(r"^Status:\s+\*\*(\w+)\*\*(?:\s+—\s+(.*))?$")
# The engine writes the quote check last on the bullet line, after the source and
# its locator; only that one counts, never a look-alike inside a title.
QUOTE_CHECK = re.compile(r"quote check: \*\*(\w+)\*\*(?: \(([\d.]+)\))?\s*$")
LOCATOR = re.compile(r"@ (\w+) `([^`]*)`")


def _strip_bold(value: str) -> str:
    return value.strip().strip("*").strip()


def _markdown_tables(text: str) -> list[list[list[str]]]:
    """Every pipe table in the document, as lists of already-stripped cells."""
    tables: list[list[list[str]]] = []
    current: list[list[str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|"):
            cells = [_cell_text(cell) for cell in _split_row(stripped)[0]]
            if all(SEPARATOR_CELL.match(cell) for cell in cells if cell):
                continue
            current.append(cells)
            continue
        if current:
            tables.append(current)
            current = []
    if current:
        tables.append(current)
    return tables


def _id_table(text: str, prefix: str) -> tuple[list[str], list[list[str]]]:
    """The first table whose first column holds <prefix>-### ids, plus its header."""
    for table in _markdown_tables(text):
        header: list[str] = []
        rows: list[list[str]] = []
        for cells in table:
            match = ID_CELL.match(cells[0]) if cells else None
            if match and match.group(1) == prefix:
                rows.append(cells)
            elif not rows:
                header = [cell.lower() for cell in cells]
        if rows:
            return header, rows
    return [], []


def _column(header: list[str], *names: str) -> int | None:
    for name in names:
        if name in header:
            return header.index(name)
    return None


def _cell(cells: list[str], index: int | None) -> str:
    if index is None or index >= len(cells):
        return ""
    return cells[index].strip()


def parse_claim_ledger(path: Path) -> dict[str, dict[str, Any]]:
    """`| ID | Claim | Type | Importance | Status | Evidence | Scope, action, caveat | Groups |`

    Type and Importance are v2 columns. A v1 ledger without them still parses; the
    two fields then come back empty and the caller falls back to its defaults.
    """
    if not path.is_file():
        return {}
    header, rows = _id_table(path.read_text(encoding="utf-8", errors="replace"), "CL")
    text_col = _column(header, "claim", "text")
    type_col = _column(header, "type", "claim type")
    importance_col = _column(header, "importance")
    status_col = _column(header, "status")
    evidence_col = _column(header, "evidence", "sources")
    detail_col = _column(header, "scope, action, caveat", "scope", "detail")
    groups_col = _column(header, "groups", "origin groups")
    out: dict[str, dict[str, Any]] = {}
    for cells in rows:
        cid = cells[0]
        groups_raw = _cell(cells, groups_col)
        try:
            groups = int(groups_raw)
        except ValueError:
            groups = 0
        claim_type = _strip_bold(_cell(cells, type_col)).lower()
        importance = _strip_bold(_cell(cells, importance_col)).lower()
        out[cid] = {
            "id": cid,
            "text": _cell(cells, text_col if text_col is not None else 1),
            "claim_type": claim_type if claim_type in CLAIM_TYPES else "",
            "importance": importance if importance in IMPORTANCE else "",
            "status": _strip_bold(_cell(cells, status_col)).lower() or "unknown",
            "source_ids": [
                part.strip()
                for part in _cell(cells, evidence_col).split(",")
                if ID_CELL.match(part.strip())
            ],
            "detail": _cell(cells, detail_col),
            "groups": groups,
        }
    return out


def parse_gaps(path: Path) -> dict[str, dict[str, Any]]:
    """`| ID | Open question | Impact | Next action |` (+ optional Status/Closed by)."""
    if not path.is_file():
        return {}
    header, rows = _id_table(path.read_text(encoding="utf-8", errors="replace"), "GAP")
    q_col = _column(header, "open question", "question")
    impact_col = _column(header, "impact")
    action_col = _column(header, "next action", "next_action")
    status_col = _column(header, "status")
    closed_col = _column(header, "closed by", "closed_by")
    out: dict[str, dict[str, Any]] = {}
    for cells in rows:
        gid = cells[0]
        out[gid] = {
            "id": gid,
            "question": _cell(cells, q_col if q_col is not None else 1),
            "impact": _strip_bold(_cell(cells, impact_col)).lower() or "material",
            "next_action": _cell(cells, action_col),
            "status": _strip_bold(_cell(cells, status_col)).lower() or "open",
            # The published table renders an empty cell as an em dash placeholder;
            # reading that back as a real value silently closes every open gap.
            "closed_by": (_cell(cells, closed_col).strip(" -–—") or None),
        }
    return out


def parse_source_register(path: Path) -> dict[str, dict[str, Any]]:
    """`| ID | Tier | Accessed | Source | Origin group | Use | Limitations |`

    `Origin group` is a v2 column. Sources with no URL at all are legal: the
    Source cell is then plain `Publisher, Title` text, and the origin group is the
    only thing that can carry independence information.
    """
    if not path.is_file():
        return {}
    header, rows = _id_table(path.read_text(encoding="utf-8", errors="replace"), "S")
    tier_col = _column(header, "tier")
    accessed_col = _column(header, "accessed", "accessed at")
    source_col = _column(header, "source")
    group_col = _column(header, "origin group", "group")
    use_col = _column(header, "use")
    limits_col = _column(header, "limitations", "limits")
    out: dict[str, dict[str, Any]] = {}
    for cells in rows:
        sid = cells[0]
        tier_raw = _cell(cells, tier_col).lstrip("Tt")
        try:
            tier = int(tier_raw)
        except ValueError:
            tier = 5
        source_cell = _cell(cells, source_col)
        link = MD_LINK.search(source_cell)
        if link:
            title = link.group(1)
            url = link.group(2)
            publisher = source_cell.split("[")[0].strip().rstrip(",").strip() or "unknown"
        else:
            url = ""
            publisher, _, rest = source_cell.partition(", ")
            publisher = publisher.strip() or "unknown"
            title = rest.strip() or source_cell
        group = _cell(cells, group_col).strip("—").strip()
        out[sid] = {
            "id": sid,
            "tier": max(1, min(5, tier)),
            "accessed_at": _cell(cells, accessed_col) or "unknown",
            "publisher": publisher,
            "title": title,
            "url": url,
            "origin_group": group,
            "use": _cell(cells, use_col).strip("—").strip(),
            "limitations": _cell(cells, limits_col).strip("—").strip(),
        }
    return out


def parse_evidence_map(path: Path) -> dict[str, dict[str, Any]]:
    """`### CL-### — text`, `Status: **x** — reason`, then one bullet per edge."""
    if not path.is_file():
        return {}
    out: dict[str, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    edge: dict[str, Any] | None = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        heading = MAP_HEADING.match(line.strip())
        if heading:
            current = {
                "id": heading.group(1),
                "text": _undefang(heading.group(2).strip()),
                "status": "",
                "status_reason": "",
                "edges": [],
            }
            out[current["id"]] = current
            edge = None
            continue
        if current is None:
            continue
        status = MAP_STATUS.match(line.strip())
        if status:
            current["status"] = status.group(1).lower()
            current["status_reason"] = _undefang((status.group(2) or "").strip())
            continue
        bullet = EV_BULLET.match(line.rstrip())
        if bullet:
            tail = bullet.group(4)
            link = MD_LINK.search(tail)
            locators = list(LOCATOR.finditer(tail))  # the engine's own is the last one
            locator = locators[-1] if locators else None
            check = QUOTE_CHECK.search(tail)
            edge = {
                "id": bullet.group(1),
                "relation": bullet.group(2).lower(),
                "decision": bullet.group(3).lower(),
                "publisher": _undefang(tail.split("—")[0].split("[")[0].strip().rstrip(",").strip()),
                "title": _undefang(link.group(1)) if link else "",
                "url": link.group(2) if link else "",
                "locator_kind": locator.group(1) if locator else "section",
                "locator_value": _undefang(locator.group(2)) if locator else "",
                "quote_check": check.group(1).lower() if check else "adopted",
                "quote_score": float(check.group(2)) if check and check.group(2) else None,
                "quote": "",
            }
            current["edges"].append(edge)
            continue
        if edge is not None and line.strip().startswith(">"):
            fragment = _quote_text(line.strip().lstrip(">").strip())
            edge["quote"] = (edge["quote"] + " " + fragment).strip() if edge["quote"] else fragment
            continue
        if line.strip().startswith("- ") or not line.strip():
            edge = None
    return out


def parse_note_frontmatter(path: Path) -> dict[str, Any]:
    """Frontmatter of one published note, including its `claims:` list."""
    text = path.read_text(encoding="utf-8", errors="replace")
    meta = _parse_frontmatter(text)
    claims: list[str] = []
    capturing = False
    for line in _frontmatter_block(text).splitlines():
        if line.startswith("claims:"):
            inline = line.partition(":")[2].strip()
            if inline.startswith("[") and inline.endswith("]"):
                claims = [item.strip() for item in inline[1:-1].split(",") if item.strip()]
                break
            capturing = True
            continue
        if capturing:
            if line.strip().startswith("- "):
                claims.append(line.strip()[2:].strip())
            elif line.strip() and not line.startswith(" "):
                break
    return {
        "path": path,
        "title": meta.get("title") or path.stem,
        "basename": path.stem,
        "type": meta.get("type") or "unknown",
        "status": meta.get("status") or "",
        "domain": meta.get("domain") or "",
        "evidence_level": meta.get("evidence_level") or "",
        "claims": [cid for cid in claims if ID_CELL.match(cid)],
        "frontmatter": meta,
    }


# --------------------------------------------------------------------------
# adopt — turn a published vault into the seed state of an expand run
# --------------------------------------------------------------------------

META_SUFFIXES = {
    "Claim Ledger": "claim_ledger",
    "Source Register": "source_register",
    "Evidence Map": "evidence_map",
    "Gaps and Backlog": "gaps",
    "Contradictions": "contradictions",
    "Wanted Notes": "wanted_notes",
    "Change Log": "change_log",
    "Metadata Schema": "metadata_schema",
    "Validation Report": "validation_report",
}

CONTENT_FOLDER = re.compile(r"^(\d{2}) (.+)$")


def _common_leading_tokens(stems: list[str]) -> list[str]:
    if not stems:
        return []
    split = [stem.split() for stem in stems]
    common: list[str] = []
    for index in range(min(len(tokens) for tokens in split)):
        token = split[0][index]
        if all(tokens[index] == token for tokens in split):
            common.append(token)
        else:
            break
    # A prefix can never swallow a whole file name.
    while common and any(len(tokens) <= len(common) for tokens in split):
        common.pop()
    return common


def infer_prefix(vault: Path) -> dict[str, Any]:
    evidence_dir = vault / "90 Evidence"
    stems = sorted(p.stem for p in evidence_dir.glob("*.md")) if evidence_dir.is_dir() else []
    from_evidence = " ".join(_common_leading_tokens(stems))

    homes = sorted(vault.glob("00 *Home.md"))
    from_home = ""
    if homes:
        tokens = homes[0].stem.split()
        if len(tokens) >= 2 and tokens[0] == "00" and tokens[-1] == "Home":
            from_home = " ".join(tokens[1:-1])

    folder_slug = slugify(vault.name, 40)
    evidence = {
        "evidence_basenames": stems,
        "from_90_evidence": from_evidence,
        "from_home_note": from_home,
        "home_note": homes[0].name if homes else None,
        "folder_name": vault.name,
    }

    if not stems and not homes:
        stop(
            f"{vault} has neither a '90 Evidence' folder nor a '00 * Home.md'; "
            "this does not look like a published research vault. Point adopt at the vault "
            "directory itself, or publish it first.",
            evidence,
        )
    if from_evidence and from_home and from_evidence != from_home:
        stop(
            "prefix is ambiguous: the 90 Evidence basenames say "
            f"{from_evidence!r} but the Home note says {from_home!r}. "
            "Rename by hand so both agree, then re-run adopt.",
            evidence,
        )
    prefix = from_evidence or from_home
    if not prefix and stems and len(stems) < 2:
        stop(
            "prefix cannot be inferred from a single file in 90 Evidence and there is no "
            "prefixed Home note. State the prefix by hand with --prefix.",
            evidence,
        )
    if not prefix:
        confidence = "none"
    elif from_evidence and from_home:
        confidence = "confirmed"
    elif slugify(prefix, 40) == folder_slug:
        confidence = "confirmed"
    else:
        confidence = "inferred"
    evidence["folder_slug_matches"] = bool(prefix) and slugify(prefix, 40) == folder_slug
    return {"prefix": prefix, "confidence": confidence, "evidence": evidence}


def _meta_names(vault: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    for folder in ("90 Evidence", "99 Meta"):
        directory = vault / folder
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            for suffix, key in META_SUFFIXES.items():
                if path.stem == suffix or path.stem.endswith(" " + suffix):
                    found[key] = path.stem
    homes = sorted(vault.glob("00 *Home.md"))
    if homes:
        found["home"] = homes[0].stem
    return found


def _moc_sections(path: Path) -> dict[str, Any]:
    """Description paragraph and the `## Questions this domain owns` bullets."""
    if not path.is_file():
        return {"description": "", "questions": []}
    text = path.read_text(encoding="utf-8", errors="replace")
    body = text[text.find("\n---", 3) + 4 :] if text.startswith("---") else text
    description = ""
    questions: list[str] = []
    section = ""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            section = stripped[3:].strip().lower()
            continue
        if stripped.startswith("# ") or not stripped:
            continue
        if section.startswith("questions"):
            if stripped.startswith("- "):
                questions.append(stripped[2:].strip())
        elif not section and not description and not stripped.startswith(("-", ">", "|")):
            description = stripped
    return {"description": description, "questions": questions}


def _high_water(ids: list[str]) -> int:
    best = 0
    for value in ids:
        match = ID_CELL.match(value)
        if match:
            best = max(best, int(match.group(2)))
    return best


def scan_vault(vault: Path, prefix_override: str | None = None) -> dict[str, Any]:
    """Everything the five parsers can see, with no run attached."""
    if not vault.is_dir():
        die(f"not a directory: {vault}")
    if prefix_override is not None:
        inferred = {
            "prefix": prefix_override,
            "confidence": "declared",
            "evidence": {"folder_name": vault.name},
        }
    else:
        inferred = infer_prefix(vault)
    names = _meta_names(vault)
    evidence_dir = vault / "90 Evidence"

    def meta_path(key: str) -> Path:
        return evidence_dir / f"{names.get(key, key)}.md"

    ledger = parse_claim_ledger(meta_path("claim_ledger"))
    register = parse_source_register(meta_path("source_register"))
    emap = parse_evidence_map(meta_path("evidence_map"))
    gaps = parse_gaps(meta_path("gaps"))
    contradiction_ids: list[str] = []
    cx_path = meta_path("contradictions")
    if cx_path.is_file():
        _, rows = _id_table(cx_path.read_text(encoding="utf-8", errors="replace"), "CX")
        contradiction_ids = [cells[0] for cells in rows]

    folders: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []
    for directory in sorted(p for p in vault.iterdir() if p.is_dir()):
        match = CONTENT_FOLDER.match(directory.name)
        if not match or directory.name.startswith(("90 ", "99 ")):
            continue
        moc_path = directory / f"{directory.name} MOC.md"
        if not moc_path.is_file():
            candidates = sorted(directory.glob("* MOC.md"))
            moc_path = candidates[0] if candidates else moc_path
        sections = _moc_sections(moc_path)
        folder_notes: list[dict[str, Any]] = []
        for path in sorted(directory.glob("*.md")):
            if path == moc_path:
                continue
            meta = parse_note_frontmatter(path)
            meta["folder"] = directory.name
            folder_notes.append(meta)
            notes.append(meta)
        folders.append(
            {
                "folder": directory.name,
                "index": match.group(1),
                "name": match.group(2),
                "moc": moc_path.stem if moc_path.is_file() else None,
                "description": sections["description"],
                "questions": sections["questions"],
                "notes": [note["basename"] for note in folder_notes],
            }
        )

    edge_ids = [edge["id"] for record in emap.values() for edge in record["edges"]]
    return {
        "vault": str(vault),
        "vault_name": vault.name,
        "prefix": inferred["prefix"],
        "prefix_confidence": inferred["confidence"],
        "prefix_evidence": inferred["evidence"],
        "meta_names": names,
        "folders": folders,
        "notes": notes,
        "ledger": ledger,
        "register": register,
        "evidence_map": emap,
        "gaps": gaps,
        "contradiction_ids": contradiction_ids,
        "id_high_water": {
            "claims": _high_water(list(ledger) + list(emap)),
            "sources": _high_water(list(register)),
            "edges": _high_water(edge_ids),
            "gaps": _high_water(list(gaps)),
            "contradictions": _high_water(contradiction_ids),
            "excerpts": len(edge_ids),
            "notes": len(notes),
            "tables": 0,
        },
    }


def vault_index(vault: Path, prefix_override: str | None = None) -> dict[str, Any]:
    """Machine-readable picture of a published vault. Read-only, no run needed."""
    scan = scan_vault(vault, prefix_override)
    ledger, emap = scan["ledger"], scan["evidence_map"]
    cited: dict[str, list[str]] = {}
    for note in scan["notes"]:
        for cid in note["claims"]:
            cited.setdefault(cid, []).append(note["basename"])
    by_status: dict[str, list[str]] = {}
    for cid, claim in sorted(ledger.items()):
        by_status.setdefault(claim["status"], []).append(cid)
    open_gaps = {gid: gap for gid, gap in scan["gaps"].items() if gap["status"] == "open"}
    return {
        "vault": scan["vault"],
        "vault_name": scan["vault_name"],
        "prefix": scan["prefix"],
        "prefix_confidence": scan["prefix_confidence"],
        "prefix_evidence": scan["prefix_evidence"],
        "meta_names": scan["meta_names"],
        "counts": {
            "folders": len(scan["folders"]),
            "notes": len(scan["notes"]),
            "claims": len(ledger),
            "sources": len(scan["register"]),
            "edges": sum(len(rec["edges"]) for rec in emap.values()),
            "gaps": len(scan["gaps"]),
            "open_gaps": len(open_gaps),
            "contradictions": len(scan["contradiction_ids"]),
        },
        "id_high_water": scan["id_high_water"],
        "claims_by_status": by_status,
        "claims": [
            {
                "id": cid,
                "status": claim["status"],
                "text": claim["text"],
                "groups": claim["groups"],
                "source_ids": claim["source_ids"],
                "status_reason": emap.get(cid, {}).get("status_reason", ""),
                "cited_by": cited.get(cid, []),
            }
            for cid, claim in sorted(ledger.items())
        ],
        "open_gaps": [
            {"id": gid, "impact": gap["impact"], "question": gap["question"], "next_action": gap["next_action"]}
            for gid, gap in sorted(open_gaps.items())
        ],
        "sources": [
            {"id": sid, "tier": s["tier"], "url": s["url"], "publisher": s["publisher"],
             "origin_group": fallback_origin_group(s["url"]) if s["url"] else "unknown"}
            for sid, s in sorted(scan["register"].items())
        ],
        "folders": [
            {k: v for k, v in folder.items() if k != "notes"} | {"notes": folder["notes"]}
            for folder in scan["folders"]
        ],
        "linkable_titles": sorted(
            {note["basename"] for note in scan["notes"]}
            | {folder["moc"] for folder in scan["folders"] if folder["moc"]}
            | set(scan["meta_names"].values())
        ),
    }


def adopt_vault(
    vault: Path,
    runs_root: Path,
    brain: Path | None = None,
    prefix_override: str | None = None,
    force: bool = False,
    profile: str | None = None,
) -> dict[str, Any]:
    """Seed an expand run from a published vault.

    Adopted records carry `origin: "adopted"`; their excerpts verify as `adopted`.
    They count as existing support forever, and never as a *new* origin group —
    that distinction is what stops a re-read of the vault from promoting a claim.
    """
    scan = scan_vault(vault, prefix_override)
    problems = vault_table_problems(vault)
    if problems:
        stop(
            "the vault's evidence tables hold rows that cannot be trusted: a malformed row (a raw "
            "newline or pipe inside a cell) can sit next to a forged one, such as a status nobody "
            "computed. Repair those rows by hand, run `validate`, then adopt again. Nothing was written.",
            {"vault": vault.name, "problems": problems},
        )
    label = scan["prefix"] or scan["vault_name"]
    run_id = f"expand-{slugify(scan['vault_name'], 32)}-{sha(str(vault.resolve()), 8)}"
    question = f"Expand {label}: close open gaps and lift qualified claims"
    run = Run.create(question, "expand", runs_root, kind="research", profile=profile, run_id=run_id)
    manifest = run.manifest
    if manifest.get("adopted_from") and not force:
        die(
            f"run {run_id} has already adopted {manifest['adopted_from']}; "
            "pass --force to re-adopt (this rewrites the adopted records)"
        )

    ledger, emap = scan["ledger"], scan["evidence_map"]
    register = scan["register"]

    sources: dict[str, Any] = {}
    for sid, item in sorted(register.items()):
        try:
            curl = canonical_url(item["url"]) if item["url"] else ""
        except ValueError:
            curl = ""
        sources[sid] = {
            "id": sid,
            "canonical_url": curl,
            "url": _redact_url(item["url"]) if item["url"] else "",  # never a stored credential
            "title": item["title"],
            "publisher": item["publisher"],
            "authors": [],
            "published_at": "unknown",
            "accessed_at": item["accessed_at"],
            "source_type": "unknown",
            "authority_tier": item["tier"],
            # A published Origin group column is authoritative: it is the only
            # place a URL-less source can state what it is independent of.
            "origin_group": (
                item.get("origin_group")
                or (fallback_origin_group(curl) if curl else f"adopted:{sid}")
            ),
            "limitations": item["limitations"],
            "use": item["use"],
            "domain_id": "",
            "task_id": "adopt",
            "origin": "adopted",
        }
    url_to_sid = {rec["canonical_url"]: sid for sid, rec in sources.items() if rec["canonical_url"]}
    title_to_sid: dict[str, str] = {}
    host_to_sid: dict[str, list[str]] = {}
    for sid, rec in sources.items():
        if rec["title"]:
            title_to_sid.setdefault(normalize_quote(rec["title"]), sid)
        if rec["canonical_url"]:
            host_to_sid.setdefault(_host_of(rec["canonical_url"]), []).append(sid)

    excerpts: dict[str, Any] = {}
    edges: dict[str, Any] = {}
    counter = 0
    for cid, record in sorted(emap.items()):
        for edge in record["edges"]:
            counter += 1
            xid = f"X-{counter:03d}"
            try:
                curl = canonical_url(edge["url"]) if edge["url"] else ""
            except ValueError:
                curl = ""
            # The Evidence Map and the Source Register describe the same sources;
            # match on URL, then on title, before minting an orphan record.
            sid = url_to_sid.get(curl) or title_to_sid.get(normalize_quote(edge["title"]))
            if sid is None:
                host_matches = host_to_sid.get(_host_of(curl), []) if curl else []
                inherited = sources[host_matches[0]]["authority_tier"] if len(host_matches) == 1 else None
                sid = f"S-A{counter:03d}"
                sources[sid] = {
                    "id": sid,
                    "canonical_url": curl,
                    "url": _redact_url(edge["url"]) if edge["url"] else "",
                    "title": edge["title"],
                    "publisher": edge["publisher"] or "unknown",
                    "authors": [],
                    "published_at": "unknown",
                    "accessed_at": "unknown",
                    "source_type": "unknown",
                    "authority_tier": inherited or 5,
                    "tier_estimated": inherited is None,
                    "origin_group": fallback_origin_group(curl) if curl else f"adopted:{sid}",
                    "limitations": "adopted from the Evidence Map; not present in the Source Register",
                    "use": "",
                    "domain_id": "",
                    "task_id": "adopt",
                    "origin": "adopted",
                }
                if curl:
                    url_to_sid[curl] = sid
            excerpts[xid] = {
                "id": xid,
                "source_id": sid,
                "quote": edge["quote"],
                "quote_hash": sha(normalize_quote(edge["quote"]), 24),
                "locator": {"kind": edge["locator_kind"], "value": edge["locator_value"]},
                "language": "en",
                "context_before": "",
                "context_after": "",
                "retrieved_at": "adopted",
                "verification": {
                    "status": "adopted",
                    "score": edge["quote_score"],
                    "checked_at": None,
                    "adopted_check": edge["quote_check"],
                },
                "task_id": "adopt",
                "origin": "adopted",
            }
            edges[edge["id"]] = {
                "id": edge["id"],
                "claim_id": cid,
                "excerpt_id": xid,
                "relation": edge["relation"],
                "strength": "moderate",
                "rationale": "adopted from the published Evidence Map",
                "decision": "accepted" if edge["decision"] == "accepted" else "provisional",
                "task_id": "adopt",
                "origin": "adopted",
            }

    note_domains: dict[str, str] = {}
    domains: list[dict[str, Any]] = []
    for index, folder in enumerate(scan["folders"], start=1):
        did = f"D{index}"
        domains.append(
            {
                "id": did,
                "name": folder["name"],
                "folder": folder["folder"],
                "moc": folder["moc"],
                "description": folder["description"],
                "questions": folder["questions"],
                "status": "adopted",
                "kind": "research",
            }
        )
        for basename in folder["notes"]:
            note_domains[basename] = did

    claims: dict[str, Any] = {}
    claim_domains: dict[str, list[str]] = {}
    for note in scan["notes"]:
        did = note_domains.get(note["basename"], "")
        for cid in note["claims"]:
            if did and did not in claim_domains.setdefault(cid, []):
                claim_domains[cid].append(did)
    for cid, claim in sorted(ledger.items()):
        mapped = emap.get(cid, {})
        status = claim["status"]
        reason = mapped.get("status_reason", "") or claim["detail"]
        requires_two = None
        if status == "qualified" and "origin group" in reason:
            requires_two = True
        elif status == "supported":
            requires_two = False
        claims[cid] = {
            "id": cid,
            "claim_key": claim_key(claim["text"]),
            "text": claim["text"],
            "claim_type": claim.get("claim_type") or "descriptive",
            "importance": claim.get("importance") or "major",
            "scope": claim["detail"],
            "time_scope": "",
            "action": "",
            "caveat": "",
            "topic_tags": [],
            "domain_ids": claim_domains.get(cid, []),
            "status": status,
            "status_reason": reason,
            "adopted_status": status,
            "adopted_groups": claim["groups"],
            "requires_two_groups": requires_two,
            "supersedes": None,
            "task_id": "adopt",
            "origin": "adopted",
        }

    gaps: dict[str, Any] = {}
    for gid, gap in sorted(scan["gaps"].items()):
        gaps[gid] = {
            "id": gid,
            "fingerprint": sha(normalize_quote(gap["question"]), 20),
            "question": gap["question"],
            "impact": gap["impact"],
            "next_action": gap["next_action"],
            "domain_id": "",
            "status": gap["status"],
            "closed_by": gap["closed_by"],
            "task_id": "adopt",
            "origin": "adopted",
        }

    notes: dict[str, Any] = {}
    for index, note in enumerate(scan["notes"], start=1):
        nid = f"N-{index:03d}"
        notes[nid] = {
            "id": nid,
            "title": note["title"],
            "basename": note["basename"],
            "domain_id": note_domains.get(note["basename"], ""),
            "note_type": note["type"],
            "body_md": "",
            "claim_ids": note["claims"],
            "source_ids": [],
            "summary": "",
            "path": str(note["path"]),
            "task_id": "adopt",
            "origin": "adopted",
        }

    run.save_records("sources", sources)
    run.save_records("excerpts", excerpts)
    run.save_records("claims", claims)
    run.save_records("edges", edges)
    run.save_records("gaps", gaps)
    run.save_records("notes", notes)
    run.save_records("contradictions", {})
    run.save_records("tables", {})
    write_json(run.root / "taxonomy.json", {"domains": domains})

    high_water = dict(scan["id_high_water"])
    high_water["sources"] = max(high_water["sources"], _high_water(list(sources)))
    high_water["excerpts"] = len(excerpts)
    high_water["notes"] = len(notes)
    manifest["counters"] = {key: high_water.get(key, 0) for key in ID_PREFIX}
    manifest["adopted_from"] = str(vault.resolve())
    manifest["brain_root"] = str(brain.resolve()) if brain else None
    manifest["vault_prefix"] = scan["prefix"]
    manifest["status"] = "adopted"
    run.save_manifest(manifest)

    profile_doc = {
        "vault": str(vault.resolve()),
        "vault_name": scan["vault_name"],
        "brain_root": str(brain.resolve()) if brain else None,
        "prefix": scan["prefix"],
        "prefix_confidence": scan["prefix_confidence"],
        "prefix_evidence": scan["prefix_evidence"],
        "meta_names": scan["meta_names"],
        "folders": [
            {k: v for k, v in folder.items()} for folder in scan["folders"]
        ],
        "note_titles": sorted(note["basename"] for note in scan["notes"]),
        "linkable_titles": sorted(
            {note["basename"] for note in scan["notes"]}
            | {folder["moc"] for folder in scan["folders"] if folder["moc"]}
            | set(scan["meta_names"].values())
        ),
        "id_high_water": high_water,
        "adopted_at": now(),
    }
    write_json(run.root / "vault_profile.json", profile_doc)

    report = {
        "run_id": run_id,
        "run_dir": str(run.root),
        "vault": str(vault.resolve()),
        "prefix": scan["prefix"],
        "prefix_confidence": scan["prefix_confidence"],
        "adopted": {
            "claims": len(claims),
            "sources": len(sources),
            "excerpts": len(excerpts),
            "edges": len(edges),
            "gaps": len(gaps),
            "notes": len(notes),
            "domains": len(domains),
        },
        "id_high_water": high_water,
        "next_ids": {
            ID_PREFIX[key]: f"{ID_PREFIX[key]}-{high_water.get(key, 0) + 1:03d}" for key in ID_PREFIX
        },
    }
    write_json(run.reports / "adopt.json", report)
    return report


# --------------------------------------------------------------------------
# ingest
# --------------------------------------------------------------------------

REQUIRED = {
    "sources": {"key", "url", "title", "publisher", "source_type", "authority_tier"},
    "excerpts": {"key", "source_key", "text", "locator"},
    "claims": {"key", "text", "claim_type", "importance"},
    "edges": {"claim_key", "excerpt_key", "relation"},
}

SOURCE_TYPES = {
    "primary", "official", "standard", "regulation", "scholarly",
    "dataset", "news", "analysis", "vendor", "community", "unknown",
}
CLAIM_TYPES = {
    "descriptive", "numerical", "causal", "comparative",
    "predictive", "normative", "platform_statement",
}
IMPORTANCE = {"central", "major", "supporting"}
RELATIONS = {"supports", "refutes", "qualifies"}
FIDELITY = {"verbatim", "derived", "observed"}
HANDBACK_STATUS = {"ok", "partial", "capped", "blocked"}
HANDBACK_STATUS_ALIASES = {"complete": "ok", "completed": "ok", "success": "ok"}


def _require(obj: dict, keys: set[str], where: str) -> None:
    missing = keys - set(obj)
    if missing:
        die(f"{where}: missing keys {sorted(missing)}")


def add_gap(
    run: "Run",
    gaps: dict[str, Any],
    manifest: dict[str, Any],
    question: str,
    *,
    impact: str = "material",
    next_action: str = "",
    domain_id: str = "",
    task_id: str = "",
    kind: str = "research",
    url: str = "",
) -> str | None:
    """One place that mints GAP ids, so policy rejections and hand-backs cannot
    silently skip the deduplication every other gap goes through. A gap about one
    URL (a hand-back's unread page, a policy rejection) is keyed on its kind and
    canonical URL, not its wording: the same page handed back by several workers,
    each with its own rationale, is one gap."""
    question = _one_line(question)
    next_action = _one_line(next_action)
    if not question:
        return None
    curl = canonical_url_safe(url) if url.strip() else ""
    fingerprint = sha(f"{kind}|{curl}", 20) if curl else sha(normalize_quote(question), 20)
    if any(rec.get("fingerprint") == fingerprint for rec in gaps.values()):
        return None
    if curl and any(rec.get("url") == curl and rec.get("gap_kind") == kind for rec in gaps.values()):
        return None
    gid = run.next_id("gaps", manifest)
    gaps[gid] = {
        "id": gid,
        "fingerprint": fingerprint,
        "question": question,
        "impact": impact,
        "next_action": next_action,
        "domain_id": domain_id,
        "status": "open",
        "closed_by": None,
        "gap_kind": kind,
        "task_id": task_id,
        "origin": "new",
    }
    if curl:
        gaps[gid]["url"] = curl
    return gid


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:20]


def inbox_not_ready(path: Path) -> str | None:
    """Why an inbox file may still be mid-write, or None when it is ready: it was
    modified less than INGEST_MIN_AGE seconds ago, or it does not parse as JSON."""
    try:
        age = time.time() - path.stat().st_mtime
        raw = _retry(path.read_bytes)
    except OSError as exc:
        return f"unreadable: {exc}"
    if age < INGEST_MIN_AGE:
        return f"modified {max(age, 0.0):.1f} s ago (under {INGEST_MIN_AGE:g} s)"
    try:
        json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        return f"not valid JSON yet: {exc}"
    return None


def ingested_name(run: Run, path: Path, digest: str) -> str:
    """Where an inbox file is kept once ingested; a reused task id never
    overwrites an earlier raw result (the prospector's shortlist lives there)."""
    if not (run.inbox / "_ingested" / path.name).exists():
        return path.name
    return f"{path.stem}.{digest[:8]}{path.suffix}"


def ingest_file(run: Run, path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    payload = read_json(path)
    if not isinstance(payload, dict):
        die(f"{path.name}: result must be a JSON object")
    task_id = str(payload.get("task_id") or path.stem)
    domain_id = str(payload.get("domain_id") or "")
    arm = str(payload.get("brief") or "")
    if not arm:
        # a worker that dropped the field still belongs to the arm its packet named
        issued = _payload(run.root / "packets" / f"{task_id}.json").get("issued") or {}
        arm = str(issued.get("brief") or "framed")
    if arm not in BRIEFS:
        die(f"{path.name}: brief must be one of {list(BRIEFS)}")
    stats = {"new": {}, "duplicate": {}, "rejected": {}}
    reports: dict[str, Any] = {}

    sources = run.records("sources")
    excerpts = run.records("excerpts")
    claims = run.records("claims")
    edges = run.records("edges")
    contradictions = run.records("contradictions")
    gaps = run.records("gaps")
    notes = run.records("notes")
    tables = run.records("tables")

    policy = policy_of(manifest)
    profile = resolve_profile(load_profiles(), policy["profile"])
    rejections: list[dict[str, Any]] = []
    rejected_source_keys: set[str] = set()
    rejected_excerpt_keys: set[str] = set()

    # Keyed on the stored and the current canonical form, so a source stored
    # before a canonicalisation rule existed still deduplicates.
    url_index: dict[str, str] = {}
    for rid, rec in sources.items():
        for form in (rec.get("canonical_url"), canonical_url_safe(str(rec.get("canonical_url") or ""))):
            if form:
                url_index.setdefault(form, rid)
    quote_index = {(rec["source_id"], rec["quote_hash"]): rid for rid, rec in excerpts.items()}
    claim_index = {rec["claim_key"]: rid for rid, rec in claims.items()}
    edge_index = {
        (rec["claim_id"], rec["excerpt_id"], rec["relation"]): rid for rid, rec in edges.items()
    }

    local_source: dict[str, str] = {}
    local_excerpt: dict[str, str] = {}
    local_claim: dict[str, str] = {}

    def bump(bucket: str, kind: str) -> None:
        stats[bucket][kind] = stats[bucket].get(kind, 0) + 1

    # sources ---------------------------------------------------------
    for item in payload.get("sources", []):
        _require(item, REQUIRED["sources"], f"{path.name} source")
        try:
            curl = canonical_url(str(item["url"]))
        except ValueError as exc:
            # A URL no record may hold (another scheme, `user:password@`, a host
            # that is not public): this source and its excerpts are refused, the
            # rest of the file lands. Logged without credentials; no gap, since a
            # gap would publish the URL.
            rejected_source_keys.add(str(item["key"]))
            rejections.append({
                "url": _redact_url(str(item["url"])), "title": _one_line(item.get("title") or ""),
                "source_type": str(item.get("source_type") or ""), "authority_tier": item.get("authority_tier"),
                "profile": policy["profile"], "rule": "url:refused", "reason": str(exc),
                "task_id": task_id, "domain_id": domain_id, "rejected_at": now(),
            })
            bump("rejected", "sources")
            continue
        stype = str(item["source_type"])
        if stype not in SOURCE_TYPES:
            die(f"{path.name}: source_type {stype!r} not in {sorted(SOURCE_TYPES)}")
        tier = item.get("authority_tier")
        if not isinstance(tier, int) or not 1 <= tier <= 5:
            die(f"{path.name}: authority_tier must be an integer 1..5")
        verdict = evaluate_source(profile, policy["exceptions"], curl, stype, tier, domain_id)
        if not verdict["admitted"]:
            rejected_source_keys.add(str(item["key"]))
            rejections.append(
                {
                    "url": curl,
                    "title": _one_line(item.get("title") or ""),
                    "source_type": stype,
                    "authority_tier": tier,
                    "profile": policy["profile"],
                    "rule": verdict["rule"],
                    "reason": verdict["reason"],
                    "task_id": task_id,
                    "domain_id": domain_id,
                    "rejected_at": now(),
                }
            )
            add_gap(
                run,
                gaps,
                manifest,
                f"Source rejected by policy profile '{policy['profile']}': {curl} — {verdict['reason']}. "
                "What admissible source establishes the same material?",
                impact="material",
                next_action=(
                    # a validated host, shell-quoted anyway: this line is copied into a shell
                    f"Find a source that satisfies profile '{policy['profile']}' for this material, "
                    f"or raise an exception with `policy --allow {shlex.quote(_host_of(curl))} "
                    "--reason '<why>' --confirmed`."
                ),
                domain_id=domain_id,
                task_id=task_id,
                kind="policy_rejection",
                url=curl,
            )
            bump("rejected", "sources")
            continue
        if curl in url_index:
            existing = sources[url_index[curl]]
            # another form of the same page (an arXiv pdf for the abs page): its
            # cache file is keyed on the URL that worker fetched, so remember it
            raw = str(item["url"]).strip()
            if raw != existing.get("url") and raw not in (existing.get("seen_urls") or []):
                existing.setdefault("seen_urls", []).append(raw)
            local_source[str(item["key"])] = url_index[curl]
            bump("duplicate", "sources")
            continue
        sid = run.next_id("sources", manifest)
        # Every field that lands in a table cell, a heading or a link label is
        # made one line here (a quote stays verbatim; it is rendered on one line).
        group = _one_line(item.get("origin_group") or "")
        sources[sid] = {
            "id": sid,
            "canonical_url": curl,
            "url": str(item["url"]).strip(),
            "title": _one_line(item["title"]),
            "publisher": _one_line(item.get("publisher") or "unknown") or "unknown",
            "authors": [_one_line(author) for author in item.get("authors") or []],
            "published_at": _one_line(item.get("published_at") or "unknown"),
            "accessed_at": _one_line(item.get("accessed_at") or today()),
            "source_type": stype,
            "authority_tier": tier,
            # an aggregator host (arxiv.org, doi.org, …) is never a group: the paper is
            "origin_group": (
                group if group and not is_aggregator_group(group) else fallback_origin_group(curl)
            ),
            "limitations": _one_line(item.get("limitations") or ""),
            "use": _one_line(item.get("use") or ""),
            "domain_id": domain_id,
            "task_id": task_id,
            "origin": "new",
            "policy": {"profile": policy["profile"], "rule": verdict["rule"]},
        }
        url_index[curl] = sid
        local_source[str(item["key"])] = sid
        bump("new", "sources")

    # excerpts --------------------------------------------------------
    for item in payload.get("excerpts", []):
        _require(item, REQUIRED["excerpts"], f"{path.name} excerpt")
        skey = str(item["source_key"])
        if skey in rejected_source_keys:
            rejected_excerpt_keys.add(str(item["key"]))
            bump("rejected", "excerpts")
            continue
        if skey not in local_source:
            die(f"{path.name}: excerpt references unknown source key {skey!r}")
        sid = local_source[skey]
        qhash = sha(normalize_quote(str(item["text"])), 24)
        if (sid, qhash) in quote_index:
            local_excerpt[str(item["key"])] = quote_index[(sid, qhash)]
            bump("duplicate", "excerpts")
            continue
        locator = item["locator"]
        if not isinstance(locator, dict) or not locator.get("value"):
            die(f"{path.name}: excerpt locator must be an object with a value")
        xid = run.next_id("excerpts", manifest)
        excerpts[xid] = {
            "id": xid,
            "source_id": sid,
            "quote": str(item["text"]),
            "quote_hash": qhash,
            "locator": {"kind": _one_line(locator.get("kind") or "section") or "section",
                        "value": _one_line(locator["value"])},
            "language": str(item.get("language") or "en"),
            "context_before": str(item.get("context_before") or ""),
            "context_after": str(item.get("context_after") or ""),
            "retrieved_at": str(item.get("retrieved_at") or today()),
            "verification": {"status": "unchecked", "score": None, "checked_at": None},
            "task_id": task_id,
            "origin": "new",
        }
        quote_index[(sid, qhash)] = xid
        local_excerpt[str(item["key"])] = xid
        bump("new", "excerpts")

    # claims ----------------------------------------------------------
    for item in payload.get("claims", []):
        _require(item, REQUIRED["claims"], f"{path.name} claim")
        ctype = str(item["claim_type"])
        if ctype not in CLAIM_TYPES:
            die(f"{path.name}: claim_type {ctype!r} not in {sorted(CLAIM_TYPES)}")
        importance = str(item["importance"])
        if importance not in IMPORTANCE:
            die(f"{path.name}: importance must be one of {sorted(IMPORTANCE)}")
        key = claim_key(str(item["text"]))
        if key in claim_index:
            cid = claim_index[key]
            local_claim[str(item["key"])] = cid
            # keep the strongest importance seen and remember every domain
            existing = claims[cid]
            order = {"supporting": 0, "major": 1, "central": 2}
            if order[importance] > order[existing["importance"]]:
                existing["importance"] = importance
            if domain_id and domain_id not in existing["domain_ids"]:
                existing["domain_ids"].append(domain_id)
            # Re-captures feed the coverage estimate. Both lists are created
            # lazily, so records written before them still load unchanged.
            first_arm = str(existing.get("arm") or "framed")
            if arm != first_arm:
                seen = existing.setdefault("seen_by_arms", [first_arm])
                if arm not in seen:
                    seen.append(arm)
            if task_id != existing.get("task_id") and task_id not in (existing.get("seen_by_tasks") or []):
                existing.setdefault("seen_by_tasks", []).append(task_id)
            bump("duplicate", "claims")
            continue
        cid = run.next_id("claims", manifest)
        claims[cid] = {
            "id": cid,
            "claim_key": key,
            "text": _one_line(item["text"]),
            "claim_type": ctype,
            "importance": importance,
            "scope": _one_line(item.get("scope") or ""),
            "time_scope": _one_line(item.get("time_scope") or ""),
            "action": _one_line(item.get("action") or ""),
            "caveat": _one_line(item.get("caveat") or ""),
            "topic_tags": [_one_line(tag) for tag in item.get("topic_tags", [])],
            "domain_ids": [domain_id] if domain_id else [],
            "status": "proposed",
            "status_reason": "not yet verified",
            "supersedes": str(item["supersedes"]) if item.get("supersedes") else None,
            "requires_two_groups": None,
            "task_id": task_id,
            "arm": arm,
            "origin": "new",
        }
        claim_index[key] = cid
        local_claim[str(item["key"])] = cid
        bump("new", "claims")

    # edges -----------------------------------------------------------
    for item in payload.get("edges", []):
        _require(item, REQUIRED["edges"], f"{path.name} edge")
        ckey, xkey = str(item["claim_key"]), str(item["excerpt_key"])
        if xkey in rejected_excerpt_keys:
            bump("rejected", "edges")
            continue
        if ckey not in local_claim:
            # An expand worker may attach evidence to a claim that already exists in the
            # store (typically an adopted CL-###) rather than redeclaring its text.
            if ckey in claims:
                local_claim[ckey] = ckey
            else:
                die(f"{path.name}: edge references unknown claim key {ckey!r}")
        if xkey not in local_excerpt:
            die(f"{path.name}: edge references unknown excerpt key {xkey!r}")
        relation = str(item["relation"])
        if relation not in RELATIONS:
            die(f"{path.name}: relation must be one of {sorted(RELATIONS)}")
        cid, xid = local_claim[ckey], local_excerpt[xkey]
        if (cid, xid, relation) in edge_index:
            bump("duplicate", "edges")
            continue
        eid = run.next_id("edges", manifest)
        edges[eid] = {
            "id": eid,
            "claim_id": cid,
            "excerpt_id": xid,
            "relation": relation,
            "strength": str(item.get("strength") or "moderate"),
            "rationale": str(item.get("rationale") or ""),
            "decision": "pending",
            "task_id": task_id,
            "origin": "new",
        }
        edge_index[(cid, xid, relation)] = eid
        bump("new", "edges")

    # contradictions --------------------------------------------------
    for item in payload.get("contradictions", []):
        ids = [local_claim.get(str(key), str(key)) for key in item.get("claim_keys", [])]
        ids = [cid for cid in ids if cid in claims]
        if len(ids) < 1:
            continue
        fingerprint = sha("|".join(sorted(ids)) + normalize_quote(str(item.get("summary", ""))), 20)
        if any(rec.get("fingerprint") == fingerprint for rec in contradictions.values()):
            bump("duplicate", "contradictions")
            continue
        xid = run.next_id("contradictions", manifest)
        contradictions[xid] = {
            "id": xid,
            "fingerprint": fingerprint,
            "claim_ids": ids,
            "summary": _one_line(item.get("summary") or ""),
            "axis": _one_line(item.get("axis") or "factual"),
            "severity": _one_line(item.get("severity") or "medium"),
            "status": _one_line(item.get("status") or "unresolved"),
            "resolution": item.get("resolution"),
            "task_id": task_id,
        }
        bump("new", "contradictions")

    # gaps ------------------------------------------------------------
    for item in payload.get("gaps", []):
        detail = item if isinstance(item, dict) else {}
        question = str(detail.get("question", "")) if isinstance(item, dict) else str(item)
        gid = add_gap(
            run,
            gaps,
            manifest,
            question,
            impact=str(detail.get("impact") or "material"),
            next_action=str(detail.get("next_action") or ""),
            domain_id=domain_id,
            task_id=task_id,
        )
        if gid:
            bump("new", "gaps")
        elif question.strip():
            bump("duplicate", "gaps")

    # gap closures -----------------------------------------------------
    for item in payload.get("closed_gaps", []):
        gid = str(item.get("id") or "")
        if gid not in gaps:
            die(f"{path.name}: closed_gaps references unknown gap {gid!r}")
        state = str(item.get("status") or "closed")
        if state not in {"open", "closed", "superseded"}:
            die(f"{path.name}: gap status must be open, closed or superseded")
        gaps[gid]["status"] = state
        gaps[gid]["closed_by"] = str(item.get("closed_by") or "") or None
        gaps[gid]["closed_note"] = str(item.get("note") or "")
        bump("new", "gap_closures")

    # data tables ------------------------------------------------------
    for item in payload.get("tables", []):
        _require(item, {"title", "columns", "rows"}, f"{path.name} table")
        columns = [_one_line(col) for col in item["columns"]]
        table_source = local_source.get(str(item.get("source_key") or ""), "")
        rows_out: list[dict[str, Any]] = []
        for index, row in enumerate(item["rows"], start=1):
            if not isinstance(row, dict) or "cells" not in row:
                die(f"{path.name}: table row {index} must be an object with 'cells'")
            fidelity = str(row.get("fidelity") or "verbatim")
            if fidelity not in FIDELITY:
                die(f"{path.name}: row {index} fidelity must be one of {sorted(FIDELITY)}")
            cells = [str(cell) for cell in row["cells"]]
            if len(cells) != len(columns):
                die(f"{path.name}: row {index} has {len(cells)} cells for {len(columns)} columns")
            row_source = local_source.get(str(row.get("source_key") or ""), table_source)
            if not row_source:
                die(f"{path.name}: row {index} has no resolvable source_key")
            record = {
                "index": index,
                "cells": cells,
                "fidelity": fidelity,
                "source_id": row_source,
                "locator": str(row.get("locator") or item.get("locator") or ""),
                "as_of": str(row.get("as_of") or item.get("as_of") or "unknown"),
                "note": str(row.get("note") or ""),
                "fidelity_result": "unchecked",
                "cell_results": [],
            }
            if fidelity == "derived":
                formula = str(row.get("formula") or "")
                inputs = [str(value) for value in row.get("inputs", [])]
                if not formula or not inputs:
                    die(
                        f"{path.name}: row {index} is 'derived' and must carry both a "
                        "'formula' and non-empty 'inputs' (verbatim values it was computed from)"
                    )
                record["formula"] = formula
                record["inputs"] = inputs
            if fidelity == "observed":
                evidence_note = str(row.get("evidence_note") or "")
                artifact = str(row.get("artifact") or "")
                if not evidence_note or not artifact:
                    die(
                        f"{path.name}: row {index} is 'observed' and must carry both an "
                        "'evidence_note' and an 'artifact' path under cache/raw"
                    )
                resolved = Path(artifact)
                if not resolved.is_absolute():
                    resolved = run.root / artifact
                try:
                    resolved.resolve().relative_to((run.cache / "raw").resolve())
                except ValueError:
                    die(f"{path.name}: row {index} artifact must live under {run.cache / 'raw'}")
                if not resolved.is_file():
                    die(f"{path.name}: row {index} artifact does not exist: {resolved}")
                record["evidence_note"] = evidence_note
                record["artifact"] = str(resolved)
            rows_out.append(record)

        did = run.next_id("tables", manifest)
        tables[did] = {
            "id": did,
            "title": _one_line(item["title"]),
            "columns": columns,
            "rows": rows_out,
            "as_of": str(item.get("as_of") or "unknown"),
            "locator": str(item.get("locator") or ""),
            "source_id": table_source,
            "domain_id": domain_id,
            "task_id": task_id,
            "origin": "new",
        }
        bump("new", "tables")
        for conflict in _table_disagreements(tables[did], sources):
            fingerprint = sha(conflict["fingerprint"], 20)
            if any(rec.get("fingerprint") == fingerprint for rec in contradictions.values()):
                continue
            xid = run.next_id("contradictions", manifest)
            contradictions[xid] = {
                "id": xid,
                "fingerprint": fingerprint,
                "claim_ids": [],
                "table_id": did,
                "summary": conflict["summary"],
                "axis": "numerical",
                "severity": "medium",
                "status": "unresolved",
                "resolution": None,
                "task_id": task_id,
            }
            bump("new", "contradictions")

    # hand-back --------------------------------------------------------
    work_status = str(payload.get("status") or "ok")
    work_status = HANDBACK_STATUS_ALIASES.get(work_status, work_status)
    if work_status not in HANDBACK_STATUS:
        die(f"{path.name}: status must be one of {sorted(HANDBACK_STATUS)}")
    handback = payload.get("handback") or {}
    if work_status != "ok" or handback:
        record = {
            "task_id": task_id,
            "role": str(payload.get("role") or ""),
            "status": work_status,
            "reason": str(handback.get("reason") or ""),
            "consumed": handback.get("consumed") or {},
            "done": handback.get("done") or "",
            "next_action": str(handback.get("next_action") or ""),
            "unread": [],
            "gaps_opened": [],
            "recorded_at": now(),
        }
        for unread in handback.get("unread", []) or []:
            if isinstance(unread, str):  # a worker handed back a bare URL string; treat it as {"url": ...}
                unread = {"url": unread}
            if not isinstance(unread, dict):
                continue
            url = str(unread.get("url") or "").strip()
            if not url:
                continue
            try:
                canonical_url(url)
            except ValueError as exc:
                # never a gap (or a fetch) for a URL no record may hold
                record.setdefault("refused", []).append({"url": _redact_url(url), "why": str(exc)})
                continue
            why = _one_line(unread.get("why_it_matters") or "")
            yield_note = _one_line(unread.get("expected_yield") or "")
            record["unread"].append({"url": url, "why_it_matters": why, "expected_yield": yield_note})
            gid = add_gap(
                run,
                gaps,
                manifest,
                f"Unread source handed back by {record['role'] or 'a worker'} "
                f"({work_status}): {url} — {why or 'no rationale given'}",
                impact="material",
                next_action=f"Fetch and extract {url}. Expected yield: {yield_note or 'unknown'}.",
                domain_id=domain_id,
                task_id=task_id,
                kind="handback",
                url=url,
            )
            if gid:
                record["gaps_opened"].append(gid)
                bump("new", "gaps")
        history = read_json(run.reports / "handbacks.json", [])
        history.append(record)
        reports["handbacks.json"] = history
        stats["handback"] = {
            "status": work_status,
            "unread": len(record["unread"]),
            "gaps_opened": record["gaps_opened"],
        }
        if record.get("refused"):
            stats["handback"]["refused_urls"] = record["refused"]

    # notes (synthesist) ----------------------------------------------
    for item in payload.get("notes", []):
        # The prospector schema uses `notes` for free-text messages to the coordinator,
        # while the synthesist uses it for note objects. Ignore the string form here.
        if not isinstance(item, dict):
            continue
        title = _one_line(item.get("title") or "")
        if not title:
            die(f"{path.name}: note without a title")
        body = str(item.get("body_md") or "")
        summary = str(item.get("summary") or "")
        if any(EXEC_CONTENT.search(text) for text in (title, body, summary)):
            # Core Obsidian only: a note that would run code or load a remote
            # embed in a reader's vault is refused; the rest of the file lands.
            stats.setdefault("rejected_notes", []).append(
                {"title": title, "why": "executable or remote-embedded content (publishing.md: core Obsidian only)"})
            bump("rejected", "notes")
            continue
        nid = run.next_id("notes", manifest)
        notes[nid] = {
            "id": nid,
            "title": title,
            "domain_id": str(item.get("domain_id") or domain_id),
            "note_type": str(item.get("note_type") or "concept"),
            "body_md": body,
            "claim_ids": [str(cid) for cid in item.get("claim_ids", [])],
            "source_ids": [str(sid) for sid in item.get("source_ids", [])],
            "summary": summary,
            "task_id": task_id,
        }
        bump("new", "notes")

    role = str(payload.get("role") or "")
    if role in manifest["work_units"]:
        manifest["work_units"][role] += 1

    # A result that carries its own token count lands in the token ledger. A count
    # the coordinator records with `budget --spend` for the same task wins.
    tokens = payload.get("tokens")
    if tokens is not None:
        if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0:
            digest = file_digest(path)
            ledger = read_jsonl(run.reports / TOKEN_LEDGER)
            if not any(row.get("digest") == digest for row in ledger if isinstance(row, dict)):
                ledger.append({"task_id": task_id, "role": role or task_role(run, task_id, manifest),
                               "tokens": tokens, "at": now(), "via": "result", "digest": digest})
                reports[TOKEN_LEDGER] = ledger
            stats["tokens"] = tokens
        else:
            stats["tokens"] = "ignored: `tokens` must be a non-negative integer"

    if rejections:
        history = read_json(run.reports / "rejected.json", [])
        history.extend(rejections)
        reports["rejected.json"] = history
        stats["rejections"] = rejections
    # The marker travels in the same commit as the records: after any crash the
    # store either has this file's effect and knows it, or has neither.
    digest = file_digest(path)
    manifest.setdefault("ingested_files", {})[ingested_name(run, path, digest)] = {
        "file": path.name, "digest": digest, "task_id": task_id,
        "role": role, "domain_id": domain_id, "arm": arm, "wave": manifest.get("wave", 0), "at": now(),
    }
    manifest["ingest_commits"] = int(manifest.get("ingest_commits", 0)) + 1
    # The novelty log: one line per ingested file, in order. A report, not a
    # record: it travels in the same commit so a crash cannot lose or double it.
    log = read_jsonl(run.reports / "ingest-log.jsonl")
    log.append({
        "task_id": task_id, "role": role, "arm": arm, "domain_id": domain_id,
        "new_claims": stats["new"].get("claims", 0),
        "duplicate_claims": stats["duplicate"].get("claims", 0),
        "rejected_sources": stats["rejected"].get("sources", 0),
        "at": now(),
    })
    reports["ingest-log.jsonl"] = log
    run.commit(manifest, {
        "sources": sources, "excerpts": excerpts, "claims": claims, "edges": edges,
        "contradictions": contradictions, "gaps": gaps, "notes": notes, "tables": tables,
    }, reports)
    return stats


def _table_disagreements(table: dict[str, Any], sources: dict[str, Any]) -> list[dict[str, Any]]:
    """Two sources, same entity, different value for the same column. Both rows
    stay; a contradiction records the conflict. Nothing is ever averaged."""
    conflicts: list[dict[str, Any]] = []
    by_entity: dict[str, list[dict[str, Any]]] = {}
    for row in table["rows"]:
        if not row["cells"]:
            continue
        by_entity.setdefault(normalize_quote(row["cells"][0]), []).append(row)
    for entity, rows in sorted(by_entity.items()):
        if len(rows) < 2:
            continue
        for column_index in range(1, len(table["columns"])):
            seen: dict[str, list[dict[str, Any]]] = {}
            for row in rows:
                value = normalize_quote(row["cells"][column_index])
                if value:
                    seen.setdefault(value, []).append(row)
            if len(seen) < 2:
                continue
            origins = {row["source_id"] for row in rows}
            if len(origins) < 2:
                continue
            column = table["columns"][column_index]
            detail = "; ".join(
                f"{sources.get(row['source_id'], {}).get('publisher', row['source_id'])} "
                f"({row['source_id']}): {row['cells'][column_index]}"
                for row in rows
            )
            conflicts.append(
                {
                    "fingerprint": f"{table['id']}|{entity}|{column}|{detail}",
                    "summary": (
                        f"Sources disagree on '{column}' for '{rows[0]['cells'][0]}' in "
                        f"{table['title']} ({table['id']}): {detail}. Both rows are kept; "
                        "the values are not reconciled or averaged."
                    ),
                }
            )
    return conflicts


# --------------------------------------------------------------------------
# verification + claim status
# --------------------------------------------------------------------------


def verify_run(run: Run) -> dict[str, Any]:
    """Recompute over the merged adopted + new set.

    Adopted excerpts are never re-checked against a cache they predate: they keep
    `verified: adopted` and their recorded edge decision, so adopted evidence stays
    existing support. What they cannot do is create a *new* origin group — a claim
    only rises above the status it was adopted with when non-adopted evidence
    brings a group the vault did not already have.
    """
    sources = run.records("sources")
    excerpts = run.records("excerpts")
    claims = run.records("claims")
    edges = run.records("edges")
    manifest = run.manifest
    profile = resolve_profile(load_profiles(), policy_of(manifest)["profile"])

    views: dict[str, dict[str, Any]] = {}
    for sid, source in sources.items():
        if not source.get("canonical_url"):
            continue
        texts = run.cache_texts(source)
        if texts:
            views[sid] = quote_views(texts)

    tally = {"exact": 0, "fuzzy": 0, "mismatch": 0, "no_cache": 0, "adopted": 0}
    for excerpt in excerpts.values():
        if excerpt.get("origin") == "adopted":
            excerpt["verification"]["status"] = "adopted"
            tally["adopted"] += 1
            continue
        check = check_quote(excerpt["quote"], views.get(excerpt["source_id"]))
        excerpt["verification"] = {**check, "checked_at": now()}
        tally[check["status"]] += 1

    accepted_excerpts = {
        xid for xid, rec in excerpts.items() if rec["verification"]["status"] in {"exact", "fuzzy"}
    }
    for edge in edges.values():
        if edge.get("origin") == "adopted":
            continue  # the published vault already adjudicated this one
        edge["decision"] = "accepted" if edge["excerpt_id"] in accepted_excerpts else "provisional"

    superseded = {
        str(claim["supersedes"]) for claim in claims.values() if claim.get("supersedes")
    }

    # claim status arithmetic
    status_tally: dict[str, int] = {}
    for cid, claim in claims.items():
        supporting = [
            edge for edge in edges.values()
            if edge["claim_id"] == cid and edge["relation"] == "supports" and edge["decision"] == "accepted"
        ]
        refuting = [
            edge for edge in edges.values()
            if edge["claim_id"] == cid and edge["relation"] == "refutes" and edge["decision"] == "accepted"
        ]

        def _group(edge: dict[str, Any]) -> str | None:
            excerpt = excerpts.get(edge["excerpt_id"])
            if not excerpt:
                return None
            source = sources.get(excerpt["source_id"])
            return source["origin_group"] if source else None

        adopted_groups = {
            group for edge in supporting
            if edge.get("origin") == "adopted" and (group := _group(edge))
        }
        new_groups = {
            group for edge in supporting
            if edge.get("origin") != "adopted" and (group := _group(edge))
        }
        groups = adopted_groups | new_groups
        # A guessed tier carries no information, so it must not drive a ceiling.
        tiers = {
            sources[excerpts[edge["excerpt_id"]]["source_id"]]["authority_tier"]
            for edge in supporting
            if edge["excerpt_id"] in excerpts
            and excerpts[edge["excerpt_id"]]["source_id"] in sources
            and not sources[excerpts[edge["excerpt_id"]]["source_id"]].get("tier_estimated")
        }
        declared = claim.get("requires_two_groups")
        needs_two = (
            declared
            if isinstance(declared, bool)
            else (claim["importance"] == "central" or claim["claim_type"] in CORROBORATION_REQUIRED)
        )
        if cid in superseded:
            claim["status"] = "superseded"
            claim["status_reason"] = "replaced by a later claim carrying `supersedes`"
        elif refuting and supporting:
            claim["status"] = "disputed"
            claim["status_reason"] = f"{len(refuting)} accepted refuting edge(s) against {len(supporting)} supporting"
        elif refuting:
            claim["status"] = "refuted"
            claim["status_reason"] = f"{len(refuting)} accepted refuting edge(s), no accepted support"
        elif not supporting:
            claim["status"] = "unsupported"
            claim["status_reason"] = "no accepted supporting evidence"
        elif needs_two and len(groups) < 2:
            claim["status"] = "qualified"
            claim["status_reason"] = (
                f"needs two independent origin groups; has {len(groups)} ({', '.join(sorted(groups)) or 'none'})"
            )
        else:
            claim["status"] = "supported"
            claim["status_reason"] = f"{len(supporting)} accepted edge(s) across {len(groups)} origin group(s)"

        ceiling = tier_status_ceiling(profile, min(tiers) if tiers else None)
        if (
            ceiling
            and claim["status"] in STATUS_RANK
            and ceiling in STATUS_RANK
            and STATUS_RANK[claim["status"]] > STATUS_RANK[ceiling]
        ):
            claim["status"] = ceiling
            claim["status_reason"] = (
                f"best supporting source is tier {min(tiers)}; profile "
                f"'{profile.get('label', '?')}' caps such a claim at {ceiling}"
            )
        claim["origin_groups"] = sorted(groups)
        claim["origin_groups_new"] = sorted(new_groups - adopted_groups)
        claim["origin_groups_adopted"] = sorted(adopted_groups)
        status_tally[claim["status"]] = status_tally.get(claim["status"], 0) + 1

    gaps = run.records("gaps")
    for gap in gaps.values():
        gap.setdefault("closed_by", None)
        if gap.get("closed_by") and gap.get("status") == "open":
            gap["status"] = "closed"

    run.save_records("excerpts", excerpts)
    run.save_records("edges", edges)
    run.save_records("claims", claims)
    run.save_records("gaps", gaps)

    report = {
        "checked_at": now(),
        "profile": profile.get("label", "open"),
        "excerpts": tally,
        "claims": status_tally,
        "accepted_edges": sum(1 for edge in edges.values() if edge["decision"] == "accepted"),
        "adopted_edges": sum(1 for edge in edges.values() if edge.get("origin") == "adopted"),
        "total_edges": len(edges),
        "open_gaps": sum(1 for gap in gaps.values() if gap.get("status") == "open"),
    }
    write_json(run.reports / "verify.json", report)
    return report


def relabel_edge(run: Run, edge_id: str, relation: str, why: str) -> dict[str, Any]:
    """The one audited correction of an edge record: its relation. The old value
    and the reason go into the edge's rationale and `relabels` list, an audit line
    into reports/ingest-log.jsonl, all in one commit. Claim status is untouched
    until the next `verify` recomputes it."""
    why = why.strip()
    if not why:
        die("relabel-edge needs --why: the reason is kept on the edge and in the log")
    if relation not in RELATIONS:
        die(f"--relation must be one of {sorted(RELATIONS)}")
    manifest = run.manifest
    edges = run.records("edges")
    edge = edges.get(edge_id)
    if edge is None:
        die(f"unknown edge {edge_id!r}")
    if edge.get("origin") == "adopted":
        die(f"{edge_id} was adopted from a published vault; correct it there, not in the run")
    previous = str(edge.get("relation"))
    if previous == relation:
        die(f"{edge_id} is already '{relation}'; nothing to change")
    twin = next((eid for eid, other in edges.items() if eid != edge_id
                 and other.get("claim_id") == edge.get("claim_id")
                 and other.get("excerpt_id") == edge.get("excerpt_id")
                 and other.get("relation") == relation), None)
    if twin:
        die(f"{twin} already links this claim and excerpt as '{relation}'; nothing to relabel")
    at = now()
    note = f"[relabelled {previous} -> {relation}: {why}]"
    rationale = str(edge.get("rationale") or "").strip()
    edge["rationale"] = f"{rationale} {note}".strip()
    edge["relation"] = relation
    edge["decision"] = "pending"  # verify decides again
    edge.setdefault("relabels", []).append({"from": previous, "to": relation, "why": why, "at": at})
    log = read_jsonl(run.reports / "ingest-log.jsonl")
    log.append({"edge": edge_id, "claim_id": edge.get("claim_id"), "from": previous, "to": relation,
                "why": why, "at": at})
    # a store commit: `resume` names `verify` until verify has run again
    manifest["ingest_commits"] = int(manifest.get("ingest_commits", 0)) + 1
    run.commit(manifest, {"edges": edges}, {"ingest-log.jsonl": log})
    return {
        "edge": edge_id,
        "claim_id": edge.get("claim_id"),
        "from": previous,
        "to": relation,
        "why": why,
        "next": "re-run verify; claim status still reflects the old relation until it does",
        "verify_with": f"verify --run {shlex.quote(str(manifest['run_id']))}",
    }


def _best_window(needle: str, haystack: str) -> tuple[float, int]:
    """Cheap sliding comparison: the best ratio of the quote against a window of
    the page of the same length, and where that window starts. The window steps
    by a quarter of the quote's length, so a near miss can score below its best
    alignment; the check errs towards `mismatch`. A high ratio separates a typo
    from a paraphrase, not one meaning from another: `meaning_change` does that."""
    if not needle or not haystack:
        return 0.0, 0
    size = len(needle)
    if size > len(haystack):
        return round(difflib.SequenceMatcher(None, needle, haystack).ratio(), 4), 0
    best, best_start = 0.0, 0
    step = max(1, size // 4)
    for start in range(0, len(haystack) - size + 1, step):
        window = haystack[start : start + size]
        ratio = difflib.SequenceMatcher(None, needle, window).quick_ratio()
        if ratio > best:
            exact = difflib.SequenceMatcher(None, needle, window).ratio()
            if exact > best:
                best, best_start = exact, start
            if best >= 0.999:
                break
    return round(best, 4), best_start


def _best_window_ratio(needle: str, haystack: str) -> float:
    return _best_window(needle, haystack)[0]


# The meaning guard on a ratio match. "Increased" and "decreased" are one
# letter apart, and 25 and 35 one digit, so a window ratio above the threshold
# does not show that a quote says what its page says. A ratio match is `fuzzy`
# only when the quote and the passage it lined up with carry the same digit
# sequence and no word that differs between them is in FLIP_WORDS, ends in
# "n't", or is the other word with a negating prefix (likely / unlikely).
# Otherwise it is a `mismatch`. This catches the edits that flip a claim; it
# does not judge meaning. Whether a passage entails a claim is the verifier's
# judgment.
FLIP_WORDS = frozenset(
    # negation
    "not no never none nor neither nobody nothing nowhere without cannot non lack lacks lacked absent "
    "hardly barely scarcely "
    # hedges on a number
    "about approximately roughly nearly almost around exactly percent percentage "
    # direction and change
    "increase increases increased increasing decrease decreases decreased decreasing rise rises rose "
    "risen rising fall falls fell fallen falling raise raises raised lower lowers lowered reduce reduces "
    "reduced reducing gain gains gained lose loses lost loss improve improves improved worsen worsens "
    "worsened grow grows grew shrink shrinks shrank up down above below over under upward downward "
    "positive positively negative negatively plus minus before after earlier later inside outside "
    # comparison
    "more less fewer most least higher lower highest lowest high low greater greatest smaller smallest "
    "larger largest bigger biggest better best worse worst faster slower longer shorter exceed exceeds "
    "exceeded max min maximum minimum significant significantly insignificant nonsignificant "
    "same different similar identical equal true false "
    # quantity, frequency, certainty
    "all every each any some few many several both either only half majority minority always often "
    "usually sometimes occasionally rarely seldom frequently generally may might must can could should "
    "will would likely unlikely possible impossible probably certainly "
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty ninety hundred "
    "thousand million billion once twice double triple single first second third quarter dozen".split()
)
NEGATING_PREFIXES = ("un", "in", "im", "il", "ir", "non", "dis")
QUOTE_WORD = re.compile(r"[^\W_]+(?:'[^\W_]+)*")


def _flip_reason(left: list[str], right: list[str]) -> str | None:
    for word in left + right:
        if word in FLIP_WORDS or word.endswith("n't"):
            return f"changed word: {' '.join(left) or '(none)'} / {' '.join(right) or '(none)'}"
    for a in left:
        for b in right:
            short, long = sorted((a, b), key=len)
            if len(short) >= 3 and any(long == prefix + short for prefix in NEGATING_PREFIXES):
                return f"negated word: {a} / {b}"
    return None


def meaning_change(needle: str, haystack: str, start: int) -> str | None:
    """Why a ratio match at `start` may not count as `fuzzy`, or None. The quote
    is lined up word by word with the page around the matched window; the
    digit sequences must be equal, and no differing word may flip the meaning
    (FLIP_WORDS above)."""
    size = len(needle)
    pad = max(16, size // 2)
    lo = max(0, start - pad)
    region = haystack[lo : start + size + pad]
    quote_words = QUOTE_WORD.findall(needle)
    page_words = QUOTE_WORD.findall(region)
    if not quote_words or not page_words:
        return None
    matcher = difflib.SequenceMatcher(None, quote_words, page_words, autojunk=False)
    blocks = [block for block in matcher.get_matching_blocks() if block.size]
    if blocks:
        # Line up on the longest common run; a stray short match far from it (a
        # repeated "the") is context, not part of the passage.
        main = max(blocks, key=lambda block: block.size)
        slack = max(2, len(quote_words) // 8)
        kept = [b for b in blocks if abs((b.b - b.a) - (main.b - main.a)) <= slack]
        first, last = kept[0], kept[-1]
        begin = max(0, first.b - first.a)
        end = min(len(page_words), last.b + last.size + (len(quote_words) - last.a - last.size))
        passage = page_words[begin:end]
    else:
        passage = page_words
    digits_quote = [run for word in quote_words for run in re.findall(r"\d+", word)]
    digits_page = [run for word in passage for run in re.findall(r"\d+", word)]
    if digits_quote != digits_page:
        return f"digits differ: {' '.join(digits_quote) or '(none)'} / {' '.join(digits_page) or '(none)'}"
    diff = difflib.SequenceMatcher(None, quote_words, passage, autojunk=False)
    for tag, i1, i2, j1, j2 in diff.get_opcodes():
        if tag != "equal":
            reason = _flip_reason(quote_words[i1:i2], passage[j1:j2])
            if reason:
                return reason
    return None


def datacheck_run(run: Run) -> dict[str, Any]:
    """Cell-level fidelity check for `records/tables.json`.

    A `verbatim` row is only as good as its worst cell: every non-empty cell must
    be findable in the cached fetch of its own source. `derived` and `observed`
    rows are not checked here — their contract (formula + verbatim inputs, or
    artifact + evidence note) was enforced at ingest.
    """
    tables = run.records("tables")
    sources = run.records("sources")
    cache_text: dict[str, str] = {}
    for sid, source in sources.items():
        if not source.get("canonical_url"):
            continue
        text = run.cache_text(source)
        if text is not None:
            cache_text[sid] = normalize_quote(text)

    tally = {"exact": 0, "fuzzy": 0, "mismatch": 0, "no_cache": 0, "derived": 0, "observed": 0}
    flagged: list[dict[str, Any]] = []
    for did, table in sorted(tables.items()):
        for row in table["rows"]:
            if row["fidelity"] != "verbatim":
                row["fidelity_result"] = row["fidelity"]
                row["cell_results"] = []
                tally[row["fidelity"]] += 1
                continue
            body = cache_text.get(row["source_id"])
            results: list[dict[str, Any]] = []
            for index, cell in enumerate(row["cells"]):
                value = normalize_quote(cell)
                if not value:
                    results.append({"column": table["columns"][index], "status": "exact", "score": 1.0})
                    continue
                if body is None:
                    results.append({"column": table["columns"][index], "status": "no_cache", "score": None})
                    continue
                if value in body:
                    results.append({"column": table["columns"][index], "status": "exact", "score": 1.0})
                    continue
                score, start = _best_window(value, body)
                changed = meaning_change(value, body, start) if score >= FUZZY_THRESHOLD else None
                result = {
                    "column": table["columns"][index],
                    "status": "fuzzy" if score >= FUZZY_THRESHOLD and not changed else "mismatch",
                    "score": score,
                }
                if changed:
                    result["meaning_change"] = changed
                results.append(result)
            statuses = {item["status"] for item in results}
            for worst in ("mismatch", "no_cache", "fuzzy", "exact"):
                if worst in statuses:
                    break
            row["cell_results"] = results
            row["fidelity_result"] = worst
            tally[worst] += 1
            if worst in {"mismatch", "no_cache"}:
                flagged.append(
                    {
                        "table_id": did,
                        "row": row["index"],
                        "entity": row["cells"][0] if row["cells"] else "",
                        "result": worst,
                        "cells": [item for item in results if item["status"] == worst],
                    }
                )
    run.save_records("tables", tables)
    report = {
        "checked_at": now(),
        "tables": len(tables),
        "rows": sum(len(table["rows"]) for table in tables.values()),
        "results": tally,
        "flagged": flagged,
        "rule": (
            "A mismatch row is published with a warning marker, never silently dropped and "
            "never treated as trustworthy."
        ),
    }
    write_json(run.reports / "datacheck.json", report)
    return report


# --------------------------------------------------------------------------
# coverage — measured at run end, never asserted
#
# Four numbers say how much discovery may have missed: (i) the overlap of two
# discovery arms in one domain and the Chapman (bias-corrected Lincoln–Petersen)
# estimate of the claim population, valid only between arms with the same brief;
# (ii) off-skeleton claims per page read; (iii) the recall of a claim-blind
# re-read of sampled cached pages; (iv) the novelty log, new claims per worker.
# --------------------------------------------------------------------------

IMPORTANCE_LEVELS = ("high", "medium", "low")
DISCOVERY_ROLES = {"prospector", "extractor"}


def _id_order(value: str) -> tuple[int, str]:
    tail = value.rsplit("-", 1)[-1]
    return (int(tail), value) if tail.isdigit() else (10 ** 9, value)


def validate_reread(data: Any, claims: dict[str, Any], sources: dict[str, Any]) -> list[str]:
    """Every problem in a re-read file; an empty list means it is well formed."""
    problems: list[str] = []
    if not isinstance(data, dict):
        return ["the file must hold a JSON object"]
    seed = data.get("seed")
    if not isinstance(seed, int) or isinstance(seed, bool):
        problems.append("'seed' must be an integer (the seed the sample was drawn with)")
    pages = data.get("pages")
    if not isinstance(pages, list) or not pages:
        return problems + ["'pages' must be a non-empty list"]
    seen_pages: set[str] = set()
    for index, page in enumerate(pages, start=1):
        where = f"pages[{index}]"
        if not isinstance(page, dict):
            problems.append(f"{where} must be an object")
            continue
        sid = page.get("source_id")
        if not isinstance(sid, str) or sid not in sources:
            problems.append(f"{where}.source_id {sid!r} is not a source of this run")
        elif sid in seen_pages:
            problems.append(f"{where}.source_id {sid} appears twice")
        else:
            seen_pages.add(sid)
        findings = page.get("findings")
        if not isinstance(findings, list):
            problems.append(f"{where}.findings must be a list (empty when the page held nothing)")
            continue
        for number, finding in enumerate(findings, start=1):
            at = f"{where}.findings[{number}]"
            if not isinstance(finding, dict):
                problems.append(f"{at} must be an object")
                continue
            if not isinstance(finding.get("text"), str) or not finding["text"].strip():
                problems.append(f"{at}.text must be a non-empty string")
            if finding.get("importance") not in IMPORTANCE_LEVELS:
                problems.append(f"{at}.importance must be one of {'|'.join(IMPORTANCE_LEVELS)}")
            if "matched_claim" not in finding:
                problems.append(f"{at}.matched_claim is required (a claim id, or null)")
            elif finding["matched_claim"] is not None and finding["matched_claim"] not in claims:
                problems.append(f"{at}.matched_claim {finding['matched_claim']!r} is not a claim of this run")
    return problems


def reread_summary(data: dict[str, Any]) -> dict[str, Any]:
    """Recall of the claim set against a claim-blind re-read, overall and per importance."""
    by_level = {level: {"findings": 0, "matched": 0} for level in IMPORTANCE_LEVELS}
    unmatched_high: list[dict[str, str]] = []
    for page in data["pages"]:
        for finding in page["findings"]:
            level = by_level[finding["importance"]]
            level["findings"] += 1
            if finding["matched_claim"]:
                level["matched"] += 1
            elif finding["importance"] == "high":
                unmatched_high.append({"source_id": page["source_id"], "text": finding["text"].strip()})
    for level in by_level.values():
        level["recall"] = round(level["matched"] / level["findings"], 3) if level["findings"] else None
    total = sum(level["findings"] for level in by_level.values())
    matched = sum(level["matched"] for level in by_level.values())
    high = by_level["high"]
    return {
        "seed": data["seed"],
        "pages": len(data["pages"]),
        "findings": total,
        "matched": matched,
        "recall": round(matched / total, 3) if total else None,
        "high": high,
        "missed_high_share": round(1 - high["recall"], 3) if high["recall"] is not None else None,
        "by_importance": by_level,
        "unmatched_high": unmatched_high,
    }


def coverage_report(run: Run, sample: int | None = None, seed: int = 0,
                    reread: dict[str, Any] | None = None) -> dict[str, Any]:
    manifest = run.manifest
    claims = run.records("claims")
    sources = run.records("sources")
    excerpts = run.records("excerpts")
    previous = read_json(run.reports / "coverage.json", {})

    # one entry per worker result: role, domain and arm of each task id
    tasks: dict[str, dict[str, str]] = {}
    for mark in (manifest.get("ingested_files") or {}).values():
        tid = str(mark.get("task_id") or "")
        if tid and tid not in tasks:
            tasks[tid] = {"role": str(mark.get("role") or ""), "domain_id": str(mark.get("domain_id") or ""),
                          "arm": str(mark.get("arm") or "framed")}

    # (a) arms: the claims each arm minted, and how they fared in verify
    arms: dict[str, dict[str, Any]] = {}
    for claim in claims.values():
        if claim.get("origin") == "adopted":
            continue
        entry = arms.setdefault(str(claim.get("arm") or "framed"), {"claims": 0, "seen": 0, "status": {}})
        entry["claims"] += 1
        entry["status"][claim["status"]] = entry["status"].get(claim["status"], 0) + 1
    for claim in claims.values():
        for arm in claim.get("seen_by_arms") or []:
            if arm != (claim.get("arm") or "framed") and arm in arms:
                arms[arm]["seen"] += 1
    for entry in arms.values():
        entry["seen"] += entry["claims"]
        entry["unsupported_pct"] = round(100 * entry["status"].get("unsupported", 0) / entry["claims"], 1)

    # (b) overlap: capture-recapture between the discovery arms of one domain
    captures: dict[str, set[str]] = {tid: set() for tid, task in tasks.items() if task["role"] == "prospector"}
    for cid, claim in claims.items():
        for tid in [claim.get("task_id"), *(claim.get("seen_by_tasks") or [])]:
            if tid in captures:
                captures[tid].add(cid)
    by_domain: dict[str, list[str]] = {}
    for tid in sorted(captures):
        by_domain.setdefault(tasks[tid]["domain_id"], []).append(tid)
    pairs: list[dict[str, Any]] = []
    for did, tids in sorted(by_domain.items()):
        for i, first in enumerate(tids):
            for second in tids[i + 1:]:
                n1, n2 = len(captures[first]), len(captures[second])
                m = len(captures[first] & captures[second])
                same_brief = tasks[first]["arm"] == tasks[second]["arm"]
                # No shared claim: no recapture, so no population estimate at all.
                label = "undefined" if not m else "valid" if same_brief else "indicative"
                pairs.append({
                    "domain_id": did,
                    "arm_a": {"task_id": first, "brief": tasks[first]["arm"]},
                    "arm_b": {"task_id": second, "brief": tasks[second]["arm"]},
                    "n1": n1, "n2": n2, "m": m,
                    "chapman": round((n1 + 1) * (n2 + 1) / (m + 1) - 1, 1) if m else None,
                    "lincoln_petersen": round(n1 * n2 / m, 1) if m else None,
                    "valid": label == "valid",
                    "label": label,
                })
    overlap = {
        "claims_seen_by_multiple_arms": sum(1 for c in claims.values() if len(set(c.get("seen_by_arms") or [])) >= 2),
        "pairs": pairs,
        "rule": ("N-hat = (n1+1)(n2+1)/(m+1) - 1 over claims proposed by two prospectors of one domain. "
                 "Valid only between arms with the same brief; between a framed and a blind arm the "
                 "arms are built to differ, so the estimate is indicative. With no shared claim (m = 0) "
                 "there is no estimate: undefined."),
    }

    # (c) off-skeleton claims per cached page an extractor read
    extractor_tasks = {tid for tid, task in tasks.items() if task["role"] == "extractor"}
    pages_read = {
        x["source_id"] for x in excerpts.values()
        if x.get("task_id") in extractor_tasks and x["source_id"] in sources
        and run.cache_files(sources[x["source_id"]])
    }
    off = sum(1 for c in claims.values() if OFF_SKELETON_TAG in (c.get("topic_tags") or []))
    off_skeleton = {"claims": off, "pages_read": len(pages_read),
                    "per_page": round(off / len(pages_read), 2) if pages_read else None}

    # (d) novelty: new claims per ingested worker result, in order
    cumulative = 0
    curve: list[dict[str, Any]] = []
    for row in read_jsonl(run.reports / "ingest-log.jsonl"):
        # one line per ingested file; skipped files and edge relabels are audit lines
        if not isinstance(row, dict) or "new_claims" not in row:
            continue
        cumulative += int(row.get("new_claims") or 0)
        curve.append({**row, "cumulative_new_claims": cumulative})
    streak = 0
    for row in reversed([r for r in curve if r.get("role") in DISCOVERY_ROLES]):
        if int(row.get("new_claims") or 0):
            break
        streak += 1
    novelty = {"rows": curve, "new_claims_total": cumulative, "trailing_zero_new": streak}

    # (e) the pages a claim-blind reader re-reads
    sample_block = previous.get("sample")
    if sample is not None:
        cached = sorted((sid for sid, s in sources.items() if s.get("origin") != "adopted" and run.cache_files(s)),
                        key=_id_order)
        picked = sorted(random.Random(seed).sample(cached, min(sample, len(cached))), key=_id_order)
        sample_block = {
            "seed": seed, "requested": sample,
            "pages": [{"source_id": sid, "url": sources[sid].get("url") or sources[sid].get("canonical_url"),
                       "cache_file": str(run.cache_files(sources[sid])[0])} for sid in picked],
        }
    reread_block = previous.get("reread")
    if reread is not None:
        reread_block = reread_summary(reread)
        if sample_block:
            reread_block["seed_matches_sample"] = reread_block["seed"] == sample_block.get("seed")

    report = {
        "run_id": manifest["run_id"],
        "generated_at": now(),
        "arms": arms,
        "overlap": overlap,
        "off_skeleton": off_skeleton,
        "novelty": novelty,
        "sample": sample_block,
        "reread": reread_block,
        "rule": "Coverage is measured, never asserted: publish these numbers beside the gaps.",
    }
    write_json(run.reports / "coverage.json", report)
    return report


def print_coverage(report: dict[str, Any]) -> None:
    """The short human table; to stderr, so stdout stays machine-readable."""
    out = sys.stderr
    out.write(f"coverage for {report['run_id']}\n")
    for measure, value in coverage_rows(report):
        out.write(f"  {measure:<20} {value}\n")
    novelty = report["novelty"]
    out.write(f"  {'Novelty':<20} {novelty['new_claims_total']} new claims over {len(novelty['rows'])} "
              f"ingested results; last {novelty['trailing_zero_new']} discovery result(s) added none\n")
    for page in (report.get("sample") or {}).get("pages", []):
        out.write(f"  sample  {page['source_id']:<8} {page['url']}\n          {page['cache_file']}\n")


# --------------------------------------------------------------------------
# status / packet
# --------------------------------------------------------------------------


def build_status(run: Run) -> dict[str, Any]:
    manifest = run.manifest
    taxonomy = run.taxonomy
    claims = run.records("claims")
    sources = run.records("sources")
    gaps = run.records("gaps")
    contradictions = run.records("contradictions")

    per_domain = []
    for domain in taxonomy.get("domains", []):
        did = domain["id"]
        domain_claims = [c for c in claims.values() if did in c.get("domain_ids", [])]
        domain_sources = [s for s in sources.values() if s.get("domain_id") == did]
        per_domain.append(
            {
                "id": did,
                "name": domain["name"],
                "status": domain.get("status", "pending"),
                "claims": len(domain_claims),
                "supported": sum(1 for c in domain_claims if c["status"] == "supported"),
                "sources": len(domain_sources),
                "open_gaps": sum(1 for g in gaps.values() if g.get("domain_id") == did and g["status"] == "open"),
            }
        )

    limits = limits_of(manifest)
    return {
        "run_id": manifest["run_id"],
        "question": manifest["question"],
        "mode": manifest["mode"],
        "kind": manifest.get("kind", "research"),
        "wave": manifest["wave"],
        "work_units": manifest["work_units"],
        "policy": policy_of(manifest),
        "budget": {
            "sources_used": len(sources),
            "sources_ceiling": limits["sources"],
            "waves_used": manifest["wave"],
            "waves_ceiling": limits["waves"],
            "opus_issued": manifest.get("opus_issued", {}),
            "max_opus_per_wave": limits["max_opus_per_wave"],
            **token_usage(run, manifest),
            "limits": limits,
        },
        "totals": {
            "sources": len(sources),
            "claims": len(claims),
            "supported": sum(1 for c in claims.values() if c["status"] == "supported"),
            "qualified": sum(1 for c in claims.values() if c["status"] == "qualified"),
            "disputed": sum(1 for c in claims.values() if c["status"] in {"disputed", "refuted"}),
            "unsupported": sum(1 for c in claims.values() if c["status"] == "unsupported"),
            "tables": len(run.records("tables")),
            "open_gaps": sum(1 for g in gaps.values() if g["status"] == "open"),
            "unresolved_contradictions": sum(
                1 for x in contradictions.values() if x["status"] != "resolved"
            ),
        },
        "domains": per_domain,
        "pending_inbox": sorted(p.name for p in run.inbox.glob("*.json")),
    }


def build_expand_surface(run: Run) -> dict[str, Any]:
    """The open surface of an adopted vault, ranked. This list is what the
    coordinator turns into a pick-list — and the pick-list is the taxonomy gate."""
    claims = run.records("claims")
    gaps = run.records("gaps")
    edges = run.records("edges")
    notes = run.records("notes")
    taxonomy = run.taxonomy

    cited: set[str] = set()
    notes_per_domain: dict[str, int] = {}
    for note in notes.values():
        cited.update(note.get("claim_ids", []))
        notes_per_domain[note.get("domain_id", "")] = notes_per_domain.get(note.get("domain_id", ""), 0) + 1
    has_edges = {edge["claim_id"] for edge in edges.values()}

    items: list[dict[str, Any]] = []

    for gid, gap in sorted(gaps.items()):
        if gap.get("status") != "open":
            continue
        impact = gap.get("impact", "material")
        if impact not in {"critical", "material"}:
            continue
        items.append(
            {
                "rank": 1 if impact == "critical" else 2,
                "kind": "gap",
                "id": gid,
                "label": gap["question"],
                "why": f"{impact} gap, still open",
                "next_action_from_vault": gap.get("next_action", ""),
            }
        )

    for cid, claim in sorted(claims.items()):
        status = claim.get("status")
        if status == "disputed":
            items.append(
                {
                    "rank": 3,
                    "kind": "claim",
                    "id": cid,
                    "label": claim["text"],
                    "why": f"disputed — {claim.get('status_reason', '')}",
                    "next_action_from_vault": "",
                }
            )
        elif status == "qualified" and (
            claim.get("importance") == "central" or claim.get("requires_two_groups") is True
        ):
            items.append(
                {
                    "rank": 4,
                    "kind": "claim",
                    "id": cid,
                    "label": claim["text"],
                    "why": f"qualified and load-bearing — {claim.get('status_reason', '')}",
                    "next_action_from_vault": "",
                }
            )
        elif status == "unsupported" and cid not in has_edges and cid not in cited:
            items.append(
                {
                    "rank": 5,
                    "kind": "claim",
                    "id": cid,
                    "label": claim["text"],
                    "why": "unsupported with no evidence edge and no note citing it",
                    "next_action_from_vault": "",
                }
            )

    for domain in taxonomy.get("domains", []):
        if notes_per_domain.get(domain["id"], 0):
            continue
        for question in domain.get("questions", []):
            items.append(
                {
                    "rank": 6,
                    "kind": "question",
                    "id": domain["id"],
                    "label": question,
                    "why": f"MOC question in {domain['name']}, which has no notes yet",
                    "next_action_from_vault": "",
                }
            )

    items.sort(key=lambda item: (item["rank"], item["id"]))
    return {
        "run_id": run.manifest["run_id"],
        "vault": run.manifest.get("adopted_from"),
        "prefix": run.manifest.get("vault_prefix"),
        "ranking": [
            "1 critical gaps", "2 material gaps", "3 disputed claims",
            "4 qualified central claims", "5 uncited unsupported claims",
            "6 MOC questions in domains with no notes",
        ],
        "counts": {
            "open_surface": len(items),
            "gaps": sum(1 for item in items if item["kind"] == "gap"),
            "claims": sum(1 for item in items if item["kind"] == "claim"),
            "questions": sum(1 for item in items if item["kind"] == "question"),
        },
        "surface": items,
    }


def budget_block(run: Run, manifest: dict[str, Any]) -> dict[str, Any]:
    limits = limits_of(manifest)
    wave = str(manifest.get("wave", 0))
    issued = (manifest.get("opus_issued") or {}).get(wave, 0)
    return {
        "limits": limits,
        "wave": manifest.get("wave", 0),
        "opus_issued_this_wave": issued,
        "opus_remaining_this_wave": max(0, limits["max_opus_per_wave"] - issued),
        "sources_used": len(run.records("sources")),
        "read_tokens_per_agent": limits["read_tokens_per_agent"],
        "fetches_per_agent": limits["fetches_per_agent"],
        "searches_per_agent": limits["searches_per_agent"],
        "max_notes_per_synthesist": limits["max_notes_per_synthesist"],
        "rule": (
            "These are ceilings, not targets. On reaching one, stop and hand back with "
            '"status": "capped" and a handback block listing every unread URL.'
        ),
    }


def task_role(run: Run, task_id: str, manifest: dict[str, Any] | None = None) -> str:
    """The role a task id was issued for: its saved packet, else an ingested result."""
    issued = _payload(run.root / "packets" / f"{task_id}.json").get("issued") or {}
    if issued.get("role"):
        return str(issued["role"])
    for mark in ((manifest or run.manifest).get("ingested_files") or {}).values():
        if mark.get("task_id") == task_id and mark.get("role"):
            return str(mark["role"])
    return ""


def token_usage(run: Run, manifest: dict[str, Any]) -> dict[str, Any]:
    """Subagent tokens as recorded in reports/token-ledger.jsonl, never estimated.
    A count recorded with `budget --spend` is the coordinator's reading of what the
    harness reported; a result's own `tokens` field counts only for a task that
    has no such entry."""
    rows = [row for row in read_jsonl(run.reports / TOKEN_LEDGER) if isinstance(row, dict)]
    spent = {str(row.get("task_id")) for row in rows if row.get("via") != "result"}
    used = 0
    by_role: dict[str, int] = {}
    for row in rows:
        if row.get("via") == "result" and str(row.get("task_id")) in spent:
            continue
        tokens = row.get("tokens")
        if not isinstance(tokens, int) or isinstance(tokens, bool):
            continue
        used += tokens
        role = str(row.get("role") or "") or "unknown"
        by_role[role] = by_role.get(role, 0) + tokens
    ceiling = int(limits_of(manifest).get("max_subagent_tokens") or 0)
    return {
        "tokens_used": used,
        "tokens_ceiling": ceiling or None,
        "tokens_by_role": dict(sorted(by_role.items())),
        "token_entries": len(rows),
    }


def token_gate(run: Run, manifest: dict[str, Any], force: bool) -> None:
    """Mechanical gate: once the recorded subagent tokens reach the run's
    `max_subagent_tokens`, no packet is emitted unless forced, and a forced one
    is logged as a deviation."""
    usage = token_usage(run, manifest)
    ceiling = usage["tokens_ceiling"]
    if not ceiling or usage["tokens_used"] < ceiling:
        return
    if not force:
        stop(
            f"Token ceiling reached: {usage['tokens_used']} of {ceiling} subagent tokens recorded. "
            "No packet emitted. Raise the ceiling with `budget --set max_subagent_tokens=N`, or issue "
            "this one packet anyway with `packet ... --force` (logged as a deviation).",
            usage,
        )
    manifest.setdefault("deviations", []).append({
        "at": now(), "wave": manifest.get("wave", 0), "step": "packet",
        "note": f"token ceiling overridden with --force: {usage['tokens_used']} of {ceiling} tokens recorded",
    })
    run.save_manifest(manifest)


def source_policy_block(manifest: dict[str, Any]) -> dict[str, Any]:
    policy = policy_of(manifest)
    profile = resolve_profile(load_profiles(), policy["profile"])
    return {
        "profile": policy["profile"],
        "description": profile.get("description", ""),
        "rules": {
            key: profile.get(key)
            for key in (
                "allow_source_types", "deny_source_types", "allow_domain_suffixes",
                "deny_domain_suffixes", "max_authority_tier", "tier_status_ceiling",
            )
        },
        "exceptions": policy["exceptions"],
        "enforcement": (
            "ingest rejects a source this profile disallows, drops its excerpts and edges, "
            "and opens a GAP naming the URL. Do not argue with a rejection — report it."
        ),
    }


def claim_objective(cid: str, claim: dict[str, Any]) -> str:
    status = claim.get("status", "unknown")
    if status == "disputed":
        return (
            f"Resolve the dispute on {cid} ({claim['text']}): find evidence that settles which "
            "side is right, or establish that the conflict is real and scope-bound."
        )
    if status == "qualified":
        return (
            f"Raise {cid} from qualified to supported ({claim['text']}): find corroboration from "
            "an origin group not already behind this claim."
        )
    if status in {"unsupported", "refuted"}:
        return f"Establish or refute {cid} ({claim['text']}) with primary evidence."
    return f"Strengthen {cid} ({claim['text']})."


def _seen_urls(sources: dict[str, Any], limit: int) -> list[str]:
    """`already_seen_urls`: every source's canonical URL, recomputed so a source
    stored before a canonicalisation rule existed reads in today's form."""
    urls = {canonical_url_safe(str(s.get("canonical_url") or s.get("url") or "")) for s in sources.values()}
    return sorted(url for url in urls if url)[:limit]


def build_expand_packet(
    run: Run, picks: list[str], role: str, limit: int, manifest: dict[str, Any],
    domain_id: str = "",
) -> dict[str, Any]:
    claims = run.records("claims")
    gaps = run.records("gaps")
    sources = run.records("sources")
    notes = run.records("notes")
    edges = run.records("edges")
    excerpts = run.records("excerpts")
    profile_doc = read_json(run.root / "vault_profile.json", {})
    domains = {d["id"]: d for d in run.taxonomy.get("domains", [])}
    if domain_id and domain_id not in domains:
        die(f"unknown domain: {domain_id}")

    def _claim_origin_groups(cid: str) -> list[str]:
        """The origin groups already behind a claim. Adopted claims carry only a
        count, so walk edge -> excerpt -> source rather than trusting a field. An
        aggregator host never stands for a group (effective_origin_group)."""
        found: set[str] = set()
        for edge in edges.values():
            if edge.get("claim_id") != cid or edge.get("relation") != "supports":
                continue
            excerpt = excerpts.get(edge.get("excerpt_id", ""))
            if not excerpt:
                continue
            source = sources.get(excerpt.get("source_id", ""))
            group = effective_origin_group(source) if source else ""
            if group:
                found.add(group)
        return sorted(found)

    # A bare domain id picks every open item of that domain: the expand surface
    # filtered to the domain (its gaps, its claims, its MOC questions).
    expanded: list[str] = []
    picked_domains: list[str] = []
    surface = None
    for pick in (p.strip() for p in picks):
        if not pick:
            continue
        if pick in domains and pick not in gaps and pick not in claims:
            surface = surface or build_expand_surface(run)["surface"]
            items = [
                item for item in surface
                if (item["kind"] == "gap" and gaps.get(item["id"], {}).get("domain_id") == pick)
                or (item["kind"] == "claim" and pick in claims.get(item["id"], {}).get("domain_ids", []))
                or (item["kind"] == "question" and item["id"] == pick)
            ]
            if not items:
                die(f"domain {pick} has no open items on the expand surface (status --expand)")
            for item in items:
                key = f"{pick}?{item['label']}" if item["kind"] == "question" else item["id"]
                if key not in expanded:
                    expanded.append(key)
            if pick not in picked_domains:
                picked_domains.append(pick)
        elif pick not in expanded:
            expanded.append(pick)
    if not domain_id and len(picked_domains) == 1:
        domain_id = picked_domains[0]  # one bare domain: it is the packet's domain

    targets: list[dict[str, Any]] = []
    objectives: list[str] = []
    exclude_groups: set[str] = set()
    for pick in expanded:
        if "?" in pick and pick.split("?", 1)[0] in domains:
            did, question = pick.split("?", 1)
            objectives.append(question)
            targets.append(
                {
                    "id": did,
                    "kind": "question",
                    "objective": question,
                    "next_action_from_vault": "",
                    "exclude_origin_groups": [],
                }
            )
        elif pick in gaps:
            gap = gaps[pick]
            objectives.append(gap["question"])
            targets.append(
                {
                    "id": pick,
                    "kind": "gap",
                    "objective": gap["question"],
                    "impact": gap.get("impact", "material"),
                    "next_action_from_vault": gap.get("next_action", ""),
                    "exclude_origin_groups": [],
                }
            )
        elif pick in claims:
            claim = claims[pick]
            objective = claim_objective(pick, claim)
            objectives.append(objective)
            groups = _claim_origin_groups(pick) or [
                group for group in claim.get("origin_groups") or [] if not is_aggregator_group(group)
            ]
            exclude_groups.update(groups)
            targets.append(
                {
                    "id": pick,
                    "kind": "claim",
                    "objective": objective,
                    "claim_text": claim["text"],
                    "current_status": claim.get("status"),
                    "status_reason": claim.get("status_reason", ""),
                    "next_action_from_vault": claim.get("action", ""),
                    "exclude_origin_groups": groups,
                }
            )
        else:
            die(f"unknown pick {pick!r}: not a gap, claim or domain in this run")
    if not targets:
        die("--pick selected nothing; pass gap, claim or domain ids, comma separated")

    linkable = list(profile_doc.get("linkable_titles") or [])
    if not linkable:
        linkable = sorted({note.get("basename") or note["title"] for note in notes.values()})

    return {
        "run_id": manifest["run_id"],
        "run_dir": str(run.root),
        "mode": "expand",
        "role": role,
        "wave": manifest.get("wave", 0),
        # The domain the results belong to: copy it into the result as `domain_id`,
        # so its claims are synthesised and published under that domain.
        "domain_id": domain_id,
        "vault": manifest.get("adopted_from"),
        "vault_prefix": manifest.get("vault_prefix"),
        "objective": objectives[0] if len(objectives) == 1 else objectives,
        "next_action_from_vault": [
            target["next_action_from_vault"] for target in targets if target["next_action_from_vault"]
        ],
        "targets": targets,
        "exclude_origin_groups": sorted(exclude_groups),
        "already_seen_urls": _seen_urls(sources, 500),
        "linkable_titles": linkable[:300],
        "budget": budget_block(run, manifest),
        "source_policy": source_policy_block(manifest),
        "not_found_is_valid": True,
        "inbox": str(run.inbox),
        "cache_dir": str(run.cache / "raw"),
        "note": (
            "Item-scoped assignment. Do not widen it. An empty result with a search log is a "
            "valid, ingestable outcome — it becomes a GAP, not a failure. Never cite a source "
            "whose origin group is in exclude_origin_groups as corroboration. Pull full records "
            f"with `research.py show --run {shlex.quote(str(manifest['run_id']))} --id <ID>`."
        ),
    }


def charge_opus(run: Run, manifest: dict[str, Any], role: str) -> None:
    """Mechanical gate, not a reminder: past the cap, no Opus packet is emitted."""
    if role not in OPUS_ROLES:
        return
    limits = limits_of(manifest)
    wave = str(manifest.get("wave", 0))
    issued = (manifest.get("opus_issued") or {}).get(wave, 0)
    cap = limits["max_opus_per_wave"]
    if issued >= cap:
        stop(
            f"Opus budget for wave {wave} is spent ({issued}/{cap}). No {role} packet emitted. "
            "Advance the wave, raise the cap with `budget --set max_opus_per_wave=N`, or run this "
            "step with a Sonnet role.",
            {"wave": manifest.get("wave", 0), "role": role, "issued": issued, "max_opus_per_wave": cap},
        )
    manifest.setdefault("opus_issued", {})[wave] = issued + 1
    run.save_manifest(manifest)


def build_packet(run: Run, domain_id: str, role: str, limit: int,
                 brief: str = "framed") -> dict[str, Any]:
    """A worker's assignment packet: index-level context, never full records.

    Coverage is passed as claim one-liners so a later wave does not rediscover
    what an earlier one already found. Bodies are fetched on demand, by ID.

    A blind or frame-break prospector never sees the skeleton: its
    `already_covered_claims` is empty on purpose, so the two discovery arms stay
    independent and their overlap measures coverage (`coverage`). Seen URLs stay,
    since that arm must not reuse what the typical framing found.
    """
    manifest = run.manifest
    taxonomy = run.taxonomy
    domain = next((d for d in taxonomy.get("domains", []) if d["id"] == domain_id), None)
    if domain is None:
        die(f"unknown domain: {domain_id}")
    claims = run.records("claims")
    sources = run.records("sources")
    gaps = run.records("gaps")

    withheld = role == "prospector" and brief != "framed"
    covered = [] if withheld else [
        {"id": cid, "status": c["status"], "text": c["text"][:160]}
        for cid, c in sorted(claims.items())
    ][:limit]
    open_gaps = [
        {"id": gid, "question": g["question"], "impact": g["impact"]}
        for gid, g in sorted(gaps.items())
        if g["status"] == "open" and (g.get("domain_id") in {domain_id, ""})
    ]
    return {
        "run_id": manifest["run_id"],
        "run_dir": str(run.root),
        "role": role,
        "question": manifest["question"],
        "wave": manifest["wave"],
        "domain": domain,
        "domain_id": domain_id,
        "source_budget": max(4, manifest["limits"]["sources"] // max(1, len(taxonomy.get("domains", [])) or 1)),
        "already_covered_claims": covered,
        "skeleton_withheld": withheld,
        "already_seen_urls": _seen_urls(sources, limit),
        "open_gaps": open_gaps,
        "budget": budget_block(run, manifest),
        "source_policy": source_policy_block(manifest),
        "not_found_is_valid": True,
        "inbox": str(run.inbox),
        "cache_dir": str(run.cache / "raw"),
        "note": (
            "Do not restate covered claims. Pull full records by ID with "
            f"`research.py show --run {shlex.quote(str(manifest['run_id']))} --id <ID>` only when you "
            "need the body."
        ),
    }


# --------------------------------------------------------------------------
# publishing
# --------------------------------------------------------------------------

FRONT_KEYS = ("type", "status", "evidence_level", "published", "last_verified", "review_due")

REVIEW_DAYS = {"official": 90, "peer_reviewed": 180, "mixed": 90, "empirical": 120, "hypothesis": 60, "unsupported": 30}


def _fm(pairs: list[tuple[str, Any]]) -> str:
    lines = ["---"]
    for key, value in pairs:
        if isinstance(value, list):
            if not value:
                lines.append(f"{key}: []")
            else:
                lines.append(f"{key}:")
                lines.extend(f"  - {item}" for item in value)
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def evidence_level_for(claim_ids: list[str], claims: dict, sources: dict, edges: dict, excerpts: dict) -> str:
    tiers: set[int] = set()
    statuses: set[str] = set()
    for cid in claim_ids:
        claim = claims.get(cid)
        if not claim:
            continue
        statuses.add(claim["status"])
        for edge in edges.values():
            if edge["claim_id"] != cid or edge["decision"] != "accepted":
                continue
            excerpt = excerpts.get(edge["excerpt_id"])
            if excerpt:
                source = sources.get(excerpt["source_id"])
                if source:
                    tiers.add(source["authority_tier"])
    if not tiers:
        return "unsupported"
    if statuses & {"disputed", "refuted"}:
        return "mixed"
    if tiers <= {1}:
        return "official"
    if tiers <= {1, 2}:
        return "peer_reviewed"
    return "empirical" if statuses & {"supported", "qualified"} else "hypothesis"


DOMAIN_ID = re.compile(r"[A-Za-z0-9_-]{1,32}")
WINDOWS_RESERVED = re.compile(r"(?i)^(?:con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])(?:\..*)?$")
# File names that coding agents load as instructions when they read a folder; a note
# must never publish under one of them.
AGENT_FILE_RESERVED = re.compile(r"(?i)^(?:claude|claude\.local|agents|gemini|copilot-instructions|\.cursorrules|\.clinerules|\.windsurfrules)(?:\.md)?$")


def _safe_title(title: str) -> str:
    """A note title as a file basename: one line, no path separator or character
    a file system refuses, no leading or trailing dot or space (so never `..`),
    never a reserved device name, at most 120 characters."""
    name = re.sub(r'[\\/:*?"<>|]+', "-", _one_line(title)).strip(" .")[:120].strip(" .")
    if not name:
        return "Untitled"
    return f"Note {name}" if (WINDOWS_RESERVED.match(name) or AGENT_FILE_RESERVED.match(name)) else name


def path_component_problem(name: str) -> str | None:
    """Why `name` cannot be one folder or file name inside a vault, or None."""
    if not name or name != name.strip(" ."):
        return "empty, or starts or ends with a space or a dot"
    if re.search(r'[\\/:*?"<>|\x00-\x1f\x7f]', name):
        return 'holds a path separator or one of : * ? " < > |'
    if WINDOWS_RESERVED.match(name):
        return "is a reserved device name"
    if AGENT_FILE_RESERVED.match(name):
        return "is a file name that coding agents load as instructions"
    if len(name) > 200:
        return "is longer than 200 characters"
    return None


# --------------------------------------------------------------------------
# brain resolution, brain-wide lint
#
# A vault never writes to itself in isolation: basenames are the brain's identity
# space, so every create has to be cleared against the whole tree first.
# --------------------------------------------------------------------------

BRAIN_ENV_VARS = ("RESEARCH_VAULT_ROOT", "RESEARCH_BRAIN_ROOT")
# Last-resort fallback when no env var or --brain is given; a real install sets
# one of BRAIN_ENV_VARS or passes --brain.
KNOWN_BRAIN_PATHS = (Path.cwd() / "vault",)

# Linted, never written to.
# field-notes/ is first-hand know-how with its own schema (validate --profile field);
# the research pipeline never publishes or merges into it.
FIELD_NOTES_DIR = "field-notes"
NO_WRITE_DIRS = {"qa", "backlog", "logs", "design", ".obsidian", ".git", FIELD_NOTES_DIR}
# Not research vaults: linted for duplicate basenames, skipped by prefix rules.
FLAT_DIRS = {"qa", "backlog", "logs"}

ROUTER_HEADING = re.compile(r"^##\s+Router\b")


def is_brain_root(path: Path) -> bool:
    readme = path / "README.md"
    if not readme.is_file():
        return False
    return any(ROUTER_HEADING.match(line) for line in readme.read_text(encoding="utf-8", errors="replace").splitlines())


def resolve_brain_root(explicit: Path | None = None) -> Path:
    """`--brain` → $RESEARCH_VAULT_ROOT → $RESEARCH_BRAIN_ROOT → `./vault`.
    A directory whose README.md has no `## Router` section is not a brain and is
    refused; if nothing resolves, the error names every path tried. An explicit
    `--brain` (`~` expanded) is the only candidate: when it is not a vault root
    the command stops, it never falls back to an environment variable."""
    if explicit is not None:
        path = Path(explicit).expanduser()
        if not path.is_dir():
            die(f"--brain {path}: no such directory. An explicit --brain is never replaced by "
                "$RESEARCH_VAULT_ROOT or another default; fix the path and run the command again.")
        if not is_brain_root(path):
            die(f"--brain {path}: not a vault root (it needs a README.md with a '## Router' section). "
                "Nothing was written.")
        return path.resolve()
    candidates: list[tuple[str, Path]] = []
    for var in BRAIN_ENV_VARS:
        value = os.environ.get(var)
        if value:
            candidates.append((f"${var}", Path(value).expanduser()))
    candidates.extend((str(path), path) for path in KNOWN_BRAIN_PATHS)

    tried: list[str] = []
    for label, path in candidates:
        if not path.is_dir():
            tried.append(f"{label}: {path} (no such directory)")
            continue
        if not (path / "README.md").is_file():
            tried.append(f"{label}: {path} (no README.md)")
            continue
        if not is_brain_root(path):
            die(
                f"{path} has a README.md with no '## Router' section. "
                "Refusing to treat it as a brain root."
            )
        return path.resolve()
    die("cannot resolve a brain root. Tried:\n  " + "\n  ".join(tried or ["nothing"]))


# Group folders. Vaults may sit one or more levels down, in a GROUP folder:
# `<brain>/plants/pothos-cuttings/`. A folder is a group when it carries no vault
# marker itself (no `00 * Home.md`, no `90 Evidence/`, not a first-hand area) and
# some folder below it does. A group is never a vault and never a brain: the brain
# root is only ever the folder whose README.md has a `## Router` heading, so a
# group's own README must not use that heading (lint_brain reports it, and
# _brain_of prefers the outer brain). A vault never holds another vault: lint_brain
# reports a marker below a vault folder. Service folders (qa/, inbox/, …)
# hold no vault below them and are listed exactly as before. A vault's identity
# (prefix, Home name) is its folder's basename; its location is its path relative
# to the brain root (`plants/pothos-cuttings`), which on a brain without groups is
# the basename itself.


def _brain_child_dirs(folder: Path) -> list[Path]:
    return sorted(
        path
        for path in folder.iterdir()
        if path.is_dir() and not path.name.startswith(".") and path.name != "research-staging"
    )


def has_vault_markers(directory: Path) -> bool:
    """A `00 * Home.md`, a `90 Evidence/` folder, or a first-hand area."""
    return (
        any(path.is_file() for path in directory.glob("00 *Home.md"))
        or (directory / "90 Evidence").is_dir()
        or is_first_hand_area(directory)
    )


def group_vaults(folder: Path) -> list[Path]:
    """The vaults inside a group folder (through nested groups), in path order;
    [] when `folder` is not a group — it is a vault itself, or no vault sits below it."""
    if has_vault_markers(folder):
        return []
    found: list[Path] = []
    for child in _brain_child_dirs(folder):
        if has_vault_markers(child):
            found.append(child)
        else:
            found.extend(group_vaults(child))
    return found


def brain_vault_dirs(brain: Path) -> list[Path]:
    """Every top-level folder, except that a group folder is replaced by the vaults
    inside it. On a brain without group folders this is the plain top-level list."""
    dirs: list[Path] = []
    for path in _brain_child_dirs(brain):
        dirs.extend(group_vaults(path) or [path])
    return dirs


def brain_group_dirs(brain: Path) -> list[Path]:
    """The top-level group folders of a brain (empty on a brain without groups)."""
    return [path for path in _brain_child_dirs(brain) if group_vaults(path)]


def group_loose_notes(brain: Path, vault_dirs: list[Path] | None = None) -> list[Path]:
    """Notes inside a group folder but inside none of its vaults (a group README, …)."""
    members = set(brain_vault_dirs(brain) if vault_dirs is None else vault_dirs)
    found: list[Path] = []
    for group in brain_group_dirs(brain):
        for dirpath, dirnames, filenames in os.walk(group):
            current = Path(dirpath)
            dirnames[:] = sorted(
                name for name in dirnames if not name.startswith(".") and current / name not in members
            )
            found.extend(
                current / name for name in sorted(filenames)
                if name.lower().endswith(".md") and not name.startswith(".")
            )
    return found


def brain_rel(brain: Path, path: Path) -> str:
    """A vault's location label: `plants/pothos-cuttings`, or `pothos-cuttings`."""
    try:
        return path.relative_to(brain).as_posix()
    except ValueError:
        return path.name


def brain_structure_errors(brain: Path, vault_dirs: list[Path] | None = None) -> list[dict[str, Any]]:
    """Layouts the group rule would read wrongly, so they fail loudly instead:
    a vault holding vault folders below it (a stray `00 * Home.md` in `plants/`
    turns the whole group into one vault and hides the vaults inside it), and a
    group folder whose README has a `## Router` heading (it would pass for a
    brain). Empty on a sound brain — and always empty on a brain without groups
    and without markers below its top-level folders."""
    vault_dirs = brain_vault_dirs(brain) if vault_dirs is None else vault_dirs
    errors: list[dict[str, Any]] = []
    groups = sorted({folder for vault in vault_dirs for folder in vault.parents if brain in folder.parents})
    for group in groups:
        if is_brain_root(group):
            rel = brain_rel(brain, group)
            errors.append({
                "error": "router-in-group",
                "path": rel,
                "detail": f"{rel}/README.md has a '## Router' heading, but {rel}/ is a group folder; "
                          "only the brain root's README may carry that heading — rename it",
            })
    for vault in vault_dirs:
        nested: list[str] = []
        for dirpath, dirnames, _files in os.walk(vault):
            current = Path(dirpath)
            keep: list[str] = []
            for name in sorted(dirnames):
                if name.startswith(".") or name == "research-staging":
                    continue
                if has_vault_markers(current / name):
                    nested.append(brain_rel(brain, current / name))
                else:
                    keep.append(name)
            dirnames[:] = keep
        if nested:
            rel = brain_rel(brain, vault)
            errors.append({
                "error": "nested-vault",
                "path": rel,
                "nested": nested,
                "detail": f"{rel}/ is a vault (00 * Home.md, 90 Evidence/ or vault_kind) and holds vault "
                          "folders below it; a folder is a vault or a group, never both — remove the "
                          "stray marker or move the inner vaults out",
            })
    return errors


def is_brain_vault(brain: Path, directory: Path) -> bool:
    """Is `directory` one of the brain's vaults (top-level, or inside a group)?"""
    target = directory.resolve()
    return any(path.resolve() == target for path in brain_vault_dirs(brain))


def split_vault_path(value: str) -> tuple[str, str]:
    """`plants/Pothos Cuttings` → (`plants/pothos-cuttings`, `pothos-cuttings`).
    Each segment is slugified on its own so a group folder survives; a plain name
    gives exactly `slugify(name, 60)` twice."""
    segments = [part for part in re.split(r"[\\/]+", value.strip()) if part.strip()] or [value]
    slugs = [slugify(part, 60) for part in segments]
    return "/".join(slugs), slugs[-1]


def safe_prefix(vault: Path) -> str:
    """`infer_prefix` without the STOP: lint has to survive a messy vault, and it
    must not print the STOP explanation into the middle of a brain-wide report."""
    quiet = io.StringIO()
    try:
        with contextlib.redirect_stderr(quiet):
            return infer_prefix(vault)["prefix"]
    except SystemExit:
        return ""


def brain_basename_index(brain: Path) -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = {}
    for path in brain.rglob("*.md"):
        if any(part.startswith(".") for part in path.relative_to(brain).parts):
            continue
        index.setdefault(path.stem, []).append(path)
    return index


INDEX_BASENAMES = {"README"}


def _wikilinked_basenames(brain: Path, wanted: set[str], extra_roots: list[Path] | None = None) -> set[str]:
    """Which of `wanted` some note links by basename (`[[README]]`, `[[x/README|…]]`)."""
    found: set[str] = set()
    roots = [brain] + list(extra_roots or [])
    for root in roots:
        for path in root.rglob("*.md"):
            if any(part.startswith(".") for part in path.relative_to(root).parts):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if "[[" not in text:
                continue
            # A link quoted in code (`[[README]]`, or inside a fence) is prose
            # about a link, not a link.
            text = re.sub(r"(?ms)^\s*```.*?^\s*```", "", text.replace("\r\n", "\n"))
            text = re.sub(r"`[^`\n]*`", "", text)
            for match in WIKILINK.finditer(text):
                stem = match.group(1).strip().split("/")[-1]
                if stem.endswith(".md"):
                    stem = stem[:-3]
                if stem in wanted:
                    found.add(stem)
    return found


def lint_brain(brain: Path, extra_roots: list[Path] | None = None) -> dict[str, Any]:
    """Duplicate basenames and prefix violations across every vault.

    `extra_roots` lets a staged, not-yet-moved vault be linted as if it were
    already in the brain — the check that has to pass before anything moves.
    """
    index: dict[str, list[str]] = {}
    prefix_violations: list[dict[str, str]] = []
    vaults: list[dict[str, Any]] = []

    def record(path: Path, label: str) -> None:
        index.setdefault(path.stem, []).append(label)

    for path in sorted(brain.glob("*.md")):
        record(path, str(path.relative_to(brain)))

    vault_dirs = brain_vault_dirs(brain)
    # Notes in a group folder but in none of its vaults (a group README, …) still
    # count for basename uniqueness.
    for path in group_loose_notes(brain, vault_dirs):
        record(path, path.relative_to(brain).as_posix())

    roots: list[tuple[str, Path]] = [(brain_rel(brain, d), d) for d in vault_dirs]
    for root in extra_roots or []:
        roots.append((root.name, root))

    for name, directory in roots:
        first_hand = is_first_hand_area(directory)
        prefix = "" if name in FLAT_DIRS or first_hand else safe_prefix(directory)
        files = sorted(directory.rglob("*.md"))
        for path in files:
            if any(part.startswith(".") for part in path.relative_to(directory).parts):
                continue
            record(path, f"{name}/{path.relative_to(directory).as_posix()}")
        if prefix and name not in FLAT_DIRS:
            for path in files:
                rel = path.relative_to(directory)
                top = rel.parts[0] if len(rel.parts) > 1 else ""
                meta = top.startswith("90 ") or top.startswith("99 ")
                if meta and not path.stem.startswith(prefix + " "):
                    prefix_violations.append(
                        {"vault": name, "file": rel.as_posix(), "expected_prefix": prefix}
                    )
                if len(rel.parts) == 1 and path.stem.startswith("00 ") and prefix not in path.stem:
                    prefix_violations.append(
                        {"vault": name, "file": rel.as_posix(), "expected_prefix": prefix}
                    )
        vaults.append(
            {
                "vault": name,
                "prefix": prefix or None,
                "notes": len(files),
                "kind": "flat" if name in FLAT_DIRS else ("field" if first_hand else "vault"),
                "writable": name not in NO_WRITE_DIRS and not first_hand,
            }
        )

    # Folder indexes (brain root README.md, inbox/README.md, …) are opened by
    # path and never linked by basename, so they are exempt from uniqueness —
    # but only while no note links one by basename; then the ambiguity is real.
    repeated_index = {stem for stem, paths in index.items() if stem in INDEX_BASENAMES and len(paths) > 1}
    linked_index = _wikilinked_basenames(brain, repeated_index, extra_roots) if repeated_index else set()
    exempt = [
        {"basename": stem, "paths": sorted(index[stem])}
        for stem in sorted(repeated_index - linked_index)
    ]
    duplicates = [
        {"basename": stem, "paths": sorted(paths)}
        for stem, paths in sorted(index.items())
        if len(paths) > 1 and (stem not in repeated_index or stem in linked_index)
    ]
    # A vault holding vaults, or a group README with '## Router': the group rule
    # would misread the brain, so the lint fails instead of skipping silently.
    structure = brain_structure_errors(brain, vault_dirs)
    report: dict[str, Any] = {
        "brain": str(brain),
        "checked_at": now(),
        "vaults": vaults,
        "notes": sum(len(paths) for paths in index.values()),
        "duplicate_basenames": duplicates,
        "exempt_index_basenames": exempt,
        "prefix_violations": prefix_violations,
        "clean": not duplicates and not prefix_violations and not structure,
        "rule": (
            "qa/, backlog/ and logs/ are linted but never written to. "
            "README files are folder indexes and exempt from basename uniqueness while nothing links them by basename. "
            "Prefix violations are fixed by hand (rename + link-rewrite), never by a migration pass."
        ),
    }
    if structure:  # present only on a broken layout: a sound brain's report is unchanged
        report["structure_errors"] = structure
    return report


# --------------------------------------------------------------------------
# markdown table shapes
#
# A merge writes into whatever column layout the target vault already has. The
# renderer therefore never assumes the v2 header: it fills the columns it can
# name and leaves everything else exactly as it found it.
# --------------------------------------------------------------------------

CLAIM_COLS: dict[str, list[str]] = {
    "id": ["id"],
    "text": ["claim", "text"],
    "claim_type": ["type", "claim type"],
    "importance": ["importance"],
    "status": ["status"],
    "evidence": ["evidence", "sources"],
    "detail": ["scope, action, caveat", "scope", "detail"],
    "groups": ["groups", "origin groups"],
}
SOURCE_COLS: dict[str, list[str]] = {
    "id": ["id"],
    "tier": ["tier"],
    "accessed": ["accessed", "accessed at"],
    "source": ["source"],
    "origin_group": ["origin group", "group"],
    "use": ["use"],
    "limitations": ["limitations", "limits"],
}
GAP_COLS: dict[str, list[str]] = {
    "id": ["id"],
    "question": ["open question", "question"],
    "impact": ["impact"],
    "status": ["status"],
    "closed_by": ["closed by", "closed_by"],
    "next_action": ["next action", "next_action"],
}
CX_COLS: dict[str, list[str]] = {
    "id": ["id"],
    "claims": ["claims"],
    "axis": ["axis"],
    "severity": ["severity"],
    "status": ["status"],
    "summary": ["summary"],
}

CLAIM_HEADER = ["ID", "Claim", "Type", "Importance", "Status", "Evidence", "Scope, action, caveat", "Groups"]
SOURCE_HEADER = ["ID", "Tier", "Accessed", "Source", "Origin group", "Use", "Limitations"]
GAP_HEADER = ["ID", "Open question", "Impact", "Status", "Closed by", "Next action"]
CX_HEADER = ["ID", "Claims", "Axis", "Severity", "Status", "Summary"]


def _row_line(header: list[str], columns: dict[str, list[str]], values: dict[str, str]) -> str:
    lower = [cell.lower() for cell in header]
    cells: list[str] = []
    for name in lower:
        placed = "—"
        for key, aliases in columns.items():
            if name in aliases and key in values:
                placed = values[key]
                break
        cells.append(_md_cell(placed))
    return "| " + " | ".join(cells) + " |"


def _patch_line(raw_cells: list[str], header: list[str], columns: dict[str, list[str]],
                values: dict[str, str]) -> str:
    lower = [cell.lower() for cell in header]
    cells = list(raw_cells) + [""] * max(0, len(lower) - len(raw_cells))
    for index, name in enumerate(lower):
        for key, aliases in columns.items():
            if name in aliases and key in values:
                cells[index] = _md_cell(values[key])
                break
    return "| " + " | ".join(cell.strip() for cell in cells[: len(lower)]) + " |"


def read_id_table(path: Path, prefix: str) -> dict[str, Any]:
    """The raw shape of the first <prefix>-### table in a file: header line,
    separator line, and every row keyed by ID with its exact source line."""
    empty = {"found": False, "header": [], "header_raw": "", "sep_raw": "", "rows": {}, "order": [], "last_raw": ""}
    if not path.is_file():
        return empty
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    header: list[str] = []
    header_raw = ""
    sep_raw = ""
    rows: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    malformed: list[str] = []
    duplicates: list[str] = []
    last_raw = ""
    pending_header: tuple[str, list[str]] | None = None
    pending_sep = ""
    # An empty ID table (header and separator, no rows yet) may be followed by
    # other tables (a Coverage table under the gaps): the first header that
    # starts with an ID column is the one rows belong under.
    empty_id_table: tuple[str, list[str], str] | None = None
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|"):
            if rows:
                break
            if (empty_id_table is None and pending_header and pending_sep
                    and pending_header[1] and pending_header[1][0].strip("* ").lower() == "id"):
                empty_id_table = (pending_header[0], pending_header[1], pending_sep)
            pending_header, pending_sep = None, ""
            continue
        cells, closed = _split_row(stripped)
        if all(SEPARATOR_CELL.match(cell) for cell in cells if cell):
            pending_sep = line
            continue
        match = ID_CELL.match(cells[0]) if cells else None
        if match and match.group(1) == prefix:
            if not header and pending_header:
                header_raw, header = pending_header[0], pending_header[1]
                sep_raw = pending_sep
            if cells[0] in rows:
                duplicates.append(cells[0])
            if not closed or (header and len(cells) != len(header)):
                malformed.append(cells[0])
            rows[cells[0]] = {"cells": cells, "raw": line}
            order.append(cells[0])
            last_raw = line
        elif not rows:
            pending_header = (line, cells)
    if not header and not rows and empty_id_table is not None:
        header_raw, header, sep_raw = empty_id_table
    elif not header and pending_header and not rows:
        header_raw, header = pending_header[0], pending_header[1]
        sep_raw = pending_sep
    if not header_raw and not rows:
        return empty
    if header:  # a row read before its header was known is checked now
        malformed += [rid for rid in order if len(rows[rid]["cells"]) != len(header) and rid not in malformed]
    return {
        "found": True,
        "header": header,
        "header_raw": header_raw,
        "sep_raw": sep_raw,
        "rows": rows,
        "order": order,
        "last_raw": last_raw or sep_raw or header_raw,
        # A row with the wrong cell count or no closing pipe is what a value with
        # a raw newline or pipe leaves behind; the row next to it may be forged.
        "malformed": malformed,
        "duplicates": duplicates,
    }


def id_table_problems(path: Path, prefix: str) -> list[str]:
    """Rows of an ID table that cannot be trusted: wrong cell count, no closing
    pipe, or an ID that appears twice. Empty for a table the engine wrote."""
    shape = read_id_table(path, prefix)
    problems = [
        f"{rid}: malformed row (not {len(shape['header'])} cells, or no closing pipe)"
        for rid in dict.fromkeys(shape.get("malformed", []))
    ]
    problems += [f"{rid}: appears in more than one row" for rid in dict.fromkeys(shape.get("duplicates", []))]
    return problems


ID_TABLES = (("claim_ledger", "CL"), ("source_register", "S"), ("gaps", "GAP"), ("contradictions", "CX"))


def vault_table_problems(vault: Path) -> list[str]:
    """`id_table_problems` over a vault's four ID tables, as `<file>: <problem>`."""
    names = _meta_names(vault)
    out: list[str] = []
    for key, prefix in ID_TABLES:
        if key in names:
            rel = f"90 Evidence/{names[key]}.md"
            out += [f"{rel}: {problem}" for problem in id_table_problems(vault / rel, prefix)]
    return out


# --------------------------------------------------------------------------
# note template v2
# --------------------------------------------------------------------------

CONTENT_TYPES = {"concept", "profile", "playbook", "guide", "dataset"}
CLAIM_NOTE_TYPES = {"concept", "profile", "playbook", "guide"}

STRENGTH_RANK = {"strong": 3, "moderate": 2, "weak": 1}
CLAIM_TOKEN = re.compile(r"(?<![\w#/-])(CL-\d{3,})(?![\w-])")
CODE_OR_LINK = re.compile(r"(\[\[[^\]]*\]\]|`[^`]*`)")
INLINE_URL = re.compile(r"\]\(https?://")


def meta_basename(prefix: str, suffix: str) -> str:
    return f"{prefix} {suffix}".strip()


def home_basename(prefix: str) -> str:
    return f"00 {prefix} Home".strip() if prefix else "00 Home"


def prefix_from_vault_name(name: str) -> str:
    """`pothos-cuttings` → `Pothos Cuttings`. Permanent identity, so it is reported for
    confirmation rather than silently adopted."""
    words = [word for word in re.split(r"[-_\s]+", name.strip()) if word]
    return " ".join(word[:1].upper() + word[1:] for word in words)


def rewrite_claim_tokens(text: str, ledger_basename: str, known: set[str] | None = None) -> str:
    """A bare `CL-017` becomes a link into the ledger. Code spans, existing
    wikilinks and fenced blocks are left alone; nothing else in the prose is
    touched. With `known`, only IDs the ledger will actually carry are linked: a
    token for a claim that is not in the ledger stays plain text instead of
    becoming an anchor that lands nowhere."""
    out: list[str] = []
    fenced = False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            out.append(line)
            continue
        if fenced:
            out.append(line)
            continue
        pieces = CODE_OR_LINK.split(line)
        for index, piece in enumerate(pieces):
            if index % 2 == 0:
                pieces[index] = CLAIM_TOKEN.sub(
                    lambda m: m.group(1) if known is not None and m.group(1) not in known
                    else f"[[{ledger_basename}#{m.group(1)}]]",
                    piece,
                )
        out.append("".join(pieces))
    return "\n".join(out)


# A synthesist on a fresh run has no prefix to write: it is chosen at publish.
# It links the bare meta basename (`[[Claim Ledger#CL-004]]`, META_SUFFIXES), and
# the publisher gives that link the vault's prefix.
LINK_PARTS = re.compile(r"\[\[([^\]|#\\]+)((?:#[^\]]*|\\?\|[^\]]*)?)\]\]")
INLINE_CODE_SPLIT = re.compile(r"(`[^`]*`)")


def rewrite_meta_links(text: str, names: dict[str, str], keep: set[str] | frozenset[str] = frozenset()) -> str:
    """`[[Claim Ledger]]`, `[[Claim Ledger#CL-004]]` or `[[Claim Ledger|the ledger]]`
    becomes the same link to `<Prefix> Claim Ledger` (likewise every meta file).
    A title in `keep` (a note of that exact name) is left alone, and so are code
    spans and fenced blocks."""
    targets = {
        suffix.lower(): names[key]
        for suffix, key in META_SUFFIXES.items()
        if names.get(key) and names[key] != suffix and suffix not in keep
    }
    if not targets:
        return text

    def link(match: re.Match) -> str:
        target = targets.get(match.group(1).strip().lower())
        return f"[[{target}{match.group(2)}]]" if target else match.group(0)

    out: list[str] = []
    fenced = False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        if fenced or line.lstrip().startswith("```"):
            out.append(line)
            continue
        pieces = INLINE_CODE_SPLIT.split(line)
        out.append("".join(piece if index % 2 else LINK_PARTS.sub(link, piece)
                           for index, piece in enumerate(pieces)))
    return "\n".join(out)


# --------------------------------------------------------------------------
# claim index
#
# Prose cites a claim as `[[<Prefix> Claim Ledger#CL-017]]`. Obsidian resolves
# `#…` against a heading's full text, and it does not support links into table
# rows (block ids on rows do not work). The ledger table therefore gets a
# companion section below it: one `### CL-017` heading per claim, carrying the
# claim text and a pointer to the Evidence Map. The table stays the single
# authority for status and evidence; the index carries no status, so a
# patch-cell never has to touch it, and the merge planner refuses a claim whose
# text changes, so an index entry never goes stale.
# --------------------------------------------------------------------------

CLAIM_INDEX_HEADING = "## Claim index"
INDEX_ENTRY = re.compile(r"^###\s+(CL-\d+)\s*$")


def claim_index_entry(cid: str, text: str, evidence_map: str | None) -> str:
    line = _md_text(text or "") or "(no claim text)"
    if evidence_map:
        line += f" — evidence: [[{evidence_map}]]"
    return f"\n### {cid}\n\n{line}\n"


def render_claim_index(evidence_map: str | None, items: list[tuple[str, str]]) -> str:
    intro = (
        "One heading per claim, so a wikilink to `Claim Ledger#CL-###` lands on the claim. "
        "Status, evidence and caveats live in the table above"
        + (f"; the quoted passages are in [[{evidence_map}]]." if evidence_map else ".")
    )
    return (
        f"\n{CLAIM_INDEX_HEADING}\n\n{intro}\n"
        + "".join(claim_index_entry(cid, text, evidence_map) for cid, text in items)
    )


def claim_index_tail(text: str) -> str | None:
    """The last non-empty line of the `## Claim index` section, if it is unique in
    the file: where new entries go. The section ends at the next `#`/`##`
    heading, a `---` rule, or the end of the file."""
    lines = text.replace("\r\n", "\n").split("\n")
    start = next((i for i, line in enumerate(lines) if line.strip() == CLAIM_INDEX_HEADING), None)
    if start is None:
        return None
    end = len(lines)
    for index in range(start + 1, len(lines)):
        stripped = lines[index].strip()
        if stripped == "---" or re.match(r"^#{1,2}\s", stripped):
            end = index
            break
    for index in range(end - 1, start - 1, -1):
        if lines[index].strip():
            line = lines[index]
            return line if sum(1 for other in lines if other == line) == 1 else None
    return None


def claim_index_ids(text: str) -> list[str]:
    """IDs that already have an index heading (fenced code ignored)."""
    ids: list[str] = []
    fenced = False
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            match = INDEX_ENTRY.match(line.strip())
            if match:
                ids.append(match.group(1))
    return ids


HEADING_LINE = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$")
BLOCK_ID = re.compile(r"(?:^|\s)\^([A-Za-z0-9-]+)\s*$")
# `\|` is the alias separator inside table rows (Obsidian requires the escape
# there); it separates exactly like `|`, and its backslash is never part of a
# target or an anchor.
ANCHOR_LINK = re.compile(r"!?\[\[([^\]|#\\]*)#((?:[^\]|\\]|\\(?!\|))+)(?:\\?\|[^\]]*)?\]\]")


def _norm_heading(value: str) -> str:
    # Obsidian warns that `# | ^ : %% [[ ]]` may not work inside a link, so a
    # heading and a link subpath compare with those characters as spaces.
    value = re.sub(r"[#|^:%\[\]]", " ", value)
    return re.sub(r"\s+", " ", value).strip().lower()


def note_anchors(text: str) -> tuple[set[str], set[str]]:
    """(heading texts, block ids) a wikilink `#…` can land on, as Obsidian sees
    them: fenced code holds no headings."""
    heads: set[str] = set()
    blocks: set[str] = set()
    fenced = False
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        match = HEADING_LINE.match(line)
        if match:
            heads.add(_norm_heading(match.group(1)))
        block = BLOCK_ID.search(line)
        if block:
            blocks.add(block.group(1).lower())
    return heads, blocks


def anchor_links(text: str) -> list[tuple[str, str]]:
    """Every `[[Target#Anchor]]` outside fenced code, as (target, anchor)."""
    out: list[tuple[str, str]] = []
    fenced = False
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        for match in ANCHOR_LINK.finditer(line):
            out.append((match.group(1).strip().split("/")[-1], match.group(2).strip()))
    return out


def anchor_resolves(anchor: str, anchors: tuple[set[str], set[str]]) -> bool:
    last = anchor.split("#")[-1].strip()
    if last.startswith("^"):
        return last[1:].lower() in anchors[1]
    return _norm_heading(last) in anchors[0]


def strongest_edge_source(cid: str, edges: dict, excerpts: dict, sources: dict) -> dict[str, Any] | None:
    best: tuple[int, int, dict] | None = None
    for edge in edges.values():
        if edge["claim_id"] != cid or edge["relation"] != "supports":
            continue
        if edge.get("decision") not in {"accepted"}:
            continue
        excerpt = excerpts.get(edge["excerpt_id"])
        if not excerpt:
            continue
        source = sources.get(excerpt["source_id"])
        if not source or not source.get("url"):
            continue
        rank = (STRENGTH_RANK.get(str(edge.get("strength")), 2), 6 - int(source.get("authority_tier", 5)))
        score = (rank[0], rank[1])
        if best is None or score > (best[0], best[1]):
            best = (score[0], score[1], source)
    return best[2] if best else None


def citation_for(source: dict[str, Any]) -> str:
    date = source.get("published_at") or "unknown"
    if not date or date == "unknown":
        date = source.get("accessed_at") or "undated"
    publisher = _md_label(source.get("publisher") or "unknown")
    return f"([{publisher}, {_md_label(date)}]({_md_url(source['url'])}))"


def add_inline_citations(body: str, edges: dict, excerpts: dict, sources: dict) -> str:
    """Every factual paragraph ends with a resolvable source URL. A paragraph that
    already carries one is left as written."""
    blocks = body.split("\n\n")
    out: list[str] = []
    for block in blocks:
        stripped = block.strip()
        first = stripped.split("\n")[0].lstrip() if stripped else ""
        prose = bool(stripped) and not first.startswith(("#", "-", "*", ">", "|", "```", "!["))
        if not prose or INLINE_URL.search(block):
            out.append(block)
            continue
        cited = CLAIM_TOKEN.findall(block)
        source = None
        for cid in cited:
            source = strongest_edge_source(cid, edges, excerpts, sources)
            if source:
                break
        if source is None:
            out.append(block)
            continue
        lines = block.rstrip().split("\n")
        lines[-1] = lines[-1].rstrip() + " " + citation_for(source)
        out.append("\n".join(lines))
    return "\n\n".join(out)


def related_links(basename: str, claim_ids: list[str], neighbours: list[dict[str, Any]]) -> list[str]:
    """3–8 links chosen by claim-id overlap, topped up from the same publish set."""
    mine = set(claim_ids)
    scored: list[tuple[int, str]] = []
    for other in neighbours:
        if other["basename"] == basename:
            continue
        overlap = len(mine & set(other.get("claim_ids") or []))
        if overlap:
            scored.append((-overlap, other["basename"]))
    scored.sort()
    chosen: list[str] = []
    for _, name in scored:
        if name not in chosen:
            chosen.append(name)
    chosen = chosen[:8]
    if len(chosen) < 3:
        for other in neighbours:
            if other["basename"] == basename or other["basename"] in chosen:
                continue
            chosen.append(other["basename"])
            if len(chosen) >= 3:
                break
    return chosen[:8]


# --------------------------------------------------------------------------
# projection — one description of what this run says, used by both the fresh
# publish and the merge planner. Nothing below writes anything.
# --------------------------------------------------------------------------


def _support_ids(cid: str, edges: dict, excerpts: dict) -> list[str]:
    return sorted(
        {
            excerpts[edge["excerpt_id"]]["source_id"]
            for edge in edges.values()
            if edge["claim_id"] == cid and edge["decision"] == "accepted" and edge["excerpt_id"] in excerpts
        }
    )


def claim_values(cid: str, claim: dict, edges: dict, excerpts: dict) -> dict[str, str]:
    detail = "; ".join(part for part in (claim.get("scope"), claim.get("action"), claim.get("caveat")) if part)
    return {
        "id": cid,
        "text": claim["text"],
        "claim_type": claim.get("claim_type") or "descriptive",
        "importance": claim.get("importance") or "major",
        "status": f"**{claim['status']}**",
        "evidence": ", ".join(_support_ids(cid, edges, excerpts)) or "—",
        "detail": detail or "—",
        "groups": str(len(claim.get("origin_groups", []))),
    }


def source_values(sid: str, source: dict) -> dict[str, str]:
    title = source.get("title") or sid
    url = source.get("url") or ""
    cell = f"{_md_label(source.get('publisher') or 'unknown')}, " + (
        f"[{_md_label(title, nested=True)}]({_md_url(url)})" if url else _md_label(title))
    return {
        "id": sid,
        "tier": f"T{source.get('authority_tier', 5)}",
        "accessed": source.get("accessed_at") or "unknown",
        "source": cell,
        "origin_group": source.get("origin_group") or "unknown",
        "use": source.get("use") or "—",
        "limitations": source.get("limitations") or "—",
    }


def gap_values(gid: str, gap: dict) -> dict[str, str]:
    return {
        "id": gid,
        "question": gap["question"],
        "impact": gap.get("impact") or "material",
        "status": gap.get("status") or "open",
        "closed_by": gap.get("closed_by") or "—",
        "next_action": gap.get("next_action") or "—",
    }


def contradiction_values(xid: str, item: dict) -> dict[str, str]:
    return {
        "id": xid,
        "claims": ", ".join(item.get("claim_ids") or []) or "—",
        "axis": item.get("axis") or "factual",
        "severity": item.get("severity") or "medium",
        "status": f"**{item.get('status') or 'unresolved'}**",
        "summary": item.get("summary") or "—",
    }


def evidence_block(cid: str, claim: dict, edges: dict, excerpts: dict, sources: dict) -> dict[str, Any] | None:
    related = sorted(
        (edge for edge in edges.values() if edge["claim_id"] == cid), key=lambda e: e["id"]
    )
    if not related:
        return None
    bullets: list[dict[str, str]] = []
    for edge in related:
        excerpt = excerpts.get(edge["excerpt_id"])
        if not excerpt:
            continue
        source = sources.get(excerpt["source_id"], {})
        check = excerpt["verification"]
        url = source.get("url", "")
        title = source.get("title", "?")
        link = f"[{_md_label(title, nested=True)}]({_md_url(url)})" if url else _md_label(title)
        locator_kind = re.sub(r"\W", "", str(excerpt["locator"].get("kind") or "")) or "section"
        locator_value = _md_text(excerpt["locator"].get("value", "")).replace("`", "'")
        head = (
            f"- `{edge['id']}` **{edge['relation']}** ({edge['decision']}) — "
            f"{_md_label(source.get('publisher', '?'))}, {link} "
            f"@ {locator_kind} `{locator_value}` "
            f"· quote check: **{check['status']}**"
            + (f" ({check['score']})" if check.get("score") is not None else "")
        )
        bullets.append({"id": edge["id"], "text": head + "\n  > " + _md_quote(excerpt["quote"])})
    if not bullets:
        return None
    return {
        "id": cid,
        "heading": f"### {cid} — {_md_text(claim['text'])}",
        "status_line": f"Status: **{claim['status']}** — {_md_text(claim['status_reason'])}",
        "bullets": bullets,
    }


def _note_frontmatter(note: dict, level: str, domain_name: str, run_id: str) -> str:
    return _fm(
        [
            ("title", note["title"]),
            ("type", note["note_type"]),
            ("status", "active"),
            ("evidence_level", level),
            ("domain", domain_name),
            ("published", today()),
            ("last_verified", today()),
            ("review_due", plus_days(REVIEW_DAYS.get(level, 90))),
            ("run_id", run_id),
            ("claims", note["claim_ids"]),
            ("tags", ["research", slugify(domain_name, 30)]),
        ]
    )


def render_content_note(
    note: dict,
    *,
    names: dict[str, str],
    moc: str,
    domain_name: str,
    level: str,
    run_id: str,
    claims: dict,
    edges: dict,
    excerpts: dict,
    sources: dict,
    related: list[str],
    note_titles: set[str] | frozenset[str] = frozenset(),
) -> str:
    ledger = names["claim_ledger"]
    body = note.get("body_md", "").strip()
    body = add_inline_citations(body, edges, excerpts, sources)
    body = rewrite_meta_links(rewrite_claim_tokens(body, ledger, set(claims)), names, note_titles)
    summary = rewrite_meta_links(
        rewrite_claim_tokens(note.get("summary", "").strip(), ledger, set(claims)), names, note_titles)

    parts = [_note_frontmatter(note, level, domain_name, run_id), f"\n# {_md_text(note['title'])}\n"]
    if summary:
        parts.append(f"\n{summary}\n")
    parts.append(f"\n{body}\n")
    if note["claim_ids"]:
        parts.append("\n## Claims used\n\n")
        for cid in note["claim_ids"]:
            claim = claims.get(cid)
            if claim:
                parts.append(f"- [[{ledger}#{cid}]] **{claim['status']}** — {_md_text(claim['text'])}\n")
    if related:
        parts.append("\n## Related\n\n")
        parts.extend(f"- [[{name}]]\n" for name in related)
    parts.append(
        f"\n---\n\nUp: [[{moc}]] · [[{ledger}]] · [[{names['source_register']}]]\n"
    )
    return "".join(parts)


def render_dataset_note(
    table: dict,
    *,
    names: dict[str, str],
    moc: str,
    domain_name: str,
    run_id: str,
    sources: dict,
) -> str:
    """`type: dataset`, inside the owning domain folder. No `claims:` key — the
    gate for a dataset is `source_urls` plus a per-row fidelity column."""
    urls = sorted(
        {
            sources[row["source_id"]]["url"]
            for row in table["rows"]
            if row.get("source_id") in sources and sources[row["source_id"]].get("url")
        }
    )
    fidelities = sorted({row["fidelity"] for row in table["rows"]})
    mismatched = [row for row in table["rows"] if row.get("fidelity_result") in {"mismatch", "no_cache"}]
    front = _fm(
        [
            ("title", table["title"]),
            ("type", "dataset"),
            ("status", "active"),
            ("evidence_level", "empirical" if not mismatched else "mixed"),
            ("domain", domain_name),
            ("published", today()),
            ("last_verified", today()),
            ("review_due", plus_days(90)),
            ("run_id", run_id),
            ("as_of", table.get("as_of") or "unknown"),
            ("fidelity", fidelities),
            ("source_urls", urls),
            ("tags", ["dataset", slugify(domain_name, 30)]),
        ]
    )
    header = "| " + " | ".join([_md_cell(col) for col in table["columns"]] + ["Fidelity", "Source", "As of"]) + " |"
    sep = "|" + "---|" * (len(table["columns"]) + 3)
    lines = [header, sep]
    for row in table["rows"]:
        flag = " ⚠" if row.get("fidelity_result") in {"mismatch", "no_cache"} else ""
        source = sources.get(row["source_id"], {})
        url = source.get("url") or ""
        publisher = _md_label(source.get("publisher", row["source_id"]))
        cell = f"[{publisher}]({_md_url(url)})" if url else publisher
        first = [_md_cell(value) for value in row["cells"]]
        if first:
            # a link target holds no `[`, `]`, `|`, `#`, `^` or backslash
            target = LINK_TARGET_UNSAFE.sub("-", _md_text(row["cells"][0]))
            first[0] = f"[[{target}]]"
        lines.append(
            "| " + " | ".join(first + [f"{row['fidelity']}{flag}", cell, _md_cell(row.get("as_of") or "—")]) + " |"
        )
    derived = [row for row in table["rows"] if row["fidelity"] == "derived"]
    observed = [row for row in table["rows"] if row["fidelity"] == "observed"]

    def code(value: Any) -> str:
        return _md_text(value).replace("`", "'")

    parts = [
        front,
        f"\n# {_md_text(table['title'])}\n\n",
        f"As of {_md_text(table.get('as_of') or 'unknown')}. Locator: `{code(table.get('locator') or '—')}`. "
        "First-column entities are wikilinked whether or not a note exists yet; a broken link "
        f"is a note to be written and is listed in [[{names['wanted_notes']}]].\n\n",
        "\n".join(lines),
        "\n\n## Derived values\n\n",
    ]
    if derived:
        for row in derived:
            parts.append(
                f"- {_md_text(row['cells'][0])}: `{code(row.get('formula', ''))}` from inputs "
                f"{_md_text(', '.join(row.get('inputs', [])))}\n"
            )
    else:
        parts.append("- _No derived rows: every value in this table was read verbatim._\n")
    if observed:
        parts.append("\n## Observed values\n\n")
        for row in observed:
            parts.append(f"- {_md_text(row['cells'][0])}: {_md_text(row.get('evidence_note', ''))} (artifact on file)\n")
    parts.append("\n## Design reading\n\n")
    parts.append(
        "- These are recorded values, not recommendations. Two sources disagreeing appear as two "
        f"rows here and as an entry in [[{names['contradictions']}]] — they are never averaged.\n"
    )
    parts.append("\n## Gaps\n\n")
    if mismatched:
        for row in mismatched:
            parts.append(
                f"- ⚠ Row {row['index']} ({row['cells'][0] if row['cells'] else '?'}) did not verify "
                f"against the cached source ({row.get('fidelity_result')}). Re-read the source before using it.\n"
            )
    else:
        parts.append("- Every verbatim cell verified against its cached source.\n")
    parts.append(f"\n---\n\nUp: [[{moc}]] · [[{names['source_register']}]]\n")
    return "".join(parts)


def render_moc(folder: dict, names: dict[str, str], notes: list[dict[str, str]]) -> str:
    moc = folder["moc"]
    parts = [
        _fm(
            [
                ("title", moc),
                ("type", "moc"),
                ("status", "active"),
                ("evidence_level", "mixed"),
                ("published", today()),
                ("last_verified", today()),
                ("review_due", plus_days(90)),
                ("tags", ["moc"]),
            ]
        ),
        f"\n# {moc}\n\n{_md_text(folder.get('description', ''))}\n\n## Notes\n\n",
    ]
    for note in notes:
        parts.append(f"- [[{note['basename']}]] — {_md_text(note['summary'])}\n")
    if not notes:
        parts.append(f"- _No notes yet. Tracked in [[{names['gaps']}]]._\n")
    parts.append("\n## Questions this domain owns\n\n")
    for question in folder.get("questions", []):
        parts.append(f"- {_md_text(question)}\n")
    parts.append(f"\n---\n\nUp: [[{names['home']}]]\n")
    return "".join(parts)


def render_home(manifest: dict, names: dict[str, str], folders: list[dict]) -> str:
    parts = [
        _fm(
            [
                ("title", names["home"]),
                ("type", "home"),
                ("status", "active"),
                ("evidence_level", "mixed"),
                ("published", today()),
                ("last_verified", today()),
                ("review_due", plus_days(90)),
                ("run_id", manifest["run_id"]),
                ("tags", ["home"]),
            ]
        ),
        f"\n# {_md_text(manifest['question'])}\n\n",
        f"Knowledge base generated by `/megavault` on {today()}. Mode: `{manifest['mode']}`.\n\n",
        "## Domains\n\n",
    ]
    for folder in folders:
        parts.append(f"- [[{folder['moc']}]] — {_md_text(folder.get('description', ''))}\n")
    parts.append(
        "\n## Evidence layer\n\n"
        f"- [[{names['source_register']}]] — every source, with tier, origin group, access date, use and limits\n"
        f"- [[{names['claim_ledger']}]] — every claim, with type, importance, status, scope, action and caveat\n"
        f"- [[{names['evidence_map']}]] — claim to excerpt links and their verification state\n"
        f"- [[{names['contradictions']}]] — conflicts and how they were handled\n"
        f"- [[{names['gaps']}]] — what is not answered yet, and what has been closed\n"
        f"- [[{names['wanted_notes']}]] — every broken link, grouped by the note that wants it\n"
        "\n## Meta\n\n"
        f"- [[{names['metadata_schema']}]] — frontmatter contract\n"
        f"- [[{names['change_log']}]] — what each run changed\n"
        f"- [[{names['validation_report']}]] — graph and policy check for this vault\n"
        "\n> [!warning] Read the status column\n"
        "> A claim marked `qualified`, `disputed` or `unsupported` is not a finding. "
        "Statuses are computed from verified evidence, not from confidence.\n"
    )
    return "".join(parts)


def _table_note(title: str, kind: str, intro: str, header: list[str], rows: list[str],
                footer: str, review: int, tags: list[str], empty_note: str,
                appendix: str = "") -> str:
    sep = "|" + "---|" * len(header)
    lines = ["| " + " | ".join(header) + " |", sep] + rows
    body = "\n".join(lines)
    if not rows:
        body += f"\n\n{empty_note}"
    if appendix:
        body += "\n" + appendix.rstrip("\n")
    return (
        _fm(
            [
                ("title", title),
                ("type", kind),
                ("status", "active"),
                ("evidence_level", "mixed"),
                ("published", today()),
                ("last_verified", today()),
                ("review_due", plus_days(review)),
                ("tags", tags),
            ]
        )
        + f"\n# {title}\n\n{intro}\n\n"
        + body
        + f"\n\n---\n\n{footer}\n"
    )


def render_wanted_notes(title: str, names: dict[str, str], wanted: dict[str, list[str]]) -> str:
    parts = [
        _fm(
            [
                ("title", title),
                ("type", "backlog"),
                ("status", "active"),
                ("evidence_level", "mixed"),
                ("published", today()),
                ("last_verified", today()),
                ("review_due", plus_days(60)),
                ("tags", ["gaps", "links"]),
            ]
        ),
        f"\n# {title}\n\n",
        "Every wikilink in this vault that does not resolve to a note, grouped by the note that "
        "wants it. A broken link is a work item, not an error — delete nothing here, write the note "
        "or drop the link.\n",
    ]
    if not wanted:
        parts.append("\n_Every wikilink in this vault resolves._\n")
    for referrer in sorted(wanted):
        parts.append(f"\n## From [[{referrer}]]\n\n")
        for target in sorted(set(wanted[referrer])):
            parts.append(f"- [[{target}]]\n")
    parts.append(f"\n---\n\nUp: [[{names['home']}]]\n")
    return "".join(parts)


def render_metadata_schema(title: str, names: dict[str, str]) -> str:
    return (
        _fm(
            [
                ("title", title),
                ("type", "schema"),
                ("status", "active"),
                ("evidence_level", "official"),
                ("published", today()),
                ("last_verified", today()),
                ("review_due", plus_days(180)),
                ("tags", ["meta"]),
            ]
        )
        + f"\n# {title}\n\n"
        "Every note carries these keys. `validate` fails the publish if one is missing.\n\n"
        "| Key | Values |\n|---|---|\n"
        "| `type` | `home`, `moc`, `concept`, `profile`, `playbook`, `guide`, `dataset`, `register`, "
        "`ledger`, `map`, `backlog`, `schema`, `log` |\n"
        "| `status` | `active`, `draft`, `needs_review`, `deprecated` |\n"
        "| `evidence_level` | `official`, `peer_reviewed`, `empirical`, `mixed`, `hypothesis`, `unsupported` |\n"
        "| `published` / `last_verified` / `review_due` | ISO dates |\n"
        "| `claims` | required on concept, profile, playbook and guide notes |\n"
        "| `fidelity`, `source_urls`, `as_of` | required on `dataset` notes, which carry no `claims` |\n\n"
        "`evidence_level` is computed from the source tiers behind a note's claims. It is not an "
        f"editorial opinion, and should not be edited by hand.\n\n---\n\nUp: [[{names['home']}]]\n"
    )


def meta_names_for(prefix: str) -> dict[str, str]:
    return {
        "home": home_basename(prefix),
        "claim_ledger": meta_basename(prefix, "Claim Ledger"),
        "source_register": meta_basename(prefix, "Source Register"),
        "evidence_map": meta_basename(prefix, "Evidence Map"),
        "contradictions": meta_basename(prefix, "Contradictions"),
        "gaps": meta_basename(prefix, "Gaps and Backlog"),
        "wanted_notes": meta_basename(prefix, "Wanted Notes"),
        "change_log": meta_basename(prefix, "Change Log"),
        "metadata_schema": meta_basename(prefix, "Metadata Schema"),
        "validation_report": meta_basename(prefix, "Validation Report"),
    }


META_LOCATION = {
    "claim_ledger": "90 Evidence",
    "source_register": "90 Evidence",
    "evidence_map": "90 Evidence",
    "contradictions": "90 Evidence",
    "gaps": "90 Evidence",
    "wanted_notes": "90 Evidence",
    "change_log": "99 Meta",
    "metadata_schema": "99 Meta",
    "validation_report": "99 Meta",
}


def project_run(run: Run, prefix: str, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    """Everything this run wants the vault to say, as data. The fresh publish
    renders it into files; the merge planner diffs it against what is there."""
    manifest = run.manifest
    claims = run.records("claims")
    sources = run.records("sources")
    excerpts = run.records("excerpts")
    edges = run.records("edges")
    contradictions = run.records("contradictions")
    gaps = run.records("gaps")
    notes = run.records("notes")
    tables = run.records("tables")
    domains = run.taxonomy.get("domains", [])

    names = meta_names_for(prefix)
    if existing:
        for key, value in (existing.get("meta_names") or {}).items():
            if key in names and value:
                names[key] = value
        if existing.get("meta_names", {}).get("home"):
            names["home"] = existing["meta_names"]["home"]

    existing_folders = {}
    used_indices: set[int] = set()
    for folder in (existing or {}).get("folders", []) or []:
        existing_folders[folder["name"]] = folder
        try:
            used_indices.add(int(folder["index"]))
        except (TypeError, ValueError):
            pass

    folders: list[dict[str, Any]] = []
    not_researched: list[dict[str, str]] = []
    next_index = 1
    for domain in domains:
        found = existing_folders.get(domain["name"])
        if (
            domain.get("status") == "deprecated"
            and not found
            and not any(domain["id"] in claim.get("domain_ids", []) for claim in claims.values())
            and not any(note.get("domain_id") == domain["id"] for note in notes.values())
            and not any(table.get("domain_id") == domain["id"] for table in tables.values())
        ):
            # an empty MOC folder would advertise research that never happened
            not_researched.append({"id": domain["id"], "name": domain["name"]})
            continue
        if found:
            dir_name = found["folder"]
            moc = found.get("moc") or f"{dir_name} MOC"
            fresh = False
        else:
            while next_index in used_indices:
                next_index += 1
            used_indices.add(next_index)
            dir_name = f"{next_index:02d} {domain['name']}"
            moc = f"{dir_name} MOC"
            fresh = True
        folders.append(
            {
                "domain_id": domain["id"],
                "folder": dir_name,
                "moc": moc,
                "name": domain["name"],
                "description": domain.get("description", ""),
                "questions": domain.get("questions", []),
                "is_new": fresh,
            }
        )
    folder_by_domain = {folder["domain_id"]: folder for folder in folders}

    published_basenames = {
        note["basename"] for note in (existing or {}).get("notes", []) or []
    }

    # notes that this run actually authored (adopted note records carry no body)
    new_notes: list[dict[str, Any]] = []
    for nid, note in sorted(notes.items()):
        if note.get("origin") == "adopted":
            continue
        folder = folder_by_domain.get(note.get("domain_id")) or (folders[0] if folders else None)
        if folder is None:
            die("cannot publish notes without a taxonomy: no domains defined")
        basename = _safe_title(note["title"])
        new_notes.append(
            {
                "id": nid,
                "basename": basename,
                "title": note["title"],
                "note_type": note.get("note_type") or "concept",
                "summary": note.get("summary") or note.get("note_type") or "",
                "body_md": note.get("body_md") or "",
                "claim_ids": [cid for cid in note.get("claim_ids", []) if cid in claims],
                "folder": folder["folder"],
                "moc": folder["moc"],
                "domain_name": folder["name"],
                "already_published": basename in published_basenames,
            }
        )

    dataset_notes: list[dict[str, Any]] = []
    for did, table in sorted(tables.items()):
        folder = folder_by_domain.get(table.get("domain_id")) or (folders[0] if folders else None)
        if folder is None:
            die("cannot publish a dataset without a taxonomy: no domains defined")
        dataset_notes.append(
            {
                "id": did,
                "table": table,
                "basename": _safe_title(table["title"]),
                "title": table["title"],
                "folder": folder["folder"],
                "moc": folder["moc"],
                "domain_name": folder["name"],
                "summary": f"Dataset, {len(table['rows'])} rows, as of {table.get('as_of') or 'unknown'}",
            }
        )

    neighbours = [
        {"basename": note["basename"], "claim_ids": note["claim_ids"], "folder": note["folder"]}
        for note in new_notes
    ] + [
        {
            "basename": note["basename"],
            "claim_ids": note.get("claims") or [],
            "folder": note.get("folder", ""),
        }
        for note in (existing or {}).get("notes", []) or []
    ]

    rendered_notes: list[dict[str, Any]] = []
    note_titles = {note["basename"] for note in neighbours}
    for note in new_notes:
        level = evidence_level_for(note["claim_ids"], claims, sources, edges, excerpts)
        text = render_content_note(
            note,
            names=names,
            moc=note["moc"],
            domain_name=note["domain_name"],
            level=level,
            run_id=manifest["run_id"],
            claims=claims,
            edges=edges,
            excerpts=excerpts,
            sources=sources,
            related=related_links(note["basename"], note["claim_ids"], neighbours),
            note_titles=note_titles,
        )
        rendered_notes.append({**note, "text": text, "path": f"{note['folder']}/{note['basename']}.md"})

    for note in dataset_notes:
        text = render_dataset_note(
            note["table"],
            names=names,
            moc=note["moc"],
            domain_name=note["domain_name"],
            run_id=manifest["run_id"],
            sources=sources,
        )
        rendered_notes.append(
            {
                **note,
                "note_type": "dataset",
                "claim_ids": [],
                "text": text,
                "path": f"{note['folder']}/{note['basename']}.md",
                "already_published": note["basename"] in published_basenames,
            }
        )

    return {
        "prefix": prefix,
        "names": names,
        "folders": folders,
        "not_researched": not_researched,
        "coverage": read_json(run.reports / "coverage.json", {}) or None,
        "notes": rendered_notes,
        "claims": claims,
        "sources": sources,
        "excerpts": excerpts,
        "edges": edges,
        "gaps": gaps,
        "contradictions": contradictions,
        "manifest": manifest,
        "claim_values": {cid: claim_values(cid, claim, edges, excerpts) for cid, claim in claims.items()},
        "source_values": {sid: source_values(sid, src) for sid, src in sources.items()},
        "gap_values": {gid: gap_values(gid, gap) for gid, gap in gaps.items()},
        "cx_values": {xid: contradiction_values(xid, item) for xid, item in contradictions.items()},
        "evidence_blocks": {
            cid: block
            for cid, claim in sorted(claims.items())
            if (block := evidence_block(cid, claim, edges, excerpts, sources))
        },
    }


# --------------------------------------------------------------------------
# fresh vault assembly
# --------------------------------------------------------------------------


def collect_wanted(files: dict[str, str]) -> dict[str, list[str]]:
    """Broken wikilinks in a file set, grouped by the note that wants them."""
    known = {Path(rel).stem for rel in files}
    wanted: dict[str, list[str]] = {}
    for rel, text in sorted(files.items()):
        stem = Path(rel).stem
        for match in WIKILINK.finditer(text):
            target = match.group(1).strip().split("/")[-1]
            if target and target not in known:
                wanted.setdefault(stem, []).append(target)
    return wanted


COVERAGE_HEADING = "## Coverage"
NOT_RESEARCHED_HEADING = "## Not researched"


def _pct(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)}%" if whole else "n/a"


def _n(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def coverage_rows(report: dict[str, Any]) -> list[tuple[str, str]]:
    """(measure, value) rows of the published Coverage table, from coverage.json."""
    rows: list[tuple[str, str]] = []
    arms = report.get("arms") or {}
    if arms:
        order = {brief: index for index, brief in enumerate(BRIEFS)}
        rows.append(("Discovery arms", "; ".join(
            f"{arm}: {_n(item['claims'], 'claim')}, {item['unsupported_pct']}% unsupported"
            for arm, item in sorted(arms.items(), key=lambda pair: (order.get(pair[0], 99), pair[0]))
        )))
    overlap = report.get("overlap") or {}
    pairs = overlap.get("pairs") or []
    if pairs:
        rows.append(("Arm overlap", "; ".join(
            f"{pair['domain_id']} {pair['arm_a']['brief']} x {pair['arm_b']['brief']}: "
            + (f"{pair['m']} shared of {pair['n1']} and {pair['n2']}, Chapman estimate "
               f"{pair['chapman']} claims ({pair['label']})"
               if pair.get("m") and pair.get("chapman") is not None else
               f"{pair['n1']} and {pair['n2']} claims, Chapman estimate undefined (0 shared)")
            for pair in pairs
        )))
    else:
        rows.append(("Arm overlap", "no domain had two discovery arms; no estimate"))
    off = report.get("off_skeleton") or {}
    if off:
        per_page = off.get("per_page")
        rows.append(("Off-skeleton claims", (
            f"{off.get('claims', 0)} over {_n(off.get('pages_read', 0), 'page')} read"
            + (f" ({per_page} per page)" if per_page is not None else "")
        )))
    reread = report.get("reread") or {}
    if reread:
        high = reread.get("high") or {}
        rows.append(("Re-read recall", (
            f"{reread.get('matched', 0)} of {_n(reread.get('findings', 0), 'finding')} matched a claim "
            f"({_pct(reread.get('matched', 0), reread.get('findings', 0))}); high importance "
            f"{high.get('matched', 0)} of {high.get('findings', 0)} "
            f"({_pct(high.get('matched', 0), high.get('findings', 0))}), "
            f"{_n(reread.get('pages', 0), 'sampled page')}"
        )))
    return rows


def coverage_table(report: dict[str, Any]) -> str:
    lines = ["| Measure | Value |", "|---|---|"]
    lines += [f"| {measure} | {value.replace('|', '/')} |" for measure, value in coverage_rows(report)]
    return "\n".join(lines) + "\n"


COVERAGE_INTRO = (
    "Measured at run end, never asserted: these numbers estimate what discovery may have "
    "missed. None of them shows that the base is complete."
)


def gaps_appendix(projection: dict[str, Any]) -> str:
    """What the Gaps and Backlog file carries below its table: the measured
    coverage, then the domains that were planned but never researched."""
    parts: list[str] = []
    if projection.get("coverage"):
        parts.append(f"\n{COVERAGE_HEADING}\n\n{COVERAGE_INTRO}\n\n{coverage_table(projection['coverage'])}")
    if projection.get("not_researched"):
        parts.append(f"\n{NOT_RESEARCHED_HEADING}\n\n" + "".join(
            f"- {not_researched_line(item)}\n" for item in projection["not_researched"]
        ))
    return "".join(parts)


def not_researched_line(item: dict[str, str]) -> str:
    return f"domain {item['id']} {item['name']}: not researched"


# The ledger's own statement of the status rule. Vaults published before it
# was corrected carry LEGACY_REFUTATION_RULE, which a merge patches line-exactly.
LEDGER_REFUTATION_RULE = (
    "Accepted evidence both ways makes a claim `disputed`; accepted refuting evidence with no "
    "accepted support makes it `refuted`."
)
LEGACY_REFUTATION_RULE = "Any accepted refuting evidence makes a claim `disputed`."
LEDGER_INTRO = (
    "Status is computed, not asserted: `supported` needs accepted evidence; central, numerical, "
    "causal, comparative and predictive claims additionally need two independent origin groups, "
    "otherwise they are `qualified`. " + LEDGER_REFUTATION_RULE
)

POLICY_HEADING = "## Policy"


def policy_line(exc: dict[str, Any]) -> str:
    """One confirmed exception, as a reader needs it: what, where, why, when."""
    # user text on one line, with brackets that could open a wikilink made inert
    where = f"in domain {_md_label(exc['scope'])}" if exc.get("scope") else "run-wide"
    when = _md_label(str(exc.get("added_at") or "")[:10] or "undated")
    return (f"{_md_label(exc.get('action') or '?')} {_md_label(exc.get('domain') or '?')}, {where}, "
            f"over profile {_md_label(exc.get('profile') or '?')} ({when}): {_md_label(exc.get('reason') or '')}")


def policy_section(manifest: dict[str, Any], heading: bool = True,
                   exceptions: list[dict[str, Any]] | None = None) -> str:
    """The Source Register's record of the rules sources were admitted under:
    the profile, and every exception the user confirmed, with its reason
    (`exceptions`: only these, for a merge into a register that lists others)."""
    policy = policy_of(manifest)
    listed = policy["exceptions"] if exceptions is None else exceptions
    lines = [f"\n{POLICY_HEADING}\n"] if heading else []
    lines.append(
        f"\nRun `{_md_text(manifest.get('run_id') or '?')}` admitted sources under the source profile "
        f"{_md_label(policy['profile'])}"
        + (". Exceptions the user confirmed:\n\n" if listed else ", with no exceptions.\n")
    )
    lines += [f"- {policy_line(exc)}\n" for exc in listed]
    return "".join(lines)


def build_vault_files(run: Run, prefix: str) -> dict[str, str]:
    """A complete, fully prefixed vault as {relative path: text}. Meta names and
    every internal wikilink are born prefixed — there is no rename pass."""
    projection = project_run(run, prefix)
    names, folders = projection["names"], projection["folders"]
    manifest = projection["manifest"]
    files: dict[str, str] = {}

    notes_by_folder: dict[str, list[dict[str, Any]]] = {}
    for note in projection["notes"]:
        files[note["path"]] = note["text"]
        notes_by_folder.setdefault(note["folder"], []).append(note)

    for folder in folders:
        listed = sorted(notes_by_folder.get(folder["folder"], []), key=lambda n: n["basename"])
        files[f"{folder['folder']}/{folder['moc']}.md"] = render_moc(
            folder, names, [{"basename": n["basename"], "summary": n["summary"]} for n in listed]
        )

    files[f"{names['home']}.md"] = render_home(manifest, names, folders)

    files[f"90 Evidence/{names['source_register']}.md"] = _table_note(
        names["source_register"], "register",
        f"{len(projection['sources'])} sources. Tier 1 = primary/official/normative, 2 = peer reviewed, "
        "3 = transparent preprint or open artifact, 4 = commercial or observational, 5 = anecdotal. "
        "`Origin group` is what corroboration is counted over: two sources in the same group are one group.",
        SOURCE_HEADER,
        [_row_line(SOURCE_HEADER, SOURCE_COLS, projection["source_values"][sid])
         for sid in sorted(projection["source_values"])],
        f"Up: [[{names['home']}]]", 30, ["sources", "evidence"],
        "_No sources recorded._",
        appendix=policy_section(manifest),
    )
    files[f"90 Evidence/{names['claim_ledger']}.md"] = _table_note(
        names["claim_ledger"], "ledger", LEDGER_INTRO,
        CLAIM_HEADER,
        [_row_line(CLAIM_HEADER, CLAIM_COLS, projection["claim_values"][cid])
         for cid in sorted(projection["claim_values"])],
        f"Up: [[{names['home']}]] · [[{names['evidence_map']}]] · [[{names['source_register']}]]",
        30, ["claims", "evidence"], "_No claims recorded._",
        appendix=render_claim_index(
            names["evidence_map"],
            [(cid, projection["claim_values"][cid]["text"]) for cid in sorted(projection["claim_values"])],
        ) if projection["claim_values"] else "",
    )
    files[f"90 Evidence/{names['gaps']}.md"] = _table_note(
        names["gaps"], "backlog",
        "What this base does not answer. A gap is a result, not a failure — it is the honest boundary "
        "of the run. Closed gaps stay in the table with the claim that closed them, so a later run "
        "does not reopen them.",
        GAP_HEADER,
        [_row_line(GAP_HEADER, GAP_COLS, projection["gap_values"][gid])
         for gid in sorted(projection["gap_values"])],
        f"Up: [[{names['home']}]]", 30, ["gaps"], "_No gaps recorded._",
        appendix=gaps_appendix(projection),
    )
    files[f"90 Evidence/{names['contradictions']}.md"] = _table_note(
        names["contradictions"], "ledger",
        "Unresolved conflicts stay visible. They are never averaged, and never resolved by counting sources.",
        CX_HEADER,
        [_row_line(CX_HEADER, CX_COLS, projection["cx_values"][xid])
         for xid in sorted(projection["cx_values"])],
        f"Up: [[{names['home']}]] · [[{names['claim_ledger']}]]", 60, ["contradictions"],
        "_No contradictions recorded._",
    )

    emap = [
        _fm(
            [
                ("title", names["evidence_map"]),
                ("type", "map"),
                ("status", "active"),
                ("evidence_level", "mixed"),
                ("published", today()),
                ("last_verified", today()),
                ("review_due", plus_days(60)),
                ("tags", ["evidence"]),
            ]
        ),
        f"\n# {names['evidence_map']}\n\n",
        "Every claim-to-passage link, with the mechanical quote check. `exact` means the quote was "
        "found verbatim in the cached fetch (or in the text of a cached HTML page); `fuzzy` means it "
        "matched once punctuation, hyphenation and line numbers were ignored (no score shown), or "
        "matched above a similarity threshold with the page's digits and without a changed negation, "
        "direction or quantity word; `mismatch` (also a high score whose quote changed one of those) "
        "and `no_cache` mean the link is **not** accepted evidence. The check catches formatting "
        "differences, not every change of meaning: the quote shown is the extractor's, so read a "
        "`fuzzy` one against its source before you quote it.\n",
    ]
    for cid in sorted(projection["evidence_blocks"]):
        block = projection["evidence_blocks"][cid]
        emap.append(f"\n{block['heading']}\n\n{block['status_line']}\n\n")
        emap.extend(bullet["text"] + "\n" for bullet in block["bullets"])
    emap.append(f"\n---\n\nUp: [[{names['home']}]] · [[{names['claim_ledger']}]]\n")
    files[f"90 Evidence/{names['evidence_map']}.md"] = "".join(emap)

    files[f"99 Meta/{names['metadata_schema']}.md"] = render_metadata_schema(
        names["metadata_schema"], names
    )
    files[f"99 Meta/{names['change_log']}.md"] = (
        _fm(
            [
                ("title", names["change_log"]),
                ("type", "log"),
                ("status", "active"),
                ("evidence_level", "mixed"),
                ("published", today()),
                ("last_verified", today()),
                ("review_due", plus_days(365)),
                ("tags", ["meta"]),
            ]
        )
        + f"\n# {names['change_log']}\n\nUp: [[{names['home']}]]\n\n"
        + "One entry per publish, newest at the bottom. Status changes are the interesting rows.\n"
        + change_log_entry(run, projection, {}, first=True)
    )
    files[f"99 Meta/{names['validation_report']}.md"] = _validation_note_text(
        names["validation_report"],
        names["home"],
        {
            "checked_at": "not yet run",
            "notes": 0,
            "wikilinks": 0,
            "duplicate_basenames": 0,
            "max_depth": None,
            "valid": False,
            "errors": ["validation has not been run for this vault yet"],
            "warnings": [],
        },
    )

    # Wanted Notes must see the finished tree, itself included.
    files[f"90 Evidence/{names['wanted_notes']}.md"] = render_wanted_notes(
        names["wanted_notes"], names, {}
    )
    wanted = collect_wanted(files)
    wanted.pop(names["wanted_notes"], None)
    files[f"90 Evidence/{names['wanted_notes']}.md"] = render_wanted_notes(
        names["wanted_notes"], names, wanted
    )
    return files


def change_log_entry(run: Run, projection: dict, previous_status: dict[str, str],
                     first: bool = False, needs_rewrite: list[dict] | None = None,
                     added: list[str] | None = None) -> str:
    manifest = projection["manifest"]
    claims = projection["claims"]
    lines = [f"\n## {today()} — run `{manifest['run_id']}`\n\n"]
    if first:
        lines.append(
            f"- Initial publication: {len(claims)} claims, {len(projection['sources'])} sources, "
            f"{len(projection['gaps'])} gaps, {len(projection['notes'])} notes.\n"
        )
        return "".join(lines)
    added = added if added is not None else [cid for cid in claims if cid not in previous_status]
    lines.append(f"- Added: {len(added)} claims {', '.join(sorted(added)[:12]) or '—'}\n")
    for cid in sorted(previous_status):
        if cid in claims and previous_status[cid] != claims[cid]["status"]:
            lines.append(
                f"- Status change `{cid}`: {previous_status[cid]} → **{claims[cid]['status']}**\n"
            )
    for item in needs_rewrite or []:
        lines.append(
            f"- **NEEDS REWRITE** [[{item['note']}]]: `{item['claim']}` went "
            f"{item['from']} → **{item['to']}**. The prose still reads as if it were "
            f"{item['from']}; an Update section was appended but the body was not touched.\n"
        )
    return "".join(lines)


def _validation_note_text(title: str, home: str, report: dict[str, Any]) -> str:
    lines = [
        _fm(
            [
                ("title", title),
                ("type", "log"),
                ("status", "active"),
                ("evidence_level", "official"),
                ("published", today()),
                ("last_verified", today()),
                ("review_due", plus_days(30)),
                ("tags", ["meta", "validation"]),
            ]
        ),
        f"\n# {title}\n\nChecked {report['checked_at']}.\n\n",
        f"- Notes: {report['notes']}\n",
        f"- Wikilinks checked: {report['wikilinks']}\n",
        f"- Duplicate basenames: {report['duplicate_basenames']}\n",
        f"- Max hops from Home: {report['max_depth']}\n",
        f"- Result: **{'PASS' if report['valid'] else 'FAIL'}**\n",
    ]
    if report["errors"]:
        lines.append("\n## Errors\n\n")
        lines.extend(f"- {item}\n" for item in report["errors"])
    if report["warnings"]:
        lines.append("\n## Warnings\n\n")
        lines.extend(f"- {item}\n" for item in report["warnings"])
    lines.append(f"\n---\n\nUp: [[{home}]]\n")
    return "".join(lines)


# --------------------------------------------------------------------------
# the five write verbs
# --------------------------------------------------------------------------

VERBS = ("create", "append-row", "patch-cell", "append-section", "append-entry")


def _op(verb: str, path: str, **fields: Any) -> dict[str, Any]:
    op = {"verb": verb, "path": path}
    op.update(fields)
    return op


def apply_op(root: Path, op: dict[str, Any]) -> None:
    """One verb, one file. Never overwrite, never delete, never rename, no body
    regex. Anything that cannot be anchored exactly raises."""
    verb = op["verb"]
    target = (root / op["path"]).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        raise RuntimeError(f"path escapes the vault: {op['path']}")

    if verb == "create":
        if target.exists():
            raise RuntimeError(f"create would overwrite an existing file: {op['path']}")
        target.parent.mkdir(parents=True, exist_ok=True)
        write_text(target, op["content"])
        return

    if not target.is_file():
        raise RuntimeError(f"{verb} target does not exist: {op['path']}")
    text, newline = read_note(target)
    trailing = text.endswith("\n")
    lines = text.split("\n")
    if trailing:
        lines = lines[:-1]

    if verb == "append-row":
        anchor = op.get("after_line")
        if anchor is None:
            lines.append(op["line"])
        else:
            hits = [index for index, line in enumerate(lines) if line == anchor]
            if len(hits) != 1:
                raise RuntimeError(
                    f"append-row anchor matched {len(hits)} lines in {op['path']}: {anchor!r}"
                )
            lines.insert(hits[0] + 1, op["line"])
    elif verb == "patch-cell":
        hits = [index for index, line in enumerate(lines) if line == op["old_line"]]
        if len(hits) > 1 and op.get("id"):
            # Identical status lines repeat across Evidence Map blocks; the one to
            # patch is the one inside this claim's own `### <id> — …` block.
            heads = [i for i, line in enumerate(lines) if line.startswith(f"### {op['id']} ")]
            if len(heads) == 1:
                ends = [i for i, line in enumerate(lines) if i > heads[0] and line.startswith("### ")]
                end = ends[0] if ends else len(lines)
                hits = [i for i in hits if heads[0] < i < end]
        if len(hits) != 1:
            raise RuntimeError(
                f"patch-cell old line matched {len(hits)} lines in {op['path']}: {op['old_line']!r}"
            )
        lines[hits[0]] = op["new_line"]
    elif verb == "append-section":
        anchor = op.get("insert_after")
        block = op["content"].split("\n")
        if block and block[-1] == "":
            block = block[:-1]
        if anchor is None:
            lines.extend(block)
        else:
            hits = [index for index, line in enumerate(lines) if line == anchor]
            if len(hits) != 1:
                raise RuntimeError(
                    f"append-section anchor matched {len(hits)} lines in {op['path']}: {anchor!r}"
                )
            lines[hits[0] + 1 : hits[0] + 1] = block
    elif verb == "append-entry":
        block = op["content"].split("\n")
        if block and block[-1] == "":
            block = block[:-1]
        while lines and not lines[-1].strip():
            lines.pop()
        lines.extend(block)
    else:
        raise RuntimeError(f"unknown verb {verb!r}")

    write_text(target, "\n".join(lines) + ("\n" if trailing else ""), newline=newline)


# --------------------------------------------------------------------------
# merge planner
# --------------------------------------------------------------------------


def _anchor_after(lines: list[str], start: int, end: int) -> str | None:
    """The last non-empty line of a block, if it is unique in the file. An
    ambiguous anchor is no anchor: the caller falls back or asks for a paste."""
    for index in range(end - 1, start - 1, -1):
        line = lines[index]
        if not line.strip():
            continue
        return line if sum(1 for other in lines if other == line) == 1 else None
    return None


def _footer_anchor(path: Path) -> str | None:
    """The last body line above a note's `---` / `Up:` footer.

    An append-section lands *after* this line, which puts new material inside the
    body instead of below the footer. `---` itself is useless as an anchor: the
    frontmatter delimiters make it ambiguous, and an ambiguous anchor is refused.
    """
    if not path.is_file():
        return None
    lines = path.read_text(encoding="utf-8", errors="replace").split("\n")
    footer = None
    for index in range(len(lines) - 1, 3, -1):
        if lines[index].strip() == "---" and any(
            line.strip().startswith("Up:") for line in lines[index:]
        ):
            footer = index
            break
    end = footer if footer is not None else len(lines)
    for index in range(end - 1, -1, -1):
        line = lines[index]
        if not line.strip():
            continue
        return line if sum(1 for other in lines if other == line) == 1 else None
    return None


def home_ops(path: Path, rel: str, run_id: str, manual: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A merge into a vault that has a Home: bump its `last_verified`, and list
    this run in `run_ids`. `run_id` stays the run that first published the vault;
    `run_ids` lists it first, then every later run. Line-exact verbs only."""
    lines = path.read_text(encoding="utf-8", errors="replace").split("\n")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if not lines or lines[0].strip() != "---" or end is None:
        manual.append({"path": rel, "line": f"run_ids: [{run_id}]", "why": "the Home has no frontmatter"})
        return []
    front = lines[1:end]

    def unique(line: str) -> bool:
        return sum(1 for other in lines if other == line) == 1

    def value(line: str) -> str:
        return line.split(":", 1)[1].strip().strip("'\"")

    ops: list[dict[str, Any]] = []
    stamp = next((line for line in front if line.startswith("last_verified:")), None)
    if stamp is not None and stamp.strip() != f"last_verified: {today()}" and unique(stamp):
        ops.append(_op("patch-cell", rel, old_line=stamp, new_line=f"last_verified: {today()}",
                       reason="this run re-checked the vault"))
    first = next((value(line) for line in front if line.startswith("run_id:")), "")
    at = next((i for i, line in enumerate(front) if line.startswith("run_ids:")), None)
    if at is not None:
        head = front[at]
        inline = head.split(":", 1)[1].strip()
        if inline.startswith("["):
            items = [item.strip().strip("'\"") for item in inline.strip("[]").split(",") if item.strip()]
            if run_id in items:
                return ops
            new_head = f"run_ids: [{', '.join(items + [run_id])}]"
            if unique(head):
                ops.append(_op("patch-cell", rel, old_line=head, new_line=new_head,
                               reason="list this run in the vault's run_ids"))
            else:
                manual.append({"path": rel, "line": new_head, "why": "run_ids line is not unique"})
            return ops
        listed: list[str] = []
        for line in front[at + 1:]:
            if not re.match(r"^\s+-\s", line):
                break
            listed.append(line)
        if run_id in [line.split("-", 1)[1].strip().strip("'\"") for line in listed]:
            return ops
        anchor = listed[-1] if listed else head
        if unique(anchor):
            ops.append(_op("append-row", rel, line=f"  - {run_id}", after_line=anchor,
                           reason="list this run in the vault's run_ids"))
        else:
            manual.append({"path": rel, "line": f"  - {run_id}", "why": "no unique run_ids line to anchor to"})
        return ops
    if first == run_id:
        return ops  # the run that first published the vault, again: nothing to list
    block = "run_ids:\n" + "".join(f"  - {item}\n" for item in [first, run_id] if item)
    anchor = next((line for line in front if line.startswith("run_id:")), None) or stamp or (
        front[-1] if front else None)
    if anchor and unique(anchor):
        ops.append(_op("append-section", rel, content=block, insert_after=anchor,
                       reason="list this run in the vault's run_ids"))
    else:
        manual.append({"path": rel, "line": block.strip(), "why": "no unique frontmatter line to anchor to"})
    return ops


def plan_merge(run: Run, vault: Path, brain: Path | None) -> dict[str, Any]:
    """Dry run, always first. Produces the whole write set as five verbs and the
    hard stops that must clear before any of it may be applied."""
    if not vault.is_dir():
        die(f"not a directory: {vault}")
    vault = vault.resolve()

    # A no-write area is refused before anything is even read, so that a target
    # like qa/ never gets as far as "this does not look like a vault".
    if vault.name in NO_WRITE_DIRS or any(part in NO_WRITE_DIRS for part in vault.parts[-2:]):
        die(
            f"{vault.name}/ is a no-write area (qa/, backlog/, logs/, design/). "
            "Nothing was planned and nothing was written."
        )

    if is_first_hand_area(vault):
        die(
            f"{vault.name}/ is a first-hand area (vault_kind: first-hand); the research pipeline "
            "never writes into it. Nothing was planned and nothing was written."
        )
    scan = scan_vault(vault)
    prefix = scan["prefix"]
    projection = project_run(run, prefix, existing=scan)
    names = projection["names"]

    stops: list[str] = []
    manual: list[dict[str, Any]] = []
    ops: list[dict[str, Any]] = []

    # --- hard stop: the target must be a writable, prefixed vault -------
    # (directly under the brain root, or inside one of its group folders)
    if brain is not None and vault.parent.resolve() != brain.resolve() and not is_brain_vault(brain, vault):
        stops.append(f"target {vault} is not a vault directly under the brain root {brain}")

    if not prefix:
        stops.append(
            f"target vault {vault.name} has no inferable prefix; its meta files are unprefixed. "
            "Rename by hand (see lint-brain) before merging into it."
        )
    else:
        for folder in ("90 Evidence", "99 Meta"):
            directory = vault / folder
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.md")):
                if not path.stem.startswith(prefix + " "):
                    stops.append(f"unprefixed meta file in the target vault: {folder}/{path.name}")

    # --- hard stop: an ID that already exists with different text -------
    for cid, claim in sorted(projection["claims"].items()):
        old = scan["ledger"].get(cid)
        if old and normalize_quote(old["text"]) != normalize_quote(claim["text"]):
            stops.append(
                f"{cid} already exists in the vault with different text; refusing to write. "
                f"vault: {old['text'][:80]!r} run: {claim['text'][:80]!r}"
            )
    for sid, source in sorted(projection["sources"].items()):
        old = scan["register"].get(sid)
        if old and source.get("url") and old["url"] and canonical_url_safe(old["url"]) != canonical_url_safe(source["url"]):
            stops.append(
                f"{sid} already exists in the vault pointing at a different URL; refusing to write. "
                f"vault: {old['url']} run: {source['url']}"
            )
    for gid, gap in sorted(projection["gaps"].items()):
        old = scan["gaps"].get(gid)
        if old and normalize_quote(old["question"]) != normalize_quote(gap["question"]):
            stops.append(f"{gid} already exists in the vault with a different question")

    # --- brain-wide basename collisions for every create ----------------
    brain_index: dict[str, list[Path]] = brain_basename_index(brain) if brain else {}
    collisions: list[dict[str, Any]] = []
    existing_basenames = {note["basename"] for note in scan["notes"]}

    # --- creates: new notes, new MOCs -----------------------------------
    for note in projection["notes"]:
        target = vault / note["path"]
        if target.exists() or note["basename"] in existing_basenames:
            continue
        clash = [p for p in brain_index.get(note["basename"], []) if p != target]
        if clash:
            collisions.append(
                {
                    "basename": note["basename"],
                    "wanted_at": note["path"],
                    "already_at": [str(p.relative_to(brain)) for p in clash],
                }
            )
            continue
        ops.append(_op("create", note["path"], id=note["id"], content=note["text"],
                       reason=f"new {note['note_type']} note"))

    for folder in projection["folders"]:
        rel = f"{folder['folder']}/{folder['moc']}.md"
        listed = sorted(
            (n for n in projection["notes"] if n["folder"] == folder["folder"]),
            key=lambda n: n["basename"],
        )
        if not (vault / rel).is_file():
            clash = [p for p in brain_index.get(folder["moc"], []) if p != vault / rel]
            if clash:
                collisions.append(
                    {"basename": folder["moc"], "wanted_at": rel,
                     "already_at": [str(p.relative_to(brain)) for p in clash]}
                )
                continue
            ops.append(_op("create", rel, content=render_moc(
                folder, names, [{"basename": n["basename"], "summary": n["summary"]} for n in listed]
            ), reason="new domain MOC"))
        else:
            text = (vault / rel).read_text(encoding="utf-8", errors="replace")
            existing_lines = text.split("\n")
            last_bullet = None
            in_notes = False
            for line in existing_lines:
                if line.strip().startswith("## "):
                    in_notes = line.strip().lower().startswith("## notes")
                    continue
                if in_notes and line.strip().startswith("- "):
                    last_bullet = line
            for note in listed:
                bullet = f"- [[{note['basename']}]] — {_md_text(note['summary'])}"
                if f"[[{note['basename']}]]" in text:
                    continue
                if last_bullet is None:
                    manual.append({"path": rel, "line": bullet,
                                   "why": "the MOC has no '## Notes' bullet to anchor to"})
                    continue
                ops.append(_op("append-row", rel, line=bullet, after_line=last_bullet,
                               id=note["basename"], reason="list the new note in its MOC"))
                last_bullet = bullet

    # --- ledger, register, gaps, contradictions -------------------------
    def table_ops(rel: str, prefix_id: str, values: dict[str, dict[str, str]],
                  columns: dict[str, list[str]], default_header: list[str],
                  label: str) -> str | None:
        """Plan the table's ops; return the line the table will end on."""
        path = vault / rel
        shape = read_id_table(path, prefix_id)
        if not shape["found"]:
            for key in sorted(values):
                manual.append({"path": rel, "line": _row_line(default_header, columns, values[key]),
                               "why": f"no {label} table found to anchor to"})
            return None
        header = shape["header"] or default_header
        anchor = shape["last_raw"]
        lower = {name.lower() for name in header}
        missing = [
            key for key, aliases in columns.items()
            if not any(alias in lower for alias in aliases)
        ]
        if missing:
            manual.append(
                {
                    "path": rel,
                    "line": "| " + " | ".join(header) + " |",
                    "why": (
                        f"the {label} table has no {', '.join(missing)} column(s); those values "
                        "cannot be recorded until the header is extended by hand — a header "
                        "rewrite is not one of the five verbs"
                    ),
                }
            )
        for key in sorted(values):
            row = shape["rows"].get(key)
            if row is None:
                line = _row_line(header, columns, values[key])
                ops.append(_op("append-row", rel, id=key, line=line, after_line=anchor,
                               reason=f"new {label} {key}"))
                anchor = line
                continue
            # Rendered against rendered: padding and column alignment in the
            # published table must never read as a content change.
            before = _patch_line(row["cells"], header, columns, {})
            patched = _patch_line(row["cells"], header, columns, values[key])
            if patched != before:
                ops.append(_op("patch-cell", rel, id=key, old_line=row["raw"], new_line=patched,
                               reason=f"{label} {key} changed"))
        return anchor

    ledger_rel = f"90 Evidence/{names['claim_ledger']}.md"
    table_end = table_ops(ledger_rel, "CL", projection["claim_values"],
                          CLAIM_COLS, CLAIM_HEADER, "claim")

    # --- claim index: every ledger claim needs its `### CL-###` heading -----
    # An older ledger without an index gets the whole section in one op, placed
    # directly under the table's final row (after this run's appended rows);
    # a ledger that has one gets the missing entries at the end of the index.
    ledger_path = vault / ledger_rel
    if table_end is not None and ledger_path.is_file():
        ledger_text = ledger_path.read_text(encoding="utf-8", errors="replace")
        indexed = set(claim_index_ids(ledger_text))
        map_exists = (vault / f"90 Evidence/{names['evidence_map']}.md").is_file()
        map_name = names["evidence_map"] if map_exists or projection["evidence_blocks"] else None
        texts = {cid: claim["text"] for cid, claim in scan["ledger"].items()}
        texts.update({cid: values["text"] for cid, values in projection["claim_values"].items()})
        missing = [cid for cid in sorted(texts) if cid not in indexed]
        if missing:
            has_section = any(line.strip() == CLAIM_INDEX_HEADING for line in ledger_text.splitlines())
            if has_section:
                content = "".join(claim_index_entry(cid, texts[cid], map_name) for cid in missing)
                anchor = claim_index_tail(ledger_text)
                why = f"{len(missing)} claim index entr{'y' if len(missing) == 1 else 'ies'}"
            else:
                content = render_claim_index(map_name, [(cid, texts[cid]) for cid in missing])
                anchor = table_end
                why = f"create the claim index ({len(missing)} entries) so #CL anchors resolve"
            if anchor is None:
                manual.append({"path": ledger_rel, "line": CLAIM_INDEX_HEADING,
                               "why": "no unique line to anchor the claim index to"})
            else:
                ops.append(_op("append-section", ledger_rel, content=content,
                               insert_after=anchor, reason=why))
    # A ledger published before the refutation rule was stated correctly gets
    # that one generated sentence corrected, line-exactly.
    if ledger_path.is_file():
        legacy = [line for line in ledger_path.read_text(encoding="utf-8", errors="replace").splitlines()
                  if LEGACY_REFUTATION_RULE in line]
        if len(legacy) == 1:
            ops.append(_op("patch-cell", ledger_rel, old_line=legacy[0],
                           new_line=legacy[0].replace(LEGACY_REFUTATION_RULE, LEDGER_REFUTATION_RULE),
                           reason="the ledger's statement of the status rule: refuting evidence with no "
                                  "support makes a claim refuted, not disputed"))

    register_rel = f"90 Evidence/{names['source_register']}.md"
    register_end = table_ops(register_rel, "S", projection["source_values"],
                             SOURCE_COLS, SOURCE_HEADER, "source")
    # Every confirmed policy exception is published with its reason: under the
    # source table, or at the end of an existing Policy section.
    register_path = vault / register_rel
    exceptions = policy_of(projection["manifest"])["exceptions"]
    register_text = register_path.read_text(encoding="utf-8", errors="replace") if register_path.is_file() else ""
    unlisted = [exc for exc in exceptions if policy_line(exc) not in register_text]
    if unlisted:
        lines = register_text.split("\n")
        if any(line.strip() == POLICY_HEADING for line in lines):
            start = next(i for i, line in enumerate(lines) if line.strip() == POLICY_HEADING)
            end = next((i for i in range(start + 1, len(lines))
                        if lines[i].strip() == "---" or re.match(r"^#{1,2}\s", lines[i])), len(lines))
            content = policy_section(projection["manifest"], heading=False, exceptions=unlisted)
            anchor = _anchor_after(lines, start, end)
        else:
            content = policy_section(projection["manifest"], exceptions=unlisted)
            anchor = register_end or _footer_anchor(register_path)
        if not register_path.is_file() or anchor is None:
            manual.append({"path": register_rel, "line": content.strip().split("\n")[-1],
                           "why": "no unique line to anchor the policy exceptions to"})
        else:
            ops.append(_op("append-section", register_rel, content=content, insert_after=anchor,
                           reason=f"{len(unlisted)} policy exception(s) of this run, with their reasons"))
    gaps_rel = f"90 Evidence/{names['gaps']}.md"
    gaps_end = table_ops(gaps_rel, "GAP", projection["gap_values"], GAP_COLS, GAP_HEADER, "gap")
    gaps_path = vault / gaps_rel
    gaps_text = gaps_path.read_text(encoding="utf-8", errors="replace") if gaps_path.is_file() else ""
    # Coverage and never-researched domains land under the gaps table (after this
    # run's appended rows), or, in a file that already has a Coverage section, as
    # one more block at the end of it. Additive only: an append-section.
    blocks: list[tuple[str, str | None, str]] = []
    under_table = ""
    if projection.get("coverage"):
        table = coverage_table(projection["coverage"])
        lines = gaps_text.split("\n")
        if any(line.strip() == COVERAGE_HEADING for line in lines):
            start = next(i for i, line in enumerate(lines) if line.strip() == COVERAGE_HEADING)
            end = next((i for i in range(start + 1, len(lines))
                        if lines[i].strip() == "---" or re.match(r"^#{1,2}\s", lines[i])), len(lines))
            blocks.append((f"\n### Run `{projection['manifest']['run_id']}`, {today()}\n\n{table}",
                           _anchor_after(lines, start, end), "coverage numbers of this run"))
        else:
            under_table += f"\n{COVERAGE_HEADING}\n\n{COVERAGE_INTRO}\n\n{table}"
    missing = [item for item in projection.get("not_researched") or []
               if not_researched_line(item) not in gaps_text]
    if missing:
        under_table += f"\n{NOT_RESEARCHED_HEADING}\n\n" + "".join(
            f"- {not_researched_line(item)}\n" for item in missing)
    if under_table:
        blocks.insert(0, (under_table, gaps_end or _footer_anchor(gaps_path),
                          "coverage and never-researched domains, beside the gaps"))
    for content, anchor, why in blocks:
        if not gaps_path.is_file() or anchor is None:
            manual.append({"path": gaps_rel, "line": content.strip().split("\n")[0],
                           "why": f"no unique line to anchor the {why} to"})
            continue
        ops.append(_op("append-section", gaps_rel, content=content, insert_after=anchor, reason=why))
    table_ops(f"90 Evidence/{names['contradictions']}.md", "CX", projection["cx_values"],
              CX_COLS, CX_HEADER, "contradiction")

    # --- evidence map: keyed on EV-###, never on excerpt ids ------------
    map_rel = f"90 Evidence/{names['evidence_map']}.md"
    map_path = vault / map_rel
    map_text = map_path.read_text(encoding="utf-8", errors="replace") if map_path.is_file() else ""
    map_lines = map_text.split("\n")
    published_edges = {
        match.group(1) for match in re.finditer(r"^- `(EV-\d+)`", map_text, re.MULTILINE)
    }
    heading_positions = [
        (index, line) for index, line in enumerate(map_lines) if MAP_HEADING.match(line.strip())
    ]
    map_footer = _footer_anchor(map_path)
    for cid in sorted(projection["evidence_blocks"]):
        block = projection["evidence_blocks"][cid]
        existing = scan["evidence_map"].get(cid)
        if existing is None:
            content = f"\n{block['heading']}\n\n{block['status_line']}\n\n" + "".join(
                bullet["text"] + "\n" for bullet in block["bullets"]
            )
            ops.append(_op("append-section", map_rel, id=cid, content=content,
                           insert_after=map_footer, reason=f"evidence block for {cid}"))
            continue
        fresh = [b for b in block["bullets"] if b["id"] not in published_edges]
        if fresh:
            # land the new edges at the end of this claim's own block
            anchor = map_footer
            for position, (index, line) in enumerate(heading_positions):
                if not line.strip().startswith(f"### {cid} "):
                    continue
                end = (
                    heading_positions[position + 1][0]
                    if position + 1 < len(heading_positions)
                    else len(map_lines)
                )
                anchor = _anchor_after(map_lines, index, end) or map_footer
                break
            content = "".join(bullet["text"] + "\n" for bullet in fresh)
            ops.append(_op("append-section", map_rel, id=cid, content=content,
                           insert_after=anchor, reason=f"{len(fresh)} new edge(s) on {cid}"))
        old_status = existing.get("status")
        if old_status and old_status != projection["claims"][cid]["status"]:
            old_line = None
            capture = False
            for line in map_lines:
                if MAP_HEADING.match(line.strip()):
                    capture = line.strip().startswith(f"### {cid} ")
                    continue
                if capture and MAP_STATUS.match(line.strip()):
                    old_line = line
                    break
            if old_line is not None:
                ops.append(_op("patch-cell", map_rel, id=cid, old_line=old_line,
                               new_line=block["status_line"],
                               reason=f"{cid} status {old_status} → {projection['claims'][cid]['status']}"))

    # --- published notes: status changes, and re-authored ones ----------
    # A note that already exists is never rewritten. It gets an Update section,
    # its claims list is extended line-exactly, and its last_verified is bumped.
    previous_status = {cid: claim["status"] for cid, claim in scan["ledger"].items()}
    needs_rewrite: list[dict[str, str]] = []
    updated_notes: list[str] = []
    reauthored = {note["basename"]: note for note in projection["notes"]}
    for note in scan["notes"]:
        changed = [
            cid for cid in note["claims"]
            if cid in projection["claims"] and previous_status.get(cid)
            and previous_status[cid] != projection["claims"][cid]["status"]
        ]
        candidate = reauthored.get(note["basename"])
        new_claims = [
            cid for cid in (candidate["claim_ids"] if candidate else [])
            if cid not in note["claims"] and cid in projection["claims"]
        ]
        if not changed and not new_claims:
            continue
        rel = note["path"].relative_to(vault).as_posix()
        lines = note["path"].read_text(encoding="utf-8", errors="replace").split("\n")
        update = [f"\n## Update {today()}\n"]
        for cid in changed:
            was, is_now = previous_status[cid], projection["claims"][cid]["status"]
            update.append(
                f"\n- [[{names['claim_ledger']}#{cid}]] moved **{was} → {is_now}**: "
                f"{_md_text(projection['claims'][cid]['status_reason'])}\n"
            )
            if was == "supported" and is_now in {"refuted", "disputed"}:
                needs_rewrite.append({"note": note["basename"], "claim": cid, "from": was, "to": is_now})
                update.append(
                    "  - **NEEDS REWRITE** — the prose above still reads as if this claim held. "
                    "It has not been edited; a human decides what the note should now say.\n"
                )
        for cid in new_claims:
            update.append(
                f"\n- [[{names['claim_ledger']}#{cid}]] added: {_md_text(projection['claims'][cid]['text'])}\n"
            )
        ops.append(_op("append-section", rel, content="".join(update),
                       insert_after=_footer_anchor(note["path"]),
                       reason=(
                           "claims cited by this note changed status" if changed
                           else "this run added claims to an already published note"
                       )))
        updated_notes.append(note["basename"])

        # frontmatter: extend claims: line-exactly, bump last_verified
        claim_lines = [line for line in lines if re.match(r"^  - CL-\d+$", line)]
        anchor = claim_lines[-1] if claim_lines else None
        for cid in new_claims:
            line = f"  - {cid}"
            if anchor is None:
                manual.append({"path": rel, "line": line, "why": "no claims: list to extend"})
                continue
            ops.append(_op("append-row", rel, id=cid, line=line, after_line=anchor,
                           reason="extend the note's claims list"))
            anchor = line
        for line in lines:
            if line.startswith("last_verified: ") and line.strip() != f"last_verified: {today()}":
                ops.append(_op("patch-cell", rel, old_line=line,
                               new_line=f"last_verified: {today()}",
                               reason="the note's claims were re-checked by this run"))
                break

    # --- change log -----------------------------------------------------
    added_claims = [cid for cid in projection["claims"] if cid not in scan["ledger"]]
    log_rel = f"99 Meta/{names['change_log']}.md"
    if (vault / log_rel).is_file():
        ops.append(_op("append-entry", log_rel, content=change_log_entry(
            run, projection, previous_status, needs_rewrite=needs_rewrite, added=added_claims
        ), reason="one entry per publish"))
    else:
        manual.append({"path": log_rel, "line": "(change log missing)",
                       "why": "the vault has no Change Log to append to"})

    # --- Home: last_verified, and every run that wrote this vault --------
    home_rel = f"{names['home']}.md"
    if (vault / home_rel).is_file():
        ops.extend(home_ops(vault / home_rel, home_rel, projection["manifest"]["run_id"], manual))

    # --- wanted notes ---------------------------------------------------
    wanted_rel = f"90 Evidence/{names['wanted_notes']}.md"
    known = {p.stem for p in vault.rglob("*.md")} | {
        Path(op["path"]).stem for op in ops if op["verb"] == "create"
    }
    fresh_wanted: dict[str, list[str]] = {}
    for note in projection["notes"]:
        for match in WIKILINK.finditer(note["text"]):
            target = match.group(1).strip().split("/")[-1]
            if target and target not in known:
                fresh_wanted.setdefault(note["basename"], []).append(target)
    if fresh_wanted:
        if (vault / wanted_rel).is_file():
            content = [f"\n## Added {today()}\n"]
            for referrer in sorted(fresh_wanted):
                content.append(f"\n### From [[{referrer}]]\n\n")
                content.extend(f"- [[{target}]]\n" for target in sorted(set(fresh_wanted[referrer])))
            ops.append(_op("append-section", wanted_rel, content="".join(content),
                           insert_after=_footer_anchor(vault / wanted_rel),
                           reason="new broken links from this run"))
        else:
            ops.append(_op("create", wanted_rel, content=render_wanted_notes(
                names["wanted_notes"], names, fresh_wanted
            ), reason="the vault has no Wanted Notes yet"))

    if collisions:
        stops.extend(
            f"brain-wide basename collision: {item['basename']!r} already exists at "
            + ", ".join(item["already_at"])
            for item in collisions
        )

    summary = {verb: sum(1 for op in ops if op["verb"] == verb) for verb in VERBS}
    plan = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now(),
        "run_id": projection["manifest"]["run_id"],
        "run_dir": str(run.root),
        "vault": str(vault),
        "brain": str(brain) if brain else None,
        "prefix": prefix,
        "summary": summary,
        "operations": ops,
        "collisions": collisions,
        "prefix_violations": [stop for stop in stops if "unprefixed" in stop],
        "brain_duplicates": [item["basename"] for item in collisions],
        "hard_stops": stops,
        "manual": manual,
        # What the vault already fails on. The apply gate is regression against
        # this number, not zero — older vaults carry errors this run did not make.
        "baseline_errors": len(validate_vault(vault, write_report=False)["errors"]),
        "needs_rewrite": needs_rewrite,
        "updated_notes": updated_notes,
        "new_claims": sorted(added_claims),
        "open_gaps": [
            {"id": gid, "impact": gap["impact"], "question": gap["question"]}
            for gid, gap in sorted(projection["gaps"].items())
            if gap.get("status") == "open"
        ],
        "fingerprints": _fingerprints(vault, ops),
    }
    return plan


def canonical_url_safe(url: str) -> str:
    """The canonical form, or the URL as given (without credentials) when it has none."""
    try:
        return canonical_url(url)
    except ValueError:
        return _redact_url(url.strip())


def _fingerprints(vault: Path, ops: list[dict[str, Any]]) -> dict[str, str]:
    """A plan is only replayable while the files it read are unchanged."""
    out: dict[str, str] = {}
    for op in ops:
        path = vault / op["path"]
        if op["verb"] == "create":
            out[op["path"]] = "absent"
        elif path.is_file():
            out[op["path"]] = sha(path.read_text(encoding="utf-8", errors="replace"), 32)
    return out


def print_merge_plan(plan: dict[str, Any]) -> None:
    """The reviewable table. It goes to stderr so stdout stays machine-readable."""
    def print(*args: Any) -> None:  # noqa: A001 - deliberate local shadow
        sys.stderr.write(" ".join(str(item) for item in args) + "\n")

    print(f"merge plan for {plan['vault']} (prefix {plan['prefix'] or '—'})")
    print("  " + "  ".join(f"{verb}={plan['summary'][verb]}" for verb in VERBS))
    print(f"  collisions={len(plan['collisions'])}  prefix violations="
          f"{len(plan['prefix_violations'])}  brain duplicates={len(plan['brain_duplicates'])}"
          f"  manual={len(plan['manual'])}")
    for op in plan["operations"]:
        marker = op.get("id") or ""
        print(f"    {op['verb']:<15} {op['path']:<52} {marker:<10} {op.get('reason', '')}")
    for item in plan["manual"]:
        print(f"    MANUAL PASTE   {item['path']}: {item['line']}  ({item['why']})")
    for item in plan["needs_rewrite"]:
        print(f"    NEEDS REWRITE  {item['note']}: {item['claim']} {item['from']} → {item['to']}")
    for stop_reason in plan["hard_stops"]:
        print(f"    HARD STOP      {stop_reason}")


# --------------------------------------------------------------------------
# atomic apply: staging → staged validate → os.replace, clean-and-abort
# --------------------------------------------------------------------------

STAGING_DIR = ".research-staging"


def _copy_vault(vault: Path, staging: Path) -> None:
    shutil.copytree(
        vault,
        staging,
        ignore=shutil.ignore_patterns(STAGING_DIR, ".git", ".obsidian"),
    )


def apply_merge(plan: dict[str, Any], vault: Path) -> dict[str, Any]:
    """Replay a reviewed merge plan. Nothing is written until the whole set has
    been applied to a copy and that copy has validated clean."""
    if plan["hard_stops"]:
        die("refusing to apply a plan with hard stops:\n  " + "\n  ".join(plan["hard_stops"]))
    ops = plan["operations"]
    if not ops:
        return {"applied": 0, "message": "nothing to apply"}

    for rel, fingerprint in (plan.get("fingerprints") or {}).items():
        path = vault / rel
        current = sha(path.read_text(encoding="utf-8", errors="replace"), 32) if path.is_file() else "absent"
        if current != fingerprint:
            die(
                f"the vault changed since this plan was made ({rel}). "
                "Re-run merge-plan and review it again."
            )

    # An older vault can carry hundreds of pre-existing errors (broken links that
    # predate Wanted Notes). The gate is regression, not perfection: an error the
    # vault already had is not this merge's fault, and a new one blocks it.
    brain_root = _brain_of(vault)
    baseline = set(validate_vault(vault, write_report=False, brain=brain_root)["errors"])

    staging = vault / STAGING_DIR
    if staging.exists():
        shutil.rmtree(staging)
    _copy_vault(vault, staging)
    try:
        # A plan anchors every op on the vault as it was *before* the merge. When a
        # patch-cell rewrites a line that a later append-row/append-section uses as
        # its anchor, follow the rewrite instead of failing on the stale text.
        rewritten: dict[str, dict[str, str]] = {}
        for op in ops:
            moved = rewritten.get(op["path"], {})
            for key in ("after_line", "insert_after"):
                if op.get(key) in moved:
                    op = {**op, key: moved[op[key]]}
            apply_op(staging, op)
            if op["verb"] == "patch-cell":
                moved = rewritten.setdefault(op["path"], {})
                for old, new in list(moved.items()):
                    if new == op["old_line"]:
                        moved[old] = op["new_line"]
                moved[op["old_line"]] = op["new_line"]
        report = validate_vault(staging, write_report=False, brain=brain_root)
        regressions = [item for item in report["errors"] if item not in baseline]
        if regressions:
            raise RuntimeError(
                f"staged validation found {len(regressions)} new error(s):\n  "
                + "\n  ".join(regressions[:20])
            )
    except Exception as exc:  # noqa: BLE001 - abort is the whole point
        shutil.rmtree(staging, ignore_errors=True)
        die(f"merge aborted, nothing written: {exc}")

    touched = sorted({op["path"] for op in ops})
    backup = staging / ".backup"
    replaced: list[str] = []
    created: list[str] = []
    try:
        for rel in touched:
            live = vault / rel
            staged = staging / rel
            if live.is_file():
                keep = backup / rel
                keep.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(live, keep)
                os.replace(staged, live)
                replaced.append(rel)
            else:
                live.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staged, live)
                created.append(rel)
    except Exception as exc:  # noqa: BLE001
        for rel in replaced:
            shutil.copyfile(backup / rel, vault / rel)
        for rel in created:
            (vault / rel).unlink(missing_ok=True)
        shutil.rmtree(staging, ignore_errors=True)
        die(f"apply failed on {rel}; rolled back, nothing written: {exc}")

    shutil.rmtree(staging, ignore_errors=True)
    return {
        "applied": len(ops),
        "files": touched,
        "created": created,
        "patched": replaced,
        "by_verb": plan["summary"],
        "baseline_errors": len(baseline),
    }


# --------------------------------------------------------------------------
# validate v2
# --------------------------------------------------------------------------

WIKILINK = re.compile(r"\[\[([^\]|#\\]+)(?:#[^\]]*|\\?\|[^\]]*)?\]\]")

BASE_FRONT_KEYS = ("type", "status", "evidence_level", "published", "last_verified", "review_due")
TYPE_FRONT_KEYS = {
    "dataset": ("fidelity", "source_urls", "as_of"),
    "concept": ("claims",),
    "profile": ("claims",),
    "playbook": ("claims",),
    "guide": ("claims",),
}
GENERATED_ARTIFACTS = ("Wanted Notes", "Validation Report")


def _iter_notes(vault: Path) -> list[Path]:
    return sorted(
        path
        for path in vault.rglob("*.md")
        if not any(part.startswith(".") for part in path.relative_to(vault).parts)
    )


def validate_vault(vault: Path, write_report: bool = True, brain: Path | None = None,
                   _idx: "BrainIndex | None" = None) -> dict[str, Any]:
    """The research-vault gate. Links resolve brain-wide (basename = identity
    across the brain) when the vault sits under a brain root or `brain` is given.
    Notes of a first-hand type (case-study, practice-guide, field-note) are
    checked by their own rules, not by the research-note rules."""
    if not vault.is_dir():
        die(f"not a directory: {vault}")
    files = _iter_notes(vault)
    if not files:
        die(f"no markdown notes under {vault}")
    if brain is None:
        brain = _brain_of(vault)
    idx = _idx if _idx is not None else BrainIndex(brain if brain else vault, extra=files)

    errors: list[str] = []
    warnings: list[str] = []
    prefix = safe_prefix(vault)

    by_basename: dict[str, list[Path]] = {}
    frontmatter: dict[Path, dict[str, str]] = {}
    links: dict[Path, list[str]] = {}
    bodies: dict[Path, str] = {}

    for path in files:
        by_basename.setdefault(path.stem, []).append(path)
        text = path.read_text(encoding="utf-8", errors="replace")
        bodies[path] = text
        frontmatter[path] = _parse_frontmatter(text)
        links[path] = [match.group(1).strip() for match in WIKILINK.finditer(text)]
    first_hand = {path for path, meta in frontmatter.items() if meta.get("type", "") in FIRST_HAND_TYPES}

    for stem, paths in sorted(by_basename.items()):
        if len(paths) > 1:
            errors.append(
                f"duplicate basename {stem!r}: " + ", ".join(p.relative_to(vault).as_posix() for p in paths)
            )

    # -- Home ---------------------------------------------------------------
    homes = sorted(vault.glob("00 *Home.md"))
    if not homes:
        errors.append("no '00 * Home.md' in the vault root")
    elif len(homes) > 1:
        errors.append("more than one '00 * Home.md': " + ", ".join(p.name for p in homes))

    # -- Wanted Notes coupling ---------------------------------------------
    wanted_files = sorted((vault / "90 Evidence").glob("* Wanted Notes.md")) if (vault / "90 Evidence").is_dir() else []
    wanted_text = wanted_files[0].read_text(encoding="utf-8", errors="replace") if wanted_files else ""
    wanted_listed = {match.group(1).strip() for match in WIKILINK.finditer(wanted_text)}

    known = set(by_basename)

    def resolves(stem: str) -> bool:
        return stem in known or (brain is not None and idx.resolve(stem) is not None)

    for path, targets in sorted(links.items()):
        if (wanted_files and path == wanted_files[0]) or path in first_hand:
            continue
        for target in targets:
            stem = target.split("/")[-1]
            if resolves(stem):
                continue
            warnings.append(f"{path.relative_to(vault).as_posix()}: broken wikilink [[{stem}]]")
            if stem not in wanted_listed:
                errors.append(
                    f"{path.relative_to(vault).as_posix()}: broken wikilink [[{stem}]] is not listed in "
                    "Wanted Notes — a broken link is a work item and must be queued there"
                )

    # -- heading anchors: [[Note#Heading]] must land on a heading -----------
    # Obsidian silently opens the top of the note when an anchor misses, so a
    # dead `#CL-###` looks fine to a reader and to the broken-link check above.
    anchor_cache: dict[Path, tuple[set[str], set[str]]] = {}
    for path in files:
        if path in first_hand:
            continue  # first-hand pages: every dead anchor is an error, below
        unresolved: dict[str, list[str]] = {}
        for target, anchor in anchor_links(bodies[path]):
            stem = target or path.stem
            local = by_basename.get(stem)
            candidate = local[0] if local else (idx.resolve(stem) if brain is not None else None)
            if candidate is None:
                continue  # a broken link, already reported above
            if candidate not in anchor_cache:
                anchor_cache[candidate] = note_anchors(
                    bodies[candidate] if candidate in bodies else idx.text(candidate)
                )
            if not anchor_resolves(anchor, anchor_cache[candidate]):
                unresolved.setdefault(stem, []).append(anchor)
        rel = path.relative_to(vault).as_posix()
        for stem, anchors in sorted(unresolved.items()):
            for anchor in sorted(set(anchors)):
                heading = anchor.split("#")[-1].strip()
                if re.fullmatch(r"CL-\d+", heading):
                    errors.append(
                        f"{rel}: claim anchor [[{stem}#{anchor}]] — no heading '{heading}' in {stem} "
                        f"(the ledger needs a '{CLAIM_INDEX_HEADING}' with one '### CL-###' per claim)"
                    )
                else:
                    warnings.append(f"{rel}: link [[{stem}#{anchor}]] — no heading '{heading}' in {stem}")

    # -- frontmatter, citation gate, link density --------------------------
    for path, meta in sorted(frontmatter.items()):
        rel = path.relative_to(vault).as_posix()
        if not meta:
            errors.append(f"{rel}: missing or unparseable frontmatter")
            continue
        note_type = meta.get("type", "")
        if path in first_hand:
            # a first-hand page inside a research vault: its own rules
            relp = rel
            check_links(relp, path, bodies[path], idx, errors, warnings)
            if note_type == "field-note":
                e, w = check_field_note(relp, path, field_frontmatter(bodies[path]) or {}, idx, vault,
                                        idx.base, brain)
            else:
                e, w = check_first_hand_page(relp, path, idx)
            errors.extend(e)
            warnings.extend(w)
            warnings.extend(english_warning(relp, bodies[path]))
            continue
        required = list(BASE_FRONT_KEYS) + list(TYPE_FRONT_KEYS.get(note_type, ()))
        for key in required:
            if key not in meta:
                errors.append(f"{rel}: frontmatter missing {key!r} (type: {note_type or 'unset'})")
        if note_type == "dataset" and "claims" in meta:
            warnings.append(f"{rel}: a dataset note should not carry a 'claims' key")
        if note_type in CONTENT_TYPES:
            body = bodies[path][len(_frontmatter_block(bodies[path])) :]
            if not re.search(r"CL-\d{3,}", body) and not re.search(r"https?://", body):
                errors.append(
                    f"{rel}: factual note with no citation — it needs at least one CL-### link "
                    "or one http(s) source URL"
                )
        if not links[path]:
            warnings.append(f"{rel}: no outgoing wikilinks")

    # -- first-hand knowledge is never evidence: no ledger/register row cites it
    evidence_tables = [p for p in files if len(p.relative_to(vault).parts) > 1
                       and p.relative_to(vault).parts[0] == "90 Evidence"
                       and (p.stem.endswith(" Claim Ledger") or p.stem.endswith(" Source Register"))]
    for table_path in evidence_tables:
        for number, line in enumerate(bodies[table_path].replace("\r\n", "\n").split("\n"), start=1):
            if not line.lstrip().startswith("|"):
                continue
            for target, _anchor in _prose_links(line):
                found = by_basename.get(target, [None])[0] or (idx.resolve(target) if target else None)
                if found is not None and idx.note_type(found) in FIRST_HAND_TYPES:
                    errors.append(
                        f"{table_path.relative_to(vault).as_posix()}:{number}: row cites first-hand page [[{target}]] — "
                        "first-hand knowledge never enters a claim ledger or source register"
                    )

    # -- web-derived text: a forged table row, code that runs on open ---------
    errors.extend(vault_table_problems(vault))
    for path in files:
        if path in first_hand:
            continue
        hit = EXEC_CONTENT.search(bodies[path])
        if hit:
            # named, never quoted: the error text lands in the Validation Report note
            errors.append(
                f"{path.relative_to(vault).as_posix()}: executable or remote-embedded content "
                f"({_exec_kind(hit.group(0))}) is not allowed (core Obsidian only)"
            )

    pages = [p for p in first_hand if frontmatter[p].get("type", "") in FIRST_HAND_PAGE_TYPES]
    if pages:
        errors.extend(lesson_reachability(sorted(pages), idx, lambda p: p.relative_to(vault).as_posix()))

    # -- callouts: "Tested first-hand" must land on a lesson --------
    callout_errors, callout_warnings = check_callouts(
        files, idx, lambda p: p.relative_to(vault).as_posix()
    )
    errors.extend(callout_errors)
    warnings.extend(callout_warnings)

    # -- prefix gate --------------------------------------------------------
    if prefix:
        for path in files:
            rel = path.relative_to(vault)
            top = rel.parts[0] if len(rel.parts) > 1 else ""
            if (top.startswith("90 ") or top.startswith("99 ")) and not path.stem.startswith(prefix + " "):
                errors.append(
                    f"{rel.as_posix()}: meta file without the vault prefix {prefix!r} — generic meta names "
                    "create ghost links once vaults share a brain"
                )
    else:
        warnings.append("vault prefix could not be inferred; prefix gate skipped")

    # -- reachability and depth --------------------------------------------
    depths: dict[str, int] = {}
    if homes and len(homes) == 1:
        depths = _bfs_depth(homes[0].stem, by_basename, links)
        for stem in sorted(known - set(depths)):
            if any(stem.endswith(artifact) for artifact in GENERATED_ARTIFACTS):
                warnings.append(f"generated artifact not linked from Home: {stem!r}")
                continue
            errors.append(f"unreachable from Home: {stem!r}")
        for stem, depth in sorted(depths.items()):
            paths = by_basename.get(stem, [])
            note_type = frontmatter.get(paths[0], {}).get("type", "") if paths else ""
            if note_type in CONTENT_TYPES and depth > 2:
                errors.append(f"more than 2 hops from Home: {stem!r} ({depth})")
            elif note_type not in CONTENT_TYPES and depth > 3:
                warnings.append(f"more than 3 hops from Home: {stem!r} ({depth})")

    # -- claim status sanity ------------------------------------------------
    ledgers = sorted((vault / "90 Evidence").glob("* Claim Ledger.md")) if (vault / "90 Evidence").is_dir() else []
    if not ledgers:
        ledgers = sorted(vault.rglob("*Claim Ledger.md"))
    weak: set[str] = set()
    if ledgers:
        for cid, claim in parse_claim_ledger(ledgers[0]).items():
            if claim["status"] in {"unsupported", "refuted"}:
                weak.add(cid)
    for path, meta in sorted(frontmatter.items()):
        cited = _frontmatter_list(path, "claims")
        bad = [cid for cid in cited if cid in weak]
        if bad:
            warnings.append(
                f"{path.relative_to(vault).as_posix()}: cites unsupported/refuted claims {', '.join(bad)} "
                "— check for a NEEDS REWRITE entry in the Change Log"
            )

    report = {
        "vault": str(vault),
        "checked_at": now(),
        "prefix": prefix or None,
        "notes": len(files),
        "first_hand_pages": len(first_hand),
        "wikilinks": sum(len(v) for v in links.values()),
        "duplicate_basenames": sum(1 for paths in by_basename.values() if len(paths) > 1),
        "broken_links": sum(1 for item in warnings if "broken wikilink" in item),
        "max_depth": max(depths.values()) if depths else None,
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
    }
    if write_report:
        # The file ships inside the vault: it names the vault by its brain-relative
        # path (or folder name), never by an absolute local path.
        write_json(vault / "99 Meta" / "validation.json",
                   {**report, "vault": brain_rel(brain.resolve(), vault.resolve()) if brain else vault.name})
        title = meta_basename(prefix, "Validation Report") if prefix else "Validation Report"
        home = homes[0].stem if homes else "00 Home"
        write_text(vault / "99 Meta" / f"{title}.md", _validation_note_text(title, home, report))
    return report


def validate_brain(brain: Path) -> dict[str, Any]:
    """Every vault by its kind: research vaults (with `90 Evidence`) through
    validate_vault, first-hand areas through validate_first_hand_area; then the
    callout check over every file no vault gate covered (root, inbox, …). Vaults
    inside group folders are found by brain_vault_dirs and reported by their
    brain-relative path (`plants/pothos-cuttings`)."""
    lint = lint_brain(brain)
    idx = BrainIndex(brain)
    per_vault: list[dict[str, Any]] = []
    covered: list[Path] = []
    for directory in brain_vault_dirs(brain):
        name = brain_rel(brain, directory)
        if name in FLAT_DIRS:
            continue
        if is_first_hand_area(directory):
            report = validate_first_hand_area(directory, brain)
            profile = "first-hand"
        elif (directory / "90 Evidence").is_dir():
            report = validate_vault(directory, write_report=False, brain=brain, _idx=idx)
            profile = "research"
        else:
            continue
        covered.append(directory.resolve())
        per_vault.append(
            {
                "vault": name,
                "profile": profile,
                "notes": report["notes"],
                "valid": report["valid"],
                "errors": report["errors"],
                "warnings": len(report["warnings"]),
            }
        )
    rest = [p for p in _iter_notes(brain) if not any(c in p.resolve().parents for c in covered)]
    callout_errors, callout_warnings = check_callouts(
        rest, idx, lambda p: p.resolve().relative_to(brain.resolve()).as_posix()
    )
    structure = lint.get("structure_errors", [])
    report: dict[str, Any] = {
        "brain": str(brain),
        "checked_at": now(),
        "duplicate_basenames": lint["duplicate_basenames"],
        "exempt_index_basenames": lint.get("exempt_index_basenames", []),
        "prefix_violations": lint["prefix_violations"],
        "vaults": per_vault,
        "callouts_outside_vaults": {"errors": callout_errors, "warnings": callout_warnings},
        "valid": not lint["duplicate_basenames"] and all(item["valid"] for item in per_vault)
        and not callout_errors and not structure,
    }
    if structure:  # a vault holding vaults, or a Router README in a group: never silently valid
        report["structure_errors"] = structure
    return report


# --------------------------------------------------------------------------
# field notes — validate --profile field
#
# First-hand know-how (own measurements, session lessons, negative results,
# tool gotchas) enters the brain through field-notes/, never through a claim
# ledger: it has no http(s) source to cite. Its gate is its own. Exempt from the
# vault gates (claims/ledger, Evidence/Meta folders, 2-hop depth); instead every
# note carries conditions and an invalidation trigger, sits in a MOC, and may
# link only to notes that exist. Freshness is driven by `invalidated_by`, not by
# a calendar tier, so `review_due` is not required.
# --------------------------------------------------------------------------

FIELD_HOME = "00 Field Notes Home"
FIELD_PREFIX = "Field Notes"
FIELD_MOC_PREFIX = "Field Notes - "
FIELD_REQUIRED = (
    "type", "kind", "status", "scope", "evidence_number", "evidence_generalization",
    "conditions", "sample", "artifacts", "measured_at", "invalidated_by", "supersedes",
    "superseded_by", "origin", "published", "last_verified", "tags",
)
FIELD_ENUMS = {
    "kind": {"finding", "method", "procedure", "gotcha", "negative-result", "benchmark"},
    "status": {"active", "superseded", "needs_review"},
    "scope": {"general", "domain", "project"},
    "evidence_number": {"measured", "verified-in-code", "observed", "reasoned", "external", "n/a"},
    "evidence_generalization": {"measured", "verified-in-code", "observed", "reasoned", "external", "n/a"},
}
# Rule: the condition and the invalidation trigger are mandatory on every note.
FIELD_MUST_FILL = ("conditions", "artifacts", "invalidated_by")
FIELD_HEADINGS = ("## Claim", "## When it does not hold", "## Evidence", "## How to reuse", "## Related")
FIELD_MAX_WORDS = 600
# ASCII only: English filenames, nothing Obsidian or Windows treats specially.
FIELD_NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ,.'()&+_-]*$")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$")
ARTIFACT_FORMS = (
    re.compile(r"^https?://\S+$"),                                    # URL
    re.compile(r"^\S.*@[0-9a-f]{7,40}$"),                             # repo path + commit
    re.compile(r"^\S.*\.(log|json|jsonl|ndjson|csv|tsv|txt|md|out)(:\d+|#L\d+)?$", re.I),  # log/JSON path
    re.compile(r"^session:[\w.-]+(:\d+|#L\d+)$"),                     # session or log id + line
)
TRIGGER_FORMS = {
    "decision:": re.compile(r"^decision:[A-Za-z]+-?\d+$"),
    "version:": re.compile(r"^version:[^@\s]+@\S+$"),
    "file:": re.compile(r"^file:\S.*@[0-9a-f]{7,40}$"),
}
SMALL_SAMPLE = re.compile(
    r"\bn\s*=\s*[01]\b|\b(one|single|1)\s+(chain|run|seed|trial|sample)s?\b", re.I
)
CODE_SPAN = re.compile(r"`[^`\n]*`")


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def field_frontmatter(text: str) -> dict[str, Any] | None:
    """The YAML subset field notes use: scalars, `[a, b]` and block lists."""
    block = _frontmatter_block(text.replace("\r\n", "\n"))
    if not block:
        return None
    meta: dict[str, Any] = {}
    key: str | None = None
    for line in block.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = line.strip()
        if key is not None and (line[0] in " \t" or item.startswith("- ")):
            if item.startswith("- ") or item == "-":
                current = meta.get(key)
                if not isinstance(current, list):
                    current = [] if current in ("", None) else [current]
                value = _unquote(item[1:].strip())
                if value:
                    current.append(value)
                meta[key] = current
            elif isinstance(meta.get(key), str):
                meta[key] = (meta[key] + " " + item).strip()
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            key, value = key.strip(), value.strip()
            if value in ("|", ">", "|-", ">-"):
                value = ""
            if value.startswith("[") and value.endswith("]") and not value.startswith("[["):
                inner = value[1:-1].strip()
                meta[key] = [_unquote(part) for part in inner.split(",") if part.strip()] if inner else []
            else:
                meta[key] = _unquote(value)
    return meta


def _field_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, list):
        return not [item for item in value if not _field_empty(item)]
    return str(value).strip().lower() in ("", "null", "~", "none", "[]", "n/a", "-")


def _field_items(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if not _field_empty(item)]
    return [str(value).strip()] if not _field_empty(value) else []


def _link_target(value: str) -> str:
    value = value.strip()
    if value.startswith("[[") and value.endswith("]]"):
        value = value[2:-2]
    return value.split("|")[0].split("#")[0].strip().split("/")[-1]


def _prose_lines(text: str) -> list[str]:
    """Lines outside fenced code, with inline code spans blanked."""
    out: list[str] = []
    fenced = False
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            out.append(CODE_SPAN.sub("", line))
    return out


def _section(lines: list[str], heading: str) -> list[str]:
    for index, line in enumerate(lines):
        if line.strip() == heading:
            end = next(
                (i for i in range(index + 1, len(lines)) if re.match(r"^#{1,2}\s", lines[i])),
                len(lines),
            )
            return lines[index + 1 : end]
    return []


def validate_field_notes(root: Path, brain: Path | None = None) -> dict[str, Any]:
    """Back-compatible name: validate one first-hand area (see validate_first_hand_area)."""
    return validate_first_hand_area(root, brain)


# --------------------------------------------------------------------------
# first-hand knowledge — validated by note TYPE, wherever the note lives
#
# Page types: `case-study` (one investigation; lessons are `### Lesson: …`
# sections) and `practice-guide` (a procedure; `### Lesson: …` under
# `## Procedure`, `### Trap: …` under `## Traps`), plus the older stand-alone
# `field-note`. Each `Lesson:`/`Trap:` section ends in a metadata table.
#
# A first-hand AREA is `field-notes/` or any vault folder of the brain (top-level,
# or inside a group folder such as `notes/`) whose Home (`00 <Name> Home.md`)
# declares `vault_kind: first-hand` in its frontmatter.
# Areas skip the research-vault gates (claims, ledger, CL/URL citation, 90
# Evidence) and get the first-hand gate instead. A research vault may also hold
# first-hand pages: each note is checked by the rules of its own type.
# --------------------------------------------------------------------------

FIRST_HAND_PAGE_TYPES = {"case-study", "practice-guide"}
FIRST_HAND_TYPES = FIRST_HAND_PAGE_TYPES | {"field-note"}
VAULT_KIND_KEY = "vault_kind"
FIRST_HAND_KIND = "first-hand"
PAGE_REQUIRED = (
    "type", "status", "project", "period", "topics", "bears_on", "artifacts",
    "origin", "published", "last_verified", "tags",
)
PAGE_STATUS = {"draft", "active", "superseded", "needs_review"}
PAGE_HEADINGS = {
    "case-study": ("## What we set out to learn", "## Setup and conditions", "## What we measured",
                   "## Lessons", "## What stayed open", "## Related"),
    "practice-guide": ("## When to use this", "## Procedure", "## Traps", "## Related"),
}
PAGE_MAX_WORDS = 2800
LESSON_HEADING = re.compile(r"^###\s+((?:Lesson|Trap)(?::|\s+-)\s*\S.*?)\s*$")
# Obsidian says `# | ^ : %% [[ ]]` may not work inside a link; a linked heading avoids them.
LINK_UNSAFE = re.compile(r"[:^%\[\]]")
COLON_WARNING = "colon in a linked heading may not resolve in Obsidian; use `Lesson - ` / `Trap - `"
LESSON_ROWS = ("Kind", "Scope", "Evidence (number / generalization)", "Sample",
               "Invalidated by", "Supersedes", "Bears on")
LESSON_KINDS = FIELD_ENUMS["kind"]
EVIDENCE_GRADES = FIELD_ENUMS["evidence_number"]
SCOPE_LEAD = re.compile(r"^\s*(general|domain|project)\b", re.I)
PERIOD = re.compile(r"^\d{4}-\d{2}-\d{2}\.\.\d{4}-\d{2}-\d{2}$")
CALLOUT_HEAD = re.compile(r"^\s*>\s*\[!example\][+-]?\s*Tested first-hand\b", re.I)
DETAILS_LINK = re.compile(r"Details and data:\s*\[\[([^\]|#\\]*)#((?:[^\]|\\]|\\(?!\|))+)(?:\\?\|[^\]]*)?\]\]")
# Non-English prose check (a warning, never an error). A letter outside ASCII,
# and high-frequency function words of six languages that rarely occur in
# English text. Name particles (de, da, di, del, der, van, von, la) and words
# English also uses (die, per, met, pour, plus, et al., non-, um) are left out.
NON_ASCII_LETTERS = re.compile(r"[^\x00-\x7F]")
NON_ENGLISH_STOPWORDS = {
    # de
    "und", "das", "nicht", "ist", "mit", "sich", "auf", "für", "ein", "eine",
    "auch", "oder", "aber", "wird", "sind",
    # es
    "el", "los", "las", "es", "una", "por", "para", "pero", "como", "muy",
    "también", "está", "cuando", "sobre", "entre",
    # fr
    "le", "les", "des", "est", "une", "dans", "pas", "que", "qui", "sur",
    "avec", "sont", "mais", "nous", "très",
    # it
    "il", "che", "sono", "anche", "più", "nel", "alla", "questo", "gli", "dei",
    "delle", "degli", "molto", "ancora", "quando",
    # nl
    "het", "een", "en", "niet", "zijn", "voor", "ook", "maar", "wordt", "worden",
    "naar", "bij", "geen", "nog", "deze",
    # pt
    "não", "uma", "com", "mais", "mas", "dos", "das", "pelo", "pela", "são",
    "isso", "ainda", "muito", "foi", "ao",
}


def _brain_of(directory: Path) -> Path | None:
    """The brain a folder belongs to: the outermost brain root above it that lists
    it as one of its grouped vaults (`<brain>/plants/<vault>`), else its parent when
    that is a brain root (the flat layout). Anything else (a vault's subfolder,
    `.research-staging/<vault>`, a folder outside any brain) has no brain. A group
    folder is never a brain (see brain_vault_dirs)."""
    directory = directory.resolve()
    parent = directory.parent
    # The outermost brain that lists this folder among its vaults wins, so a
    # group README that wrongly carries '## Router' never narrows the brain to
    # the group (lint_brain reports that README as a structure error).
    outer = None
    for ancestor in parent.parents:
        if is_brain_root(ancestor) and is_brain_vault(ancestor, directory):
            outer = ancestor
    if outer is not None:
        return outer
    return parent if is_brain_root(parent) else None


def area_home(directory: Path) -> Path | None:
    homes = sorted(p for p in directory.glob("00 *Home.md") if p.is_file())
    return homes[0] if len(homes) == 1 else None


def is_first_hand_area(directory: Path) -> bool:
    """`field-notes/`, or a folder whose Home says `vault_kind: first-hand`."""
    if directory.name == FIELD_NOTES_DIR:
        return True
    home = area_home(directory)
    if home is None:
        return False
    meta = field_frontmatter(home.read_text(encoding="utf-8", errors="replace")) or {}
    return str(meta.get(VAULT_KIND_KEY, "")).strip().lower() == FIRST_HAND_KIND


def area_names(directory: Path) -> tuple[str, str, str]:
    """(home basename, MOC prefix, meta prefix) of a first-hand area. The Home's
    frontmatter may declare `moc_prefix`; otherwise `00 X Home` gives `X - `."""
    if directory.name == FIELD_NOTES_DIR:
        default = (FIELD_HOME, FIELD_MOC_PREFIX, FIELD_PREFIX)
    else:
        default = ("", "", "")
    home = area_home(directory)
    if home is None:
        return default
    name = re.sub(r"^00\s+", "", home.stem)
    name = re.sub(r"\s*Home$", "", name).strip()
    meta = field_frontmatter(home.read_text(encoding="utf-8", errors="replace")) or {}
    moc_prefix = str(meta.get("moc_prefix") or f"{name} - ")
    return home.stem, moc_prefix, name


class BrainIndex:
    """Basename → path, note types and anchors for a brain (or one folder)."""

    def __init__(self, base: Path, extra: list[Path] | None = None) -> None:
        self.base = base.resolve()
        self.by_stem: dict[str, list[Path]] = {}
        paths = list(_iter_notes(self.base)) + [p for p in (extra or [])]
        for path in paths:
            resolved = path.resolve()
            bucket = self.by_stem.setdefault(path.stem, [])
            if resolved not in bucket:
                bucket.append(resolved)
        self._text: dict[Path, str] = {}
        self._meta: dict[Path, dict[str, Any]] = {}
        self._anchors: dict[Path, tuple[set[str], set[str]]] = {}

    def resolve(self, stem: str) -> Path | None:
        stem = stem.strip()
        if stem.endswith(".md"):
            stem = stem[:-3]
        found = self.by_stem.get(stem.split("/")[-1]) or []
        return found[0] if found else None

    def text(self, path: Path) -> str:
        path = path.resolve()
        if path not in self._text:
            self._text[path] = path.read_text(encoding="utf-8", errors="replace")
        return self._text[path]

    def meta(self, path: Path) -> dict[str, Any]:
        path = path.resolve()
        if path not in self._meta:
            self._meta[path] = field_frontmatter(self.text(path)) or {}
        return self._meta[path]

    def note_type(self, path: Path) -> str:
        return str(self.meta(path).get("type", "")).strip()

    def anchors(self, path: Path) -> tuple[set[str], set[str]]:
        path = path.resolve()
        if path not in self._anchors:
            self._anchors[path] = note_anchors(self.text(path))
        return self._anchors[path]

    def all_paths(self) -> list[Path]:
        return sorted({p for paths in self.by_stem.values() for p in paths})


def _body_lines(text: str) -> list[tuple[int, str]]:
    """(1-based line number, line) after the frontmatter."""
    lines = text.replace("\r\n", "\n").split("\n")
    start = 0
    if lines and lines[0].strip() == "---":
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                start = index + 1
                break
    return [(index + 1, lines[index]) for index in range(start, len(lines))]


def _unfenced(numbered: list[tuple[int, str]]) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    fenced = False
    for number, line in numbered:
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            out.append((number, line))
    return out


def lesson_sections(text: str) -> list[dict[str, Any]]:
    """Every `### Lesson: …` / `### Trap: …` section (fenced code ignored): its
    heading, first line number and lines up to the next heading of level ≤ 3."""
    lines = _unfenced(_body_lines(text))
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for number, line in lines:
        heading = re.match(r"^(#{1,6})\s", line)
        if heading and len(heading.group(1)) <= 3:
            if current:
                sections.append(current)
                current = None
            match = LESSON_HEADING.match(line)
            if match:
                current = {"heading": match.group(1).strip(), "line": number, "lines": []}
            continue
        if current is not None:
            current["lines"].append(line)
    if current:
        sections.append(current)
    return sections


def lesson_table(lines: list[str]) -> dict[str, str]:
    """Key → value from the section's two-column table rows (first cell = key)."""
    rows: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 2 or all(SEPARATOR_CELL.match(cell) for cell in cells if cell):
            continue
        key = cells[0].strip("* ").strip()
        if key:
            rows[key] = "|".join(cells[1:]).strip()
    return rows


def _prose_links(text: str) -> list[tuple[str, str]]:
    """(target stem, anchor or '') for every wikilink outside code."""
    out: list[tuple[str, str]] = []
    for line in _prose_lines(text):
        for match in re.finditer(r"!?\[\[([^\]|#\\]*)(?:#((?:[^\]|\\]|\\(?!\|))*))?(?:\\?\|[^\]]*)?\]\]", line):
            out.append((match.group(1).strip().split("/")[-1], (match.group(2) or "").strip()))
    return out


def check_links(rel: str, path: Path, text: str, idx: BrainIndex, errors: list[str],
                warnings: list[str] | None = None) -> None:
    """Every wikilink resolves and every #anchor lands — ERRORS for first-hand pages.
    An anchor carrying a character Obsidian may not keep in a link is a WARNING."""
    for target, anchor in _prose_links(text):
        if anchor and warnings is not None and LINK_UNSAFE.search(anchor):
            warnings.append(f"{rel}: anchor [[{target}#{anchor}]]: {COLON_WARNING if ':' in anchor else 'character may not survive in an Obsidian link'}")
        found = idx.resolve(target) if target else path
        if found is None:
            errors.append(f"{rel}: dangling wikilink [[{target}]] — first-hand pages allow no 'note to be written' links")
            continue
        if anchor and not anchor_resolves(anchor, idx.anchors(found)):
            errors.append(f"{rel}: anchor [[{target}#{anchor}]] lands on no heading")


def is_non_english(line: str) -> bool:
    """One line reads as non-English prose. Code spans, wikilinks, URLs, paths and
    identifiers are removed first. It counts with ≥ 2 distinct lower-case words
    carrying a non-ASCII letter, ≥ 2 non-English stop words, or one of each."""
    cleaned = CODE_SPAN.sub(" ", line.strip())
    cleaned = re.sub(r"\[\[[^\]]*\]\]", " ", cleaned)
    cleaned = re.sub(r"https?://\S+", " ", cleaned)
    words: list[str] = []
    for token in cleaned.split():
        if re.search(r"[/\\@_=<>{}`]|\.\w", token) or re.search(r"\d", token):
            continue
        words.extend(re.findall(r"[^\W\d_]+", token))
    # capitalised words are names (Ångström, São Paulo, Łódź, Los Angeles), not prose
    marked = {
        w for w in words
        if len(w) > 2 and w[0].islower() and any(ch.isalpha() for ch in NON_ASCII_LETTERS.findall(w))
    }
    stops = [w for w in words if w in NON_ENGLISH_STOPWORDS]
    return len(marked) >= 2 or len(stops) >= 2 or bool(marked and stops)


def non_english_lines(text: str) -> list[int]:
    """Line numbers of running prose that reads as non-English: outside fenced
    code and blockquotes, judged line by line with is_non_english."""
    hits: list[int] = []
    for number, line in _unfenced(_body_lines(text)):
        stripped = line.strip()
        if not stripped or stripped.startswith(">"):
            continue
        if is_non_english(stripped):
            hits.append(number)
    return hits


def check_first_hand_page(rel: str, path: Path, idx: BrainIndex) -> tuple[list[str], list[str]]:
    """Page- and lesson-level rules for `case-study` / `practice-guide`."""
    errors: list[str] = []
    warnings: list[str] = []
    text = idx.text(path)
    meta = idx.meta(path)
    page_type = str(meta.get("type", "")).strip()
    for key in PAGE_REQUIRED:
        if key not in meta:
            errors.append(f"{rel}: frontmatter missing {key!r} (type: {page_type})")
    if "claims" in meta:
        errors.append(f"{rel}: a first-hand page carries no 'claims:' list — it is never evidence for a claim ledger")
    status = str(meta.get("status", "")).strip()
    if "status" in meta and status not in PAGE_STATUS:
        errors.append(f"{rel}: status must be one of {'|'.join(sorted(PAGE_STATUS))} (found {status!r})")
    if status == "draft":
        warnings.append(f"{rel}: status is draft — set active once the page has been reviewed")
    if status == "superseded":
        successors = [_link_target(item) for item in _field_items(meta.get("superseded_by"))]
        if not successors:
            errors.append(f"{rel}: status superseded needs superseded_by naming the page that replaces it")
        for stem in successors:
            if idx.resolve(stem) is None:
                errors.append(f"{rel}: superseded_by names {stem!r}, which does not exist")
    if "artifacts" in meta and _field_empty(meta.get("artifacts")):
        errors.append(f"{rel}: artifacts must not be empty")
    for item in _field_items(meta.get("bears_on")):
        stem = _link_target(item)
        if idx.resolve(stem) is None:
            errors.append(f"{rel}: bears_on names {stem!r}, which is not the basename of an existing note")
    if "topics" in meta and _field_empty(meta.get("topics")):
        warnings.append(f"{rel}: topics is empty")
    period = meta.get("period")
    if isinstance(period, str) and period and not PERIOD.match(period):
        warnings.append(f"{rel}: period {period!r} is not 'YYYY-MM-DD..YYYY-MM-DD'")
    for key in ("published", "last_verified"):
        value = meta.get(key)
        if isinstance(value, str) and value and not ISO_DATE.match(value):
            warnings.append(f"{rel}: {key} {value!r} is not an ISO date")

    body = [line for _, line in _unfenced(_body_lines(text))]
    for heading in PAGE_HEADINGS.get(page_type, ()):
        if not any(line.strip() == heading for line in body):
            errors.append(f"{rel}: missing body heading '{heading}'")

    seen: set[str] = set()
    for section in lesson_sections(text):
        name = section["heading"]
        where = f"{rel}: '{name}'"
        if LINK_UNSAFE.search(name):
            warnings.append(f"{where}: {COLON_WARNING}")
        if _norm_heading(name) in seen:
            errors.append(f"{where}: duplicate lesson heading on the page (anchors would be ambiguous)")
        seen.add(_norm_heading(name))
        rows = lesson_table(section["lines"])
        missing = [row for row in LESSON_ROWS if row not in rows]
        if len(missing) == len(LESSON_ROWS):
            errors.append(f"{where}: no metadata table (rows: {', '.join(LESSON_ROWS)})")
            continue
        for row in missing:
            errors.append(f"{where}: metadata table lacks the row '{row}'")
        kind = rows.get("Kind", "").strip().strip("`")
        if "Kind" in rows and kind not in LESSON_KINDS:
            errors.append(f"{where}: Kind must be one of {'|'.join(sorted(LESSON_KINDS))} (found {kind!r})")
        if "Scope" in rows and not SCOPE_LEAD.match(rows["Scope"]):
            errors.append(f"{where}: Scope must start with general, domain or project (found {rows['Scope'][:40]!r})")
        grades = rows.get("Evidence (number / generalization)")
        if grades is not None:
            parts = [part.strip().strip("`").lower() for part in grades.split("/")]
            if len(parts) != 2 or any(part not in EVIDENCE_GRADES for part in parts):
                errors.append(
                    f"{where}: Evidence must be '<number> / <generalization>', each one of "
                    f"{'|'.join(sorted(EVIDENCE_GRADES))} (found {grades!r})"
                )
            elif parts[1] == "measured" and SMALL_SAMPLE.search(rows.get("Sample", "")):
                warnings.append(f"{where}: generalization graded 'measured' on a single chain/run sample")
        for row in ("Invalidated by", "Sample", "Supersedes", "Bears on"):
            if row in rows and not rows[row].strip(" -—"):
                errors.append(f"{where}: '{row}' must not be empty" + (" (write 'none')" if row == "Supersedes" else ""))
        bears_links = re.findall(r"\[\[[^\]]+\]\]", rows.get("Bears on", ""))
        other_links = [
            link for line in section["lines"] if not line.strip().startswith("|")
            for link in re.findall(r"\[\[[^\]]+\]\]", CODE_SPAN.sub("", line))
        ]
        if not bears_links and not other_links:
            warnings.append(f"{where}: no Bears on link and no other link — an isolated lesson")

    words = len(re.findall(r"\S+", "\n".join(line for _, line in _body_lines(text))))
    if words > PAGE_MAX_WORDS:
        warnings.append(f"{rel}: page is {words} words (> {PAGE_MAX_WORDS})")
    return errors, warnings


def english_warning(rel: str, text: str) -> list[str]:
    hits = non_english_lines(text)
    if not hits:
        return []
    shown = ", ".join(str(n) for n in hits[:12]) + (" …" if len(hits) > 12 else "")
    return [f"{rel}: non-English prose outside code and quotes at line(s) {shown}"]


def lesson_reachability(pages: list[Path], idx: BrainIndex, label) -> list[str]:
    """Each lesson on these pages must be linked from ≥ 1 MOC row and from a Home
    (a link to the lesson, or a Home table/list link to its page)."""
    moc_links: set[tuple[str, str]] = set()
    home_links: set[tuple[str, str]] = set()
    for note in idx.all_paths():
        note_type = idx.note_type(note)
        if note_type not in ("moc", "home"):
            continue
        links = {(t, _norm_heading(a)) for t, a in _prose_links(idx.text(note))}
        (moc_links if note_type == "moc" else home_links).update(links)
    errors: list[str] = []
    for page in pages:
        for section in lesson_sections(idx.text(page)):
            key = (page.stem, _norm_heading(section["heading"]))
            if key not in moc_links:
                errors.append(f"{label(page)}: '{section['heading']}' is not linked from any MOC row")
            if key not in home_links and (page.stem, "") not in home_links:
                errors.append(f"{label(page)}: '{section['heading']}' is not reachable from a Home (link the lesson or its page)")
    return errors


def check_callouts(files: list[Path], idx: BrainIndex, label) -> tuple[list[str], list[str]]:
    """Every `> [!example] Tested first-hand` block ends with a
    "Details and data" link that resolves to an existing Lesson:/Trap: section."""
    errors: list[str] = []
    warnings: list[str] = []
    for path in files:
        text = idx.text(path)
        if "Tested first-hand" not in text:
            continue
        lines = text.replace("\r\n", "\n").split("\n")
        fenced = False
        index = 0
        while index < len(lines):
            line = lines[index]
            if line.lstrip().startswith("```"):
                fenced = not fenced
            if fenced or not CALLOUT_HEAD.match(line):
                index += 1
                continue
            start = index
            block = [line]
            index += 1
            while index < len(lines) and lines[index].lstrip().startswith(">"):
                block.append(lines[index])
                index += 1
            where = f"{label(path)}:{start + 1}"
            last = next((b for b in reversed(block) if b.strip(" >")), "")
            match = DETAILS_LINK.search(last)
            if not match:
                errors.append(f"{where}: callout does not end with 'Details and data: [[<Page>#Lesson - …]]'")
                continue
            target, anchor = match.group(1).strip(), match.group(2).strip()
            if not re.match(r"^(Lesson|Trap)(:|\s+-)", anchor):
                errors.append(f"{where}: callout link anchor must name a 'Lesson - ' or 'Trap - ' section (found {anchor!r})")
            elif LINK_UNSAFE.search(anchor):
                warnings.append(f"{where}: callout anchor {anchor!r}: {COLON_WARNING}")
            found = idx.resolve(target)
            if found is None:
                errors.append(f"{where}: callout links to [[{target}]], which does not exist")
                continue
            if not anchor_resolves(anchor, idx.anchors(found)):
                errors.append(f"{where}: callout links to [[{target}#{anchor}]], which is no heading (renamed lesson?)")
            if str(idx.meta(found).get("status", "")).strip() == "superseded":
                warnings.append(f"{where}: callout points to [[{target}]], which is superseded")
            for other_target, other_anchor in _prose_links("\n".join(b.lstrip("> ") for b in block)):
                other = idx.resolve(other_target) if other_target else path
                if other is None:
                    errors.append(f"{where}: callout wikilink [[{other_target}]] does not resolve")
                elif other_anchor and not anchor_resolves(other_anchor, idx.anchors(other)):
                    if (other_target, other_anchor) != (target, anchor):
                        errors.append(f"{where}: callout anchor [[{other_target}#{other_anchor}]] lands on no heading")
    return errors, warnings


def validate_first_hand_area(root: Path, brain: Path | None = None) -> dict[str, Any]:
    """The gate for a first-hand area (field-notes/, or a vault whose Home says
    `vault_kind: first-hand`). Read-only: it never writes a report file."""
    if not root.is_dir():
        die(f"not a directory: {root}")
    root = root.resolve()
    if brain is None:
        brain = _brain_of(root)
    base = brain.resolve() if brain else root
    idx = BrainIndex(base)
    errors: list[str] = []
    warnings: list[str] = []
    # a grouped area is named by its location (`notes/x`); a top-level one by its name
    area = brain_rel(base, root) if brain and is_brain_vault(base, root) else root.name
    home_name, moc_prefix, meta_prefix = area_names(root)
    files = _iter_notes(root)

    def rel_of(path: Path) -> str:
        return path.relative_to(root).as_posix()

    homes = [p for p in files if p.parent == root and p.stem.startswith("00 ") and p.stem.endswith("Home")]
    expected = [home_name] if home_name else [p.stem for p in homes][:1]
    if [p.stem for p in homes] != expected or not homes:
        errors.append(f"expected exactly one root home '{(home_name or '00 <Name> Home')}.md', found: "
                      + (", ".join(p.name for p in homes) or "none"))
        home_name = home_name or (homes[0].stem if homes else "")

    kinds: dict[Path, str] = {}
    for path in files:
        relp = path.relative_to(root)
        rel = relp.as_posix()
        top = relp.parts[0] if len(relp.parts) > 1 else ""
        note_type = idx.note_type(path) if path.resolve() in {p for ps in idx.by_stem.values() for p in ps} \
            else str((field_frontmatter(path.read_text(encoding="utf-8", errors="replace")) or {}).get("type", "")).strip()
        if path.stem == home_name and path.parent == root:
            kinds[path] = "home"
        elif top.startswith("99 "):
            kinds[path] = "meta"
            if meta_prefix and not path.stem.startswith(meta_prefix + " "):
                errors.append(f"{rel}: meta file without the '{meta_prefix} ' prefix")
        elif path.stem.endswith(" MOC") or note_type == "moc":
            kinds[path] = "moc"
            if moc_prefix and not path.stem.startswith(moc_prefix):
                errors.append(f"{rel}: MOC basename must start with '{moc_prefix}' (brain-wide uniqueness)")
            if not CONTENT_FOLDER.match(top or ""):
                errors.append(f"{rel}: a MOC lives in a numbered topic folder ('01 ...')")
        else:
            kinds[path] = "note"

        if not FIELD_NAME_OK.match(path.stem):
            errors.append(f"{rel}: filename must be plain ASCII English (letters, digits, space , . ' ( ) & + _ -)")
        clash = [p for p in idx.by_stem.get(path.stem, []) if p != path.resolve()]
        if clash:
            where = ", ".join(p.relative_to(base).as_posix() if base in p.parents else str(p) for p in clash)
            errors.append(f"{rel}: basename {path.stem!r} is not unique brain-wide (also at {where})")
        check_links(rel, path, idx.text(path), idx, errors, warnings)

    home = next((p for p, k in kinds.items() if k == "home"), None)
    home_links = {t for t, _ in _prose_links(idx.text(home))} if home else set()
    moc_links: set[str] = set()
    for path, kind in sorted(kinds.items()):
        if kind == "moc":
            moc_links.update(t for t, _ in _prose_links(idx.text(path)))
            if path.stem not in home_links:
                errors.append(f"{rel_of(path)}: MOC is not linked from [[{home_name}]]")
        if kind == "meta" and path.stem not in home_links:
            warnings.append(f"{rel_of(path)}: meta file is not linked from [[{home_name}]]")

    notes = 0
    pages: list[Path] = []
    for path, kind in sorted(kinds.items()):
        rel = rel_of(path)
        meta = field_frontmatter(idx.text(path))
        if meta is None:
            errors.append(f"{rel}: missing or unparseable frontmatter")
            continue
        note_type = str(meta.get("type", "")).strip()
        if kind != "note":
            if note_type != kind:
                errors.append(f"{rel}: structural file needs 'type: {kind}' (found {note_type or 'unset'!r})")
            warnings.extend(english_warning(rel, idx.text(path)))
            continue
        if note_type not in FIRST_HAND_TYPES:
            errors.append(
                f"{rel}: every note in {area}/ outside Home, MOCs and 99 Meta "
                f"must be 'type: field-note', 'type: case-study' or 'type: practice-guide' "
                f"(found {note_type or 'unset'!r})"
            )
            continue
        notes += 1
        if path.stem not in home_links | moc_links:
            errors.append(f"{rel}: not linked from [[{home_name}]] or any {area} MOC")
        if note_type == "field-note":
            e, w = check_field_note(rel, path, meta, idx, root, base, brain)
        else:
            e, w = check_first_hand_page(rel, path, idx)
            pages.append(path)
        errors.extend(e)
        warnings.extend(w)
        warnings.extend(english_warning(rel, idx.text(path)))

    errors.extend(lesson_reachability(pages, idx, rel_of))
    callout_errors, callout_warnings = check_callouts(files, idx, rel_of)
    errors.extend(callout_errors)
    warnings.extend(callout_warnings)
    return {
        "vault": area,
        "profile": "first-hand",
        "root": str(root),
        "brain": str(brain) if brain else None,
        "checked_at": now(),
        "notes": len(files),
        "field_notes": notes,
        "pages": len(pages),
        "lessons": sum(len(lesson_sections(idx.text(p))) for p in pages),
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
    }


def check_field_note(rel: str, path: Path, meta: dict[str, Any], idx: BrainIndex, root: Path,
                     base: Path, brain: Path | None) -> tuple[list[str], list[str]]:
    """The older stand-alone `type: field-note` rules (one note = one lesson)."""
    errors: list[str] = []
    warnings: list[str] = []
    text = idx.text(path)
    for key in FIELD_REQUIRED:
        if key not in meta:
            errors.append(f"{rel}: frontmatter missing {key!r}")
    if "claims" in meta:
        errors.append(f"{rel}: a first-hand note carries no 'claims:' list — it is never evidence for a claim ledger")
    for key, allowed in FIELD_ENUMS.items():
        if key not in meta:
            continue
        value = meta[key]
        if isinstance(value, list) or str(value).strip() not in allowed:
            errors.append(f"{rel}: {key} must be one of {'|'.join(sorted(allowed))} (found {value!r})")
    for key in FIELD_MUST_FILL:
        if key in meta and _field_empty(meta[key]):
            errors.append(f"{rel}: {key} must not be empty")
    for item in _field_items(meta.get("artifacts")):
        token = item.split(" (")[0].strip()
        if not any(form.match(token) for form in ARTIFACT_FORMS):
            warnings.append(
                f"{rel}: artifact {item!r} is not a URL, path@commit, log/JSON path "
                "or session:<id>:<line> — a reader may not be able to find it"
            )
    for item in _field_items(meta.get("invalidated_by")):
        for lead, form in TRIGGER_FORMS.items():
            if item.startswith(lead) and not form.match(item):
                errors.append(
                    f"{rel}: invalidated_by {item!r} starts like a machine-checkable "
                    f"trigger but is malformed (expected {form.pattern})"
                )
    status = str(meta.get("status", "")).strip()
    successors = [_link_target(item) for item in _field_items(meta.get("superseded_by"))]
    if status == "superseded":
        if not successors:
            errors.append(f"{rel}: status superseded needs superseded_by naming the note that replaces it")
        for stem in successors:
            if idx.resolve(stem) is None:
                errors.append(f"{rel}: superseded_by names {stem!r}, which does not exist")
    elif successors:
        warnings.append(f"{rel}: superseded_by is set but status is {status!r}")
    for stem in (_link_target(item) for item in _field_items(meta.get("supersedes"))):
        if idx.resolve(stem) is None:
            warnings.append(f"{rel}: supersedes names {stem!r}, which does not exist")
    if str(meta.get("kind", "")).strip() == "benchmark" and _field_empty(meta.get("measured_at")):
        errors.append(f"{rel}: kind benchmark needs measured_at")
    for key in ("published", "last_verified", "measured_at"):
        value = meta.get(key)
        if isinstance(value, str) and not _field_empty(value) and not ISO_DATE.match(value):
            warnings.append(f"{rel}: {key} {value!r} is not an ISO date")
    if (str(meta.get("evidence_generalization", "")).strip() == "measured"
            and SMALL_SAMPLE.search(" ".join(_field_items(meta.get("sample"))))):
        warnings.append(
            f"{rel}: evidence_generalization is 'measured' but the sample is a single "
            "chain/run — a generalization needs n>=2"
        )
    body_text = text.replace("\r\n", "\n")
    if _frontmatter_block(body_text):
        body_text = body_text[body_text.find("\n---", 3) + 4 :]
    lines = _prose_lines(body_text)
    for heading in FIELD_HEADINGS:
        if not any(line.strip() == heading for line in lines):
            errors.append(f"{rel}: missing body heading '{heading}'")
    related = [
        match.group(1).strip().split("/")[-1]
        for line in _section(lines, "## Related")
        for match in WIKILINK.finditer(line)
    ]
    resolving = {stem for stem in related if idx.resolve(stem) is not None}
    if len(resolving) < 2:
        errors.append(f"{rel}: '## Related' needs at least 2 wikilinks that resolve (found {len(resolving)})")
    words = len(re.findall(r"\S+", body_text))
    if words > FIELD_MAX_WORDS:
        warnings.append(f"{rel}: body is {words} words (> {FIELD_MAX_WORDS}); one note = one lesson")
    topic = False
    groups: dict[Path, list[Path]] | None = None  # group folder → its vaults, looked up once
    for stem, _ in _prose_links(text):
        found = idx.resolve(stem) if stem else None
        if found is None or root in found.parents or base not in found.parents:
            continue
        parts = found.relative_to(base).parts
        if len(parts) > 1 and parts[0] not in FLAT_DIRS | {"inbox", FIELD_NOTES_DIR}:
            top = base / parts[0]
            if groups is None:
                groups = {group: group_vaults(group) for group in brain_group_dirs(base)}
            # In a group folder the topic vault is the grouped vault holding the
            # note; a note loose in the group itself is in no vault at all.
            holder = next((v for v in groups[top] if v in found.parents), None) if top in groups else top
            if holder is not None and not is_first_hand_area(holder):
                topic = True
                break
    if not topic:
        warnings.append(f"{rel}: links into no topic vault — is there really no related research?")
    return errors, warnings


def _frontmatter_list(path: Path, key: str) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    block = _frontmatter_block(text)
    if not block:
        return []
    values: list[str] = []
    capturing = False
    for line in block.splitlines():
        if line.startswith(f"{key}:"):
            capturing = True
            continue
        if capturing:
            if line.startswith("  - "):
                values.append(line[4:].strip())
            elif line.strip() and not line.startswith(" "):
                break
    return values


def _frontmatter_block(text: str) -> str:
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    return text[4:end] if end != -1 else ""


def _parse_frontmatter(text: str) -> dict[str, str]:
    block = _frontmatter_block(text)
    if not block:
        return {}
    meta: dict[str, str] = {}
    for line in block.splitlines():
        if line.startswith(" ") or not line.strip():
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    return meta


def _bfs_depth(start: str, by_basename: dict[str, list[Path]], links: dict[Path, list[str]]) -> dict[str, int]:
    outgoing: dict[str, set[str]] = {}
    for path, targets in links.items():
        outgoing.setdefault(path.stem, set()).update(t.split("/")[-1] for t in targets)
    seen = {start: 0}
    queue = [start]
    while queue:
        current = queue.pop(0)
        for target in sorted(outgoing.get(current, ())):
            if target in by_basename and target not in seen:
                seen[target] = seen[current] + 1
                queue.append(target)
    return seen


# --------------------------------------------------------------------------
# link-rewrite — exact-match basename rewrites, never a broad regex
#
# A broad regex rewrite can silently corrupt a vault; this one is deliberately
# narrow: it escapes both sides, matches only the three wikilink shapes, and
# defaults to printing.
# --------------------------------------------------------------------------


def _rewrite_pattern(old: str) -> re.Pattern[str]:
    # `[[old\|alias]]` (a table row) is rewritten too; only the basename changes,
    # so the escaped pipe is preserved as written.
    return re.compile(r"\[\[" + re.escape(old) + r"(?=[\]#|]|\\\|)")


def link_rewrite(root: Path, mapping: dict[str, str], apply_one: Path | None = None,
                 apply_all: bool = False) -> dict[str, Any]:
    if not mapping:
        die("--map must contain at least one {old: new} pair")
    patterns = {old: (_rewrite_pattern(old), f"[[{new}") for old, new in mapping.items()}
    hits: list[dict[str, Any]] = []
    per_file: dict[Path, int] = {}
    for path in _iter_notes(root):
        text = path.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.split("\n"), start=1):
            for old, (pattern, replacement) in patterns.items():
                if pattern.search(line):
                    hits.append(
                        {
                            "file": str(path.relative_to(root)),
                            "line": number,
                            "old": old,
                            "new": mapping[old],
                            "before": line.strip(),
                            "after": pattern.sub(replacement, line).strip(),
                        }
                    )
                    per_file[path] = per_file.get(path, 0) + len(pattern.findall(line))

    targets: list[Path] = []
    mode = "dry-run"
    if apply_one is not None:
        one = apply_one if apply_one.is_absolute() else root / apply_one
        one = one.resolve()
        if one not in per_file:
            die(f"--apply-one {apply_one} has no occurrences to rewrite")
        targets = [one]
        mode = "apply-one"
    elif apply_all:
        targets = sorted(per_file)
        mode = "apply"

    rewritten = 0
    for path in targets:
        text, newline = read_note(path)
        for old, (pattern, replacement) in patterns.items():
            text, count = pattern.subn(replacement, text)
            rewritten += count
        write_text(path, text, newline=newline)

    # reconciliation: count what is left over the whole tree
    remaining = 0
    for path in _iter_notes(root):
        text = path.read_text(encoding="utf-8", errors="replace")
        for old, (pattern, _replacement) in patterns.items():
            remaining += len(pattern.findall(text))

    return {
        "root": str(root),
        "mode": mode,
        "map": mapping,
        "occurrences": sum(per_file.values()),
        "files": len(per_file),
        "hits": hits,
        "rewritten": rewritten,
        "remaining": remaining,
        "reconciled": (
            remaining == 0 if mode == "apply"
            else remaining == sum(per_file.values()) - rewritten
        ),
        "next_step": (
            "review the lines above, then --apply-one <file>, grep it, then --apply"
            if mode == "dry-run" else
            "grep the rewritten file, then re-run with --apply for the rest"
            if mode == "apply-one" else "done"
        ),
    }


# --------------------------------------------------------------------------
# router row
# --------------------------------------------------------------------------


def router_row(vault_name: str, when: str, entry_point: str) -> str:
    return f"| `{vault_name}/` | {_md_cell(when)} | `{entry_point}` |"


# The counts segment of a router row's "when" cell, e.g. `12 sources, 40 claims
# (15 supported, 18 qualified), 6 open gaps, 9 notes`. Regenerated from the
# published vault, never typed.
COUNTS_SEGMENT = re.compile(
    r"\d+ sources?, \d+ claims?(?: \([^()|]*\))?(?:, \d+ open gaps?)?(?:, \d+ notes?)?"
)
STATUS_ORDER = ("supported", "qualified", "disputed", "refuted", "unsupported", "superseded")


def vault_counts(vault: Path, prefix: str | None = None) -> dict[str, Any]:
    """What a router row reports about a published vault: sources, claims by
    status, open gaps, notes. Read-only."""
    scan = scan_vault(vault, prefix_override=prefix)
    by_status: dict[str, int] = {}
    for claim in scan["ledger"].values():
        status = str(claim.get("status") or "unknown")
        by_status[status] = by_status.get(status, 0) + 1
    order = {status: index for index, status in enumerate(STATUS_ORDER)}
    return {
        "sources": len(scan["register"]),
        "claims": len(scan["ledger"]),
        "claims_by_status": dict(sorted(by_status.items(), key=lambda pair: (order.get(pair[0], 99), pair[0]))),
        "open_gaps": sum(1 for gap in scan["gaps"].values() if gap.get("status") == "open"),
        "notes": len(scan["notes"]),
    }


def counts_segment(counts: dict[str, Any]) -> str:
    statuses = ", ".join(f"{n} {status}" for status, n in counts["claims_by_status"].items() if n)
    return (
        f"{_n(counts['sources'], 'source')}, {_n(counts['claims'], 'claim')}"
        + (f" ({statuses})" if statuses else "")
        + f", {_n(counts['open_gaps'], 'open gap')}, {_n(counts['notes'], 'note')}"
    )


def find_router_row(readme: Path, vault_path: str) -> str | None:
    """The Router section's existing row for this vault (`| `plants/mini/` | … |`), if any."""
    lines = readme.read_text(encoding="utf-8", errors="replace").split("\n")
    start = next((i for i, line in enumerate(lines) if ROUTER_HEADING.match(line)), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"^#{1,2}\s", lines[i])), len(lines))
    key = vault_path.strip("/").lower() + "/"
    for line in lines[start + 1 : end]:
        if line.strip().startswith("|") and _router_cell(line).lower() == key:
            return line
    return None


def refresh_row_counts(row: str, counts: dict[str, Any]) -> str | None:
    """The row with its counts segment regenerated: replaced where the "when"
    cell has one, appended to that cell where it has none. None when the row
    has no "when" cell."""
    segment = counts_segment(counts)
    cells = re.split(r"(?<!\\)\|", row)
    if len(cells) < 4:
        return None
    when = cells[2]
    if COUNTS_SEGMENT.search(when):
        cells[2] = COUNTS_SEGMENT.sub(lambda _: segment, when, count=1)
    else:
        text = when.strip()
        cells[2] = f" {text}{'; ' if text else ''}{segment} "
    return "|".join(cells)


def _router_cell(line: str) -> str:
    """A router row's first cell without its markup: `**plants/**` → `plants/`."""
    return line.strip().strip("|").split("|")[0].strip().strip("*_`").strip()


def _group_anchor(lines: list[str], start: int, end: int, group: str) -> tuple[bool, str | None]:
    """(group found, last row of that group's section) inside the Router section
    lines[start:end]. A group section is a heading naming the group (`### Plants`,
    `### plants/`), or a group row (`| **plants/** | … |`) followed by the rows of
    its vaults (`| `plants/mini/` | … |`)."""
    key = group.lower()
    for index in range(start, end):
        heading = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", lines[index])
        if not heading:
            continue
        words = heading.group(2).strip("*_` ").split()
        if not words or words[0].strip("*_`").rstrip("/:").lower() != key:
            continue
        level = len(heading.group(1))
        last = None
        for line in lines[index + 1 : end]:
            deeper = re.match(r"^(#{1,6})\s", line)
            if deeper and len(deeper.group(1)) <= level:
                break
            if line.strip().startswith("|"):
                last = line
        return True, last
    member = f"{key}/"
    rows = [i for i in range(start, end) if lines[i].strip().startswith("|")]
    head = next((i for i in rows if _router_cell(lines[i]).rstrip("/").lower() == key), None)
    if head is None:
        head = next((i for i in rows if _router_cell(lines[i]).lower().startswith(member)), None)
    if head is None:
        return False, None
    last = head
    for index in range(head + 1, end):
        cell = _router_cell(lines[index]).lower()
        if not lines[index].strip().startswith("|") or not cell.startswith(member) or cell == member:
            break
        last = index
    return True, lines[last]


def find_router_anchor(readme: Path, vault_path: str | None = None) -> str | None:
    """The exact row a new router row goes after, or None if it is not exactly
    locatable. For a grouped vault (`plants/pothos-cuttings`) that is the last row of
    the group's own section when the router has one; otherwise the last row of the
    router table. The README is never regexed into shape."""
    lines = readme.read_text(encoding="utf-8", errors="replace").split("\n")
    start = None
    for index, line in enumerate(lines):
        if ROUTER_HEADING.match(line):
            start = index
            break
    if start is None:
        return None
    if vault_path and "/" in vault_path.strip("/"):
        end = next((i for i in range(start + 1, len(lines)) if re.match(r"^#{1,2}\s", lines[i])), len(lines))
        found, last = _group_anchor(lines, start + 1, end, vault_path.strip("/").split("/")[0])
        if found:
            return last if last is not None and sum(1 for line in lines if line == last) == 1 else None
    last = None
    seen_table = False
    for line in lines[start + 1 :]:
        if line.strip().startswith("|"):
            seen_table = True
            last = line
            continue
        if seen_table and not line.strip().startswith("|"):
            break
    if last is None:
        return None
    if sum(1 for line in lines if line == last) != 1:
        return None
    return last


def insert_router_row(readme: Path, row: str, apply: bool, vault_path: str | None = None) -> dict[str, Any]:
    anchor = find_router_anchor(readme, vault_path)
    if anchor is None:
        return {
            "inserted": False,
            "row": row,
            "reason": "the router table could not be located exactly; paste the row by hand",
        }
    text, newline = read_note(readme)
    if row in text:
        return {"inserted": False, "row": row, "reason": "the row is already in the README"}
    if not apply:
        return {"inserted": False, "row": row, "anchor": anchor, "reason": "dry run"}
    lines = text.split("\n")
    index = lines.index(anchor)
    lines.insert(index + 1, row)
    write_text(readme, "\n".join(lines), newline=newline)
    return {"inserted": True, "row": row, "anchor": anchor}


# --------------------------------------------------------------------------
# publish entry points
# --------------------------------------------------------------------------


def check_prefix(prefix: str) -> None:
    """The prefix is part of every meta file name (`<prefix> Claim Ledger.md`)."""
    problem = path_component_problem(f"00 {prefix} Home") if prefix else None
    if problem:
        die(f"prefix {prefix!r} cannot be part of a file name: {problem}")


def write_files(root: Path, files: dict[str, str]) -> list[str]:
    """Every path is checked before the first write: a file that would land
    outside `root` (a `..` or separator smuggled into a folder or note name)
    stops the whole publish with nothing written."""
    base = root.resolve()
    targets: dict[str, Path] = {}
    for rel in sorted(files):
        target = (base / rel).resolve()
        if base not in target.parents or any(path_component_problem(part) for part in Path(rel).parts):
            die(f"path escapes the vault or is not a valid file name: {rel!r}; nothing was written")
        targets[rel] = target
    for rel, text in sorted(files.items()):
        write_text(targets[rel], text)
    return sorted(files)


def publish_fresh(run: Run, out: Path, prefix: str) -> dict[str, Any]:
    """A whole new vault into an empty directory. Used directly for a scratch
    publish, and as the staging step of a brain publish."""
    if out.exists() and any(out.iterdir()):
        die(f"{out} exists and is not empty; publish never overwrites")
    check_prefix(prefix)
    files = build_vault_files(run, prefix)
    write_files(out, files)
    report = validate_vault(out)
    result = {
        "run_id": run.manifest["run_id"],
        "target": str(out),
        "prefix": prefix,
        "files_written": len(files),
        "valid": report["valid"],
        "errors": report["errors"],
        "warnings": len(report["warnings"]),
    }
    write_json(run.reports / "publish.json", result)
    return result


def publish_to_brain(run: Run, brain: Path, vault_name: str, prefix: str | None,
                     apply: bool, when: str | None = None) -> dict[str, Any]:
    """The full brain procedure: derive identity, build prefixed in staging, lint
    staging ∪ brain, validate staged, move, then the router row.

    `vault_name` may name a group folder: `plants/pothos-cuttings` publishes into
    `<brain>/plants/pothos-cuttings/`. Identity (prefix, Home, staging name) comes
    from the last segment; the location (destination, router row, report) keeps
    the whole path. The group folder must already exist and must not be a vault."""
    vault_path, vault_name = split_vault_path(vault_name)
    prefix = prefix or prefix_from_vault_name(vault_name)
    check_prefix(prefix)
    destination = brain.joinpath(*vault_path.split("/"))
    if destination.is_dir() and is_first_hand_area(destination):
        die(
            f"{vault_path}/ is a first-hand area (vault_kind: first-hand); the research pipeline never "
            "publishes or merges into it. Nothing was built and nothing was written."
        )
    if destination.exists():
        die(f"{destination} already exists; publish never overwrites a vault")
    if any(part in NO_WRITE_DIRS for part in vault_path.split("/")):
        die(f"{vault_path}/ is a no-write area")
    if vault_path != vault_name:
        group = destination.parent
        group_rel = brain_rel(brain, group)
        if not group.is_dir():
            die(f"group folder {group_rel}/ does not exist in {brain}; create it first — "
                "publish never invents a group. Nothing was built and nothing was written.")
        for folder in [group, *group.parents]:
            if folder.resolve() == brain.resolve():
                break
            if has_vault_markers(folder) or is_brain_root(folder) or folder.name == "research-staging":
                die(f"{brain_rel(brain, folder)}/ is not a group folder (it is a vault, a brain root, "
                    "or staging); a vault never nests inside another. Nothing was built and nothing was written.")

    staging_root = brain / STAGING_DIR
    if staging_root.exists():
        shutil.rmtree(staging_root)
    staging = staging_root / vault_name
    staging.mkdir(parents=True)
    files = build_vault_files(run, prefix)
    write_files(staging, files)

    lint = lint_brain(brain, extra_roots=[staging])
    staged_report = validate_vault(staging, write_report=True)
    names = meta_names_for(prefix)
    row = router_row(vault_path, when or run.manifest["question"], f"{names['home']}.md")

    blocked: list[str] = []
    if lint["duplicate_basenames"]:
        blocked.extend(
            f"duplicate basename {item['basename']!r}: {', '.join(item['paths'])}"
            for item in lint["duplicate_basenames"]
        )
    if staged_report["errors"]:
        blocked.extend(staged_report["errors"])

    result = {
        "run_id": run.manifest["run_id"],
        "brain": str(brain),
        "vault": vault_path,
        "prefix": prefix,
        "prefix_source": "derived from the vault name" if prefix == prefix_from_vault_name(vault_name) else "declared with --prefix",
        "files": len(files),
        "notes": sum(1 for rel in files if not rel.startswith(("90 ", "99 ")) and " MOC" not in rel),
        "staged_at": str(staging),
        "lint": {
            "duplicate_basenames": lint["duplicate_basenames"],
            "prefix_violations": lint["prefix_violations"],
        },
        "validation": {"valid": staged_report["valid"], "errors": staged_report["errors"],
                       "warnings": len(staged_report["warnings"])},
        "blocked": blocked,
        "router_row": row,
        "applied": False,
        "gaps": [
            {"id": gid, "impact": gap.get("impact"), "question": gap.get("question")}
            for gid, gap in sorted(run.records("gaps").items())
            if gap.get("status") == "open"
        ],
    }
    if blocked or not apply:
        shutil.rmtree(staging_root, ignore_errors=True)
        result["message"] = (
            "blocked; nothing moved" if blocked
            else "dry run: confirm the vault name and prefix, then re-run with --apply"
        )
        write_json(run.reports / "publish.json", result)
        return result

    os.replace(staging, destination)
    shutil.rmtree(staging_root, ignore_errors=True)
    result["applied"] = True
    result["target"] = str(destination)
    result["router"] = insert_router_row(brain / "README.md", row, apply=True, vault_path=vault_path)
    result["message"] = (
        "vault moved into the brain"
        + ("; router row inserted" if result["router"].get("inserted") else
           "; PASTE THE ROUTER ROW BY HAND: " + row)
    )
    write_json(run.reports / "publish.json", result)
    return result


# --------------------------------------------------------------------------
# failsafe: packets on disk, resume plan, active-runs registry
#
# The store is the memory. A session can die anywhere (token limit, crash, power
# cut); `resume` names the next step from the store alone, never from anyone's
# recollection. Only worker output not yet written to inbox/ can be lost, and a
# worker lost that way is re-dispatched from its saved packet.
# --------------------------------------------------------------------------

SYNTHESIS_READY = {"supported", "qualified", "disputed"}


def record_packet(run: Run, packet: dict[str, Any], role: str, domain_id: str,
                  picks: list[str], brief: str = "framed", scope: str = "") -> dict[str, Any]:
    """Every emitted packet is kept as <run>/packets/<task_id>.json, so a worker
    lost with a dead session is re-dispatched, not re-issued. The id is claimed by
    an exclusive create, so parallel `packet` calls never share one, and the file
    itself is the record: no manifest write here.

    A non-framed arm is visible in the id: `w1-D1-prospector-blind-1`."""
    wave = run.manifest.get("wave", 0)
    scope = scope or domain_id or "expand"
    arm = "" if brief == "framed" else f"-{brief}"
    for part in (scope, role, brief):
        if not DOMAIN_ID.fullmatch(str(part)):
            die(f"{part!r} cannot be part of a task id: letters, digits, _ and - only")
    folder = run.root / "packets"
    folder.mkdir(parents=True, exist_ok=True)
    n = 1
    while True:
        task_id = f"w{wave}-{scope}-{role}{arm}-{n}"
        path = folder / f"{task_id}.json"
        if path.resolve().parent != folder.resolve():
            die(f"task id {task_id!r} leaves the packets folder")
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            n += 1
    packet = {"task_id": task_id, **packet,
              "return_as": f"{run.inbox / (task_id + '.json')}, with \"task_id\": \"{task_id}\"",
              "issued": {"task_id": task_id, "role": role, "domain_id": domain_id, "picks": picks,
                         "scope": scope, "brief": brief, "wave": wave, "file": str(path), "at": now()}}
    with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as out:
        out.write(json.dumps(packet, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return packet


def _packets(run: Run) -> list[dict[str, Any]]:
    """Issued packets, from their files. One cut off mid-write was never handed to
    the coordinator, so it is skipped."""
    issued = [_payload(path).get("issued") for path in sorted((run.root / "packets").glob("*.json"))]
    return sorted((p for p in issued if isinstance(p, dict) and p.get("task_id")),
                  key=lambda p: (str(p.get("at")), p["task_id"]))


def _payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _results(run: Run, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Every worker result the store knows: committed (raw file in inbox/_ingested),
    or waiting in the inbox. A raw file ingested before the store kept markers is
    read as belonging to the current wave."""
    marks = manifest.get("ingested_files") or {}
    out = [dict(rec, stored_as=name, pending=False) for name, rec in marks.items()]
    wave = manifest.get("wave", 0)
    for folder, pending in ((run.inbox / "_ingested", False), (run.inbox, True)):
        for path in sorted(folder.glob("*.json")):
            if not pending and path.name in marks:
                continue
            payload = _payload(path)
            out.append({"file": path.name, "stored_as": path.name, "task_id": str(payload.get("task_id") or path.stem),
                        "role": str(payload.get("role") or ""), "domain_id": str(payload.get("domain_id") or ""),
                        "arm": str(payload.get("brief") or ""),
                        "wave": wave, "at": "9999" if pending else "", "pending": pending})
    return out


def _outstanding(packets: list[dict[str, Any]], results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Packets with no result: matched by task id, else by role, arm (and domain)
    for a worker that ignored the task id and answered after the packet was
    issued. A result without an arm is read as framed, like a packet without one."""
    used: set[int] = set()
    unmatched = []
    for packet in packets:
        hit = next((i for i, r in enumerate(results) if i not in used and r.get("task_id") == packet["task_id"]), None)
        if hit is None:
            unmatched.append(packet)
        else:
            used.add(hit)
    left = []
    for packet in unmatched:
        hit = next((i for i, r in enumerate(results) if i not in used and r.get("role") == packet["role"]
                    and (r.get("arm") or "framed") == (packet.get("brief") or "framed")
                    and (packet["scope"] == "expand" or r.get("domain_id") == packet["domain_id"])
                    and str(r.get("at", "")) >= packet["at"]), None)
        if hit is None:
            left.append(packet)
        else:
            used.add(hit)
    return left


def _unextracted(run: Run, wave: int, results: list[dict[str, Any]],
                 outstanding: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """(domain, url) shortlisted by this wave's prospectors that no extractor has
    covered yet: not admitted, not rejected, not handed back."""
    known = {_url_key(str(rec.get("canonical_url") or rec.get("url") or ""))
             for rec in run.records("sources").values()}
    known |= {_url_key(str(rec.get("url") or "")) for rec in read_json(run.reports / "rejected.json", [])}
    for record in read_json(run.reports / "handbacks.json", []):
        known |= {_url_key(str(u.get("url") or "")) for u in record.get("unread", [])}
    out: list[tuple[str, str]] = []
    for rec in results:
        if rec["pending"] or rec.get("role") != "prospector" or rec.get("wave") != wave:
            continue
        payload = _payload(run.inbox / "_ingested" / rec["stored_as"])
        domain = str(rec.get("domain_id") or "")
        urls = [str(item.get("url") or "").strip() for item in (payload.get("shortlist") or []) if isinstance(item, dict)]
        urls = [u for u in urls if _url_allowed(u)]  # never suggest a URL no record may hold
        # An extractor that found nothing leaves no source; count answers, not just URLs.
        answered = sum(1 for r in results if r.get("role") == "extractor" and r.get("domain_id") == domain
                       and r.get("wave") == wave)
        answered += sum(1 for p in outstanding if p["role"] == "extractor" and p["domain_id"] == domain)
        todo = [u for u in urls if u and _url_key(u) not in known and (domain, u) not in out]
        out += [(domain, u) for u in todo[:max(0, len(urls) - answered)]]
    return out


def _published_before_markers(run: Run) -> bool:
    """A run published before the manifest recorded it: an applied brain publish
    or a fresh publish left reports/publish.json. A merge from before then left no trace."""
    report = _payload(run.reports / "publish.json")
    return report.get("applied") is True or ("applied" not in report and bool(report.get("target")))


def _url_allowed(url: str) -> bool:
    try:
        canonical_url(url)
    except ValueError:
        return False
    return True


def _url_key(url: str) -> str:
    """Compare a shortlist with what was extracted: the canonical URL, which
    already folds every arXiv view, version and host into one `abs` URL; an arXiv
    paper's key is its lower-case id."""
    canonical = canonical_url_safe(url)
    if canonical.startswith("https://arxiv.org/abs/"):
        return f"arxiv:{canonical[len('https://arxiv.org/abs/'):].lower()}"
    return canonical


def _cli(run: Run, rest: str) -> str:
    """A command line for the coordinator to run. Every value spliced into one is
    shell-quoted (`shlex.quote`): a path, a vault name or a URL may hold `$(…)`,
    a backtick or `;`."""
    base = "python scripts/research.py"
    if run.root.parent.resolve() != RUNS_ROOT.resolve():
        base += f" --runs-root {shlex.quote(str(run.root.parent))}"
    return f"{base} {rest}"


def resume_plan(run: Run) -> dict[str, Any]:
    """The last completed step and the exact next commands, from the store alone."""
    manifest = run.manifest
    rid = manifest["run_id"]
    arg = f"--run {shlex.quote(str(rid))}"
    q = shlex.quote
    wave = int(manifest.get("wave", 0))
    ceiling = limits_of(manifest)["waves"]
    domains = [d["id"] for d in run.taxonomy.get("domains", [])]
    claims = run.records("claims")
    excerpts = run.records("excerpts")
    gaps = run.records("gaps")
    packets = _packets(run)
    results = _results(run, manifest)
    pending = [r["file"] for r in results if r["pending"]]
    outstanding = _outstanding(packets, results)
    expand = manifest.get("mode") == "expand"
    target = manifest.get("target") or {}
    into = target.get("into") or (manifest.get("adopted_from") if expand else None)
    unverified = (
        int(manifest.get("ingest_commits", 0)) > int(manifest.get("verified_through", 0))
        or any(c.get("status") == "proposed" for c in claims.values())
        or any((x.get("verification") or {}).get("status") == "unchecked" for x in excerpts.values())
    )
    new_claims = [c for c in claims.values() if c.get("origin") != "adopted" and c.get("status") in SYNTHESIS_READY]
    if expand:
        # An expand run mostly lands new evidence on adopted claims: a domain needs a
        # synthesist when any of its claims, new or adopted, gained evidence this run.
        touched = {e.get("claim_id") for e in run.records("edges").values() if e.get("origin") != "adopted"}
        new_claims += [c for cid, c in claims.items() if c.get("origin") == "adopted" and cid in touched
                       and c.get("status") in SYNTHESIS_READY]
    synth_domains = [d for d in domains if any(d in c.get("domain_ids", []) for c in new_claims)]
    synth_done = {n.get("domain_id") for n in run.records("notes").values() if n.get("origin") != "adopted"}
    synth_done |= {p["domain_id"] for p in packets if p["role"] == "synthesist"}
    synth = [_cli(run, f"packet {arg} --domain {q(d)} --role synthesist") for d in synth_domains if d not in synth_done]
    if into:
        publish = [_cli(run, f"merge-plan {arg} --into {q(str(into))}") + "   # dry run: show the user the plan",
                   _cli(run, f"publish {arg} --into {q(str(into))} --apply") + "   # only after approval"]
    else:
        where = f" --brain {q(str(target['brain']))}" if target.get("brain") else " --brain <brain>"
        where += f" --vault {q(str(target['vault'])) if target.get('vault') else '<vault-name>'}"
        publish = [_cli(run, f"publish {arg}{where}") + "   # dry run: confirm name and prefix",
                   _cli(run, f"publish {arg}{where} --apply") + "   # only after the user confirms"]

    # Issued packets with no result yet: the worker died with the session, or is still out.
    dispatch = [f"dispatch a {p['role']} with the saved packet {q(str(p['file']))} (already issued: no new packet, "
                "no new Opus seat)" for p in outstanding]
    then_ingest = [_cli(run, f"ingest {arg}") + "   # after each batch returns"] if dispatch else []
    nxt: list[str] = []
    if manifest.get("closed"):
        stage = "closed"
    elif pending:
        stage = "ingest"
        nxt = [_cli(run, f"ingest {arg}"), _cli(run, f"verify {arg}")]
    elif unverified:
        stage = "verify"
        nxt = [_cli(run, f"verify {arg}"), _cli(run, f"status {arg}")]
    elif not domains:
        stage = "taxonomy"
        nxt = [_cli(run, f"taxonomy {arg} --file <domains.json>") + "   # only after the user approves it"]
    elif manifest.get("published") or _published_before_markers(run):
        stage = "published"
        nxt = [_cli(run, f"close {arg}") + "   # after validate and the brain commit"]
    elif wave == 0 and not packets:
        stage = "ready"
        nxt = [_cli(run, f"wave {arg}") + "   # opens wave 1"]
        if expand:
            nxt += [_cli(run, f"status {arg} --expand") + "   # propose a pick-list; its approval is the gate",
                    _cli(run, f"packet {arg} --expand --pick <approved ids> --domain <domain id>")]
        else:
            nxt += [_cli(run, f"packet {arg} --domain {q(d)}") for d in domains]
    elif any(p["role"] == "synthesist" for p in packets) or synth_done - {""}:
        stage = "synthesis" if synth or dispatch else "publish"
        nxt = (dispatch + synth + then_ingest) or publish
    else:
        issued = [p for p in packets if p["wave"] == wave]
        prospected = {p["domain_id"] for p in issued if p["role"] == "prospector"}
        prospected |= {r.get("domain_id") for r in results if r.get("role") == "prospector" and r.get("wave") == wave}
        if expand:
            targets: list[str] = []
        elif wave == 1:
            targets = [d for d in domains if d not in prospected]
        elif not issued:
            targets = sorted({str(g.get("domain_id")) for g in gaps.values() if g.get("status") == "open"
                              and g.get("impact") in {"critical", "material"} and g.get("domain_id") in domains})
        else:
            targets = []
        extract = [] if expand else _unextracted(run, wave, results, outstanding)
        nxt = dispatch + [_cli(run, f"packet {arg} --domain {q(d)}") for d in targets]
        # the URL is data for the extractor's packet, never part of the command: a
        # shell-quoted comment on one line (a validated URL; see _unextracted)
        nxt += [_cli(run, f"packet {arg} --domain {q(d)} --role extractor") + f"   # url: {q(_one_line(u))}"
                for d, u in extract]
        if nxt:
            stage = f"wave-{wave}" + ("-in-flight" if dispatch else "")
            nxt += then_ingest
        else:
            stage = f"wave-{wave}-done"
            nxt = [_cli(run, f"status {arg}{' --expand' if expand else ''}")
                   + "   # gap and contradiction pass, then the stop rules (operations.md)"]
            if wave < ceiling:
                nxt.append(_cli(run, f"wave {arg}") + f"   # wave {wave + 1}/{ceiling}: only if no stop rule fired")
            nxt += synth or publish
    last = manifest.get("last_command") or {}
    return {
        "run_id": rid,
        "question": manifest.get("question"),
        "stage": stage,
        "wave": wave,
        "last_command": last.get("command"),
        "last_exit": last.get("exit"),
        "last_at": last.get("at"),
        "recovered_commit": run.recovered,
        "ingested_files": len(manifest.get("ingested_files") or {}),
        "pending_inbox": pending,
        "outstanding_packets": [{k: p[k] for k in ("task_id", "role", "domain_id", "wave", "file")}
                                for p in outstanding],
        "next_commands": nxt,
        "rule": ("Nothing ingested is redone: an inbox file is committed once, with its records. Only worker "
                 "output not yet in inbox/ is lost; re-dispatch it from its saved packet."),
    }


def active_update(run: Run, plan: dict[str, Any]) -> dict[str, Any]:
    """Upsert this run in <runs root>/ACTIVE.json. A closed run moves from `open`
    to `closed`; nothing is ever deleted from the file."""
    path = run.root.parent / ACTIVE_FILE
    registry: Any = {}
    if path.is_file():
        try:
            registry = json.loads(_retry(lambda: path.read_text(encoding="utf-8")))
        except ValueError:
            registry = None
        if not (isinstance(registry, dict) and all(isinstance(registry.get(key, []), list)
                                                   for key in ("open", "closed"))):
            # set aside, never deleted; the registry is rebuilt as runs are touched
            path.replace(path.with_name(f"{ACTIVE_FILE}.unreadable-{now().replace(':', '')}"))
            registry = {}
    registry.setdefault("schema_version", "1")
    open_runs = registry.setdefault("open", [])
    closed = registry.setdefault("closed", [])
    manifest = run.manifest
    rid = manifest["run_id"]
    entry = next((e for e in open_runs if e.get("run_id") == rid), None)
    if entry is None and manifest.get("closed"):
        entry = next((e for e in closed if e.get("run_id") == rid), None)
    if entry is None:
        entry = {"run_id": rid, "question": manifest.get("question"), "mode": manifest.get("mode"),
                 "started": manifest.get("created_at")}
        open_runs.append(entry)
    target = manifest.get("target") or {}
    last = manifest.get("last_command") or {}
    entry.update({
        "profile": policy_of(manifest)["profile"],
        "brain": target.get("brain") or manifest.get("brain_root"),
        "vault": target.get("vault") or target.get("into") or manifest.get("adopted_from"),
        "run_dir": str(run.root),
        "status": plan["stage"],
        "last_command": last.get("command"),
        "last_exit": last.get("exit"),
        "next_command": (plan["next_commands"] or [None])[0],
        "updated": now(),
    })
    if manifest.get("closed") and any(e is entry for e in open_runs):
        open_runs[:] = [e for e in open_runs if e is not entry]
        entry["closed"] = manifest["closed"]
        closed.append(entry)
    write_json(path, registry)
    return entry


def mark_published(run: Run, how: str, target: Any, close: bool) -> None:
    """`publish --apply` writes outside the store; the store records that it did,
    and an applied brain publish or clean merge closes the run."""
    manifest = run.manifest
    manifest["published"] = {"at": now(), "how": how, "target": str(target)}
    if close:
        manifest["closed"] = {"at": now(), "by": f"publish ({how}) --apply"}
    run.save_manifest(manifest)


def _changes_state(args: argparse.Namespace) -> bool:
    if args.command in {"status", "show", "resume"}:
        return False
    if args.command == "taxonomy":
        return bool(args.file)
    if args.command == "budget":
        return bool(args.assignments or args.spend)
    if args.command == "policy":
        return bool(args.profile or ((args.allow or args.deny) and args.confirmed))
    return True


def track_step(ctx: dict[str, Any], code: Any) -> None:
    """After every run command: record the step in the manifest and refresh the
    run's ACTIVE.json entry. Never fails the command it follows."""
    args = ctx["args"]
    if args.command != "resume" and not _changes_state(args):
        return
    try:
        run: Run = ctx["run"]
        if not (run.root / "manifest.json").is_file():
            return
        if args.command != "resume":
            manifest = run.manifest
            words, skip = [], False
            for word in ctx["argv"]:
                if skip or word == "--runs-root":
                    skip = not skip
                    continue
                words.append(shlex.quote(word))  # a record of the call, safe to paste back
            manifest["last_command"] = {"command": " ".join(words), "exit": code, "at": now(),
                                        "wave": manifest.get("wave", 0)}
            target = manifest.setdefault("target", {})
            for key in ("brain", "vault", "into"):
                if getattr(args, key, None):
                    target[key] = str(getattr(args, key))
            run.save_manifest(manifest)
        active_update(run, resume_plan(run))
    except (Exception, SystemExit) as exc:  # bookkeeping must never fail the real step
        print(f"warning: run tracking skipped: {exc}", file=sys.stderr)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """The CLI, plus one bookkeeping step after any run command, even a failed one:
    the manifest's `last_command` and the run's ACTIVE.json entry."""
    ctx: dict[str, Any] = {"argv": list(sys.argv[1:] if argv is None else argv)}
    code: Any = 1
    try:
        code = _main(argv, ctx)
        return code
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        raise
    finally:
        if ctx.get("run") is not None:
            track_step(ctx, code)


def _main(argv: list[str] | None, ctx: dict[str, Any]) -> int:
    # Vault text is full of dashes and curly quotes; a console with a legacy single-byte codepage must not be
    # the reason a report cannot be printed.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(prog="research.py", description=__doc__)
    parser.add_argument("--runs-root", type=Path, default=RUNS_ROOT)
    parser.add_argument(
        "--vault-root", type=Path, default=VAULT_ROOT,
        help="parent directory for a fresh scratch vault when `publish` gets no "
             "--brain/--out (default: $RESEARCH_VAULT_ROOT, or ./vault)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create a run")
    p.add_argument("--question", required=True)
    p.add_argument("--mode", default="standard", choices=sorted(MODES))
    p.add_argument("--kind", default="research", choices=sorted(RUN_KINDS))
    p.add_argument("--profile", help=f"source policy profile (default: {DEFAULT_PROFILE})")
    p.add_argument("--max-opus", type=int, dest="max_opus", help="max Opus packets per wave")
    p.add_argument("--read-budget", type=int, dest="read_budget", help="read tokens per agent")
    p.add_argument("--brain", type=Path, help="brain root the run will publish into (recorded only)")
    p.add_argument("--vault", help="vault name it will publish as (recorded only)")

    p = sub.add_parser("resume", help="after any interruption: last step and the exact next commands")
    p.add_argument("run_id", nargs="?", help="run id (default: the newest run)")
    p.add_argument("--run")

    p = sub.add_parser("close", help="mark a run done, in its manifest and in ACTIVE.json")
    p.add_argument("--run")
    p.add_argument("--note", default="")

    p = sub.add_parser("adopt", help="seed an expand run from a published vault")
    p.add_argument("vault", type=Path)
    p.add_argument("--brain", type=Path, help="brain root this vault lives under")
    p.add_argument("--prefix", help="declare the vault prefix instead of inferring it")
    p.add_argument("--profile", help="source policy profile for the expand run")
    p.add_argument("--force", action="store_true", help="re-adopt, rewriting adopted records")

    p = sub.add_parser("vault-index", help="machine-readable index of a published vault")
    p.add_argument("vault", type=Path)
    p.add_argument("--prefix", help="declare the vault prefix instead of inferring it")

    p = sub.add_parser("budget", help="show or change this run's limits; record a worker's tokens")
    p.add_argument("--run")
    p.add_argument("--set", dest="assignments", nargs="+", default=[], metavar="KEY=VALUE")
    p.add_argument("--spend", nargs=2, metavar=("TASK_ID", "TOKENS"),
                   help="record the tokens one worker used, as the harness reported them, "
                        "in reports/token-ledger.jsonl")
    p.add_argument("--role", default="", help="with --spend: the worker's role (default: from its packet)")

    p = sub.add_parser("policy", help="show or change the source policy")
    p.add_argument("--run")
    p.add_argument("--profile", help="switch to a named profile")
    p.add_argument("--allow", help="domain to admit despite the profile")
    p.add_argument("--deny", help="domain to reject despite the profile")
    p.add_argument("--reason", default="")
    p.add_argument("--scope", default="", help="limit the exception to one domain id, e.g. D5")
    p.add_argument("--confirmed", action="store_true", help="apply the exception")

    p = sub.add_parser("datacheck", help="cell-level fidelity check over records/tables.json")
    p.add_argument("--run")

    p = sub.add_parser("deviation", help="record a deliberate departure from the default route")
    p.add_argument("--run")
    p.add_argument("--step", default="")
    p.add_argument("--note", required=True)

    p = sub.add_parser("taxonomy", help="set or show the domain taxonomy")
    p.add_argument("--run")
    p.add_argument("--file", type=Path, help="JSON with {\"domains\": [...]}")

    p = sub.add_parser("ingest", help="ingest inbox results (the only path from workers into the run store)")
    p.add_argument("--run")

    p = sub.add_parser("packet", help="emit a bounded assignment packet")
    p.add_argument("--run")
    p.add_argument("--domain", help="domain id; with --expand, the domain the results belong to")
    p.add_argument("--role", default="prospector")
    p.add_argument("--limit", type=int, default=80)
    p.add_argument("--expand", action="store_true", help="item-scoped expand packet")
    p.add_argument("--pick", default="", help="comma separated gap/claim ids for --expand; a bare "
                                              "domain id (D2) picks every open item of that domain")
    p.add_argument("--brief", default="framed", choices=BRIEFS,
                   help="discovery arm: framed (default), or a blind / frame-break prospector that "
                        "follows --brief-text and never sees the skeleton")
    p.add_argument("--brief-text", dest="brief_text", default="",
                   help="the coordinator's own brief for this arm, copied into the packet")
    p.add_argument("--searches", type=int,
                   help="search ceiling written into the packet as max_searches (default for a prospector: "
                        f"the run's searches_per_agent; at least {TARGETED_SEARCHES} for an expand packet "
                        "or from wave 2 on)")
    p.add_argument("--force", action="store_true",
                   help="emit the packet even past max_subagent_tokens; logged as a deviation")

    p = sub.add_parser("relabel-edge", help="correct one edge's relation, with an audit trail; then verify")
    p.add_argument("--run")
    p.add_argument("--edge", required=True, help="edge id, e.g. EV-012")
    p.add_argument("--relation", required=True, choices=sorted(RELATIONS))
    p.add_argument("--why", required=True, help="why the relation was wrong for this claim text")

    p = sub.add_parser("coverage", help="coverage numbers: arm overlap, off-skeleton rate, novelty, re-read recall")
    p.add_argument("--run")
    p.add_argument("--sample", type=int, help="draw N cached pages for a claim-blind re-read")
    p.add_argument("--seed", type=int, default=0, help="seed of the --sample draw (default 0)")
    p.add_argument("--reread", type=Path,
                   help='re-read findings matched to claims: {"seed": S, "pages": [{"source_id": …, '
                        '"findings": [{"text", "importance": high|medium|low, "matched_claim": id|null}]}]}')

    p = sub.add_parser("verify", help="check quotes against cache, recompute claim status")
    p.add_argument("--run")

    p = sub.add_parser("status", help="claim, source, gap and budget summary")
    p.add_argument("--run")
    p.add_argument("--expand", action="store_true", help="ranked open surface of an adopted vault")
    p.add_argument("--budget", action="store_true", help="budget block only")

    p = sub.add_parser("show", help="print full records by ID")
    p.add_argument("--run")
    p.add_argument("--id", nargs="+", required=True)

    p = sub.add_parser("wave", help="advance the wave counter")
    p.add_argument("--run")

    p = sub.add_parser("merge-plan", help="dry run: the whole write set as five verbs")
    p.add_argument("--run")
    p.add_argument("--into", type=Path, required=True, help="the vault to merge into")
    p.add_argument("--brain", type=Path, help="brain root for the basename collision check")
    p.add_argument("--no-brain", action="store_true", help="skip the brain-wide collision check")

    p = sub.add_parser("publish", help="write a vault: fresh, merged, or into the brain")
    p.add_argument("--run")
    p.add_argument("--out", type=Path, help="write a whole new vault into this directory")
    p.add_argument("--into", type=Path, help="merge into an existing vault")
    p.add_argument("--brain", type=Path, help="brain root")
    p.add_argument("--vault", help="new vault name (kebab) under the brain root; "
                                   "`group/name` publishes into an existing group folder")
    p.add_argument("--prefix", help="override the derived Title Case prefix")
    p.add_argument("--when", help="the router row's 'when to look here' text")
    p.add_argument("--apply", action="store_true", help="actually write (default: dry run)")
    p.add_argument("--no-brain", action="store_true", help="skip the brain-wide collision check")

    p = sub.add_parser("validate", help="graph and policy gate over a published vault")
    p.add_argument("vault", type=Path, nargs="?")
    p.add_argument("--brain", type=Path,
                   help="brain root: alone, validate every vault in it (grouped vaults included); "
                        "with a vault, resolve that vault's links across it")
    p.add_argument("--no-report", action="store_true", help="do not write the Validation Report")
    p.add_argument("--profile", choices=("vault", "field", "first-hand"), default="vault",
                   help="'first-hand' (alias 'field') checks first-hand areas (field-notes/, vaults whose "
                        "Home says vault_kind: first-hand) with their own schema; never writes")

    p = sub.add_parser("lint-brain", help="duplicate basenames and prefix violations, brain-wide")
    p.add_argument("--brain", type=Path)

    p = sub.add_parser("link-rewrite", help="exact-match basename rewrites (dry run by default)")
    p.add_argument("--map", dest="map_file", type=Path, required=True,
                   help='JSON {"old basename": "new basename"}')
    p.add_argument("--root", type=Path, required=True, help="vault or brain root to rewrite in")
    p.add_argument("--apply-one", dest="apply_one", type=Path, help="rewrite exactly one file first")
    p.add_argument("--apply", action="store_true", help="rewrite the rest")

    p = sub.add_parser("router-row", help="print the proposed router row and stop")
    p.add_argument("--brain", type=Path)
    p.add_argument("--vault", required=True, help="vault name, or `group/name` for a grouped vault")
    p.add_argument("--prefix")
    p.add_argument("--when", default="")

    args = parser.parse_args(argv)
    ctx["args"] = args
    runs_root: Path = args.runs_root
    runs_root.mkdir(parents=True, exist_ok=True)
    vault_root: Path = args.vault_root

    if args.command == "init":
        overrides: dict[str, Any] = {}
        if args.max_opus is not None:
            overrides["max_opus_per_wave"] = args.max_opus
        if args.read_budget is not None:
            overrides["read_tokens_per_agent"] = args.read_budget
        run = Run.create(
            args.question, args.mode, runs_root, kind=args.kind,
            overrides=overrides, profile=args.profile,
        )
        ctx["run"] = run
        manifest = run.manifest
        print(json.dumps({"run_id": run.root.name, "run_dir": str(run.root),
                          "inbox": str(run.inbox), "cache": str(run.cache / "raw"),
                          "kind": manifest.get("kind"), "policy": policy_of(manifest),
                          "limits": limits_of(manifest)}, indent=2))
        return 0

    if args.command == "validate":
        # `validate <vault> --brain <root>` checks that one vault, with links
        # resolving across <root>; only `validate [--brain <root>]` without a
        # vault checks the whole brain.
        if args.profile in ("field", "first-hand") or (
            args.vault is not None and is_first_hand_area(args.vault.resolve())
        ):
            if args.vault is not None:
                brain = args.brain.resolve() if args.brain else None
                report = validate_first_hand_area(args.vault.resolve(), brain)
            else:
                brain = resolve_brain_root(args.brain)
                areas = [validate_first_hand_area(d, brain) for d in brain_vault_dirs(brain)
                         if brain_rel(brain, d) not in FLAT_DIRS and is_first_hand_area(d)]
                report = {"brain": str(brain), "profile": "first-hand", "areas": areas,
                          "valid": all(a["valid"] for a in areas)}
            print(json.dumps(report, indent=2, ensure_ascii=False))
            return 0 if report["valid"] else 1
        if args.vault is None:
            brain = resolve_brain_root(args.brain)
            report = validate_brain(brain)
            print(json.dumps(report, indent=2, ensure_ascii=False))
            return 0 if report["valid"] else 1
        brain = resolve_brain_root(args.brain) if args.brain else None
        report = validate_vault(args.vault.resolve(), write_report=not args.no_report, brain=brain)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report["valid"] else 1

    if args.command == "lint-brain":
        report = lint_brain(resolve_brain_root(args.brain))
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report["clean"] else 1

    if args.command == "link-rewrite":
        mapping = read_json(args.map_file)
        if not isinstance(mapping, dict):
            die("--map must be a JSON object of {old basename: new basename}")
        report = link_rewrite(
            args.root.resolve(),
            {str(k): str(v) for k, v in mapping.items()},
            apply_one=args.apply_one,
            apply_all=args.apply,
        )
        # The line-by-line review goes to stderr so stdout stays machine-readable.
        for hit in report["hits"]:
            print(f"{hit['file']}:{hit['line']}: {hit['before']}", file=sys.stderr)
            print(f"{' ' * len(hit['file'])}  → {hit['after']}", file=sys.stderr)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report["reconciled"] else 1

    if args.command == "router-row":
        # `plants/pothos-cuttings`: the row keeps the group path, the prefix
        # comes from the last segment
        vault_path, name = split_vault_path(args.vault)
        prefix = args.prefix or prefix_from_vault_name(name)
        row = router_row(vault_path, args.when or "TODO: describe when to open this vault",
                         f"{home_basename(prefix)}.md")
        anchor = existing = None
        brain = None
        if args.brain or any(os.environ.get(var) for var in BRAIN_ENV_VARS):
            brain = resolve_brain_root(args.brain)
            anchor = find_router_anchor(brain / "README.md", vault_path)
            existing = find_router_row(brain / "README.md", vault_path)
        out: dict[str, Any] = {
            "vault": vault_path,
            "prefix": prefix,
            "row": row,
            "anchor_found": anchor is not None,
            "instruction": (
                "insert this row directly after the anchor line" if anchor
                else "the router table was not exactly locatable; paste this row by hand"
            ),
            "anchor": anchor,
        }
        if existing is not None and brain is not None:
            # The vault is already routed: never a second row. Its counts come from
            # the published vault; the coordinator replaces the line with row_update.
            vault_dir = brain.joinpath(*vault_path.split("/"))
            counts = vault_counts(vault_dir, prefix) if has_vault_markers(vault_dir) else None
            update = refresh_row_counts(existing, counts) if counts else None
            out.update({
                "row_exists": True,
                "existing_row": existing,
                "current_counts": counts,
                "row_update": update,
                "instruction": (
                    "the vault already has a router row: replace existing_row with row_update "
                    "(one line changed, nothing inserted)" if update and update != existing
                    else "the vault already has a router row and its counts are current; change nothing"
                    if update else
                    "the vault already has a router row, but its counts could not be regenerated "
                    "(no published vault there, or a row without a 'when' cell); edit it by hand"
                ),
            })
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0

    if args.command == "adopt":
        report = adopt_vault(
            args.vault.resolve(), runs_root, brain=args.brain,
            prefix_override=args.prefix, force=args.force, profile=args.profile,
        )
        ctx["run"] = Run(Path(report["run_dir"]))
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0

    if args.command == "vault-index":
        print(json.dumps(vault_index(args.vault.resolve(), args.prefix), indent=2, ensure_ascii=False))
        return 0

    run = Run.open(getattr(args, "run", None) or getattr(args, "run_id", None), runs_root)
    ctx["run"] = run

    if args.command == "resume":
        print(json.dumps(resume_plan(run), indent=2, ensure_ascii=False))
        return 0

    if args.command == "close":
        manifest = run.manifest
        if not manifest.get("closed"):  # a run `publish --apply` closed keeps that record
            manifest["closed"] = {"at": now(), "by": "close", "note": args.note}
            run.save_manifest(manifest)
        print(json.dumps({"run_id": manifest["run_id"], "closed": manifest["closed"]}, indent=2))
        return 0

    if args.command == "taxonomy":
        if args.file:
            payload = read_json(args.file)
            domains = payload.get("domains") if isinstance(payload, dict) else payload
            if not isinstance(domains, list) or not domains:
                die("taxonomy file must contain a non-empty 'domains' list")
            seen = set()
            for index, domain in enumerate(domains, start=1):
                if not isinstance(domain, dict):
                    die(f"domain {index} must be an object with an id and a name")
                domain.setdefault("id", f"D{index}")
                if not domain.get("name"):
                    die(f"domain {domain['id']} has no name")
                # The id becomes part of task ids and packet file names, the name
                # a vault folder (`01 <name>`): neither may climb out of its folder.
                if not isinstance(domain["id"], str) or not DOMAIN_ID.fullmatch(domain["id"]):
                    die(f"domain id {domain['id']!r}: 1-32 letters, digits, _ and - only")
                problem = (path_component_problem(f"01 {domain['name']}")
                           or path_component_problem(f"01 {domain['name']} MOC.md")
                           if isinstance(domain["name"], str) else "not text")
                if problem:
                    die(f"domain {domain['id']} name {domain['name']!r} cannot be a folder name: {problem}")
                if domain["id"] in seen:
                    die(f"duplicate domain id {domain['id']}")
                seen.add(domain["id"])
                domain.setdefault("questions", [])
                domain.setdefault("description", "")
                domain.setdefault("status", "pending")
                domain.setdefault("kind", run.manifest.get("kind", "research"))
                if domain["kind"] not in RUN_KINDS:
                    die(f"domain {domain['id']} kind must be one of {', '.join(sorted(RUN_KINDS))}")
            write_json(run.root / "taxonomy.json", {"domains": domains})
        print(json.dumps(run.taxonomy, indent=2))
        return 0

    if args.command == "ingest":
        pending = sorted(run.inbox.glob("*.json"))
        if not pending:
            print(json.dumps({"ingested": 0, "message": "inbox is empty"}, indent=2))
            return 0
        # Readiness gate: a file a worker may still be writing is left in place,
        # logged, and picked up by the next `ingest`. Never rejected, never moved.
        not_ready = []
        for path in list(pending):
            reason = inbox_not_ready(path)
            if reason is None:
                continue
            pending.remove(path)
            task = str(_payload(path).get("task_id") or path.stem)
            not_ready.append({"file": path.name, "task_id": task, "why": reason})
        if not_ready:
            log = read_jsonl(run.reports / "ingest-log.jsonl")
            log += [{"task_id": item["task_id"], "skipped": "not-ready", "file": item["file"],
                     "why": item["why"], "at": now()} for item in not_ready]
            write_jsonl(run.reports / "ingest-log.jsonl", log)
        manifest = run.manifest
        done = run.inbox / "_ingested"
        done.mkdir(exist_ok=True)
        summary = []
        for path in pending:
            # One commit per file (records, counters, marker): a later file failing
            # never rolls back IDs already handed out, and a file whose commit
            # landed before a crash (marker present, never moved) is only moved,
            # never ingested twice. A deliberate re-drop (e.g. after `policy
            # --allow`, copied or moved back) is marked moved: ingested again.
            digest = file_digest(path)
            landed = next((name for name, mark in (manifest.get("ingested_files") or {}).items()
                           if mark.get("digest") == digest and not mark.get("moved")
                           and not (done / name).exists()), None)
            if landed:
                summary.append({"file": path.name, "skipped": "committed before an interruption; moved only"})
            else:
                landed = ingested_name(run, path, digest)
                stats = ingest_file(run, path, manifest)
                summary.append({"file": path.name, **stats})
            shutil.move(str(path), str(done / landed))
            manifest["ingested_files"][landed]["moved"] = True
            run.save_manifest(manifest)
        result: dict[str, Any] = {"ingested": len(summary), "files": summary}
        if not_ready:
            result["not_ready"] = not_ready
            result["message"] = (f"{len(not_ready)} inbox file(s) not ready (written less than "
                                 f"{INGEST_MIN_AGE:g} s ago, or not yet valid JSON); left in place: "
                                 "run ingest again")
        print(json.dumps(result, indent=2))
        return 0

    if args.command == "packet":
        manifest = run.manifest
        for label, value in (("domain id", args.domain), ("role", args.role)):
            if value and not DOMAIN_ID.fullmatch(value):  # both become part of a file name
                die(f"{label} {value!r}: letters, digits, _ and - only")
        if args.expand:
            picks = [pick for pick in args.pick.split(",") if pick.strip()]
            if not picks:
                die("--expand needs --pick GAP-003,CL-017 (or a domain id: --pick D2)")
            packet = build_expand_packet(run, picks, args.role, args.limit, manifest,
                                         domain_id=args.domain or "")
        else:
            if not args.domain:
                die("packet needs --domain, or --expand --pick …")
            packet = build_packet(run, args.domain, args.role, args.limit, brief=args.brief)
        # The arm travels with the packet; the worker copies `brief` into its result.
        packet["brief"] = args.brief
        packet["brief_text"] = args.brief_text
        # The prospector's search ceiling travels in the packet, not in its definition.
        if args.searches is not None or args.role == "prospector":
            searches = args.searches
            if searches is None:
                searches = limits_of(manifest)["searches_per_agent"]
                if args.expand or int(manifest.get("wave", 0)) >= 2:
                    searches = max(searches, TARGETED_SEARCHES)
            if searches < 1:
                die("--searches must be a positive number")
            packet["max_searches"] = searches
            packet["budget"]["searches_per_agent"] = searches
        # Gated and charged only once the packet is known to be buildable.
        token_gate(run, run.manifest, args.force)
        charge_opus(run, run.manifest, args.role)
        packet = record_packet(run, packet, args.role,
                               packet.get("domain_id", "") if args.expand else args.domain,
                               picks if args.expand else [], brief=args.brief,
                               scope="expand" if args.expand else "")
        print(json.dumps(packet, indent=2, ensure_ascii=False))
        return 0

    if args.command == "coverage":
        reread = None
        if args.sample is not None and args.sample < 1:
            die("--sample must be a positive number of pages")
        if args.reread is not None:
            if not args.reread.is_file():
                die(f"missing file: {args.reread}")
            try:
                reread = json.loads(args.reread.read_text(encoding="utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                print(json.dumps({"valid": False, "problems": [f"not JSON: {exc}"]}, indent=2))
                return 1
            problems = validate_reread(reread, run.records("claims"), run.records("sources"))
            if problems:
                print(json.dumps({"valid": False, "problems": problems}, indent=2, ensure_ascii=False))
                return 1
        report = coverage_report(run, sample=args.sample, seed=args.seed, reread=reread)
        print_coverage(report)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0

    if args.command == "verify":
        report = verify_run(run)
        manifest = run.manifest
        manifest["verified_through"] = manifest.get("ingest_commits", 0)  # written after the records
        run.save_manifest(manifest)
        print(json.dumps(report, indent=2))
        return 0

    if args.command == "relabel-edge":
        print(json.dumps(relabel_edge(run, args.edge.strip(), args.relation, args.why),
                         indent=2, ensure_ascii=False))
        return 0

    if args.command == "datacheck":
        report = datacheck_run(run)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 1 if report["results"]["mismatch"] else 0

    if args.command == "status":
        if args.expand:
            print(json.dumps(build_expand_surface(run), indent=2, ensure_ascii=False))
            return 0
        if args.budget:
            print(json.dumps({**budget_block(run, run.manifest), **token_usage(run, run.manifest)}, indent=2))
            return 0
        print(json.dumps(build_status(run), indent=2))
        return 0

    if args.command == "budget":
        manifest = run.manifest
        limits = limits_of(manifest)
        changed = {}
        for assignment in args.assignments:
            key, _, value = assignment.partition("=")
            key = key.strip()
            if key not in DEFAULT_LIMITS:
                die(f"unknown limit {key!r}; known: {', '.join(sorted(DEFAULT_LIMITS))}")
            try:
                limits[key] = int(value)
            except ValueError:
                die(f"limit {key} must be an integer, got {value!r}")
            changed[key] = limits[key]
        if changed:
            manifest["limits"] = limits
            run.save_manifest(manifest)
        spent = None
        if args.spend:
            task_id, value = args.spend[0].strip(), args.spend[1]
            try:
                tokens = int(value)
            except ValueError:
                die(f"--spend tokens must be an integer, got {value!r}")
            if not task_id or tokens < 0:
                die("--spend needs a task id and a non-negative token count")
            spent = {"task_id": task_id, "role": args.role or task_role(run, task_id, manifest),
                     "tokens": tokens, "at": now(), "via": "spend"}
            write_jsonl(run.reports / TOKEN_LEDGER, read_jsonl(run.reports / TOKEN_LEDGER) + [spent])
        out = {"changed": changed, **budget_block(run, run.manifest), **token_usage(run, run.manifest)}
        if spent:
            out["recorded"] = spent
        print(json.dumps(out, indent=2))
        return 0

    if args.command == "policy":
        manifest = run.manifest
        profiles = load_profiles()
        policy = policy_of(manifest)
        if args.profile:
            resolve_profile(profiles, args.profile)
            policy["profile"] = args.profile
            manifest["policy"] = policy
            run.save_manifest(manifest)
        if args.allow or args.deny:
            domain = (args.allow or args.deny).strip().lower().lstrip(".")
            # a host name only: no scheme, credentials, port or path, and a public one
            if not re.fullmatch(r"[^/:@\s]+", domain):
                problem = "not a bare host name"
            elif "." not in domain:  # a whole public suffix (`edu`) is a valid exception
                problem = None if TOP_LABEL.fullmatch(domain) and "." + domain not in LOCAL_SUFFIXES \
                    and domain != "localhost" else "not a public host name or suffix"
            else:
                problem = host_problem(domain)
            if problem:
                die(f"policy exception for {_redact_url(domain)!r}: {problem}; give a public host name "
                    "such as forum.example")
            proposal = {
                "action": "allow" if args.allow else "deny",
                "domain": domain,
                "reason": args.reason,
                "scope": args.scope,
                "profile": policy["profile"],
                "added_at": now(),
            }
            if not args.reason:
                die("an exception needs --reason; it is published in the Source Register")
            if not args.confirmed:
                print(
                    json.dumps(
                        {
                            "proposed_exception": proposal,
                            "applied": False,
                            "effect": (
                                f"{proposal['action']} {proposal['domain']}"
                                + (f" within {proposal['scope']}" if proposal["scope"] else " run-wide")
                                + f", overriding profile '{policy['profile']}'"
                            ),
                            "confirm_with": "re-run the same command with --confirmed",
                        },
                        indent=2,
                        ensure_ascii=False,
                    )
                )
                return 3
            policy["exceptions"] = [
                exc for exc in policy["exceptions"]
                if not (exc.get("domain") == proposal["domain"] and exc.get("scope", "") == proposal["scope"])
            ] + [proposal]
            manifest["policy"] = policy
            run.save_manifest(manifest)
        print(json.dumps(source_policy_block(run.manifest), indent=2, ensure_ascii=False))
        return 0

    if args.command == "deviation":
        manifest = run.manifest
        entry = {"at": now(), "wave": manifest.get("wave", 0), "step": args.step, "note": args.note}
        manifest.setdefault("deviations", []).append(entry)
        run.save_manifest(manifest)
        print(json.dumps({"deviations": manifest["deviations"]}, indent=2, ensure_ascii=False))
        return 0

    if args.command == "show":
        out = {}
        pools = {prefix: run.records(kind) for kind, prefix in ID_PREFIX.items()}
        for record_id in args.id:
            prefix = record_id.split("-")[0]
            pool = pools.get(prefix, {})
            out[record_id] = pool.get(record_id, "not found")
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0

    if args.command == "wave":
        manifest = run.manifest
        manifest["wave"] += 1
        ceiling = manifest["limits"]["waves"]
        run.save_manifest(manifest)
        over = manifest["wave"] > ceiling
        print(json.dumps({"wave": manifest["wave"], "ceiling": ceiling, "over_budget": over}, indent=2))
        return 1 if over else 0

    if args.command in {"merge-plan", "publish"} and getattr(args, "into", None):
        vault = args.into.resolve()
        brain: Path | None = None
        if not args.no_brain:
            own = _brain_of(vault)  # the parent brain root, or the brain above a group folder
            brain = resolve_brain_root(args.brain) if args.brain or own is None else own
        plan = plan_merge(run, vault, brain)
        write_json(run.reports / "merge-plan.json", plan)
        print_merge_plan(plan)  # the human-readable table goes to stderr
        if plan["hard_stops"]:
            print(f"\nexit 2: {len(plan['hard_stops'])} hard stop(s); nothing was written.",
                  file=sys.stderr)
            print(json.dumps({
                "applied": False, "hard_stops": plan["hard_stops"],
                "collisions": plan["collisions"], "summary": plan["summary"],
                "plan": str(run.reports / "merge-plan.json"),
            }, indent=2, ensure_ascii=False))
            return 2
        if args.command == "merge-plan" or not args.apply:
            print(json.dumps({
                "applied": False,
                "plan": str(run.reports / "merge-plan.json"),
                "summary": plan["summary"],
                "manual": plan["manual"],
                "needs_rewrite": plan["needs_rewrite"],
                "baseline_errors": plan["baseline_errors"],
                "apply_with": f"publish --run {shlex.quote(run.root.name)} --into {shlex.quote(str(vault))} --apply",
            }, indent=2, ensure_ascii=False))
            return 0
        stored = read_json(run.reports / "merge-plan.json")
        result = apply_merge(stored, vault)
        after = validate_vault(vault)
        regressed = len(after["errors"]) > result.get("baseline_errors", 0)
        result["validation"] = {
            "valid": after["valid"],
            "errors": len(after["errors"]),
            "errors_before_merge": result.get("baseline_errors", 0),
            "regressed": regressed,
            "warnings": len(after["warnings"]),
            "detail": after["errors"][:20],
        }
        result["needs_rewrite"] = stored["needs_rewrite"]
        result["open_gaps"] = stored["open_gaps"]
        mark_published(run, "merge", vault, close=not regressed)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 1 if regressed else 0

    if args.command == "publish":
        if args.vault:
            brain = resolve_brain_root(args.brain)
            report = publish_to_brain(
                run, brain, args.vault, args.prefix, apply=args.apply, when=args.when
            )
            if report.get("applied"):
                mark_published(run, "brain", report["target"], close=True)
            print(json.dumps(report, indent=2, ensure_ascii=False))
            return 0 if not report["blocked"] else 2
        slug = slugify(run.manifest["question"], 50)
        out = (args.out or (vault_root / slug)).resolve()
        if args.out is None:
            # A scratch vault inside a vault root leaves an untracked folder and duplicate
            # basenames there, which block the later `publish --brain`. Keep it in the run.
            inside = next((p for p in (out.parent, *out.parent.parents) if is_brain_root(p)), None)
            if inside is not None:
                out = (run.root / "vault" / slug).resolve()
                print(f"note: {inside} is a vault root; the scratch vault goes to {out} instead. "
                      "Publish into the vault root with --brain <root> --vault <name>.", file=sys.stderr)
        prefix = args.prefix or prefix_from_vault_name(out.name)
        report = publish_fresh(run, out, prefix)
        mark_published(run, "fresh", out, close=False)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report["valid"] else 1

    die(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
