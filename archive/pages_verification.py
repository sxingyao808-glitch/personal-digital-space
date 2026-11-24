"""Low-volume, resumable Pages probes and allowlisted evidence."""

from __future__ import annotations

import hashlib
import re

from .cloudflare_pages import CF_RAY, MAX_ASSET_BYTES, PagesError
from .db import utcnow

PROBE_BATCH = 8
NEGATIVE_ERROR_BYTES = 64 * 1024
REQUIRED_CLEAN_ROUNDS = 2
CACHE_OBSERVATIONS = {"UNSUPPORTED", "UNKNOWN", "COMPLETED", "FAILED", "TIMEOUT"}
CACHE_HEADERS = {"HIT", "MISS", "BYPASS", "DYNAMIC", "EXPIRED", "STALE", "REVALIDATED", "UPDATING", "NONE"}


def independent_host(value, domain):
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"https://([a-f0-9]{8}\." + re.escape(domain) + r")/?", value)
    return match[1] if match else None


def public_observation(probe, outlet, host, path, method, *, limit, negative=False):
    evidence = {"outlet": outlet, "host": host, "path": "/" + path,
                "method": method, "at": utcnow(), "http_status": None}
    options = {"method": method, "limit": limit}
    if negative and method == "GET":
        options["error_limit"] = NEGATIVE_ERROR_BYTES
    try:
        status, headers, body = probe(host, path, **options)
    except PagesError as error:
        evidence["error"] = "timeout" if error.details.get("timeout") else "network"
        return evidence, {}, b""
    evidence["http_status"] = status
    ray = headers.get("CF-Ray", "")
    if CF_RAY.fullmatch(ray):
        evidence["ray_id"] = ray[:6] + "…" + (ray[-4:] if "-" in ray else "")
        if "-" in ray:
            evidence["pop"] = ray[-3:]
    cache = headers.get("CF-Cache-Status", "").upper()
    if cache in CACHE_HEADERS:
        evidence["cache_status"] = cache
    age = headers.get("Age", "")
    if re.fullmatch(r"[0-9]{1,10}", age):
        evidence["age"] = int(age)
    if method == "GET" and (200 <= status < 300 or status == 404):
        bound = NEGATIVE_ERROR_BYTES if status == 404 and negative else limit
        evidence["body_bytes"] = len(body)
        if len(body) <= bound and status != 206:
            evidence["sha256"] = hashlib.sha256(body).hexdigest()
        else:
            evidence["body_truncated"] = True
    return evidence, headers, body


def retirement_pair(probes, host, path, old_facts):
    """Each qualifying PoP must return a clean HEAD and GET through one exit."""
    results = []
    exits = []
    old_hashes = {fact["sha256"] for fact in old_facts}
    limit = max((fact["size"] for fact in old_facts), default=MAX_ASSET_BYTES)
    limit = min(MAX_ASSET_BYTES, max(1, limit))
    for outlet, probe in probes.items():
        pair = []
        for method in ("HEAD", "GET"):
            observation, _, _ = public_observation(
                probe, outlet, host, path, method, limit=limit if method == "GET" else 0, negative=True,
            )
            if observation.get("sha256") in old_hashes:
                observation["old_bytes"] = True
            pair.append(observation)
        results.extend(pair)
        exits.append(pair)
    clean = set()
    bad = set()
    all_missing = True
    for pair in exits:
        missing = [row["http_status"] == 404 and not row.get("old_bytes") and not row.get("body_truncated")
                   for row in pair]
        all_missing &= all(missing)
        bad.update(row["pop"] for row, ok in zip(pair, missing) if not ok and row.get("pop"))
        if all(missing) and pair[0].get("pop") and pair[0].get("pop") == pair[1].get("pop"):
            clean.add(pair[0]["pop"])
    return results, clean - bad, bad, all_missing


def track_blockers(blocking, path, clean, bad):
    unresolved = (set(blocking.get(path, [])) - clean) | bad
    if unresolved:
        blocking[path] = sorted(unresolved)
    else:
        blocking.pop(path, None)
    return not unresolved
