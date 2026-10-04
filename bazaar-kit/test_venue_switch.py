"""venue_switch.py con una API falsa: sin red, sin procesos y sin escribir nada en el servidor."""
import contextlib
import io
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import market_broker as mb
import venue_switch as vs

KEY = "bk_SECRETO_NUEVO"
STARTER = "bk_SECRETO_PUESTO"


class FakeTeam:
    def __init__(self, cash=300, level=3, venue=None, t_hours=6.2, tick=670, schedule=None, feed=None,
                 open_resp=None, me_key=False):
        self.cash, self.level, self.t_hours, self.tick = cash, level, t_hours, tick
        self.venue = venue or {"venue": "v15", "starter": True, "rules": {"mechanism": "auto"}, "status": "open"}
        self.sched = [{"action": "bench", "at_hours": 7.0, "note": "The Market Test"},
                      {"action": "duels", "at_hours": 6.5}] if schedule is None else schedule
        self.feed_ = {"events": [{"type": "bench.started", "tick": 441,
                                  "payload": {"session": 2, "ticks": 16, "start_tick": 441}}]} if feed is None else feed
        self.open_resp = {"venue": "v22", "broker_key": KEY, "bond": 250} if open_resp is None else open_resp
        self.me_key, self.opened = me_key, []

    def me(self):
        d = {"id": "t15", "cash": self.cash, "level": self.level, "venue": self.venue,
             "starter_broker_key": STARTER}
        if self.me_key and self.opened:
            d["venue"] = {**d["venue"], "broker_key": KEY}
        return d

    def my_offers(self):
        return {"offers": []}

    def clock(self):
        return {"tick": self.tick, "t_hours": self.t_hours, "tick_seconds": 30.0, "doors": "open"}

    def feed(self, limit=150):
        return self.feed_

    def schedule(self):
        return self.sched

    def open_venue(self, name, fee_bps=300, fee_per_card=0, rules=None, description=""):
        self.opened.append({"name": name, "fee_bps": fee_bps, "fee_per_card": fee_per_card, "rules": rules})
        return self.open_resp


def args(*argv):
    return vs.parse(list(argv))


def ok_selftest(mode, probe):
    return True, "falso"


def run(fn, *a, **k):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = fn(*a, **k)
    return rc, out.getvalue() + err.getvalue()


class Parsing(unittest.TestCase):
    def test_next_bench_list_and_dict(self):
        items = [{"action": "bench", "at_hours": 9.0}, {"action": "bench", "at_hours": 7.0},
                 {"action": "duels", "at_hours": 6.5}, {"action": "bench", "at_hours": 5.0}]
        self.assertEqual(vs.next_bench(vs.schedule_items(items), 6.0)["at_hours"], 7.0)
        self.assertEqual(vs.next_bench(vs.schedule_items({"schedule": items}), 8.0)["at_hours"], 9.0)
        self.assertIsNone(vs.next_bench(vs.schedule_items({"nada": 1}), 6.0))

    def test_running_bench_from_feed(self):
        feed = {"events": [{"type": "bench.started", "tick": 201, "payload": {"session": 1, "ticks": 16,
                                                                               "start_tick": 201}},
                           {"type": "bench.started", "tick": 441, "session": 2, "ticks": 16, "start_tick": 441}]}
        self.assertEqual(vs.running_bench(feed, 450), (2, 457))
        self.assertIsNone(vs.running_bench(feed, 457))
        self.assertEqual(vs.running_bench([], 450), "?")

    def test_find_broker_key_never_returns_the_starter_key(self):
        self.assertEqual(vs.find_broker_key({"venue": "v22", "broker_key": KEY}), KEY)
        self.assertEqual(vs.find_broker_key({"venue": {"id": "v22", "key": KEY}}), KEY)
        self.assertEqual(vs.find_broker_key({"data": [{"x": KEY}]}), KEY)
        self.assertIsNone(vs.find_broker_key({"starter_broker_key": STARTER}))
        self.assertEqual(vs.find_venue_id({"venue": {"venue": "v22"}}), "v22")

    def test_redact_hides_every_key(self):
        r = str(vs.redact({"venue": "v22", "broker_key": KEY, "x": [KEY], "token": "tk-abc"}))
        self.assertNotIn("SECRETO", r)
        self.assertNotIn("tk-abc", r)
        self.assertIn("v22", r)


