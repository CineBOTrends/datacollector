/**
 * District CINEMA-page Worker  (deploy on YOUR Cloudflare account)
 * ----------------------------------------------------------------
 * Returns ALL movies + sessions for ONE cinema on ONE date, by scraping the
 * cinema page's embedded __NEXT_DATA__.
 *
 *   GET /?cinema_id=1088533&slug=inox-m5-ecity-bengaluru&city=bengaluru&date=2026-07-02
 *
 * - cinema_id : required (the CD<id> number)
 * - slug/city : build the page path; District resolves by the CD id.
 * - date      : optional YYYY-MM-DD -> District's ?fromdate= (advance dates).
 * - debug=1   : returns discovered structure + a trimmed JSON skeleton.
 *
 * Required headers: User-Agent + x-api-key (your invented secrets).
 *
 * Output (matches parser.py cinema parser):
 *   { meta:{cinema:{name,address,chainKey}, movies:[{id,name,lang}]},
 *     pageData:{sessions:[{mid,showTime,lang,scrnFmt,audi,sid,areas:[...]}]},
 *     sessionDates:[...], cinema_id, city, date }
 *
 * Real District layout this targets (serverState[<cinemaId|cinemaId+date>]):
 *   meta.cinema   = {id, name, address, chainKey, ...}
 *   meta.movies[] = {id:"OB7K3U", contentId:212841, name, label, lang, scrnFmt}
 *   pageData.sessions[] = {sid, cid, mid:"OB7K3U", showTime, audi, lang,
 *                          scrnFmt, total, avail, areas:[{sTotal,sAvail,...,price}]}
 *   data.sessionDates[] = ["YYYY-MM-DD", ...]
 * Sessions join to movies by the SHORT mid (meta.movies[].id), not contentId.
 */

const ALLOWED_UA = "";   // set via Worker secret ALLOWED_UA
const ALLOWED_KEY = "";  // set via Worker secret ALLOWED_KEY  (NEVER hardcode)

const BROWSER_UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36";

