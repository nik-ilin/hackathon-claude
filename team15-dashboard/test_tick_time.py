import unittest

import tick_time


class TickTimeTests(unittest.TestCase):
    def test_exact_and_interpolated_observations_are_local(self):
        samples = [{"tick": 10, "captured_at": 1_800_000_000},
                   {"tick": 20, "captured_at": 1_800_000_600}]
        self.assertEqual(tick_time.estimate(10, current_tick=20, captured_at=1_800_000_600,
                                            clock={}, samples=samples)["quality"], "observed")
        middle = tick_time.estimate(15, current_tick=20, captured_at=1_800_000_600,
                                    clock={}, samples=samples)
        self.assertEqual(middle["quality"], "interpolated")
        self.assertIsNotNone(middle["label"])

    def test_future_tick_is_estimated_and_pause_is_not_extrapolated(self):
        estimate = tick_time.estimate(12, current_tick=10, captured_at=1000,
                                      clock={"tick_seconds": 10, "next_tick_in": 4})
        self.assertEqual(estimate["quality"], "estimated")
        paused = tick_time.estimate(12, current_tick=10, captured_at=1000,
                                   clock={"paused": True, "tick_seconds": 10})
        self.assertEqual(paused["quality"], "paused")

    def test_unknown_historical_tick_is_not_fabricated(self):
        result = tick_time.estimate(1, current_tick=10, captured_at=1000,
                                    clock={"tick_seconds": 10})
        self.assertEqual(result, {"label": None, "quality": "unknown"})


if __name__ == "__main__":
    unittest.main()
