#!/usr/bin/env python3
"""Lint the conformance suite. Stdlib only; uses `jsonschema` when importable.

    python3 tools/check_fixtures.py

Checks
  1. every fixture and case file is valid JSON and has the required shape;
  2. `spec/gossip-config.schema.json` is itself a valid JSON Schema;
  3. every gossip case's `expect.json_text` (and `expect.file_after.json_text`)
     parses AND validates against that schema;
  4. every such text obeys the byte rules of CLIENT_SPEC.md §6.3 — canonical key
     order, 2-space indent, exactly one trailing \\n, no CRLF;
  5. every IPv4 literal anywhere in fixtures/ is an RFC 5737 documentation
     address (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24). No real peer IP
     may ever enter this repo.
"""
from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gen_fixtures import KNOWN_ORDER, render  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTTP = os.path.join(ROOT, "fixtures", "http")
CASES = os.path.join(ROOT, "fixtures", "gossip", "cases")
SCHEMA = os.path.join(ROOT, "spec", "gossip-config.schema.json")

DOC_NETS = ("192.0.2.", "198.51.100.", "203.0.113.")

# CLIENT_SPEC.md §6.4 — the closed set of warning codes.
WARNING_CODES = {
    "many_roots_gt_24", "many_roots_gt_64", "chain_absent",
    "try_new_peers_false_multi_root", "truncated",
    "exclude_overrides_keep_existing",
}
QUAD = re.compile(r"\b(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\b")

errors: list[str] = []
warnings: list[str] = []


def fail(msg):
    errors.append(msg)


def check_ips(path, text):
    for m in QUAD.finditer(text):
        octets = [int(x) for x in m.groups()]
        if any(o > 255 for o in octets):
            continue  # deliberately-malformed literal, e.g. 999.0.2.10
        ip = m.group(0)
        if not ip.startswith(DOC_NETS):
            fail("%s: non-documentation IPv4 %r (RFC 5737 ranges only)"
                 % (os.path.relpath(path, ROOT), ip))


def check_byte_rules(where, text):
    if "\r" in text:
        fail("%s: CRLF in rendered JSON" % where)
    if not text.endswith("\n"):
        fail("%s: no trailing newline" % where)
    if text.endswith("\n\n"):
        fail("%s: more than one trailing newline" % where)
    try:
        obj = json.loads(text)
    except Exception as e:  # noqa: BLE001
        fail("%s: not valid JSON (%s)" % (where, e))
        return None
    if not isinstance(obj, dict):
        fail("%s: expected a JSON object" % where)
        return None
    if render(obj) != text:
        fail("%s: not in canonical form (key order / indent / separators)" % where)
    keys = [k for k in obj if k in KNOWN_ORDER]
    if keys != [k for k in KNOWN_ORDER if k in obj]:
        fail("%s: known keys out of canonical order" % where)
    return obj


