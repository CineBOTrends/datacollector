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


_CITY_WORDS = None


def _city_word_set(canon_city):
    """
    All words that could denote this (already-canonicalised) city across
    sources - the canonical spelling itself plus every raw alias that maps
    to it (e.g. canon "bengaluru" also covers "bangalore", since venue
    strings from BMS/District embed whichever raw spelling that source used).
    """
    global _CITY_WORDS
    if _CITY_WORDS is None:
        aliases = {}
        try:
            # reuse the same table canon_city() loads, built lazily here too
            # in case _distinctive() is called before canon_city()
            import json as _json
            import os as _os
            fp = _os.path.join(
                _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                "venues", "city_aliases.json",
            )
            with open(fp, encoding="utf-8") as f:
                aliases = _json.load(f)
        except Exception:
            aliases = {}
        _CITY_WORDS = {}
        for raw, mapped in aliases.items():
            _CITY_WORDS.setdefault(mapped.strip().lower(), set()).update(_words(mapped))
            _CITY_WORDS[mapped.strip().lower()].update(_words(raw))
    words = set(_words(canon_city))
    words |= _CITY_WORDS.get(canon_city, set())
    return words


def _distinctive(venue, city):
    """Venue words minus generic cinema words and every known spelling of the city."""
    city_w = _city_word_set(city)
    return {w for w in _words(venue) if w not in GENERIC and w not in city_w and len(w) > 1}


def same_venue(v1, v2, city):
    """Do these two venue names refer to the same theatre?"""
    a, b = _distinctive(v1, city), _distinctive(v2, city)
    if not a or not b:
        return False
    return a <= b or b <= a          # full containment of the shorter name


# The two sources also disagree on CITY names for the same place:
#     District "New Delhi"  vs  BMS "Delhi"
#     District "Gurgaon"    vs  BMS "Gurugram (Gurgaon)"
# The dedupe groups by (movie, city, time), so a mismatched city name meant the
# duplicate pair was never even compared — every Delhi show was counted twice,
# once under BMS/"NCR" and once under District/"Unknown".
_ALIASES = None


def canon_city(city):
    """Map a city to its canonical (BMS) spelling for keying."""
    global _ALIASES
    if _ALIASES is None:
        import json as _json
        import os as _os
        fp = _os.path.join(
            _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
            "venues", "city_aliases.json",
        )
        try:
            with open(fp, encoding="utf-8") as f:
                _ALIASES = {k.casefold(): v for k, v in _json.load(f).items()}
        except Exception:
            _ALIASES = {}
    c = " ".join((city or "").split())
    return _ALIASES.get(c.casefold(), c).strip().lower()


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
        k = (canon_movie(r.get("movie")), canon_city(r.get("city")),
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
        city = canon_city(r.get("city"))
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