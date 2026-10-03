import unittest

import egg_probe as ep


class FakeApi:
    def __init__(self, threads=(), unlocked=("abuela",), events=(), reply="Ay, hijo, qué bien que preguntas."):
        self.me_d = {"id": "t15", "cash": 10, "assets": [], "unlocked": list(unlocked), "score": {}}
        self.threads, self.events, self.reply, self.calls = list(threads), list(events), reply, []

    def me(self): return self.me_d
    def clock(self): return {"tick": 100, "tick_seconds": 0, "limits": {"max_open_threads_per_team": 6}}
    def my_threads(self): return {"threads": self.threads}
    def dealers(self): return {"personas": [{"id": "abuela", "enabled": True, "status": "active"}]}
    def feed(self, n): return {"events": self.events}
    def open_thread(self, w, topic=None): self.calls.append(("open", w, topic)); return {"id": 9}
    def say(self, tid, text="", price=None, offer=None): self.calls.append(("say", tid, text, price, offer)); return {"ok": 1}
    def thread(self, tid): return {"status": "open", "messages": [{"id": 1, "tick": 101, "sender": "abuela", "text": self.reply}]}
    def close_thread(self, tid): self.calls.append(("close", tid))


class Probe(unittest.TestCase):
    def setUp(self):
        self.old = ep.DATA
        import tempfile, pathlib
        self.d = tempfile.TemporaryDirectory()
        ep.DATA = pathlib.Path(self.d.name)

    def tearDown(self):
        ep.DATA = self.old
        self.d.cleanup()

    def test_dry_run_writes_nothing_to_the_server(self):
        api = FakeApi()
        r = ep.run(api, False, [])
        self.assertTrue(r["dry_run"] and not r["sent"])
        self.assertEqual(api.calls, [])

    def test_blockers(self):
        self.assertTrue(any("ejecutor" in p for p in ep.run(FakeApi(), True, ["pid coordinator.py"])["problems"]))
        busy = FakeApi(threads=[{"with": "abuela", "status": "open"}])
        r = ep.run(busy, True, [])
        self.assertTrue(any("ABIERTO" in p for p in r["problems"]))
        self.assertEqual(busy.calls, [])
        self.assertTrue(ep.run(FakeApi(unlocked=()), True, [])["problems"])

    def test_alongside_agent_allows_running_executor_but_not_an_open_abuela_thread(self):
        self.assertEqual(ep.run(FakeApi(), False, ["coordinator.py"], alongside=True)["problems"], [])
        busy = FakeApi(threads=[{"with": "abuela", "status": "open"}])
        self.assertTrue(ep.run(busy, True, ["coordinator.py"], alongside=True)["problems"])
        self.assertEqual(busy.calls, [])

    def test_one_message_no_price_no_offer_then_close(self):
        api = FakeApi()
        r = ep.run(api, True, [], sleep=lambda s: None)
        says = [c for c in api.calls if c[0] == "say"]
        self.assertEqual(len(says), 1)
        self.assertEqual((says[0][2], says[0][3], says[0][4]), (ep.MESSAGE, None, None))
        self.assertEqual(api.calls[0], ("open", "abuela", None))
        self.assertEqual(api.calls[-1][0], "close")
        self.assertEqual(r["outcome"], "respondió sin disparar huevo")
        self.assertTrue((ep.DATA / ep.LOG).exists())

    def test_egg_is_detected_only_from_public_server_events(self):
        ev = [{"type": "egg.found", "tick": 101, "payload": {"team": "t15", "persona": "abuela"}},
              {"type": "egg.found", "tick": 101, "payload": {"team": "t03", "persona": "abuela"}}]
        r = ep.run(FakeApi(events=ev), True, [], sleep=lambda s: None)
        self.assertTrue(r["outcome"].startswith("HUEVO"))
        self.assertEqual(len(ep.find_egg(ev, "t15", 100)["events"]), 1)
        self.assertFalse(ep.find_egg(ev, "t15", 200)["found"])

    def test_stops_on_warning(self):
        r = ep.run(FakeApi(reply="Basta, hijo, no more of this."), True, [], sleep=lambda s: None)
        self.assertIn("detenida", r["outcome"])


if __name__ == "__main__":
    unittest.main()
