"""Cambio del puesto gratuito (auto) a un venue `board` propio con market_broker.py vivo, y su supervisor.

    BAZAAR_KEY=tk_... python3 venue_switch.py                       # EN SECO (por defecto): comprobaciones y plan
    BAZAAR_KEY=tk_... python3 venue_switch.py --execute --announce  # abre el venue y deja el broker corriendo
    BROKER_KEY=bk_... python3 venue_switch.py --resume              # solo relanza el supervisor (venue ya abierto)

Por qué. En el Market Test un broker solo actúa en un venue `board`; en el puesto (auto) el motor cruza antes que nadie.
Los `board` con broker vivo (t12 v02, t10 v07, t06 v01) sacan 12,5 / 12,1 / 11,6 frente a 7,5 del puesto; los `board`
sin proceso vivo no cruzan nada (t13 5,5, t03 3,6).

Comprobaciones previas (solo GET; en seco no se escribe nada):
- caja >= 270 P (fianza 250 reembolsable + 20 de apertura) + colchón (`--cushion`, 20 P por defecto);
- nivel >= 2 y que no tengamos ya un venue propio (entonces: `--resume`);
- horario: puertas abiertas, ninguna sesión del Market Test en curso (`bench.started` del feed + sus ticks) y al menos
  `--min-lead` minutos hasta la próxima (de /api/schedule y /api/clock; si no se puede leer, `--next-bench-in MIN`).
  La lista de venues de una sesión se fija al empezar: hay que abrir antes y nunca a mitad de una;
- market_broker importa y pasa su autotest (libros malformados, suelo del puesto y banco offline frente al puesto).

Con `--execute`: repite las comprobaciones, abre `board` a 0 bps y 0 P por carta (POST /api/venues, la ÚNICA escritura
además del anuncio opcional), toma la broker key de la respuesta SOLO en memoria y la pasa al entorno del proceso hijo
(market_broker.py, sin BAZAAR_KEY); nunca se imprime, ni se escribe a disco ni a un log. El supervisor relanza el broker
si muere o si su latido se para, con espera creciente y ALERTA en stderr (y `--alert-cmd`, si se da). Tras
`--fallback-after` caídas seguidas en 10 min pasa a starter_broker.py (oficial, cruza como el puesto): mejor la mitad de
los puntos que cero. Si la clave es rechazada (EXIT_FATAL), para y alerta: reintentar no arreglaría nada.

RIESGOS
- **Si el proceso cae, el venue no cruza nada** y esa sesión puntúa como los board muertos (o 0). El supervisor y el
  latido lo mitigan, no lo eliminan: la máquina tiene que estar encendida, sin suspenderse y con red durante las sesiones.
- **La broker key se devuelve una sola vez** (bazaar_sdk.open_venue) y aquí solo vive en memoria. Si el supervisor muere,
  `--resume` la busca en /api/me (si el servidor la expone, como hace con `starter_broker_key`) o en BROKER_KEY; si no
  está en ningún sitio, el venue queda sin broker para siempre. Tras abrir, el script dice si /api/me la expone.
- Coste: 20 P perdidos seguro y 250 P inmovilizados (la caja deja de servir para packs o pujas).

VUELTA ATRÁS (RULES.md, «Your own market»)
- Cerrar: `POST /api/venues/{id}/close`; la fianza vuelve tras un cooldown, los 20 P no.
- **No hay vuelta documentada al puesto gratuito.** RULES.md solo dice que el puesto se da a los equipos sin venue al
  empezar (+3 h) y que abrir uno propio lo reemplaza; en market.db los puestos de t01, t03 y t09 se cerraron con
  `replaced: true, refund: 0` al abrir y nadie ha cerrado nunca su venue propio, así que no hay evidencia de que vuelva.
  Y «cada sesión cuenta tu mejor venue abierto (ninguno cuenta 0)»: cerrar sin puesto deja las sesiones a 0. La vuelta
  atrás real es `--broker-mode stall` (o starter_broker.py), que en un board cruza igual que el puesto, no cerrar.
"""
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
BOND, OPEN_FEE, MIN_LEVEL = 250, 20, 2
NAME_MAX = 40  # RULES.md: un nombre de venue guarda 40 caracteres
DEFAULT_NAME = "Team 15 · broker 0 %"
DEFAULT_DESC = "Board venue, 0 % fee, 0 P per card. A broker crosses every crossing pair every tick at the midpoint."
DEFAULT_ANNOUNCE = ("Team 15 market ({venue}): board venue, 0 % fee and 0 P per card. Our broker pairs crossing "
                    "offers card by card every tick at the midpoint. El Rastro takes 5 % + 1 P a card.")
