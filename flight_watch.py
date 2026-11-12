#!/usr/bin/env python3
"""
Multi-origin flight deal watcher.

Alerts when EVERY origin city can fly round trip to the SAME destination on the
SAME dates for under a price cap. Data: SerpApi "Google Flights Calendar",
which returns, for one origin -> one destination and one trip length, the
round-trip price for every departure date in a range (one search).

Cascade (keeps the search count low):
    For each destination and trip length, ask origins in config order. After
    each origin, keep only the departure dates that are still under the cap for
    everyone asked so far. Stop as soon as no dates are left. Put the origins
    that are hardest to satisfy first.

New deals are then looked up once per origin on Google Flights to add stops
and airline, so all-nonstop deals can be flagged ("detail_lookups").

Usage:
    python3 flight_watch.py config.json                    # live run
    python3 flight_watch.py config.json --no-notify        # live, print only
    python3 flight_watch.py config.json --dump raw/        # save raw responses
    python3 flight_watch.py config.json --mock mock.json   # offline test

SERPAPI_KEY (and optional SMTP_*) come from the environment, or from a .env
file next to the config. Stdlib only; Python 3.9+.
"""
import argparse
import datetime as dt
import json
import os
import smtplib
import sys
import urllib.parse
import urllib.request
from email.message import EmailMessage
from pathlib import Path

API = "https://serpapi.com/search.json"
ACCOUNT_API = "https://serpapi.com/account.json"
MAX_RANGE_DAYS = 200     # longer ranges return "no results" (tested Nov 2026)


# ---------------------------------------------------------------- API layer
class SerpApi:
    def __init__(self, key, cfg, mock=None, dump_dir=None):
        self.key, self.cfg, self.mock, self.calls = key, cfg, mock, 0
        self.dump_dir = Path(dump_dir) if dump_dir else None
        if self.dump_dir:
            self.dump_dir.mkdir(parents=True, exist_ok=True)

    def _get(self, tag, **q):
        """One search. `tag` names it in mock files and dumps."""
        self.calls += 1
        if self.mock is not None:
            return self.mock.get(tag, {})
        q.update(currency="USD", gl="us", hl="en", api_key=self.key)
        try:
            with urllib.request.urlopen(API + "?" + urllib.parse.urlencode(q),
                                        timeout=120) as r:
                data = json.load(r)
        except Exception as e:  # one failed search shouldn't kill the run
            print(f"  ! {tag}: {e}", file=sys.stderr)
            return {}
        if self.dump_dir:
            name = tag.replace("/", "_").replace("|", "_") + ".json"
            (self.dump_dir / name).write_text(json.dumps(data, indent=1))
        if data.get("error"):
            print(f"  ! {tag}: {data['error']}", file=sys.stderr)
        return data

    def calendar(self, origin, dest_id, nights, start, end):
        """{outbound_date: price} for every departure date in [start, end]."""
        data = self._get(
            f"cal|{origin}|{dest_id}|{nights}",
            engine="google_flights_calendar", departure_id=origin,
            arrival_id=dest_id, type=1, round_trip_days=nights,
            date_range_start=start.isoformat(), date_range_end=end.isoformat())
        out = {}
        for p in data.get("prices") or []:
            if isinstance(p.get("price"), (int, float)) and p.get("outbound_date"):
                out[p["outbound_date"]] = p["price"]
        return out

    def detail(self, origin, dest_id, start, end):
        """Cheapest itinerary on exact dates: {stops, airline, price} or {}."""
        data = self._get(
            f"detail|{origin}|{dest_id}|{start}",
            engine="google_flights", departure_id=origin, arrival_id=dest_id,
            type=1, outbound_date=start, return_date=end)
        options = [o for o in (data.get("best_flights") or [])
                   + (data.get("other_flights") or [])
                   if isinstance(o.get("price"), (int, float)) and o.get("flights")]
        if not options:
            return {}
        best = min(options, key=lambda o: o["price"])
        legs = best["flights"]
        airlines = list(dict.fromkeys(l.get("airline", "") for l in legs))
        return {
            "stops": len(legs) - 1,          # outbound leg of the round trip
            "airline": " + ".join(a for a in airlines if a),
            "price": best["price"],
            "code": (legs[-1].get("arrival_airport") or {}).get("id", ""),
        }

    def searches_left(self):
        """Remaining SerpApi searches (free call), or None if unknown."""
        try:
            url = ACCOUNT_API + "?" + urllib.parse.urlencode({"api_key": self.key})
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.load(r).get("total_searches_left")
        except Exception as e:
            print(f"  ! could not check SerpApi quota: {e}", file=sys.stderr)
            return None


