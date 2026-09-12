"""Pure offline proposals from retained inputs. No write-to-estate or collection mode."""

import base64
import copy
import csv
import hashlib
import io
import json
import tempfile
from contextlib import redirect_stdout
from dataclasses import asdict
from importlib import import_module
from pathlib import Path

from people_sync import captures, parsers
from people_sync.scrape.profile import ExtractError

PROFILE_SOURCES = ("instagram", "facebook", "linkedin", "partiful", "spotify", "strava", "venmo")


def normalized(result: dict) -> dict:
    """Compare proposed values, excluding source/code metadata and volatile Profile.raw."""
    out = {
        k: copy.deepcopy(result[k])
        for k in ("status", "profile", "records", "observations")
        if k in result
    }
    if "profile" in out:
        out["profile"].pop("raw", None)
    return out


def export_entries(source, data: bytes):
    """Expose original row order for the report; field interpretation stays in parsers.py."""
    if source == "linkedin":
        rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
        header = next(i for i, row in enumerate(rows) if row[:1] == ["First Name"])
        return [dict(zip(rows[header], row)) for row in rows[header + 1 :]]
    obj = json.loads(data)
    if source == "instagram":
        entries = obj if isinstance(obj, list) else obj["relationships_following"]
    else:
        entries = obj["friends_v2" if source == "facebook" else "Friends"]
    if not isinstance(entries, list):
        raise ValueError("invalid export list")
    return entries


def _export(capture: dict) -> dict:
    source = capture["source"]
    files = capture["payload"]["files"]
    with tempfile.TemporaryDirectory(prefix="people-sync-replay-") as directory:
        paths, originals = {}, {}
        for file in files:
            data = base64.b64decode(file["data"], validate=True)
            originals[file["role"]] = export_entries(source, data)
            path = Path(directory).resolve() / (file["role"] + "." + file["format"])
            captures.write_private(path, data)
            paths[file["role"]] = str(path)
        args = (
            [paths["followers"], paths["following"]] if source == "instagram" else [paths["export"]]
        )
        # Existing parsers log skipped ordinals. Keep CLI stdout a single JSON document.
        with redirect_stdout(io.StringIO()):
            records = getattr(parsers, f"parse_{source}")(*args)
    observations = []
    for file in files:
        role = file["role"]
        by_raw = {}
        for record in records:
            raw = record.raw.get(role) if source == "instagram" else record.raw
            by_raw.setdefault(captures.encode(raw), set()).add(record.row_id)
        for ordinal, entry in enumerate(originals[role]):
            ids = sorted(by_raw.get(captures.encode(entry), ()))
            observations.append(
                {
                    "file": file["filename"],
                    "role": role,
                    "ordinal": ordinal,
                    "status": "parsed" if ids else "skipped",
                    "record_ids": ids,
                }
            )
    return {"records": [asdict(r) for r in records], "observations": observations}


def replay_capture(capture) -> dict:
    result = {"status": "invalid", "verification": "unverified", "limitations": []}
    try:
        c = copy.deepcopy(capture)
        if not isinstance(c, dict):
            raise ValueError()
        legacy = "schema_version" not in c
        if not legacy:
            captures.validate(c)
            result["verification"] = "payload-checksum"
        else:
            source = c.get("source") or c.get("platform")
            if source not in PROFILE_SOURCES:
                result["limitations"].append("legacy capture requires explicit source metadata")
                return result
            parsed_only = "eval" not in c
            c = {
                "source": source,
                "kind": "profile",
                "capture_id": None,
                "record_id": c.get("record_id"),
                "captured_at": c.get("captured_at"),
                "completeness": "legacy-parsed-only" if parsed_only else "extracted-only",
                "payload_sha256": hashlib.sha256(captures.encode(c)).hexdigest(),
                "payload": c,
                "exclusions": [],
            }
            result["limitations"].append("legacy input has no historical expected checksum")
        result.update(
            {
                key: c[key]
                for key in (
                    "source",
                    "kind",
                    "capture_id",
                    "record_id",
                    "captured_at",
                    "completeness",
                )
            }
        )
        result.update(
            input_sha256=c["payload_sha256"],
            parser_fingerprint=captures.code_fingerprint(),
            exclusions=c["exclusions"],
        )
        if c["completeness"] == "legacy-parsed-only":
            result.update(status="unsupported")
            result["limitations"].append("parsed cache cannot reconstruct missing source inputs")
            return result
        if c["kind"] == "export":
            result.update(_export(c), status="ok")
            result["limitations"].append("skipped ordinals include malformed or superseded rows")
            return result
        if c["kind"] != "profile" or c["source"] not in PROFILE_SOURCES:
            result.update(status="unsupported")
            result["limitations"].append("no offline parser for this source and kind")
            return result
        result["limitations"].append("replayed extracted fields; retained DOM was not re-extracted")
        raw = c["payload"]["eval"]
        raw = json.loads(raw) if isinstance(raw, str) else raw
        captures.encode(raw)
        if not isinstance(raw, dict) or not raw:
            raise ValueError()
        if raw.get("error"):
            result["status"] = (
                "unavailable" if raw["error"] in ("unavailable", "no-profile") else "parse-failed"
            )
            return result
        module = import_module("people_sync.scrape." + c["source"])
        profile = module.parse(raw, c["payload"].get("captured", []))
        if not (profile.display_name or profile.platform_id):
            raise ValueError()
        profile.record_id = c["record_id"]
        result.update(status="ok", profile=asdict(profile))
    except ExtractError:
        result["status"] = "parse-failed"
        result["limitations"].append("source parser rejected retained input")
    except (ValueError, TypeError, KeyError, AttributeError, StopIteration, RecursionError):
        result["status"] = "malformed" if "source" in result else "invalid"
        result["limitations"].append("invalid envelope, checksum, or source input")
    except Exception:
        result["status"] = "parse-failed"
        result["limitations"].append("offline replay failed; exception details withheld")
    return result