WINDOW = 600.0  # caídas contadas para pasar al broker de reserva (s)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    blocking: bool = True


@dataclass
class Plan:
    checks: list = field(default_factory=list)
    cash: int = 0
    need: int = 0
    venue: dict | None = None

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks if c.blocking)

    @property
    def deficit(self) -> int:
        return max(self.need - self.cash, 0)


# ---------------------------------------------------------------------------------------------------- lectura (GET)
def schedule_items(payload) -> list:
    """Entradas de /api/schedule: la API real devuelve una lista; se aceptan también {schedule|events|items: [...]}."""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for k in ("schedule", "events", "items", "upcoming"):
            if isinstance(payload.get(k), list):
                return [x for x in payload[k] if isinstance(x, dict)]
    return []


def next_bench(items: list, now_h: float):
    """La próxima sesión del Market Test (acción 'bench') después de now_h, o None."""
    future = [x for x in items if x.get("action") == "bench" and isinstance(x.get("at_hours"), (int, float))
              and x["at_hours"] > now_h]
    return min(future, key=lambda x: x["at_hours"], default=None)


def feed_events(payload) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in ("events", "feed", "items"):
            if isinstance(payload.get(k), list):
                return payload[k]
    return []


def running_bench(feed, tick: int):
    """(sesión, tick final) del último bench.started si sigue en curso en `tick`; None si ya acabó; '?' si el feed no
    trae ninguno."""
    last = None
    for e in feed_events(feed):
        if not isinstance(e, dict) or e.get("type") != "bench.started":
            continue
        p = e.get("payload") if isinstance(e.get("payload"), dict) else e.get("data") if isinstance(e.get("data"), dict) else e
        start = p.get("start_tick", e.get("tick"))
        if isinstance(start, int) and (last is None or start > last[0]):
            last = (start, start + int(p.get("ticks") or 16), p.get("session"))
    if last is None:
        return "?"  # el feed ya no llega a la última sesión: no se sabe
    if last[0] <= tick < last[1]:
        return last[2], last[1]
    return None


def find_broker_key(payload, path: str = ""):
    """La broker key de una respuesta (recorre dicts y listas). Prefiere un campo *broker_key* que no sea del puesto;
    si no, cualquier cadena 'bk_...'. Nunca devuelve starter_broker_key: tras abrir, el puesto ya no existe."""
    found = []

    def walk(x, k=""):
        if isinstance(x, dict):
            for kk, v in x.items():
                walk(v, kk)
        elif isinstance(x, list):
            for v in x:
                walk(v, k)
        elif isinstance(x, str) and "starter" not in k.lower():
            if "broker_key" in k.lower() or k.lower() == "key" and x.startswith("bk_"):
                found.append((0, x))
            elif x.startswith("bk_"):
                found.append((1, x))
    walk(payload)
    return min(found)[1] if found else None


def find_venue_id(payload):
    if isinstance(payload, dict):
        for k in ("venue", "id", "venue_id"):
            v = payload.get(k)
            if isinstance(v, str) and v.startswith("v"):
                return v
            if isinstance(v, dict):
                got = find_venue_id(v)
                if got:
                    return got
    return None


def redact(x):
    """Copia sin ningún valor que pueda ser una clave, para poder imprimir una respuesta."""
    if isinstance(x, dict):
        return {k: "***" if "key" in k.lower() else redact(v) for k, v in x.items()}
    if isinstance(x, list):
        return [redact(v) for v in x]
    if isinstance(x, str) and (x.startswith("bk_") or x.startswith("tk")):
        return "***"
    return x


