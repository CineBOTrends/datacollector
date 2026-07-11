"""
Cross-source duplicate removal (District vs BookMyShow).

Both sources list the SAME physical theatre under different names, e.g.
    BMS      "Cine Square Dolby Atmos A/C: Guntur"
    District "Cine Square Dolby Atmos A/C, Gorantla, Guntur"
so the same show was counted twice, inflating shows / tickets / gross.

We compare the *distinctive* words of the venue name (dropping generic cinema
words and the city) for rows that share the same movie, city and showtime.
BMS is kept (its gross uses real per-seat prices); the District copy is dropped.

Deliberately conservative: a missed duplicate merely shows twice (visible),
whereas a wrong merge would silently delete real shows.
"""
import re

# words that carry no identity - every cinema has them
GENERIC = {
    "cinema", "cinemas", "cineplex", "multiplex", "theatre", "theatres",
    "theater", "theaters", "talkies", "screen", "screens", "cine",
    "dolby", "atmos", "rgb", "laser", "4k", "2k", "3d", "2d", "70mm", "35mm",
    "ac", "a", "c", "the", "mall", "near", "road", "street", "opp", "and",
    "digital", "gold", "plus", "new", "old",
}

# A pair is the same theatre only when the SHORTER name's distinctive words are
# fully contained in the longer one ("Cine Prime" c "Cine Prime, Srinivasarao Pet").
# Partial overlap is rejected: "Vidya Theatre, Tambaram" vs "National Theatre,
# Tambaram" share a word but are different cinemas.


def _words(s):
    return [w for w in re.split(r"[^a-z0-9]+", (s or "").lower()) if w]


def _distinctive(venue, city):
    """Venue words minus generic cinema words and the city name."""
    city_w = set(_words(city))
    return {w for w in _words(venue) if w not in GENERIC and w not in city_w and len(w) > 1}


def same_venue(v1, v2, city):
    """Do these two venue names refer to the same theatre?"""
    a, b = _distinctive(v1, city), _distinctive(v2, city)
    if not a or not b:
        return False
    return a <= b or b <= a          # full containment of the shorter name


def canon_movie(title):
    """'Maa Inti Bangaaram (2D - Telugu)' -> 'maaintibangaaram'."""
    t = re.sub(r"\([^)]*\)", " ", title or "")
    return re.sub(r"[^a-z0-9]+", "", t.lower())


def cross_source_dedupe(rows, project_root=None):
    """
    Drop District rows for shows BMS already reported at the same theatre+time.
    Returns (rows, dropped_count).
    """
    # index BMS venues by (movie, city, time)
    bms_index = {}
    for r in rows:
        if r.get("source") == "District":
            continue
        k = (canon_movie(r.get("movie")), (r.get("city") or "").strip().lower(),
             r.get("time", ""))
        bms_index.setdefault(k, set()).add(r.get("venue", ""))

    # 1-to-1: one BMS venue may absorb at most ONE District venue per
    # (movie, city, time) group, so two distinct District theatres can never
    # both be deleted against a single BMS entry.
    claimed = {}                       # (movie,city,time) -> {bms_venue: district_venue}
    out, dropped = [], 0
    for r in rows:
        if r.get("source") != "District":
            out.append(r)
            continue
        city = (r.get("city") or "").strip().lower()
        k = (canon_movie(r.get("movie")), city, r.get("time", ""))
        taken = claimed.setdefault(k, {})
        hit = None
        for bv in bms_index.get(k, ()):
            owner = taken.get(bv)
            if owner not in (None, r.get("venue", "")):
                continue               # this BMS venue already matched another theatre
            if same_venue(r.get("venue", ""), bv, city):
                hit = bv
                break
        if hit is not None:
            taken[hit] = r.get("venue", "")
            dropped += 1
            continue
        out.append(r)
    return out, dropped
