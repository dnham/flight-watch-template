"""Offline tests for the cascade and matching logic. Run: python3 -m unittest -v"""
import datetime as dt
import json
import unittest
from pathlib import Path

import flight_watch as fw

HERE = Path(__file__).parent
TODAY = dt.date(2026, 11, 12)
LAS, DENVER, PHX = "/m/0cv3w", "/m/02cl1", "/m/0d35y"
CFG = {
    "origins": ["DSM", "SEA", "DEN", "LAX"],
    "origin_cities": {"DSM": "/m/02j3w", "DEN": DENVER,
                      "SEA": "/m/0d9jr", "LAX": "/m/030qb3t"},
    "destinations": {"Las Vegas": LAS, "Denver": DENVER, "Phoenix": PHX},
    "max_price": 200,
    "trip_nights": [3],
    "window_start": "2026-12-01",
    "window_end": "2027-03-31",
    "allow_origin_as_destination": True,
}


class Recorder(fw.SerpApi):
    """Mock API that also records which searches were made."""
    def __init__(self, cfg):
        super().__init__("", cfg, json.loads((HERE / "mock.json").read_text()))
        self.tags = []

    def _get(self, tag, **q):
        self.tags.append(tag)
        return super()._get(tag, **q)


def run(cfg=CFG):
    api = Recorder(cfg)
    return fw.search(cfg, api, TODAY), api


class SearchTest(unittest.TestCase):
    def setUp(self):
        self.hits, self.api = run()
        self.keys = [h["key"] for h in self.hits]

    def test_hits_need_every_origin_on_same_dates(self):
        # LAS Dec 11: DEN is $250. LAS Dec 18: DSM is $300. Phoenix: SEA $230.
        self.assertEqual(sorted(self.keys), [
            f"{DENVER}|2027-01-15|2027-01-18",
            f"{DENVER}|2027-01-22|2027-01-25",
            f"{LAS}|2026-12-04|2026-12-07",
        ])

    def test_totals_and_sorting(self):
        self.assertEqual([h["total"] for h in self.hits], [383, 389, 409])
        las = self.hits[-1]
        self.assertEqual({o: f["price"] for o, f in las["fares"].items()},
                         {"DSM": 148, "SEA": 117, "DEN": 89, "LAX": 55})

    def test_home_origin_is_exempt(self):
        den = self.hits[0]
        self.assertEqual(list(den["fares"]), ["DSM", "SEA", "LAX"])

    def test_exemption_can_be_disabled(self):
        hits, _ = run({**CFG, "allow_origin_as_destination": False})
        self.assertEqual([h["name"] for h in hits], ["Las Vegas"])

    def test_cascade_stops_early_in_config_order(self):
        phx = [t for t in self.api.tags if PHX in t]
        self.assertEqual(phx, [f"cal|DSM|{PHX}|3", f"cal|SEA|{PHX}|3"])
        self.assertEqual(self.api.calls, 4 + 3 + 2)
        self.assertLessEqual(self.api.calls, fw.planned_searches(CFG))
        self.assertEqual(fw.planned_searches(CFG), 4 + 3 + 4)

    def test_return_must_fall_inside_window(self):
        # Mar 30 + 3 nights returns Apr 2, past window_end
        self.assertFalse(any("2027-03-30" in k for k in self.keys))

    def test_request_range_is_capped_at_200_days(self):
        seen = []
        class Spy(Recorder):
            def calendar(self, origin, dest_id, nights, start, end):
                seen.append((start, end))
                return super().calendar(origin, dest_id, nights, start, end)
        cfg = {**CFG, "window_start": "2027-01-01", "window_end": "2028-01-01"}
        fw.search(cfg, Spy(cfg), TODAY)
        self.assertEqual(seen[0], (dt.date(2027, 1, 1), dt.date(2027, 7, 19)))

    def test_price_at_cap_counts_and_above_does_not(self):
        hits, _ = run({**CFG, "max_price": 148})   # DSM->LAS is exactly 148
        self.assertIn(f"{LAS}|2026-12-04|2026-12-07", [h["key"] for h in hits])
        hits, _ = run({**CFG, "max_price": 147})
        self.assertNotIn(f"{LAS}|2026-12-04|2026-12-07", [h["key"] for h in hits])