# ---------------------------------------------------------------------------------------------------- autotest
def broker_selftest(mode: str = "smart", probe: int = 3, sessions: int = 12) -> tuple:
    """(ok, detalle): market_broker importa, no se cae con libros malformados, cubre el plan del puesto y en el banco
    offline no rinde menos que el puesto en los escenarios con sombreado. 'sin_sombra' se informa, no bloquea."""
    try:
        import market_broker as mb
        import sim_bench as sim
        junk = {"bench_offers": [None, {"id": 7}, {"id": "b1-x", "give": {"cash": "9"}, "want": None},
                                 {"id": "b1-y", "give": {}, "want": {"cash": 5.0}}, {"id": "b1-z", "give": {"cash": 3},
                                                                                       "want": {"cash": 4}}],
                "fee_bps": None}
        clean = mb.clean_book(junk)
        assert [o["id"] for o in clean["bench_offers"]] == ["b1-x", "b1-y"], "clean_book no filtra bien"
        assert mb.clean_book(None)["bench_offers"] == [] and mb.clean_book([])["offers"] == []
        plan = mb.BenchBroker(mode, "cover", probe).plan(clean, 0)
        assert {(m.sell, m.buy) for m in plan} >= {(s, b) for s, b, _ in mb.stall_plan(clean)}, "no cubre al puesto"
        assert callable(mb.main) and mb.EXIT_FATAL
        mech = {"x": lambda runs, srv, T: mb.BenchBroker(mode, "cover", probe=probe)}
        out = sim.bench(["normal", "dificil", "ruidoso", "sin_sombra"], sessions, 3, mechanisms=mech)
        ratios = {k: v["x"]["ratio"] for k, v in out.items()}
    except Exception as e:  # noqa: BLE001 — cualquier fallo es un autotest suspendido
        return False, f"{type(e).__name__}: {e}"
    bad = {k: r for k, r in ratios.items() if k != "sin_sombra" and r < 0.995}
    detail = ", ".join(f"{k} ×{r:.3f}" for k, r in ratios.items()) + " frente al puesto (banco offline)"
    if ratios["sin_sombra"] < 0.995:
        detail += "; sin sombreado rinde menos: valorar --broker-mode stall si el banco real no sombrea"
    return not bad, detail


# ---------------------------------------------------------------------------------------------------- plan
def preflight(team, args, selftest=broker_selftest) -> Plan:
    plan = Plan(need=BOND + OPEN_FEE + args.cushion)
    me = team.me()
    plan.cash, level = int(me.get("cash") or 0), int(me.get("level") or 0)
    plan.venue = me.get("venue") if isinstance(me.get("venue"), dict) else None
    plan.checks.append(Check("caja", plan.cash >= plan.need,
                             f"{plan.cash} P; hacen falta {plan.need} P ({BOND} fianza + {OPEN_FEE} apertura + "
                             f"{args.cushion} colchón)" + (f"; DÉFICIT {plan.deficit} P" if plan.deficit else "")))
    plan.checks.append(Check("nivel", level >= MIN_LEVEL, f"nivel {level} (mínimo {MIN_LEVEL})"))
    v = plan.venue or {}
    own = bool(v) and not v.get("starter") and v.get("status", "open") == "open"
    plan.checks.append(Check("venue actual", not own,
                             f"{v.get('venue', '—')} {(v.get('rules') or {}).get('mechanism', '?')}"
                             f"{' (puesto gratuito)' if v.get('starter') else ''}"
                             + ("; ya tenemos venue propio: usar --resume" if own else "")))
    plan.checks.append(Check("nombre", 0 < len(args.name) <= NAME_MAX, f"«{args.name}» ({len(args.name)}/{NAME_MAX})"))
    plan.checks.append(timing(team, args))
    ok, detail = selftest(args.broker_mode, args.probe)
    plan.checks.append(Check("autotest market_broker", ok, detail))
    return plan