class Preflight(unittest.TestCase):
    def test_dry_run_reports_deficit_and_never_writes(self):
        t = FakeTeam(cash=105)
        with patch.object(vs, "broker_selftest", ok_selftest):
            rc, out = run(vs.main, [], team=t)
        self.assertEqual(rc, 1)
        self.assertEqual(t.opened, [])
        self.assertIn("DÉFICIT 185 P", out)
        self.assertNotIn("SECRETO", out)

    def test_dry_run_ready(self):
        t = FakeTeam()
        with patch.object(vs, "broker_selftest", ok_selftest):
            rc, out = run(vs.main, [], team=t)
        self.assertEqual((rc, t.opened), (0, []))
        self.assertIn("Listo para --execute", out)
        self.assertIn("en 48 min", out)

    def test_blocks_level_existing_venue_running_bench_and_short_lead(self):
        cases = [(FakeTeam(level=1), "nivel"),
                 (FakeTeam(venue={"venue": "v22", "starter": False, "rules": {"mechanism": "board"}}), "venue actual"),
                 (FakeTeam(tick=445), "horario"),
                 (FakeTeam(t_hours=6.98), "horario"),
                 (FakeTeam(t_hours=None), "horario")]
        for t, name in cases:
            with self.subTest(name):
                p = vs.preflight(t, args(), ok_selftest)
                self.assertFalse(p.ok)
                self.assertFalse(next(c for c in p.checks if c.name == name).ok)

    def test_next_bench_in_param_when_schedule_missing(self):
        t = FakeTeam(t_hours=None)
        self.assertTrue(vs.preflight(t, args("--next-bench-in", "30"), ok_selftest).ok)

    def test_feed_without_sessions_warns_but_does_not_block(self):
        p = vs.preflight(FakeTeam(feed={"events": []}), args(), ok_selftest)
        self.assertTrue(p.ok)
        self.assertIn("AVISO", next(c for c in p.checks if c.name == "horario").detail)

    def test_failed_selftest_blocks(self):
        p = vs.preflight(FakeTeam(), args(), lambda m, pr: (False, "roto"))
        self.assertFalse(p.ok)

    def test_real_selftest_passes(self):
        ok, detail = vs.broker_selftest(sessions=4)
        self.assertTrue(ok, detail)

    def test_operating_reserve_includes_open_bid_commitments(self):
        t = FakeTeam(cash=420)
        t.my_offers = lambda: {"offers": [{"status": "open", "maker": "t15", "give": {"cash": 60}},
                                           {"status": "closed", "maker": "t15", "give": {"cash": 99}}]}
        p = vs.preflight(t, args("--operating-reserve", "100"), ok_selftest)
        self.assertEqual(p.need, 450)  # 270 + 20 colchón + 100 operación + 60 comprometido
        self.assertFalse(p.ok)
        self.assertIn("60 P", next(c for c in p.checks if c.name == "compromisos").detail)

    def test_operating_reserve_blocks_if_private_offers_unavailable(self):
        t = FakeTeam(cash=500)
        t.my_offers = lambda: (_ for _ in ()).throw(RuntimeError("sin respuesta"))
        p = vs.preflight(t, args("--operating-reserve", "100"), ok_selftest)
        self.assertFalse(p.ok)
        self.assertFalse(next(c for c in p.checks if c.name == "compromisos").ok)

    def test_key_file_is_private_and_recoverable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nested", "key")
            vs.save_broker_key(path, KEY)
            self.assertEqual(vs.load_broker_key(path), KEY)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            os.chmod(path, 0o644)
            self.assertIsNone(vs.load_broker_key(path))


