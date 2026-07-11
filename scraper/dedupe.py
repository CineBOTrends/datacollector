"""
Deduplication logic for show records.
Key: (venue, time, session_id, audi)
"""


def dedupe(rows):
    """Deduplicate rows, return deduped list."""
    seen = set()
    out = []
    for r in rows:
        key = (
            r.get("venue", ""),
            r.get("time", ""),
            r.get("session_id", ""),
            r.get("audi", ""),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def dedupe_with_count(rows):
    """Deduplicate rows, return (deduped_list, duplicate_count)."""
    seen = set()
    out = []
    dupes = 0
    for r in rows:
        key = (
            r.get("venue", ""),
            r.get("time", ""),
            r.get("session_id", ""),
            r.get("audi", ""),
        )
        if key in seen:
            dupes += 1
            continue
        seen.add(key)
        out.append(r)
    return out, dupes