class DetailTest(unittest.TestCase):
    def setUp(self):
        self.hits, self.api = run()
        self.groups = fw.group_by_destination(self.hits)
        for best, _ in self.groups:
            fw.add_details(best, self.api)

    def test_grouping_keeps_cheapest_date_first(self):
        best, rest = self.groups[0]
        self.assertEqual((best["name"], best["start"]), ("Denver", "2027-01-15"))
        self.assertEqual([h["start"] for h in rest], ["2027-01-22"])

    def test_nonstop_flag_comes_from_details(self):
        den, las = self.groups[0][0], self.groups[1][0]
        self.assertTrue(las["all_nonstop"])
        self.assertEqual(las["fares"]["DSM"]["airline"], "Allegiant")
        self.assertFalse(den["all_nonstop"])          # SEA has 1 stop
        self.assertEqual(den["fares"]["SEA"]["stops"], 1)

    def test_deal_push_layout(self):
        best, rest = self.groups[0]                    # Denver, 2 dates
        self.assertEqual(fw.format_hit(best, rest, links=False).splitlines(), [
            "Fri Jan 15 \u2013 Mon Jan 18 (3 nights)",
            "",
            "DSM  $128 \u00b7 nonstop \u00b7 Frontier",
            "SEA  $157 \u00b7 1 stop \u00b7 Delta",
            "LAX  $98 \u00b7 nonstop \u00b7 United",
            "",
            "Other dates (1):",
            "Fri Jan 22 \u2013 Mon Jan 25",
            "  DSM $140 \u00b7 SEA $150 \u00b7 LAX $99",
        ])
        self.assertNotIn("total", fw.format_hit(best, rest))

    def test_links_and_title(self):
        best, rest = self.groups[1]                    # Las Vegas
        text = fw.format_hit(best, rest)
        self.assertIn("Fri Dec 4 \u2013 Mon Dec 7 (3 nights), all nonstop", text)
        self.assertIn("DSM: https://google.com/flights?q=DSM+to+LAS"
                      "+2026-12-04+to+2026-12-07\n", text)
        self.assertEqual(fw.hit_title(best),
                         "Las Vegas Dec 4-7 (all nonstop)")
        fw.hit_title(best).encode("ascii")     # must be header-safe


class ConfigFormsTest(unittest.TestCase):
    def test_destinations_as_airport_codes(self):
        cfg = fw.normalize({"origins": ["DSM", "DEN"], "max_price": 200,
                            "destinations": ["LAS", "DEN"]})
        self.assertEqual(cfg["destinations"], {"LAS": "LAS", "DEN": "DEN"})
        self.assertEqual(cfg["trip_nights"], [3])
        self.assertEqual(fw.needed_origins(cfg, "LAS"), ["DSM", "DEN"])
        self.assertEqual(fw.needed_origins(cfg, "DEN"), ["DSM"])   # home exempt

    def test_window_defaults_to_today_plus_one_search(self):
        cfg = fw.normalize({"origins": ["DSM"], "max_price": 200,
                            "destinations": ["LAS"]})
        start, end = fw.window(cfg, TODAY)
        self.assertEqual(start, TODAY)
        self.assertEqual((end - start).days, fw.MAX_RANGE_DAYS - 1 + 3)

    def test_window_start_in_the_past_moves_to_today(self):
        cfg = fw.normalize({**CFG, "window_start": "2026-01-01"})
        self.assertEqual(fw.window(cfg, TODAY)[0], TODAY)


class CombinedPushTest(unittest.TestCase):
    def setUp(self):
        self.groups = fw.group_by_destination(run()[0])

    def test_all_new_deals_share_one_body(self):
        body = fw.format_new(self.groups)
        self.assertEqual(fw.new_title(self.groups),
                         "New flight deals: Denver, Las Vegas")
        self.assertLess(body.index("DENVER"), body.index("LAS VEGAS"))
        self.assertIn("Other dates (1):", body)
        self.assertEqual(body.count("Book:"), 2)

    def test_long_bodies_shrink_to_fit_ntfy_limit(self):
        many = []
        for i in range(6):
            dates = [{"id": str(i), "name": f"City {i}",
                      "start": f"2027-02-{d:02d}", "end": f"2027-02-{d + 3:02d}",
                      "total": 500 + d, "all_nonstop": False,
                      "fares": {o: {"price": 150, "stops": 0, "airline": "Air"}
                                for o in ("DSM", "SEA", "DEN", "LAX")}}
                     for d in range(1, 21)]
            many.append((dates[0], dates[1:]))
        body = fw.format_new(many)
        self.assertLessEqual(len(body.encode()), 3800)
        for i in range(6):
            self.assertIn(f"CITY {i}", body)       # every city still present


class SummaryTest(unittest.TestCase):
    def test_summary_lists_every_match_by_destination(self):
        text = fw.format_summary(run()[0])
        self.assertEqual(text.splitlines(), [
            "Denver \u2013 2 dates",
            "Fri Jan 15 \u2013 Mon Jan 18",
            "  DSM $128 \u00b7 SEA $157 \u00b7 LAX $98",
            "Fri Jan 22 \u2013 Mon Jan 25",
            "  DSM $140 \u00b7 SEA $150 \u00b7 LAX $99",
            "",
            "Las Vegas \u2013 1 date",
            "Fri Dec 4 \u2013 Mon Dec 7",
            "  DSM $148 \u00b7 SEA $117 \u00b7 DEN $89 \u00b7 LAX $55",
        ])

    def test_long_date_lists_are_in_calendar_order_and_capped(self):
        hits = [{"id": "x", "name": "X", "start": f"2027-01-{d:02d}",
                 "end": f"2027-01-{d + 3:02d}", "total": 600 - d,
                 "fares": {"DSM": {"price": d}}}
                for d in range(20, 5, -1)]
        lines = fw.date_lines(hits)
        self.assertTrue(lines[0].startswith("Wed Jan 6"))
        self.assertEqual(lines[-1], "+3 more dates")
        self.assertEqual(len(lines), 2 * fw.MAX_DATES_LISTED + 1)

    def test_summary_when_nothing_matches(self):
        self.assertIn("No dates", fw.format_summary([]))


if __name__ == "__main__":
    unittest.main()