def timing(team, args) -> Check:
    try:
        clock = team.clock()
    except Exception as e:  # noqa: BLE001
        return Check("horario", False, f"no se puede leer /api/clock ({e})")
    if clock.get("doors") == "closed":
        return Check("horario", False, "puertas cerradas")
    tick, now_h, tick_s = clock.get("tick"), clock.get("t_hours"), float(clock.get("tick_seconds") or 30)
    running = None
    if isinstance(tick, int):
        try:
            running = running_bench(team.feed(limit=500), tick)
        except Exception:  # noqa: BLE001 — sin feed solo se pierde esta comprobación, se dice abajo
            running = "?"
    if running and running != "?":
        session, end = running
        return Check("horario", args.allow_during_bench,
                     f"sesión {session} del Market Test en curso hasta el tick {end} (~{(end - tick) * tick_s / 60:.0f}"
                     " min): abrir ahora cierra el puesto a mitad de sesión")
    minutes, src = None, ""
    if isinstance(now_h, (int, float)):
        try:
            nb = next_bench(schedule_items(team.schedule()), now_h)
            if nb:
                minutes, src = (nb["at_hours"] - now_h) * 60, f"schedule ({nb.get('note', 'bench')})"
        except Exception:  # noqa: BLE001
            pass
    if minutes is None and args.next_bench_in is not None:
        minutes, src = args.next_bench_in, "--next-bench-in"
    if minutes is None:
        return Check("horario", False, "no sé cuándo es el próximo Market Test: pasar --next-bench-in MIN")
    unknown = running == "?" or not isinstance(tick, int)
    warn = "; AVISO: el feed no permite saber si hay una sesión en curso" if unknown else ""
    return Check("horario", minutes >= args.min_lead,
                 f"próximo Market Test en {minutes:.0f} min según {src} (mínimo {args.min_lead}){warn}")