class Execute(unittest.TestCase):
    def go(self, team, *argv):
        captured = {}

        def supervise(sup):
            captured["env"], captured["cmd"] = sup.env, sup.command()
            return 0

        a = args("--execute", *argv)
        plan = vs.preflight(team, a, ok_selftest)
        with patch.object(vs.Supervisor, "start", lambda self: None):
            rc, out = run(vs.execute, team, a, plan, supervise=supervise)
        return rc, out, captured

    def test_opens_board_at_zero_and_key_only_reaches_child_env(self):
        t = FakeTeam()
        with patch.dict(os.environ, {"BAZAAR_KEY": "tk-equipo"}):
            rc, out, cap = self.go(t)
        self.assertEqual(rc, 0)
        self.assertEqual(t.opened, [{"name": vs.DEFAULT_NAME, "fee_bps": 0, "fee_per_card": 0,
                                     "rules": {"mechanism": "board"}}])
        self.assertEqual(cap["env"]["BROKER_KEY"], KEY)
        self.assertNotIn("BAZAAR_KEY", cap["env"])
        self.assertNotIn(KEY, " ".join(cap["cmd"]))
        self.assertNotIn("SECRETO", out)
        self.assertIn("v22", out)

    def test_execute_persists_recovery_key_only_when_requested(self):
        with tempfile.TemporaryDirectory() as tmp:
            key_file = os.path.join(tmp, "broker.key")
            t = FakeTeam(cash=500)
            with patch.dict(os.environ, {"BAZAAR_KEY": "tk-equipo"}):
                rc, out, cap = self.go(t, "--key-file", key_file, "--operating-reserve", "100")
            self.assertEqual(rc, 0)
            self.assertEqual(vs.load_broker_key(key_file), KEY)
            self.assertEqual(os.stat(key_file).st_mode & 0o777, 0o600)
            self.assertEqual(cap["env"]["BROKER_KEY"], KEY)
            self.assertNotIn(KEY, out)

    def test_blocked_plan_never_opens(self):
        t = FakeTeam(cash=10)
        rc, out, cap = self.go(t)
        self.assertEqual((rc, t.opened, cap), (2, [], {}))

    def test_missing_key_alerts_and_stops(self):
        t = FakeTeam(open_resp={"venue": "v22"})
        rc, out, cap = self.go(t)
        self.assertEqual((rc, cap), (4, {}))
        self.assertIn("SIN broker key", out)

    def test_key_recovered_from_me_when_response_lacks_it(self):
        t = FakeTeam(open_resp={"venue": "v22"}, me_key=True)
        rc, out, cap = self.go(t)
        self.assertEqual(cap["env"]["BROKER_KEY"], KEY)
        self.assertIn("SÍ expone", out)

    def test_announce_only_after_first_beat(self):
        said = []
        broker = SimpleNamespace(announce=lambda text: said.append(text))
        with patch.object(vs.Supervisor, "start", lambda self: None), \
                patch.object(vs, "wait_first_beat", return_value=True):
            run(vs.run_broker, KEY, args("--announce"), venue="v22", announce=True, make_broker=lambda k: broker,
                supervise=lambda s: 0)
        self.assertEqual(len(said), 1)
        self.assertIn("v22", said[0])
        self.assertIn("0 % fee", said[0])
        said.clear()
        with patch.object(vs.Supervisor, "start", lambda self: None), \
                patch.object(vs, "wait_first_beat", return_value=False):
            _, out = run(vs.run_broker, KEY, args(), announce=True, make_broker=lambda k: broker,
                         supervise=lambda s: 0)
        self.assertEqual(said, [])
        self.assertIn("no anuncio", out)


class FakeProc:
    def __init__(self, script):
        self.script, self.killed = list(script), False

    def poll(self):
        return self.script.pop(0) if self.script else None

    def kill(self):
        self.killed = True

    def wait(self):
        return -9

    def terminate(self):
        self.killed = True