# ---------------------------------------------------------------- helpers
def load_env(path):
    """Minimal .env reader: KEY=VALUE lines; real environment wins."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def normalize(cfg):
    """Fill in defaults and accept the short config forms.

    destinations may be a list of airport codes (["LAS", "PHX"]) or a dict of
    display name -> airport code or Google city id ({"Phoenix": "/m/0d35y"}).
    A city id covers every airport in that city; an airport code only itself.
    """
    dests = cfg["destinations"]
    if isinstance(dests, list):
        cfg["destinations"] = {d: d for d in dests}
    cfg.setdefault("trip_nights", [3])
    cfg.setdefault("origin_cities", {})
    return cfg


def window(cfg, today):
    """Search window. Without dates in the config: today onward, as far as
    one search reaches."""
    start = today
    if cfg.get("window_start"):
        start = max(dt.date.fromisoformat(cfg["window_start"]), today)
    if cfg.get("window_end"):
        return start, dt.date.fromisoformat(cfg["window_end"])
    return start, start + dt.timedelta(
        days=MAX_RANGE_DAYS - 1 + max(cfg["trip_nights"]))


def needed_origins(cfg, dest_id):
    """Origins that must fly; the destination's own home origin is exempt."""
    home = cfg.get("origin_cities") or {}
    if not cfg.get("allow_origin_as_destination", True):
        return list(cfg["origins"])
    return [o for o in cfg["origins"] if dest_id not in (o, home.get(o))]


def planned_searches(cfg):
    """Worst case for the calendar cascade (every origin asked every time)."""
    return sum(len(needed_origins(cfg, did))
               for did in cfg["destinations"].values()) * len(cfg["trip_nights"])


def search(cfg, api, today):
    """Run the cascade; return hits sorted by total price."""
    start, end = window(cfg, today)
    hits = []
    for name, did in cfg["destinations"].items():
        need = needed_origins(cfg, did)
        if not need:
            continue
        for nights in cfg["trip_nights"]:
            last_out = end - dt.timedelta(days=nights)   # return within window
            # One Calendar search covers at most 200 departure dates
            last_out = min(last_out, start + dt.timedelta(days=MAX_RANGE_DAYS - 1))
            if last_out < start:
                continue
            alive, prices = None, {}      # alive: dates under cap for all so far
            for o in need:
                cal = api.calendar(o, did, nights, start, last_out)
                ok = {d for d, p in cal.items()
                      if p <= cfg["max_price"]
                      and start.isoformat() <= d <= last_out.isoformat()}
                alive = ok if alive is None else alive & ok
                prices[o] = cal
                if not alive:
                    break
            for d in sorted(alive or ()):
                ret = (dt.date.fromisoformat(d) + dt.timedelta(days=nights)).isoformat()
                fares = {o: {"price": prices[o][d], "stops": None, "airline": ""}
                         for o in need}
                hits.append({
                    "key": f"{did}|{d}|{ret}", "id": did, "name": name,
                    "start": d, "end": ret, "fares": fares,
                    "total": sum(f["price"] for f in fares.values()),
                    "all_nonstop": False,
                })
    return sorted(hits, key=lambda h: h["total"])


def add_details(hit, api):
    """Look up stops/airline for each origin of one hit (1 search each)."""
    for o, f in hit["fares"].items():
        info = api.detail(o, hit["id"], hit["start"], hit["end"])
        f.update({k: info[k] for k in ("stops", "airline", "code") if k in info})
    hit["all_nonstop"] = all(f["stops"] == 0 for f in hit["fares"].values())


def group_by_destination(hits):
    """[(best_hit, [other hits])] per destination, best = lowest total."""
    groups = {}
    for h in hits:                       # hits are already sorted by total
        groups.setdefault(h["id"], []).append(h)
    return [(g[0], g[1:]) for g in groups.values()]