def main():
    with open(SCHEMA, encoding="utf-8") as fh:
        schema = json.load(fh)

    validator = None
    try:
        import jsonschema
        cls = jsonschema.validators.validator_for(schema)
        cls.check_schema(schema)
        validator = cls(schema)
        print("schema: valid JSON Schema (%s)" % cls.__name__)
    except ImportError:
        warnings.append("jsonschema not installed - schema validation skipped")
    except Exception as e:  # noqa: BLE001
        fail("spec/gossip-config.schema.json is not a valid JSON Schema: %s" % e)

    # ---- http fixtures ---------------------------------------------------
    n_http = 0
    seen_status = set()
    offered_cursors = {}   # next_cursor value -> fixture that hands it out
    served_cursors = set()  # cursor values some fixture answers to
    for name in sorted(os.listdir(HTTP)):
        if not name.endswith(".json"):
            continue
        p = os.path.join(HTTP, name)
        raw = open(p, encoding="utf-8").read()
        check_ips(p, raw)
        fx = json.loads(raw)
        n_http += 1
        for key in ("name", "description", "request", "response"):
            if key not in fx:
                fail("%s: missing %r" % (name, key))
        if fx.get("name") != name[:-5]:
            fail("%s: name %r does not match filename" % (name, fx.get("name")))
        rq, rs = fx["request"], fx["response"]
        for key in ("method", "path", "query", "headers"):
            if key not in rq:
                fail("%s: request missing %r" % (name, key))
        if "status" not in rs or "headers" not in rs:
            fail("%s: response missing status/headers" % name)
        if "body" not in rs and "body_text" not in rs:
            fail("%s: response has neither body nor body_text" % name)
        seen_status.add(rs["status"])
        if rs["status"] == 429:
            if "Retry-After" not in rs["headers"]:
                fail("%s: a 429 must carry Retry-After" % name)
            if not rs["body"].get("signup_url"):
                fail("%s: a 429 must carry signup_url" % name)
        if rs["status"] == 200 and "ETag" not in rs["headers"]:
            fail("%s: a 200 must carry an ETag" % name)

        cur = rq.get("query", {}).get("cursor")
        if cur:
            served_cursors.add(cur)
        body = rs.get("body")
        if isinstance(body, dict) and body.get("next_cursor"):
            offered_cursors[body["next_cursor"]] = fx["name"]

        # A 429 must say what a client should compute from Retry-After, so the
        # HTTP-date rule (§5.2) is testable without wall-clock time.
        if rs["status"] == 429:
            if "expect" not in fx or "retry_after_seconds" not in fx["expect"]:
                fail("%s: a 429 must carry expect.retry_after_seconds" % name)
            ra = rs["headers"].get("Retry-After", "")
            if not ra.isdigit() and fx.get("expect", {}).get("retry_after_seconds") \
                    is not None and "clock" not in fx:
                fail("%s: a non-numeric Retry-After needs clock.now to be "
                     "deterministic" % name)

    for want in (200, 304, 400, 401, 404, 429, 500, 503):
        if want not in seen_status:
            fail("no http fixture covers status %d" % want)

    # 🔴 A next_cursor nobody serves is a dead end: an iter_peers walk over that
    # fixture 404s on the mock server. Either null the cursor (single page) or
    # add the follow-up page.
    for cur, src in sorted(offered_cursors.items()):
        if cur not in served_cursors:
            fail("%s hands out next_cursor %r but no fixture serves ?cursor=%s"
                 % (src, cur, cur))

    # ---- gossip cases ----------------------------------------------------
    n_cases = n_validated = 0
    ops_seen = set()
    for name in sorted(os.listdir(CASES)):
        if not name.endswith(".json"):
            continue
        p = os.path.join(CASES, name)
        raw = open(p, encoding="utf-8").read()
        check_ips(p, raw)
        cs = json.loads(raw)
        n_cases += 1
        if cs.get("name") != name[:-5]:
            fail("%s: name does not match filename" % name)
        for key in ("description", "operations", "expect"):
            if key not in cs:
                fail("%s: missing %r" % (name, key))
        if "input_config" not in cs and "input_json_text" not in cs:
            fail("%s: needs input_config or input_json_text" % name)
        if not cs["operations"]:
            fail("%s: no operations" % name)
        for o in cs["operations"]:
            ops_seen.add(o["op"])
        exp = cs["expect"]
        if not ({"json_text", "value", "error"} & set(exp)):
            fail("%s: expect must carry json_text, value or error" % name)

        # A non-error case must state its warning set EXACTLY. An absent
        # `warnings` key is how an incidental warning hides from a runner
        # that compares sets, so absence is a failure, not a default.
        if "error" not in exp:
            if "warnings" not in exp:
                fail("%s: non-error case must carry expect.warnings ([] if none)" % name)
            else:
                w = exp["warnings"]
                if not isinstance(w, list) or sorted(set(w)) != w:
                    fail("%s: expect.warnings must be a sorted, duplicate-free list"
                         % name)
                for code in w:
                    if code not in WARNING_CODES:
                        fail("%s: unknown warning code %r" % (name, code))
                if "many_roots_gt_24" in w and "many_roots_gt_64" in w:
                    fail("%s: gt_64 fires INSTEAD OF gt_24, never alongside" % name)
        elif "warnings" in exp:
            fail("%s: an error case must not assert warnings" % name)

        texts = []
        if "json_text" in exp:
            texts.append(("%s expect.json_text" % name, exp["json_text"]))
        if isinstance(exp.get("file_after"), dict):
            texts.append(("%s expect.file_after" % name,
                          exp["file_after"]["json_text"]))
        for where, text in texts:
            obj = check_byte_rules(where, text)
            if obj is not None and validator is not None:
                errs = sorted(validator.iter_errors(obj), key=lambda e: list(e.path))
                if errs:
                    fail("%s: does not validate against the schema: %s"
                         % (where, errs[0].message))
                else:
                    n_validated += 1

    # every method named in CLIENT_SPEC §6 must be exercised somewhere
    required_ops = {
        "new", "to_json", "validate", "save",
        "add_root_ip", "remove_root_ip", "set_root_ips", "has_root_ip", "root_ips",
        "add_reserved_peer_ip", "remove_reserved_peer_ip", "set_reserved_peer_ips",
        "set_try_new_peers", "set_chain", "set_n_gossip_peers",
        "set_split_client_blocks", "unset_split_client_blocks",
        "merge_peers", "from_peers",
    }
    missing = sorted(required_ops - ops_seen)
    if missing:
        fail("no gossip case exercises: %s" % ", ".join(missing))

    print("http fixtures : %d (statuses %s)" % (n_http, sorted(seen_status)))
    print("gossip cases  : %d (%d expected outputs validated against the schema)"
          % (n_cases, n_validated))
    print("operations    : %d distinct" % len(ops_seen))
    for w in warnings:
        print("WARN  %s" % w)
    for e in errors:
        print("FAIL  %s" % e)
    if errors:
        print("\n%d failure(s)" % len(errors))
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