def report(plan: Plan, args) -> str:
    lines = ["Comprobaciones:"]
    lines += [f"  [{'OK' if c.ok else 'NO' if c.blocking else '!!'}] {c.name}: {c.detail}" for c in plan.checks]
    lines += ["", "Plan" + (" (EN SECO: no se escribe nada)" if not args.execute else "") + ":",
              f"  1. POST /api/venues name=«{args.name}» fee_bps=0 fee_per_card=0 rules.mechanism=board "
              f"(cuesta {BOND}+{OPEN_FEE} P; reemplaza el puesto al instante)",
              "  2. broker key solo en memoria → entorno de market_broker.py "
              f"--mode {args.broker_mode} --probe {args.probe} (sin BAZAAR_KEY)",
              f"  3. supervisor: relanza si muere o si no hay latido en {args.stale:.0f} s; reserva starter_broker.py "
              f"tras {args.fallback_after} caídas en {WINDOW / 60:.0f} min",
              "  4. " + ("anuncio inicial veraz (POST /api/broker/announce)" if args.announce else "sin anuncio")]
    if plan.deficit:
        lines.append(f"\nDéficit de caja: {plan.deficit} P (tenemos {plan.cash}, hacen falta {plan.need}).")
    lines.append("\nListo para --execute." if plan.ok else "\nNO se puede ejecutar: hay comprobaciones en NO.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------------- supervisor
class Supervisor:
    """Mantiene vivo el broker del venue. Inyectable (popen, reloj, sleep) para probarlo sin procesos ni red."""

    def __init__(self, env: dict, args, heartbeat: str, *, popen=subprocess.Popen, clock=time.monotonic,
                 sleep=time.sleep, alert=None, beat_age=None):
        self.env, self.args, self.heartbeat = env, args, heartbeat
        self.popen, self.clock, self.sleep = popen, clock, sleep
        self.alert = alert or (lambda msg: alert_default(msg, args.alert_cmd))
        self.beat_age = beat_age or (lambda p: time.time() - os.path.getmtime(p) if os.path.exists(p) else None)
        self.proc, self.started, self.crashes, self.fallback = None, 0.0, [], False

    def command(self) -> list:
        if self.fallback:
            return [sys.executable, str(HERE / "starter_broker.py")]
        return [sys.executable, str(HERE / "market_broker.py"), "--mode", self.args.broker_mode,
                "--probe", str(self.args.probe), "--heartbeat", self.heartbeat]

    def start(self) -> None:
        recent = [t for t in self.crashes if self.clock() - t < WINDOW]
        if not self.fallback and len(recent) >= self.args.fallback_after:
            self.fallback = True
            self.alert(f"{len(recent)} caídas en {WINDOW / 60:.0f} min: paso a starter_broker.py (cruza como el puesto)")
        self.proc, self.started = self.popen(self.command(), env=self.env, cwd=str(HERE)), self.clock()

    def crashed(self, why: str) -> None:
        self.crashes.append(self.clock())
        n = len([t for t in self.crashes if self.clock() - t < WINDOW])
        wait = min(2 ** n, 30)
        self.alert(f"broker caído ({why}); EL VENUE NO CRUZA hasta relanzarlo. Relanzo en {wait} s")
        self.proc = None
        self.sleep(wait)

    def poll_once(self):
        """Un paso del bucle. Devuelve el código de salida si hay que parar (clave rechazada), si no None."""
        from market_broker import EXIT_FATAL
        if self.proc is None:
            self.start()
            return None
        rc = self.proc.poll()
        if rc == EXIT_FATAL:
            self.alert("la broker key fue rechazada: paro. El venue NO cruza; revisar el venue y la clave a mano")
            return rc
        if rc is not None:
            self.crashed(f"salió con código {rc}")
            return None
        if self.fallback:
            return None  # starter_broker.py no escribe latido
        age = self.beat_age(self.heartbeat)
        if age is None:
            age = self.clock() - self.started
        if age > self.args.stale and self.clock() - self.started > self.args.stale:
            self.proc.kill()
            self.proc.wait()
            self.crashed(f"sin latido desde hace {age:.0f} s")
        return None

    def run(self) -> int:
        try:
            while True:
                rc = self.poll_once()
                if rc is not None:
                    return rc
                self.sleep(2.0)
        except KeyboardInterrupt:
            if self.proc is not None:
                self.proc.terminate()
            self.alert("supervisor parado a mano: EL VENUE YA NO CRUZA. Relanzar con --resume antes del próximo "
                       "Market Test (cerrar el venue no devuelve el puesto: ver «VUELTA ATRÁS» en venue_switch.py)")
            return 130


def alert_default(msg: str, cmd: str | None = None) -> None:
    print(f"\a[{time.strftime('%H:%M:%S')}] ALERTA venue: {msg}", file=sys.stderr, flush=True)
    if cmd:
        try:
            subprocess.run([*shlex.split(cmd), msg], timeout=10, check=False)
        except Exception as e:  # noqa: BLE001
            print(f"--alert-cmd falló ({e})", file=sys.stderr)


def child_env(broker_key: str) -> dict:
    """Entorno del broker: la broker key y nada de la clave del equipo (el broker no la necesita)."""
    env = {k: v for k, v in os.environ.items() if k not in ("BAZAAR_KEY", "STARTER_BROKER_KEY")}
    env["BROKER_KEY"] = broker_key
    return env


def heartbeat_path() -> str:
    d = HERE / "data"
    try:
        d.mkdir(exist_ok=True)
    except OSError:
        d = Path(tempfile.gettempdir())
    return str(d / "venue_broker.heartbeat")


def wait_first_beat(path: str, since: float, timeout: float = 60.0, sleep=time.sleep) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if os.path.exists(path) and os.path.getmtime(path) >= since:
            return True
        sleep(1.0)
    return False


# ---------------------------------------------------------------------------------------------------- ejecución
def execute(team, args, plan: Plan, *, make_broker=None, supervise=None) -> int:
    """Abre el venue, arranca el supervisor y, si se pidió, anuncia. La broker key no sale de esta función salvo hacia
    el entorno del hijo."""
    if not plan.ok:
        print("NO se ejecuta: hay comprobaciones en NO (nada enviado).")
        return 2
    resp = team.open_venue(args.name, fee_bps=0, fee_per_card=0, rules={"mechanism": "board"},
                           description=args.description)
    venue, key = find_venue_id(resp), find_broker_key(resp)
    print(f"Venue abierto: {venue or '?'} · respuesta (sin claves): {redact(resp)}")
    try:
        from_me = find_broker_key(team.me())
    except Exception:  # noqa: BLE001
        from_me = None
    in_me, key = from_me is not None, key or from_me
    print(f"/api/me {'SÍ' if in_me else 'NO'} expone la broker key del venue nuevo"
          + ("" if in_me else ": si este proceso muere, solo se puede relanzar con BROKER_KEY"))
    if key is None:
        alert_default("venue abierto pero SIN broker key en la respuesta ni en /api/me: el venue NO cruza. "
                      "Revisar a mano YA (o cerrar el venue: la fianza vuelve tras un cooldown)", args.alert_cmd)
        return 4
    return run_broker(key, args, venue=venue, announce=args.announce, make_broker=make_broker, supervise=supervise)


def run_broker(key: str, args, *, venue=None, announce=False, make_broker=None, supervise=None) -> int:
    hb = heartbeat_path()
    sup = Supervisor(child_env(key), args, hb)
    t0 = time.time()
    sup.start()
    if announce:
        if wait_first_beat(hb, t0):
            try:
                make = make_broker or default_broker
                make(key).announce(args.announce_text.format(venue=venue or "our venue"))
                print("Anuncio enviado.")
            except Exception as e:  # noqa: BLE001 — el anuncio es opcional; el broker sigue
                print(f"anuncio rechazado ({e}); el broker sigue")
        else:
            print("Sin latido del broker en 60 s: no anuncio (sería falso decir que cruzamos)")
    return (supervise or Supervisor.run)(sup)


def default_broker(key: str):
    from bazaar_sdk import Broker
    return Broker(os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai"), key)


def parse(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--execute", action="store_true", help="abrir el venue de verdad (por defecto, en seco)")
    ap.add_argument("--resume", action="store_true", help="venue ya abierto: solo arrancar el supervisor")
    ap.add_argument("--announce", action="store_true", help="anuncio inicial veraz cuando el broker ya late")
    ap.add_argument("--announce-text", default=DEFAULT_ANNOUNCE)
    ap.add_argument("--name", default=DEFAULT_NAME)
    ap.add_argument("--description", default=DEFAULT_DESC)
    ap.add_argument("--cushion", type=int, default=20, help="colchón de caja además de los 270 P")
    ap.add_argument("--min-lead", type=float, default=3.0, help="minutos mínimos hasta el próximo Market Test")
    ap.add_argument("--next-bench-in", type=float, help="minutos hasta el próximo Market Test si /api/schedule falla")
    ap.add_argument("--allow-during-bench", action="store_true", help="abrir aunque haya una sesión en curso")
    ap.add_argument("--broker-mode", choices=["smart", "stall"], default="smart")
    ap.add_argument("--probe", type=int, default=3)
    ap.add_argument("--stale", type=float, default=90.0, help="segundos sin latido que se tratan como cuelgue")
    ap.add_argument("--fallback-after", type=int, default=3, help="caídas en 10 min antes de pasar a starter_broker")
    ap.add_argument("--alert-cmd", help="comando que recibe el texto de cada alerta como último argumento")
    return ap.parse_args(argv)


def main(argv=None, team=None) -> int:
    args = parse(argv)
    url = os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai")
    if args.resume:
        key = os.environ.get("BROKER_KEY")
        if not key:
            from bazaar_sdk import Bazaar
            key = find_broker_key((team or Bazaar(url, os.environ["BAZAAR_KEY"])).me())
        if not key:
            print("Sin broker key: ni BROKER_KEY en el entorno ni en /api/me.")
            return 4
        return run_broker(key, args)
    if team is None:
        from bazaar_sdk import Bazaar
        team = Bazaar(url, os.environ["BAZAAR_KEY"])
    plan = preflight(team, args)
    print(report(plan, args))
    if not args.execute:
        return 0 if plan.ok else 1
    return execute(team, args, plan)


if __name__ == "__main__":
    sys.exit(main())