# ---------------------------------------------------------------- notify
# Push bodies are read on a phone in a proportional font: one fact per line,
# no column alignment, long links kept together at the bottom.
MAX_DATES_LISTED = 12


def fmt_day(d):
    return f"{d:%a %b} {d.day}"          # no %-d: works on Windows too


def fmt_range(h):
    d1, d2 = dt.date.fromisoformat(h["start"]), dt.date.fromisoformat(h["end"])
    return f"{fmt_day(d1)} \u2013 {fmt_day(d2)}"


def booking_url(origin, dest, start, end):
    # Shortest form Google Flights still reads as a dated round trip
    q = f"{origin} to {dest} {start} to {end}"
    return "https://google.com/flights?q=" + urllib.parse.quote_plus(q)


def price_line(h):
    return " \u00b7 ".join(f"{o} ${f['price']}" for o, f in h["fares"].items())


def date_lines(hits, max_dates=MAX_DATES_LISTED):
    """Two lines per date range (dates, then each city's price), by date."""
    rows = sorted(hits, key=lambda h: h["start"])
    lines = []
    for h in rows[:max_dates]:
        lines += [fmt_range(h), "  " + price_line(h)]
    if len(rows) > max_dates:
        lines.append(f"+{len(rows) - max_dates} more dates")
    return lines


def format_hit(h, others=(), links=True, max_dates=MAX_DATES_LISTED):
    nights = (dt.date.fromisoformat(h["end"]) - dt.date.fromisoformat(h["start"])).days
    lines = [f"{fmt_range(h)} ({nights} nights)"
             + (", all nonstop" if h["all_nonstop"] else ""),
             ""]
    for o, f in h["fares"].items():
        parts = [f"{o}  ${f['price']}"]
        if f["stops"] == 0:
            parts.append("nonstop")
        elif f["stops"] is not None:
            parts.append(f"{f['stops']} stop" + ("s" if f["stops"] > 1 else ""))
        if f["airline"]:
            parts.append(f["airline"])
        lines.append(" \u00b7 ".join(parts))
    if others:
        lines += ["", f"Other dates ({len(others)}):"] + date_lines(list(others), max_dates)
    if links:
        lines += ["", "Book:"]
        lines += [f"{o}: " + booking_url(o, f.get("code") or h["name"],
                                         h["start"], h["end"])
                  for o, f in h["fares"].items()]
    return "\n".join(lines)


def format_summary(hits):
    """Every current match, grouped by destination, dates in calendar order."""
    if not hits:
        return "No dates where every city is under the cap right now."
    blocks = []
    for best, rest in group_by_destination(hits):
        n = len(rest) + 1
        head = f"{best['name']} \u2013 {n} date{'s' if n > 1 else ''}"
        blocks.append("\n".join([head] + date_lines([best] + rest)))
    return "\n\n".join(blocks)


def format_new(groups, limit=3800):
    """All new deals in ONE push body: a block per destination.

    ntfy turns bodies over 4096 bytes into attachments, so if the full text
    is too long, list fewer dates per city, then drop the booking links.
    """
    def build(max_dates, links):
        return "\n\n\n".join(
            best["name"].upper() + "\n"
            + format_hit(best, rest, links=links, max_dates=max_dates)
            for best, rest in groups)
    for max_dates, links in [(MAX_DATES_LISTED, True), (8, True), (5, True),
                             (3, True), (3, False), (0, False)]:
        body = build(max_dates, links)
        if len(body.encode()) <= limit:
            return body
    return body.encode()[:limit].decode(errors="ignore")


def new_title(groups):
    names = [b["name"].encode("ascii", "ignore").decode() for b, _ in groups]
    return "New flight deals: " + ", ".join(names)


def hit_title(h):
    d1, d2 = dt.date.fromisoformat(h["start"]), dt.date.fromisoformat(h["end"])
    where = h["name"].encode("ascii", "ignore").decode()
    dates = (f"{d1:%b} {d1.day}-{d2.day}" if d1.month == d2.month
             else f"{d1:%b} {d1.day}-{d2:%b} {d2.day}")
    return f"{where} {dates}" + (" (all nonstop)" if h["all_nonstop"] else "")


