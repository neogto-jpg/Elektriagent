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


class ScheduledRunTest(unittest.TestCase):
    def run_at(self, hour, *, sent=False, prices=True, day=9, minute=0):
        now = datetime(2026, 11, day, hour, minute, tzinfo=pa.TALLINN)
        tomorrow = now.date() + timedelta(days=1)
        start, _ = pa.local_day_bounds(tomorrow)
        result = ({start + timedelta(hours=i): 80.0 for i in range(24)}, "Nord Pool") if prices else None
        cfg = pa.Config(ntfy_topic="t")
        with mock.patch.object(pa, "already_sent", return_value=sent) as sent_mock, \
             mock.patch.object(pa, "get_prices", return_value=result) as get_mock, \
             mock.patch.object(pa, "deliver") as deliver_mock:
            code = pa.run_scheduled(cfg, dry_run=False, now=now)
        return code, sent_mock, get_mock, deliver_mock

    def test_run_delayed_past_midnight_does_nothing(self):
        # The 2026-11-09 job started at 00:57 on the 10th and asked for the 11th.
        code, _, get_mock, deliver_mock = self.run_at(0, minute=57, day=10)
        self.assertEqual(code, 0)
        get_mock.assert_not_called()
        deliver_mock.assert_not_called()

    def test_sends_tomorrow_once_prices_are_out(self):
        code, _, get_mock, deliver_mock = self.run_at(16)
        self.assertEqual(code, 0)
        get_mock.assert_called_once_with(date(2026, 11, 10))
        payload = deliver_mock.call_args.args[0]
        self.assertIn("ee-2026-11-10", payload["tags"])

    def test_skips_when_already_sent(self):
        code, _, get_mock, deliver_mock = self.run_at(17, sent=True)
        self.assertEqual(code, 0)
        get_mock.assert_not_called()
        deliver_mock.assert_not_called()

    def test_waits_quietly_for_next_run(self):
        code, _, _, deliver_mock = self.run_at(15, prices=False)
        self.assertEqual(code, 0)
        deliver_mock.assert_not_called()

    def test_gives_up_in_the_evening(self):
        code, _, _, deliver_mock = self.run_at(21, prices=False)
        self.assertEqual(code, 1)
        payload = deliver_mock.call_args.args[0]
        self.assertIn("not available", payload["title"])
        self.assertIn("ee-2026-11-10", payload["tags"])

    def test_already_sent_reads_ntfy_cache(self):
        cfg = pa.Config(ntfy_topic="my-topic")
        body = b'{"id":"a","event":"open"}\n{"id":"b","event":"message","tags":["ee-2026-11-10"]}\n'
        fake = mock.MagicMock()
        fake.__enter__.return_value.read.return_value = body
        with mock.patch.object(pa.urllib.request, "urlopen", return_value=fake) as urlopen:
            self.assertTrue(pa.already_sent(date(2026, 11, 10), cfg))
        url = urlopen.call_args.args[0].full_url
        self.assertIn("/my-topic/json?", url)
        self.assertIn("tags=ee-2026-11-10", url)

    def test_already_sent_false_on_error(self):
        cfg = pa.Config(ntfy_topic="my-topic")
        with mock.patch.object(pa.urllib.request, "urlopen", side_effect=pa.urllib.error.URLError("down")):
            self.assertFalse(pa.already_sent(date(2026, 11, 10), cfg))


if __name__ == "__main__":
    unittest.main()