export default {
  async fetch(request, env) {
    const ua = request.headers.get("user-agent") || "";
    const key = request.headers.get("x-api-key") || "";
    const wantUA = (env && env.ALLOWED_UA) || ALLOWED_UA;
    const wantKEY = (env && env.ALLOWED_KEY) || ALLOWED_KEY;
    if (ua !== wantUA || key !== wantKEY) return json({ error: "unauthorized" }, 401);

    const url = new URL(request.url);
    const cinemaId = url.searchParams.get("cinema_id") || url.searchParams.get("content_id");
    const slug = (url.searchParams.get("slug") || "cinema").toLowerCase();
    const city = (url.searchParams.get("city") || "").toLowerCase().trim().replace(/\s+/g, "-");
    const date = url.searchParams.get("date") || "";
    const debug = url.searchParams.get("debug") === "1";
    if (!cinemaId) return json({ error: "cinema_id is required" }, 400);

    const base = `https://www.district.in/movies/${slug}-in-${city}-CD${cinemaId}`;
    const pageUrl = date ? `${base}?fromdate=${encodeURIComponent(date)}` : base;

    let html;
    try {
      const r = await fetch(pageUrl, {
        headers: { "User-Agent": BROWSER_UA, Accept: "text/html" },
        cf: { cacheTtl: 0 },
        redirect: "follow",
      });
      if (r.status !== 200) return json({ error: "district_status_" + r.status, pageUrl, sessions: [] }, 200);
      html = await r.text();
    } catch (e) {
      return json({ error: "fetch_failed", detail: String(e), sessions: [] }, 200);
    }

    const m = html.match(/<script id="__NEXT_DATA__"[^>]*>([\s\S]*?)<\/script>/);
    if (!m) return json({ error: "no_next_data", pageUrl, sessions: [] }, 200);

    let data;
    try { data = JSON.parse(m[1]); }
    catch (e) { return json({ error: "json_parse_failed", sessions: [] }, 200); }

    const isObj = (x) => x && typeof x === "object";
    const pick = (o, keys) => { for (const k of keys) if (isObj(o) && o[k] != null) return o[k]; return undefined; };

    const mapAreas = (arr) =>
      Array.isArray(arr)
        ? arr.map((a) => ({
            label: pick(a, ["label", "name", "areaLabel"]),
            sTotal: pick(a, ["sTotal", "seatsTotal", "total"]),
            sAvail: pick(a, ["sAvail", "seatsAvail", "avail", "available"]),
            seatsTotal: pick(a, ["seatsTotal", "sTotal", "total"]),
            seatsAvail: pick(a, ["seatsAvail", "sAvail", "avail", "available"]),
            price: pick(a, ["price", "priceInRs", "amount"]),
          }))
        : [];

    const sessionOut = (s, mid) => ({
      mid,
      showTime: pick(s, ["showTime", "showtime", "startTime"]),
      lang: pick(s, ["lang", "language"]) ?? null,
      scrnFmt: pick(s, ["scrnFmt", "format", "screenFormat"]) ?? null,
      audi: pick(s, ["audi", "audiName", "screen", "screenName"]) ?? null,
      sid: pick(s, ["sid", "sessionId", "id"]) ?? null,
      areas: mapAreas(s.areas),
    });

    // root under the cinema-id key (advance keys look like "<cinemaId><date>")
    const ssWrap = data?.props?.pageProps?.data?.serverState;
    const cineRoot = isObj(ssWrap)
      ? ssWrap[cinemaId] || ssWrap[Object.keys(ssWrap)[0]] || ssWrap
      : data?.props?.pageProps || data;

    const meta = cineRoot?.meta || {};

    // cinema info (direct)
    const c = meta.cinema || {};
    const cinema = {
      name: c.name || c.cinemaName || slug,
      address: c.address || "",
      chainKey: c.chainKey || c.chain || "",
    };

    // movie map: short id ("OB7K3U") AND contentId both point to the movie.
    // Carry the FULL detail set so the parser/summary can build a movie card.
    const movieByKey = {};
    for (const mv of meta.movies || []) {
      const info = {
        contentId: mv.contentId != null ? mv.contentId : mv.id,
        movieId: mv.id,
        name: mv.name || mv.label || "Unknown",
        label: mv.label || mv.name || "",
        lang: mv.lang || mv.language || "",
        scrnFmt: mv.scrnFmt || "",
        sndFmt: mv.sndFmt || "",
        censor: mv.censor || "",
        duration: mv.duration != null ? mv.duration : null,
        genres: Array.isArray(mv.grn) ? mv.grn : (Array.isArray(mv.genre) ? mv.genre : []),
        poster: mv.appImgPath || mv.imgPath || "",
        cover: mv.appCvrPath || mv.cvrPath || "",
        thumbnail: mv.thumbnail || "",
        trailer: mv.trailer && mv.trailer !== "NA" ? mv.trailer : "",
        rating: mv.rnr || null,
        isNew: !!mv.isNew,
        totalSessionCount: mv.totalSessionCount != null ? mv.totalSessionCount : null,
      };
      if (mv.id != null) movieByKey[String(mv.id)] = info;
      if (mv.contentId != null) movieByKey[String(mv.contentId)] = info;
    }

    // sessions: flat pageData.sessions joined to movies by short mid
    const movies = [];
    const used = {};
    const sessions = [];
    const flat = cineRoot?.pageData?.sessions || [];

    for (const s of flat) {
      const info = movieByKey[String(s.mid)] || movieByKey[String(s.contentId)] || null;
      const groupId = info && info.contentId != null ? info.contentId : s.mid;
      const name = (info && info.name) || s.movieName || "Unknown";
      const lang = s.lang || (info && info.lang) || "";
      if (!used[String(groupId)]) {
        used[String(groupId)] = 1;
        // keep id=groupId(contentId) so sessions join; carry all detail fields
        movies.push(info ? { ...info, id: groupId, name, lang } : { id: groupId, name, lang });
      }
      sessions.push(sessionOut(s, groupId));
    }

    // fallback: use arrangedSessions groups if the flat list was empty
    if (!sessions.length && Array.isArray(cineRoot?.arrangedSessions)) {
      for (const g of cineRoot.arrangedSessions) {
        const gd = g.data || {};
        const groupId = g.entityCode != null ? g.entityCode : gd.contentId;
        const name = g.entityName || gd.name || gd.label || "Unknown";
        const lang = gd.lang || gd.languages || "";
        const mid = groupId != null ? groupId : name;
        if (!used[String(mid)]) { used[String(mid)] = 1; movies.push({ id: mid, name, lang }); }
        for (const s of g.sessions || []) sessions.push(sessionOut(s, mid));
      }
    }

    // dedupe by (mid, showTime, sid)
    const seen = new Set();
    const uniqSessions = [];
    for (const s of sessions) {
      const k = `${s.mid}|${s.showTime}|${s.sid}`;
      if (!seen.has(k)) { seen.add(k); uniqSessions.push(s); }
    }

    // session dates (direct), else derived from sessions
    let sessionDates = cineRoot?.data?.sessionDates || meta.showDates || [];
    sessionDates = (Array.isArray(sessionDates) ? sessionDates : []).map((d) => String(d).slice(0, 10));
    if (!sessionDates.length && uniqSessions.length) {
      sessionDates = [...new Set(uniqSessions.map((s) => String(s.showTime || "").slice(0, 10)).filter(Boolean))];
    }

    if (debug) {
      return json({
        pageUrl,
        serverStateKeys: isObj(ssWrap) ? Object.keys(ssWrap) : null,
        cineRootKeys: isObj(cineRoot) ? Object.keys(cineRoot) : null,
        cinema,
        movieCount: (meta.movies || []).length,
        counts: { movies: movies.length, sessions: uniqSessions.length },
        sampleMovie: movies[0] || null,
        sampleSession: uniqSessions[0] || null,
        sessionDates,
      }, 200);
    }

    return json({
      meta: { cinema, movies },
      pageData: { sessions: uniqSessions },
      sessionDates,
      cinema_id: cinemaId,
      city,
      date,
    }, 200);
  },
};

function json(obj, status) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}
