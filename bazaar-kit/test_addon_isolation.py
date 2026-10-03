"""Demuestra que el add-on está aislado del resto del repo.

Un revisor no debería tener que leer 1.500 líneas para convencerse de que
este add-on no puede romper nada. Estas pruebas lo comprueban solas:

- la lógica no importa ningún módulo del repo ni abre la red;
- sólo `feed_watch`/`feed_stream` hablan con el servidor, y sólo para leer;
- ningún archivo ajeno se toca.

Si alguien añade más adelante un `import trading` o un `POST`, esto falla.
"""

import ast
import pathlib
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent

#: Archivos .py que ya estaban en el repo antes del add-on. Todo lo demás se
#: descubre como parte del add-on, así que un módulo nuevo no puede colarse sin
#: verificar: si aparece, estas pruebas lo cubren solas.
ORIGINAL = {
    "bazaar_sdk.py", "coordinator.py", "trading.py", "negotiation.py",
    "market_agent.py", "market_intel.py", "campaigns.py", "starter_agent.py",
    "starter_broker.py", "sim.py", "sim_day2.py",
    "duels.py", "duel_runner.py", "test_duels.py", "test_duel_runner.py",
    "page_guard.py", "test_page_guard.py", "agent_memory.py", "test_agent_memory.py",
    "test_coordinator.py", "test_trading.py", "test_market.py",
    "test_negotiation.py", "test_campaigns.py", "test_market_intel.py",
}

#: Módulos del add-on a los que SÍ se les permite red, porque su función es
#: hablar con el servidor. El resto debe ser lógica pura y testeable sin red.
NET_ALLOWED = {
    "feed_watch.py", "feed_stream.py", "dashboard.py",
    "broker_run.py", "duels_run.py",
}

#: Módulos que pueden escribir en el juego: ninguno de análisis. El broker y los
#: duelos son mecánicas de juego, no add-ons de lectura, y los ejecuta una
#: persona con la clave, nunca este código por su cuenta.
WRITE_ALLOWED = {"broker_run.py", "duels_run.py"}

#: Los módulos de juego sí pueden usar `bazaar_sdk`: es el cliente HTTP oficial
#: y reimplementarlo sería peor. Lo que no pueden es depender de la LÓGICA del
#: repo (coordinator, trading, negotiation...), que es lo que cambia.
SDK_ALLOWED = WRITE_ALLOWED

FOREIGN = {m[:-3] for m in ORIGINAL if not m.startswith("test_")}

NETWORK = {"urllib", "http", "socket", "requests", "ssl", "httpx", "urllib3"}


def addon_files() -> list[str]:
    """Los .py del add-on, descubiertos en vez de listados a mano."""
    # Este propio archivo queda fuera: contiene a propósito los literales que
    # busca, así que escanearse a sí mismo sería un falso positivo garantizado.
    return sorted(f.name for f in HERE.glob("*.py")
                  if f.name not in ORIGINAL
                  and f.name != pathlib.Path(__file__).name
                  and not f.name.startswith("conftest"))


def pure_files() -> list[str]:
    return [f for f in addon_files() if f not in NET_ALLOWED]


def imports_of(path: Path) -> set[str]:
    """Nombres de módulo raíz importados por un archivo."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


class TestAislamiento(unittest.TestCase):
    def test_hay_algo_que_comprobar(self):
        self.assertTrue(addon_files(), "no se encontró ningún módulo del add-on")

    def test_la_logica_no_depende_del_repo(self):
        for name in addon_files():
            with self.subTest(name):
                allowed = {"bazaar_sdk"} if name in SDK_ALLOWED else set()
                shared = imports_of(HERE / name) & FOREIGN - allowed
                self.assertFalse(
                    shared,
                    f"{name} importa módulos del repo: {sorted(shared)}. El "
                    f"add-on debe seguir funcionando si esos cambian.")

    def test_la_logica_pura_no_abre_la_red(self):
        for name in pure_files():
            with self.subTest(name):
                shared = imports_of(HERE / name) & NETWORK
                self.assertFalse(
                    shared, f"{name} debe ser lógica pura y testeable sin red; "
                            f"importa {sorted(shared)}")

    def test_nada_del_add_on_escribe_en_el_juego(self):
        """Un POST nuestro arruinaría la partida de quien está negociando."""
        for name in addon_files():
            if name in WRITE_ALLOWED:
                continue
            src = (HERE / name).read_text(encoding="utf-8")
            for verb in ('"POST"', "'POST'", '"PUT"', '"DELETE"', '"PATCH"'):
                with self.subTest(f"{name} {verb}"):
                    self.assertNotIn(
                        verb, src,
                        f"{name} parece escribir en el servidor. El add-on es "
                        f"de sólo lectura: otra persona está jugando en vivo.")

    def test_solo_se_persiste_bajo_data(self):
        """`data/` está en .gitignore; nada debe escribir fuera de ahí."""
        gitignore = (HERE / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("data/", gitignore)
        for name in sorted(NET_ALLOWED):
            if not (HERE / name).exists():
                continue
            src = (HERE / name).read_text(encoding="utf-8")
            self.assertIn("data", src, f"{name} debería persistir bajo data/")


if __name__ == "__main__":
    unittest.main()
