"""Capa fina que jugaría los duelos con la política de `duels.py`.

NO EJECUTAR. Otra persona del equipo está jugando en vivo con la clave del
equipo; un POST de este script le pisaría los duelos. Está construido,
documentado y quieto. Por eso:

- el modo por omisión es `--dry-run`: lee, decide, imprime y **no escribe**;
- escribir exige `--live` Y `--yes-i-have-the-key` juntos, a propósito
  incómodos;
- cada decisión se registra en `data/duels_log.jsonl` antes de enviarse, para
  poder reconstruir después qué se mandó y por qué.

No importa `bazaar_sdk` adrede: el add-on debe seguir en pie si ese módulo
cambia (lo comprueba `test_addon_isolation.py`). Habla con los mismos
endpoints que documenta el SDK:

    GET  /api/duels                     -> duelos vivos (401 sin clave)
    POST /api/duels/{id}/messages       {"text", "price"[, "days"]}
    POST /api/duels/{id}/accept
    GET  /api/clock                     -> tick actual, para no gastar el turno

SUPOSICIÓN: el formato de `/api/duels` no se pudo verificar (401 sin clave de
equipo). `duels.DuelView.from_api` es tolerante a alias por eso, y
`--dump` imprime el JSON crudo para que la primera cosa que haga quien tenga
la clave sea comprobar los nombres de campo antes de dejar que escriba.

Uso (quien tiene la clave, en su terminal, con BAZAAR_KEY en el entorno):

    # 1. mirar el formato real sin tocar nada
    python3 duels_run.py --session "Duels I" --dump

    # 2. ver qué haría, sin escribir
    python3 duels_run.py --session "Duels I" --dry-run

    # 3. jugar de verdad, un solo tick
    python3 duels_run.py --session "Duels I" --live --yes-i-have-the-key --once

    # 4. jugar la sesión entera
    python3 duels_run.py --session "Duels I" --live --yes-i-have-the-key

Creencias: el valor del rival no es observable. Se pasa a mano con
`--rival-limit` (una estimación global) o, mejor, en un JSON
`--beliefs beliefs.json` con `{"<duel_id>": {"rival_limit": 90,
"rival_days_weight": -3, "firmness": 1.2}}`. Sin nada, el prior es el propio
límite, que es deliberadamente malo: obliga a poner un número.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import oracle_duels as duels

DATA_DIR = Path(__file__).resolve().parent / "data"
LOG_PATH = DATA_DIR / "duels_log.jsonl"
DEFAULT_URL = os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai")


# --------------------------------------------------------------------- cliente

class DuelClient:
    """Cliente mínimo de duelos. `allow_writes=False` hace imposible escribir."""

    def __init__(self, url: str, key: str, *, allow_writes: bool = False,
                 timeout: float = 15.0):
        self.url = url.rstrip("/")
        self.key = key
        self.allow_writes = bool(allow_writes)
        self.timeout = timeout

    def _request(self, method: str, path: str, body: dict | None = None,
                 query: dict | None = None) -> dict:
        if method != "GET" and not self.allow_writes:
            # Red de seguridad real, no un comentario: sin --live no sale nada.
            raise PermissionError(
                f"escritura bloqueada ({method} {path}). Usa --live junto con "
                f"--yes-i-have-the-key, y sólo si eres quien tiene la clave.")
        target = f"{self.url}{path}"
        if query:
            target += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(target, data=data, method=method)
        req.add_header("X-Team-Key", self.key)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read().decode() or "{}"
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:400]
            raise RuntimeError(f"{method} {path} -> {e.code}: {detail}") from None
        return json.loads(raw)

    # Los tres endpoints que documenta el SDK, nada más.
    def duels(self) -> list[dict]:
        out = self._request("GET", "/api/duels")
        return out.get("duels", out) if isinstance(out, dict) else out

    def say(self, duel_id: int, payload: dict) -> dict:
        return self._request("POST", f"/api/duels/{int(duel_id)}/messages", payload)

    def accept(self, duel_id: int) -> dict:
        return self._request("POST", f"/api/duels/{int(duel_id)}/accept")

    def clock(self) -> dict:
        return self._request("GET", "/api/clock")


# -------------------------------------------------------------------- creencias

def load_beliefs(path: str | None, rival_limit: float | None,
                 days_weight: float | None, firmness: float) -> dict:
    """Creencias por duelo. Todo esto es SUPOSICIÓN: no se puede observar."""
    out: dict[int, duels.Beliefs] = {}
    if path:
        for k, v in json.loads(Path(path).read_text(encoding="utf-8")).items():
            out[int(k)] = duels.Beliefs(
                rival_limit=float(v["rival_limit"]),
                rival_days_weight=float(v.get("rival_days_weight", 0.0)),
                firmness=float(v.get("firmness", firmness)),
                reply_prob=float(v.get("reply_prob", 0.70)))
    return {
        "per_duel": out,
        "default_limit": rival_limit,
        "default_days_weight": days_weight,
        "firmness": firmness,
    }


def beliefs_for(duel: duels.DuelView, cfg: dict) -> duels.Beliefs:
    if duel.duel_id in cfg["per_duel"]:
        return cfg["per_duel"][duel.duel_id]
    # Sin estimación, el prior es el propio límite: pastel 0, política mínima
    # (abrir en el límite y contestar). Mejor que inventarse un número.
    return duels.Beliefs(
        rival_limit=(cfg["default_limit"] if cfg["default_limit"] is not None
                     else duel.limit),
        rival_days_weight=(cfg["default_days_weight"]
                           if cfg["default_days_weight"] is not None else 0.0),
        firmness=cfg["firmness"])


# ----------------------------------------------------------------------- bucle

def log(entry: dict) -> None:
    """Se registra ANTES de enviar: si algo falla, queda la intención escrita."""
    DATA_DIR.mkdir(exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def play_tick(client: DuelClient, params: duels.SessionParams, cfg: dict,
              *, live: bool, budget: int | None, dump: bool = False) -> int:
    """Un paso: leer, priorizar, decidir y (si live) responder. Devuelve nº de actos.

    El presupuesto existe porque con `max_concurrent` 6 y mensajes limitados por
    tick no se puede atender todo: `duels.triage` ordena por lo que cuesta
    ignorar cada duelo, y lo que no entra se queda para el tick siguiente.
    """
    raw = client.duels()
    if dump:
        print(json.dumps(raw, indent=2, ensure_ascii=False)[:4000])
    views = [duels.DuelView.from_api(d, session=params) for d in raw]
    chosen = duels.triage(
        views, {v.duel_id: beliefs_for(v, cfg) for v in views}, params,
        budget=budget if budget is not None else params.max_concurrent)

    acted = 0
    for duel in chosen:
        b = beliefs_for(duel, cfg)
        dec = duels.respond(duel, b, params)
        entry = {
            "ts": time.time(), "duel": duel.duel_id, "role": duel.role,
            "item": duel.item, "limit": duel.limit,
            "rival": [duel.rival_price, duel.rival_days],
            "action": dec.action, "price": dec.price, "days": dec.days,
            "reason": dec.reason, "ev_accept": dec.ev_accept,
            "ev_counter": dec.ev_counter, "live": live,
        }
        log(entry)
        print(f"[{duel.duel_id}] {duel.role:6} limite={duel.limit:g} "
              f"rival={duel.rival_price} -> {dec.action.upper()} "
              f"{dec.price}{'' if dec.days is None else f'/d{dec.days}'}  "
              f"({dec.reason})")
        if not live:
            continue
        try:
            if dec.action == "accept":
                client.accept(duel.duel_id)
            else:
                client.say(duel.duel_id, dec.payload())
            acted += 1
        except Exception as e:                        # noqa: BLE001
            # Un fallo en un duelo no puede dejar los otros sin contestar:
            # el silencio es el peor resultado posible.
            print(f"  ! fallo en {duel.duel_id}: {e}", file=sys.stderr)
    return acted


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--session", default="Duels I", choices=sorted(duels.SESSIONS))
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--key", default=os.environ.get("BAZAAR_KEY", ""))
    ap.add_argument("--beliefs", help="JSON {duel_id: {rival_limit, ...}}")
    ap.add_argument("--rival-limit", type=float, default=None)
    ap.add_argument("--rival-days-weight", type=float, default=None)
    ap.add_argument("--firmness", type=float, default=1.0)
    ap.add_argument("--budget", type=int, default=None,
                    help="duelos a atender por tick (por omisión, max_concurrent)")
    ap.add_argument("--dump", action="store_true", help="imprime el JSON crudo")
    ap.add_argument("--once", action="store_true", help="un solo tick")
    ap.add_argument("--dry-run", action="store_true", default=True)
    ap.add_argument("--live", action="store_true",
                    help="ESCRIBE en el servidor; exige --yes-i-have-the-key")
    ap.add_argument("--yes-i-have-the-key", dest="confirm", action="store_true")
    ap.add_argument("--cheatsheet", action="store_true",
                    help="imprime la política a mano y sale, sin tocar la red")
    args = ap.parse_args(argv)

    params = duels.SESSIONS[args.session]
    if args.cheatsheet:
        print(duels.cheat_sheet(params))
        return 0

    if args.live:
        ap.error("Ejecutor experimental: usa duel_runner.py para operar; esta política queda en análisis")
    live = False
    if args.live and not args.confirm:
        print("--live sin --yes-i-have-the-key: no se escribe nada.", file=sys.stderr)
    if not args.key:
        print("falta la clave de equipo (BAZAAR_KEY o --key); /api/duels da 401.",
              file=sys.stderr)
        return 2

    client = DuelClient(args.url, args.key, allow_writes=live)
    cfg = load_beliefs(args.beliefs, args.rival_limit, args.rival_days_weight,
                       args.firmness)
    print(duels.cheat_sheet(params))
    print(f"-- modo: {'LIVE' if live else 'dry-run (no escribe)'}")

    while True:
        try:
            play_tick(client, params, cfg, live=live, budget=args.budget,
                      dump=args.dump)
        except Exception as e:                        # noqa: BLE001
            print(f"! {e}", file=sys.stderr)
        if args.once:
            return 0
        # Esperar al siguiente tick sin quemar cuota: el reloj es público.
        try:
            time.sleep(max(1.0, float(client.clock().get("next_tick_in", 5.0))) + 0.2)
        except Exception:                             # noqa: BLE001
            time.sleep(5.0)


if __name__ == "__main__":        # pragma: no cover - nunca se ejecuta aquí
    raise SystemExit(main())