def notify(cfg, title, body):
    """Push via ntfy (any ntfy-compatible server), plus optional email.

    No Click header: tapping the push opens it in the ntfy app. NTFY_TOKEN, if
    set, is sent as a bearer token for servers or topics that require login.
    """
    n = cfg.get("notify", {})
    if n.get("ntfy_topic"):
        headers = {"Title": title, "Tags": "airplane"}
        if os.environ.get("NTFY_TOKEN"):
            headers["Authorization"] = "Bearer " + os.environ["NTFY_TOKEN"]
        server = n.get("ntfy_server", "https://ntfy.sh").rstrip("/")
        req = urllib.request.Request(f"{server}/{n['ntfy_topic']}",
                                     data=body.encode(), headers=headers)
        urllib.request.urlopen(req, timeout=30)
    if n.get("email_to"):
        msg = EmailMessage()
        msg["Subject"], msg["To"] = title, n["email_to"]
        msg["From"] = os.environ["SMTP_USER"]
        msg.set_content(body)
        with smtplib.SMTP_SSL(os.environ.get("SMTP_HOST", "smtp.gmail.com"), 465) as s:
            s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
            s.send_message(msg)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("config")
    ap.add_argument("--mock", help="JSON file of canned responses (offline test)")
    ap.add_argument("--dump", metavar="DIR", help="save raw API responses here")
    ap.add_argument("--no-notify", action="store_true",
                    help="print deals only; don't notify or record them as seen")
    ap.add_argument("--test-notify", action="store_true",
                    help="send a test notification and exit (no searches)")
    args = ap.parse_args()

    cfg_path = Path(args.config).resolve()
    cfg = normalize(json.loads(cfg_path.read_text()))
    load_env(cfg_path.parent / ".env")

    if args.test_notify:
        notify(cfg, "Flight Watch test", "Notifications are working.")
        print("Test notification sent.")
        return

    mock = json.loads(Path(args.mock).read_text()) if args.mock else None
    topic = cfg.get("notify", {}).get("ntfy_topic", "")
    if topic.upper().startswith("CHANGE-ME") and mock is None and not args.no_notify:
        sys.exit("Set notify.ntfy_topic in the config first (see README).")
    key = os.environ.get("SERPAPI_KEY", "")
    if not key and mock is None:
        sys.exit("Set SERPAPI_KEY (environment, or .env next to the config)")

    today = dt.date.today()
    api = SerpApi(key, cfg, mock, args.dump)
    print(f"--- {dt.datetime.now():%Y-%m-%d %H:%M}")

    # Don't start a run the quota can't finish
    if mock is None:
        need, left = planned_searches(cfg), api.searches_left()
        if left is not None and left < need:
            sys.exit(f"Skipping: run needs up to {need} searches, "
                     f"only {left} left this month")

    hits = search(cfg, api, today)

    # Only alert on new deals, or ones that got cheaper
    state_path = cfg_path.parent / cfg.get("state_file", "flight_watch_state.json")
    seen = json.loads(state_path.read_text()) if state_path.exists() else {}
    fresh = [h for h in hits if h["total"] < seen.get(h["key"], float("inf"))]

    # Stops/airline for the best new date of each destination
    groups = group_by_destination(fresh)
    for best, _ in groups[:cfg.get("detail_lookups", 3)]:
        add_details(best, api)
    groups.sort(key=lambda g: (not g[0]["all_nonstop"], g[0]["total"]))

    print(f"{api.calls} searches, {len(hits)} matching deals, {len(fresh)} new")
    if fresh:
        print(new_title(groups) + "\n" + format_new(groups))
    else:
        print(format_summary(hits))
    if args.no_notify:
        return
    if fresh:                            # all new deals in a single push
        notify(cfg, new_title(groups), format_new(groups))
    if not fresh:
        # Nothing new: one summary push, so a quiet run is still visible
        notify(cfg, f"Flight Watch: {len(hits)} matches, 0 new",
               format_summary(hits)[:3800])
        return
    for h in fresh:
        seen[h["key"]] = h["total"]
    if mock is None:
        state_path.write_text(json.dumps(seen, indent=1))


if __name__ == "__main__":
    main()