class Supervision(unittest.TestCase):
    def make(self, scripts, ages=None, **kw):
        now = [0.0]
        procs, alerts, cmds = [], [], []

        def popen(cmd, env, cwd):
            cmds.append(cmd)
            p = FakeProc(scripts.pop(0) if scripts else [])
            procs.append(p)
            return p

        def sleep(s):
            now[0] += s

        a = args(*kw.get("argv", ()))
        sup = vs.Supervisor({"BROKER_KEY": KEY}, a, "/no/existe", popen=popen, clock=lambda: now[0], sleep=sleep,
                            alert=alerts.append, beat_age=lambda p: ages(now[0]) if ages else None)
        return sup, procs, alerts, cmds, now

    def test_restarts_after_crash_with_alert(self):
        sup, procs, alerts, cmds, _ = self.make([[1], [None]])
        for _ in range(4):
            self.assertIsNone(sup.poll_once())
        self.assertEqual(len(procs), 2)
        self.assertTrue(any("caído" in x for x in alerts))
        self.assertIn("market_broker.py", cmds[1][1])

    def test_falls_back_to_starter_broker_after_repeated_crashes(self):
        sup, procs, alerts, cmds, _ = self.make([[1], [1], [1], [None]])
        for _ in range(8):
            sup.poll_once()
        self.assertTrue(sup.fallback)
        self.assertIn("starter_broker.py", cmds[-1][1])
        self.assertTrue(any("starter_broker" in x for x in alerts))

    def test_fatal_exit_stops_without_restart(self):
        sup, procs, alerts, _, _ = self.make([[mb.EXIT_FATAL]])
        sup.poll_once()
        self.assertEqual(sup.poll_once(), mb.EXIT_FATAL)
        self.assertEqual(len(procs), 1)
        self.assertTrue(any("rechazada" in x for x in alerts))

    def test_stale_heartbeat_kills_and_restarts(self):
        sup, procs, alerts, _, now = self.make([[], []], ages=lambda t: 500.0)
        sup.poll_once()          # arranca
        sup.poll_once()          # aún en el periodo de gracia
        self.assertFalse(procs[0].killed)
        now[0] += 200
        sup.poll_once()          # sin latido: lo mata
        self.assertTrue(procs[0].killed)
        sup.poll_once()          # relanza
        self.assertEqual(len(procs), 2)
        self.assertTrue(any("latido" in x for x in alerts))

    def test_fresh_heartbeat_keeps_running(self):
        sup, procs, alerts, _, now = self.make([[]], ages=lambda t: 2.0)
        for _ in range(5):
            now[0] += 100
            sup.poll_once()
        self.assertEqual((len(procs), alerts), (1, []))

    def test_ctrl_c_terminates_child_and_warns(self):
        sup, procs, alerts, _, _ = self.make([[]])
        sup.poll_once()
        sup.sleep = lambda s: (_ for _ in ()).throw(KeyboardInterrupt)
        self.assertEqual(sup.run(), 130)
        self.assertTrue(procs[0].killed)
        self.assertTrue(any("NO CRUZA" in x for x in alerts))

    def test_child_env_drops_team_key(self):
        with patch.dict(os.environ, {"BAZAAR_KEY": "tk-equipo", "STARTER_BROKER_KEY": STARTER}):
            env = vs.child_env(KEY)
        self.assertEqual(env["BROKER_KEY"], KEY)
        self.assertNotIn("BAZAAR_KEY", env)
        self.assertNotIn("STARTER_BROKER_KEY", env)


class Resume(unittest.TestCase):
    def test_resume_uses_env_key_without_opening(self):
        t = FakeTeam()
        seen = {}
        with patch.dict(os.environ, {"BROKER_KEY": KEY}), \
                patch.object(vs, "run_broker", lambda key, a, **k: seen.setdefault("key", key) and 0):
            rc = vs.main(["--resume"], team=t)
        self.assertEqual((rc, seen["key"], t.opened), (0, KEY, []))

    def test_resume_without_any_key(self):
        t = FakeTeam()
        env = {k: v for k, v in os.environ.items() if k != "BROKER_KEY"}
        with patch.dict(os.environ, env, clear=True):
            rc, out = run(vs.main, ["--resume"], team=t)
        self.assertEqual(rc, 4)
        self.assertNotIn("SECRETO", out)


class Heartbeat(unittest.TestCase):
    def test_wait_first_beat(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "hb")
            self.assertFalse(vs.wait_first_beat(p, 0, timeout=0.01, sleep=lambda s: None))
            mb.beat(p, 12)
            self.assertTrue(vs.wait_first_beat(p, 0, timeout=1))
            with open(p, encoding="utf-8") as f:
                self.assertTrue(f.read().startswith("12 "))


if __name__ == "__main__":
    unittest.main()
