"""Versioned observations. Retained objects are authoritative; local files are an index."""

import base64
import csv
import hashlib
import html
import io
import json
import os
import re
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote_plus, urlsplit
from uuid import uuid4

KINDS = {"profile", "export", "list", "contacts"}
COMPLETENESS = {"complete", "partial", "privacy-filtered", "extracted-only", "legacy-parsed-only"}
EXPORT_SOURCES = ("instagram", "facebook", "snapchat", "linkedin")


def encode(value) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def code_fingerprint() -> str:
    """Hash relative names and installed Python source bytes, without requiring git."""
    root = Path(__file__).parent
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def build_capture(
    source,
    kind,
    payload,
    *,
    record_id=None,
    captured_at=None,
    completeness="partial",
    exclusions=(),
) -> dict:
    payload_bytes = encode(payload)
    return validate(
        {
            "schema_version": 1,
            "capture_id": uuid4().hex,
            "source": source,
            "kind": kind,
            "record_id": record_id,
            "captured_at": captured_at
            if captured_at is not None
            else datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "collector_fingerprint": code_fingerprint(),
            "completeness": completeness,
            "exclusions": list(exclusions),
            "payload": json.loads(payload_bytes),
            "payload_sha256": hashlib.sha256(payload_bytes).hexdigest(),
        }
    )


def validate(capture) -> dict:
    """Validate the envelope, not the source parser's success. Never echo input on failure."""
    try:
        c = capture
        _require(isinstance(c, dict) and type(c["schema_version"]) is int)
        _require(c["schema_version"] == 1)
        _require(re.fullmatch(r"[a-z][a-z0-9_]{0,63}", c["source"]))
        _require(c["kind"] in KINDS and c["completeness"] in COMPLETENESS)
        _require(re.fullmatch(r"[0-9a-f]{32}", c["capture_id"]))
        _require(c["record_id"] is None or isinstance(c["record_id"], str))
        _require(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", c["captured_at"]))
        datetime.fromisoformat(c["captured_at"])
        _require(re.fullmatch(r"[0-9a-f]{64}", c["collector_fingerprint"]))
        _require(isinstance(c["exclusions"], list))
        _require(all(isinstance(e, str) for e in c["exclusions"]))
        p = c["payload"]
        _require(isinstance(p, dict))
        if c["kind"] == "profile":
            _require("eval" in p and isinstance(p.get("captured"), list))
            _require(all(isinstance(item, dict) for item in p["captured"]))
        if c["kind"] == "contacts":
            from people_sync.sources import CONTACT_POLICY, validate_contacts

            validate_contacts(c["source"], p)
            _require(c["record_id"] is None)
            _require(c["exclusions"] == [CONTACT_POLICY])
            _require(c["completeness"] == ("privacy-filtered" if p["complete"] else "partial"))
        if c["kind"] == "export":
            _require(c["source"] in EXPORT_SOURCES)
            _require(isinstance(p.get("files"), list) and p["files"])
            roles = []
            for file in p["files"]:
                _require(file["encoding"] == "base64" and file["format"] in {"json", "csv"})
                _require(isinstance(file["filename"], str) and file["filename"])
                _require(file["filename"] not in {".", ".."})
                _require(not any(char in file["filename"] for char in ("/", "\\", "\0")))
                _check_export_privacy(c["source"], base64.b64decode(file["data"], validate=True))
                _check_export_value(file["filename"])
                roles.append(file["role"])
            _require(
                sorted(roles)
                == (["followers", "following"] if c["source"] == "instagram" else ["export"])
            )
            _require(
                all(
                    file["format"] == ("csv" if c["source"] == "linkedin" else "json")
                    for file in p["files"]
                )
            )
        _require(hashlib.sha256(encode(p)).hexdigest() == c["payload_sha256"])
        encode(c)
    except (KeyError, TypeError, ValueError, RecursionError, StopIteration, csv.Error):
        raise ValueError("invalid capture envelope or payload checksum") from None
    return c


def _require(condition):
    if not condition:
        raise ValueError("invalid capture")


def _check_export_value(value, field=""):
    """Reject contact-like or ambiguous values; never redact or rewrite source text."""
    if isinstance(value, dict):
        for key, item in value.items():
            _check_export_value(key)
            _check_export_value(item, key)
        return
    if isinstance(value, list):
        for item in value:
            _check_export_value(item)
        return
    if value is None or isinstance(value, bool):
        return
    if field == "timestamp" and isinstance(value, (int, float)):
        return  # Export epoch timestamps are typed metadata, not contact numbers.
    text = str(value)
    if field in {"Connected On", "Creation Timestamp", "Last Modified Timestamp"}:
        for fmt in ("%d %b %Y", "%Y-%m-%d %H:%M:%S UTC", "%Y-%m-%d"):
            try:
                datetime.strptime(text, fmt)
                return
            except ValueError:
                pass
    # Decode only an inspection copy. Nested encodings cannot bypass the check.
    for _ in range(4):
        # Inspect original components and each decoding layer without splitting at
        # whitespace: an encoded space must not hide a query or fragment suffix.
        for match in re.finditer(r"\b[a-z][a-z0-9+.-]*://", text, re.I):
            parsed = urlsplit(text[match.start() :])
            _require(not (parsed.username or parsed.query or parsed.fragment))
        decoded = unicodedata.normalize("NFKC", html.unescape(unquote_plus(text)))
        decoded = "".join(c for c in decoded if unicodedata.category(c) != "Cf")
        if decoded == text:
            break
        text = decoded
    else:
        raise ValueError("export privacy boundary could not be established")
    _require(not re.search(r"\S+\s*(?:@|\[at\]|\(at\))\s*\S+|\d(?:[\W_]*\d){6}", text, re.I))
    _require(
        not re.search(r"\b(?:mailto|tel|sms|phone|address)\s*:|\bp\.?\s*o\.?\s*box\b", text, re.I)
    )
    # Numbered prose may be an address even without a familiar street suffix.
    # Refuse that ambiguity rather than stripping useful names/professional context.
    words = re.sub(r"[\W_]+", " ", text)
    _require(not re.search(r"\b\d{1,6}[a-z]?\s+[^\W\d_]", words, re.I))
    _require(
        not re.search(
            r"\b(?:street|st|road|rd|avenue|ave|boulevard|blvd|lane|ln|way|drive|dr|court|ct|rue|calle|strasse|straße)\.?\s+\d",
            words,
            re.I,
        )
    )


def _check_export_privacy(source: str, data: bytes) -> None:
    """The shared retention/replay boundary checks every retained value, including URLs."""
    if source != "linkedin":
        _check_export_value(json.loads(data))
        return
    rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig")), strict=True))
    start = next(i for i, row in enumerate(rows) if row[:1] == ["First Name"])
    _check_export_value(rows[: start + 1])
    for row in rows[start + 1 :]:
        for i, value in enumerate(row):
            _check_export_value(value, rows[start][i] if i < len(rows[start]) else "")


def state_directory(state_dir=None) -> Path:
    if state_dir is not None:
        return Path(state_dir)
    return Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "people-sync"


def private_path(path) -> Path:
    """Private artifacts must not land in a checkout or through a symlink."""
    path = Path(path).absolute()
    for parent in (path, *path.parents):
        if parent.is_symlink() or (parent / ".git").exists():
            raise ValueError("private output requires a path outside source control")
    return path


def write_private(path, data: bytes) -> None:
    path = private_path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".capture-", delete=False) as f:
            temporary = Path(f.name)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def retain(capture, *, state_dir=None) -> str:
    """Upload, verify the entire read-back, then atomically cache one file per observation."""
    from people_sync import photos

    try:
        c = validate(capture)
        body = encode(c)
        key = f"profiles/{c['source']}/captures/{c['capture_id']}.json"
        path = private_path(state_directory(state_dir) / "captures" / f"{c['capture_id']}.json")
        photos.put_object(key, body, content_type="application/json")
        recovered = photos.get_object(key)
        if recovered != body or hashlib.sha256(recovered).digest() != hashlib.sha256(body).digest():
            raise ValueError("read-back mismatch")
        write_private(path, body)
    except Exception:
        raise photos.ArchiveError("raw archive failed") from None
    return key


