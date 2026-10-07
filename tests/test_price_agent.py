import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import price_agent as pa


def nordpool_payload(delivery_date: date, prices_by_utc_hour: dict[datetime, float]) -> dict:
    return {
        "deliveryDateCET": delivery_date.isoformat(),
        "market": "DayAhead",
        "indexNames": ["EE"],
        "currency": "EUR",
        "resolutionInMinutes": 60,
        "areaStates": [{"state": "Final", "areas": ["EE"]}],
        "multiIndexEntries": [
            {
                "deliveryStart": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "deliveryEnd": (ts + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "entryPerArea": {"EE": price},
            }
            for ts, price in prices_by_utc_hour.items()
        ],
    }


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


class DayBoundsTest(unittest.TestCase):
    def test_summer_day_starts_21_utc(self):
        start, end = pa.local_day_bounds(date(2026, 10, 8))
        self.assertEqual(start, utc(2026, 10, 7, 21))
        self.assertEqual(end, utc(2026, 10, 8, 21))

    def test_dst_days(self):
        self.assertEqual(pa.expected_hours(date(2026, 10, 8)), 24)
        self.assertEqual(pa.expected_hours(date(2026, 10, 25)), 25)  # back to EET
        self.assertEqual(pa.expected_hours(date(2026, 3, 29)), 23)  # to EEST


class FetchNordpoolTest(unittest.TestCase):
    def test_combines_two_cet_days_into_estonian_day(self):
        day = date(2026, 10, 8)
        # CET (CEST) delivery day 2026-10-08 = 22:00Z on 7th .. 22:00Z on 8th
        cet_day = {utc(2026, 10, 7, 22) + timedelta(hours=i): 100.0 + i for i in range(24)}
        cet_prev = {utc(2026, 10, 6, 22) + timedelta(hours=i): 50.0 for i in range(24)}
        responses = {
            "2026-10-07": nordpool_payload(date(2026, 10, 7), cet_prev),
            "2026-10-08": nordpool_payload(day, cet_day),
        }
        with mock.patch.object(pa, "http_get_json", side_effect=lambda url, p: responses[p["date"]]):
            prices = pa.fetch_nordpool(day)

        self.assertEqual(len(prices), 24)
        hours = pa.to_hours(prices)
        self.assertEqual(hours[0].label, "00:00")
        self.assertEqual(hours[0].price, 50.0)  # from previous CET day
        self.assertEqual(hours[1].price, 100.0)
        self.assertEqual(hours[-1].label, "23:00")

    def test_not_published_yet(self):
        with mock.patch.object(pa, "http_get_json", return_value=None):
            self.assertEqual(pa.fetch_nordpool(date(2026, 10, 8)), {})

    def test_get_prices_falls_back_to_elering(self):
        day = date(2026, 10, 8)
        start, _ = pa.local_day_bounds(day)
        quarter_hours = [
            {"timestamp": int((start + timedelta(minutes=15 * i)).timestamp()), "price": float(i % 4)}
            for i in range(96)
        ]
        elering = {"success": True, "data": {"ee": quarter_hours}}

        def fake_get(url, params):
            return elering if url == pa.ELERING_URL else None

        with mock.patch.object(pa, "http_get_json", side_effect=fake_get):
            prices, source = pa.get_prices(day)
        self.assertEqual(source, "Elering")
        self.assertEqual(len(prices), 24)
        self.assertEqual(set(prices.values()), {1.5})  # avg of 0,1,2,3


class AnalysisTest(unittest.TestCase):
    def make_hours(self, prices: list[float]) -> list[pa.Hour]:
        start, _ = pa.local_day_bounds(date(2026, 10, 8))
        return pa.to_hours({start + timedelta(hours=i): p for i, p in enumerate(prices)})

    def test_quiet_day_has_no_alerts(self):
        hours = self.make_hours([80.0] * 24)
        msg = pa.build_message(date(2026, 10, 8), hours, "Nord Pool", pa.Config())
        self.assertEqual(msg["priority"], 3)
        self.assertNotIn("⚠️", msg["message"])
        self.assertIn("Avg 80.0 €/MWh (8.0 c/kWh)", msg["message"])

    def test_all_alerts(self):
        prices = [50.0] * 24
        prices[13] = prices[14] = -5.0
        prices[18], prices[19], prices[20] = 210.0, 260.0, 220.0
        hours = self.make_hours(prices)
        msg = pa.build_message(date(2026, 10, 8), hours, "Nord Pool", pa.Config())

        self.assertEqual(msg["priority"], 4)
        self.assertIn("Expensive (>200 €/MWh): 18:00–21:00; peak 260.0 at 19:00", msg["message"])
        self.assertIn("Very cheap (≤0 €/MWh): 13:00–15:00; lowest -5.0 at 13:00", msg["message"])
        self.assertIn("Big swing: 265.0 €/MWh", msg["message"])
        self.assertTrue(msg["title"].startswith("⚡ Price alert"))

    def test_range_ending_at_midnight(self):
        hours = self.make_hours([10.0] * 22 + [300.0, 300.0])
        alerts = pa.find_alerts(hours, pa.Config())
        self.assertIn("22:00–24:00", alerts[0])

    def test_cheapest_block(self):
        hours = self.make_hours([100.0] * 24)
        hours[3].price = hours[4].price = hours[5].price = 10.0
        block, avg = pa.cheapest_block(hours, 3)
        self.assertEqual(pa.fmt_range(block), "03:00–06:00")
        self.assertEqual(avg, 10.0)

    def test_config_from_env_ignores_empty(self):
        env = {"HIGH_PRICE": "150", "LOW_PRICE": "", "SHOW_HOURLY": "false"}
        with mock.patch.dict("os.environ", env, clear=True):
            cfg = pa.Config.from_env()
        self.assertEqual(cfg.high_price, 150.0)
        self.assertEqual(cfg.low_price, 0.0)
        self.assertFalse(cfg.show_hourly)


if __name__ == "__main__":
    unittest.main()