def _filter_export(source: str, data: bytes) -> bytes:
    """Version 1 structural allowlists. Preserve row positions, including malformed rows."""
    if source == "linkedin":
        rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
        start = next(i for i, row in enumerate(rows) if row[:1] == ["First Name"])
        allowed = {"First Name", "Last Name", "URL", "Company", "Position", "Connected On"}
        indices = [i for i, name in enumerate(rows[start]) if name in allowed]
        safe = [
            [row[i] if i < len(row) else "" for i in indices] if row else [] for row in rows[start:]
        ]
        if safe == rows:
            return data
        output = io.StringIO(newline="")
        csv.writer(output).writerows(safe)
        return output.getvalue().encode("utf-8")
    obj = json.loads(data)
    key = {"instagram": "relationships_following", "facebook": "friends_v2", "snapchat": "Friends"}[
        source
    ]
    entries = obj if source == "instagram" and isinstance(obj, list) else obj[key]
    _require(isinstance(entries, list))
    allowed = {
        "facebook": {"name", "timestamp"},
        "snapchat": {
            "Username",
            "Display Name",
            "Creation Timestamp",
            "Last Modified Timestamp",
            "Source",
        },
    }

    def fields(entry, keys):
        return (
            {
                k: v
                for k, v in entry.items()
                if k in keys and (v is None or isinstance(v, (str, int, float, bool)))
            }
            if isinstance(entry, dict)
            else {}
        )

    safe = []
    for entry in entries:
        if source == "instagram":
            items = entry.get("string_list_data", []) if isinstance(entry, dict) else []
            safe.append(
                {
                    "string_list_data": [
                        fields(item, {"href", "value", "timestamp"}) for item in items
                    ]
                }
                if isinstance(items, list)
                else {}
            )
        else:
            safe.append(fields(entry, allowed[source]))
    filtered = safe if isinstance(obj, list) else {key: safe}
    return data if filtered == obj else encode(filtered)


def capture_export(source: str, path) -> dict:
    """Capture supported file inputs after structural privacy filtering, before field parsing."""
    _require(source in EXPORT_SOURCES)
    path = Path(path)
    inputs = (
        [(role, path / f"{role}.json") for role in ("followers", "following")]
        if source == "instagram"
        else [("export", path)]
    )
    files = []
    for role, file in inputs:
        original = file.read_bytes()
        safe = _filter_export(source, original)
        files.append(
            {
                "role": role,
                "filename": file.name,
                "format": "csv" if source == "linkedin" else "json",
                "encoding": "base64",
                "data": base64.b64encode(safe).decode("ascii"),
                "verbatim": safe == original,
            }
        )
    return build_capture(
        source,
        "export",
        {"files": files},
        completeness="privacy-filtered",
        exclusions=[
            f"{source}-export-allowlist-v2: only relationship identity, names, timestamps and professional fields; "
            "other fields, preambles and unrelated lists excluded; contact-like values, ambiguous numbered text "
            "and URLs with user information, queries or fragments cause rejection without rewriting inputs",
        ],
    )
