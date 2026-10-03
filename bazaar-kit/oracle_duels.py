"""Política de duelos (torneo 1v1 programado). Lógica pura, sin red.

Add-on independiente: no importa ni modifica ningún módulo existente. En
particular NO toca `negotiation.py`, que es la política de los *dealers*:
los duelos son otro mecanismo (rival humano/agente, pastel que se encoge,
puntuación por cuota del pastel capturada).

Qué está VERIFICADO (RULES.md §Duels y `GET /api/schedule`, público):

- "Ves sólo tu propio límite (el coste de un vendedor o el valor de un
  comprador); un trato fuera de tu límite te quita puntos, **no hay trato
  da cero**, y el valor del trato se encoge con cada ronda de charla."
- Puntúa la "cuota del pastel de cada trato que capturaste".
- Endpoints: `GET /api/duels`,
  `POST /api/duels/{id}/messages {"text","price"[,"days"]}`,
  `POST /api/duels/{id}/accept`.
- Sesiones posteriores negocian precio **y** día de entrega (0-10); cada
  lado tiene un peso privado por día (`your_days_weight`) y un mensaje con
  precio sin `days` se rechaza con `missing_days`.
- Parámetros reales leídos del schedule (ver `SESSIONS`): CUATRO sesiones
  puntuables. Duels I decay 0.06 / 16 ticks / 3 a la vez, sólo precio;
  Duels II 0.08 / 16 / 6 con días; Duels III 0.10 / 12 / 4 con días;
  Final 0.10 / 12 / 4 con días. El decay sube y el reloj se acorta, así que
  nada de eso está cableado: entra por `SessionParams`.
- Duels II dice literalmente "the pie grows for teams that trade on what each
  side cares about": con dos issues el juego NO es suma cero. Por eso el
  pastel se calcula como `total_pie` = pastel de precio + lo que crea elegir
  bien el día.
- El `item` de un duelo no es necesariamente una carta: los dos `duel.closed`
  de la práctica traían `"item": "Mercado de Vallehermoso"`. El objeto se
  trata en abstracto y su valor es un parámetro (`DuelView.limit`).

INCÓGNITAS que no se pueden cerrar sin leer `/api/duels`:

- Si el rival es otro equipo (RULES.md dice round-robin entre equipos, con
  alias) o un personaje del juego. `/api/dealers` publica `traits` numéricos
  (abuela patience 0.85, chato 0.35, pilar 0.6); si los duelos los gobernaran
  esos traits, `Beliefs.firmness` debería venir de ahí. No se sabe: la
  política lo deja como un parámetro que se puede fijar a mano.
- De 1186 eventos del feed sólo 3 eran de duelos, y los dos `duel.closed`
  salieron `no_deal`. Es consistente con "menos de la mitad cerró", y es la
  razón de que `reply_prob` sea pesimista.

Qué es SUPOSICIÓN (`GET /api/duels` devuelve 401 sin clave de equipo, así
que el formato exacto del duelo no se pudo verificar):

1. Los nombres de campo de un duelo: `role`, `your_limit`, `rival_offer`,
   `deadline`, `issues`, `your_days_weight`. Salen del docstring del SDK y
   de RULES.md, no de una respuesta real. `DuelView.from_api()` es tolerante
   a alias por eso.
2. Cómo cuenta el servidor una "ronda". Aquí se asume **una ronda = un
   intercambio** (un mensaje de cada lado). Si contara por mensaje, la
   merma real sería mayor; la *regla* de decisión no cambia (es un cociente).
3. Que la merma es multiplicativa: factor `(1 - decay) ** rondas`.
4. El valor del rival y su peso por día. Nunca son observables: son
   creencias (`Beliefs`), y toda la política está escrita para degradar con
   gracia cuando esa creencia está mal.
5. Que la utilidad del día es lineal: `w * days`. El signo de `w` dice si
   queremos entrega tarde (w>0) o pronto (w<0).

La regla que sobrevive a todas las suposiciones: **el silencio da cero a
las dos partes**, así que contestar siempre bate a no contestar.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# ------------------------------------------------------------------ constantes

#: Lo que vale un duelo que nadie contesta. Es el ancla de toda la política:
#: cualquier trato dentro de nuestro límite bate a esto.
NO_ANSWER_VALUE = 0.0

#: Días de entrega admitidos por el servidor (RULES.md: 0-10).
DAYS_MIN, DAYS_MAX = 0, 10


@dataclass(frozen=True)
class SessionParams:
    """Parámetros de una sesión de duelos, tal como los publica el schedule."""
    name: str
    rounds: int
    duel_ticks: int
    decay: float
    max_concurrent: int
    issues: tuple[str, ...] = ("price",)

    @property
    def two_issue(self) -> bool:
        return "days" in self.issues


#: Leídos literalmente de `GET /api/schedule` (público, verificado el domingo
#: ~10:35 Madrid). Son CUATRO sesiones puntuables, no dos, y los parámetros
#: cambian en cada una: `decay` sube 0.06 → 0.08 → 0.10 y el reloj se acorta
#: de 16 a 12 ticks. Nada de esto está cableado en la política: todo entra por
#: `SessionParams`.
SESSIONS: dict[str, SessionParams] = {
    "Duels I": SessionParams("Duels I", 1, 16, 0.06, 3, ("price",)),
    "Duels II": SessionParams("Duels II", 2, 16, 0.08, 6, ("price", "days")),
    "Duels III": SessionParams("Duels III", 2, 12, 0.10, 4, ("price", "days")),
    "Final": SessionParams("Final", 1, 12, 0.10, 4, ("price", "days")),
}


# ------------------------------------------------------- aritmética de la merma

def pie_factor(decay: float, rounds: int) -> float:
    """Fracción del pastel que sobrevive tras `rounds` rondas de charla.

    Suposición (3): merma multiplicativa. `rounds=0` = cerrar sin charla.
    """
    if rounds < 0:
        raise ValueError("rounds no puede ser negativo")
    return (1.0 - float(decay)) ** int(rounds)


def decay_table(decay: float, pie: float, rounds: int = 5) -> list[tuple[int, float, float]]:
    """`(ronda, factor, pastel_restante)` para enseñar la merma a mano.

    Por qué existe: la intuición "una ronda más es barata" es falsa y hay que
    poder mirarla. Con decay 0.08, la ronda 3 ya quemó ~22 % del pastel.
    """
    return [(r, pie_factor(decay, r), pie * pie_factor(decay, r)) for r in range(rounds + 1)]


def round_cost_fraction(decay: float) -> float:
    """Mejora RELATIVA mínima de *nuestro* excedente que paga una ronda más.

    Si cerramos ahora con excedente `u`, cerrar una ronda más tarde con `u'`
    da `u' * (1-d)`. Sale a cuenta sólo si `u' * (1-d) > u`, o sea
    `u' > u / (1-d)`, o sea una mejora de `d / (1-d)`.

    Con d=0.06 → 6.38 %. Con d=0.08 → 8.70 %. Con d=0.10 → 11.11 %.

    Importante: es un porcentaje de NUESTRO excedente, no del pastel. Si sólo
    nos están dando una rodaja fina, una ronda más cuesta poco en primas
    absolutas y regatear sí compensa; si ya vamos gordos, cuesta caro.
    """
    d = float(decay)
    if not 0.0 <= d < 1.0:
        raise ValueError("decay debe estar en [0, 1)")
    return d / (1.0 - d) if d else 0.0


def min_worthwhile_gain(surplus: float, decay: float) -> float:
    """Primas de mejora que hay que arrancar para que una ronda más no pierda."""
    return max(0.0, float(surplus)) * round_cost_fraction(decay)


# ------------------------------------------------------------------ excedentes

def surplus(role: str, limit: float, price: float) -> float:
    """Nuestro excedente de precio. Negativo = trato fuera de límite (resta puntos).

    Vendedor: el límite es el coste, ganamos lo que cobramos por encima.
    Comprador: el límite es el valor, ganamos lo que nos ahorramos por debajo.
    """
    if role == "seller":
        return float(price) - float(limit)
    if role == "buyer":
        return float(limit) - float(price)
    raise ValueError(f"role desconocido: {role!r}")


def rival_surplus(role: str, rival_limit: float, price: float) -> float:
    """Excedente del rival al mismo precio (depende de una creencia, no de un dato)."""
    other = "buyer" if role == "seller" else "seller"
    return surplus(other, rival_limit, price)


def price_for_surplus(role: str, limit: float, want: float) -> float:
    """Precio que nos da exactamente `want` de excedente. Inversa de `surplus`."""
    return limit + want if role == "seller" else limit - want


def pie_size(role: str, limit: float, rival_limit: float) -> float:
    """Pastel de precio estimado: valor del comprador menos coste del vendedor.

    Negativo = no hay zona de acuerdo con esa creencia. Entonces NO cerramos
    fuera de límite (restaría puntos) pero SÍ contestamos.
    """
    return rival_limit - limit if role == "seller" else limit - rival_limit


def share_of_pie(role: str, limit: float, rival_limit: float, price: float) -> float:
    """Cuota del pastel que capturamos a ese precio: lo que puntúa."""
    pie = pie_size(role, limit, rival_limit)
    if pie <= 0:
        return 0.0
    return surplus(role, limit, price) / pie


# --------------------------------------------------- probabilidad de aceptación

@dataclass(frozen=True)
class Beliefs:
    """Lo que *creemos* del otro lado. Todo esto es suposición, por diseño.

    - `rival_limit`: su valor (si somos vendedor) o su coste (si somos
      comprador). Nunca observable. Sin una estimación mejor, usar el punto
      medio del rango del escenario.
    - `rival_days_weight`: su peso por día, con signo. Suposición (5).
    - `firmness`: lo duro que es. 1.0 = prior neutro calibrado abajo; >1 más
      duro (acepta menos), <1 más blando.
    - `reply_prob`: probabilidad de que conteste si contraofertamos. Prior
      0.70: un rival que YA nos mandó un precio está enganchado, pero los dos
      `duel.closed` de la práctica salieron `no_deal`, así que no se sube más.
      Bajarlo hace la política más conservadora (acepta antes).
    """
    rival_limit: float
    rival_days_weight: float = 0.0
    firmness: float = 1.0
    reply_prob: float = 0.70


def accept_prob(their_share: float, firmness: float = 1.0) -> float:
    """Prior de que acepten una oferta que les deja `their_share` del pastel.

    SUPOSICIÓN calibrada a mano, no medida: curva monótona anclada en
    - les dejamos 0 → casi nunca (0.02),
    - mitad y mitad → probable (~0.80), porque el consejo oficial es "abre con
      una oferta que el otro pueda tomar" y porque su alternativa es cero,
    - les dejamos todo → casi seguro (0.97).

    `firmness > 1` empuja la curva a la derecha: hace falta darles más.
    """
    s = min(1.0, max(0.0, float(their_share)))
    f = max(0.05, float(firmness))
    # Curva potencia: p = 0.02 + 0.95 * s**k, con k=1.5*f para que medio
    # pastel ≈ 0.80 cuando f=1 y caiga rápido cuando les dejamos migajas.
    k = 1.5 * f
    return 0.02 + 0.95 * (s ** k)


# --------------------------------------------------------------- día de entrega

def days_utility(weight: float, days: int) -> float:
    """Utilidad del día de entrega. Suposición (5): lineal, `w * days`.

    `w > 0` = nos conviene entrega tarde; `w < 0` = la queremos pronto.
    `|w|` es el precio en primas de un día.
    """
    return float(weight) * int(days)


def preferred_day(weight: float) -> int:
    """Nuestro día ideal ignorando al otro: el extremo del rango."""
    return DAYS_MAX if weight > 0 else DAYS_MIN


def efficient_day(our_weight: float, their_weight: float) -> int:
    """El día que hace el pastel MÁS GRANDE para los dos juntos.

    Con utilidad lineal el óptimo conjunto es una esquina: si la suma de
    pesos es positiva, el día 10 crea más valor del que destruye; si no, el 0.
    Ahí está el excedente de Duels II: a quien le corra más prisa el tiempo
    se lleva el día, y lo paga en precio.
    """
    return DAYS_MAX if (float(our_weight) + float(their_weight)) > 0 else DAYS_MIN


def days_trade(our_weight: float, their_weight: float) -> dict:
    """El intercambio día-por-precio, explícito.

    Todo se mide **respecto a NUESTRO día ideal**: ese es el punto de partida
    honesto, porque es lo que pediríamos si los días no se negociaran.

    - `our_loss`: utilidad que perdemos al movernos a `day`. Nunca negativa.
    - `their_gain`: lo que ganan ellos por ese mismo movimiento.
    - `created`: pastel nuevo. Nunca negativa, porque `day` maximiza la suma.
    - `compensation`: el mínimo a cobrar en precio sólo para empatar.

    Ahí está el excedente de Duels II: a quien le corra más prisa el tiempo se
    lleva el día, y lo paga en precio.
    """
    ours = preferred_day(our_weight)
    theirs = preferred_day(their_weight)
    day = efficient_day(our_weight, their_weight)
    our_loss = days_utility(our_weight, ours) - days_utility(our_weight, day)
    their_gain = days_utility(their_weight, day) - days_utility(their_weight, ours)
    return {
        "day": day,
        "our_ideal_day": ours,
        "their_ideal_day": theirs,
        "our_loss": our_loss,              # >= 0: lo que cedemos en tiempo
        "their_gain": their_gain,          # lo que ganan ellos por el mismo cambio
        "created": their_gain - our_loss,  # >= 0 siempre: `day` maximiza la suma
        "compensation": our_loss,          # mínimo a cobrar en precio para empatar
        "we_concede_time": day != ours,
    }


def price_shift(role: str, delta_utility: float) -> float:
    """Cuánto mover el precio para recuperar `delta_utility` de utilidad.

    Vendedor cobra más; comprador ofrece menos. Firmar esto a mano es el
    error típico a las 11:30, así que vive en una función.
    """
    return float(delta_utility) if role == "seller" else -float(delta_utility)


def day_penalty(weight: float, day: Optional[int]) -> float:
    """Lo que nos cuesta un día frente a nuestro día ideal. Siempre <= 0.

    Permite sumar precio y tiempo en la misma moneda sin cambiar de origen a
    mitad del cálculo (el bug fácil de este modelo).
    """
    if day is None:
        return 0.0
    return days_utility(weight, day) - days_utility(weight, preferred_day(weight))


def total_utility(role: str, limit: float, weight: float, price: float,
                  day: Optional[int]) -> float:
    """Utilidad total: excedente de precio más la penalización del día."""
    return surplus(role, limit, price) + day_penalty(weight, day)


def total_pie(price_pie: float, plan: Optional[dict]) -> float:
    """Pastel conjunto: el de precio más el que crea elegir bien el día."""
    return float(price_pie) + (float(plan["created"]) if plan else 0.0)


# ------------------------------------------------------------- vista de un duelo

@dataclass(frozen=True)
class DuelView:
    """Un duelo tal como lo leeríamos de `GET /api/duels`.

    SUPOSICIÓN (1): los nombres de campo. `from_api` acepta alias porque no
    se pudo verificar el formato real (401 sin clave).
    """
    duel_id: int
    role: str                      # "seller" | "buyer"
    limit: float                   # nuestro coste (vendedor) o valor (comprador)
    rival_price: Optional[float] = None
    rival_days: Optional[int] = None
    rounds_used: int = 0           # rondas de charla ya gastadas
    ticks_left: Optional[int] = None
    issues: tuple[str, ...] = ("price",)
    days_weight: float = 0.0       # `your_days_weight`: NUESTRO peso, observable
    item: str = ""

    @property
    def two_issue(self) -> bool:
        return "days" in self.issues

    @property
    def has_rival_offer(self) -> bool:
        return self.rival_price is not None

    @staticmethod
    def from_api(raw: dict, *, session: Optional[SessionParams] = None) -> "DuelView":
        """Normaliza un duelo del API. Tolerante: el formato es suposición."""
        def pick(*names, default=None):
            for n in names:
                if n in raw and raw[n] is not None:
                    return raw[n]
            return default

        offer = pick("rival_offer", "their_offer", "standing_offer", default=None)
        price = days = None
        if isinstance(offer, dict):
            price = offer.get("price")
            days = offer.get("days")
            inner = offer.get("offer")
            if isinstance(inner, dict):
                price = inner.get("price", price)
                days = inner.get("days", days)
        elif isinstance(offer, (int, float)):
            price = offer

        issues = pick("issues", default=None)
        if not issues:
            issues = list(session.issues) if session else ["price"]

        return DuelView(
            duel_id=int(pick("id", "duel_id", "duel", default=0)),
            role=str(pick("role", "side", default="seller")),
            limit=float(pick("your_limit", "limit", "your_cost", "your_value", default=0.0)),
            rival_price=None if price is None else float(price),
            rival_days=None if days is None else int(days),
            rounds_used=int(pick("rounds_used", "round", "messages", default=0)),
            ticks_left=pick("ticks_left", "ticks_remaining"),
            issues=tuple(issues),
            days_weight=_signed_weight(_scalar_weight(pick("your_days_weight", "days_weight", default=0.0)),
                                       raw, pick("role", "side", default="seller")),
            item=str(pick("item", "scenario", default="") or ""),
        )


def _signed_weight(w: float, raw: dict, role) -> float:
    """El servidor da un peso POSITIVO por día y `days_meaning` fija el signo (comprador: «costs you» → −; vendedor: «adds» → +).
    Este módulo usa la convención `w > 0 = nos conviene entrega tarde`: solo se firma el formato del servidor (peso ≥ 0 con
    frase o con rol); un peso ya negativo se respeta tal cual. (La política que ejecuta duel_runner es duels.py.)"""
    if w < 0:
        return w
    text = str((raw or {}).get("days_meaning") or "").lower()
    cost = "cost" in text or (not text and str(role) == "buyer")
    return -w if cost else w


def _scalar_weight(w) -> float:
    """El oráculo modela un peso escalar por día; una lista/dict (formato no documentado) no debe tumbarlo."""
    try:
        return float(w or 0.0)
    except (TypeError, ValueError):
        return 0.0


# -------------------------------------------------------------------- decisión

@dataclass(frozen=True)
class Decision:
    """Lo que haríamos en un duelo, con el por qué a la vista."""
    action: str                    # "accept" | "counter" | "open"
    price: Optional[int] = None
    days: Optional[int] = None
    text: str = ""
    reason: str = ""
    ev_accept: float = 0.0
    ev_counter: float = 0.0
    ev_silence: float = NO_ANSWER_VALUE
    detail: dict = field(default_factory=dict)

    @property
    def answers(self) -> bool:
        """Siempre True: esta política nunca calla. El silencio da cero."""
        return self.action in ("accept", "counter", "open")

    def payload(self) -> dict:
        """Cuerpo para `duel_say`. `days` va si la sesión lo pide (missing_days)."""
        body: dict = {"text": self.text}
        if self.price is not None:
            body["price"] = int(self.price)
            if self.days is not None:
                body["days"] = int(self.days)
        return body

# ------------------------------------------------------------ apertura tomable

#: Suelo de decencia: nunca pedimos tanto que al rival no le quede nada.
#: Su cero es nuestro cero, así que una oferta que no puede tomar no vale.
MIN_THEIR_SHARE = 0.20


def _days_plan(duel: "DuelView", beliefs: "Beliefs",
               params: SessionParams) -> Optional[dict]:
    """Contabilidad del día si la sesión negocia días; `None` si es sólo precio."""
    if not (duel.two_issue or params.two_issue):
        return None
    return days_trade(duel.days_weight, beliefs.rival_days_weight)


def _their_total(duel: "DuelView", beliefs: "Beliefs", price: float,
                 day: Optional[int]) -> float:
    """Utilidad total estimada del rival. Depende de creencias, no de datos."""
    s = rival_surplus(duel.role, beliefs.rival_limit, price)
    if day is not None:
        s += day_penalty(beliefs.rival_days_weight, day)
    return s


def _make_takeable(duel: "DuelView", beliefs: "Beliefs", price: float,
                   day: Optional[int], pie_total: float,
                   min_their_share: float = MIN_THEIR_SHARE) -> float:
    """Acerca el precio hasta que al rival le quede una tajada que pueda tomar.

    Sin esto, sumar la compensación del día puede empujar el precio por encima
    de lo que el rival aguanta y convertir un pastel grande en un no-trato.
    El tope por abajo es nuestro propio límite: nunca cerramos fuera de él.
    """
    floor_u = max(0.0, min_their_share * max(0.0, pie_total))
    theirs = _their_total(duel, beliefs, price, day)
    if theirs < floor_u:
        price += price_shift(duel.role, -(floor_u - theirs))
    # Nunca por debajo de nuestro límite (un trato fuera de límite resta puntos).
    if surplus(duel.role, duel.limit, price) < 0:
        price = price_for_surplus(duel.role, duel.limit, 0.0)
    return price


def opening_offer(
    duel: DuelView,
    beliefs: Beliefs,
    params: SessionParams,
    *,
    fallback_share: float = 0.35,
) -> Decision:
    """Precio de apertura que maximiza el valor esperado.

    Para cada precio entero de la banda factible compara:

        EV = p_aceptan * excedente * (1-d)^1
           + (1-p_aceptan) * p_contestan * excedente_de_reserva * (1-d)^2

    El segundo término es lo que esperamos si rechazan y seguimos una ronda
    más (`fallback_share` del pastel). Si no contestan, cero: por eso un
    `reply_prob` pesimista empuja la apertura a ser **tomable**, no un ancla.

    Un ancla agresiva maximiza el excedente del caso bueno y mata la
    probabilidad; con un pastel que se encoge y un cero por no cerrar, el
    óptimo cae cerca de dejarles un tercio del pastel.

    Con dos issues la apertura ya lleva el día eficiente y cobra en precio lo
    que nos cuesta cederlo (más una parte del pastel que ese cambio crea).
    """
    pie = pie_size(duel.role, duel.limit, beliefs.rival_limit)
    d = params.decay
    plan = _days_plan(duel, beliefs, params)
    day = plan["day"] if plan else None
    pie_t = total_pie(pie, plan)

    if pie <= 0:
        # Creemos que no hay zona de acuerdo en precio. Pedimos justo nuestro
        # límite (excedente 0, nunca negativo) y que hable el otro. Contestar,
        # siempre: el silencio da cero a los dos.
        px = price_for_surplus(duel.role, duel.limit, 0.0)
        if plan and plan["we_concede_time"]:
            px += price_shift(duel.role, plan["compensation"])
        px = int(round(px))
        return Decision(
            action="open", price=px, days=day,
            text=_opening_text(duel, px, day),
            reason="pastel de precio estimado <= 0: abrimos en el límite y no cerramos fuera de él",
            ev_accept=0.0, ev_counter=0.0,
            detail={"pie": pie, "pie_total": pie_t, "days_plan": plan,
                    "assumption": "rival_limit es creencia, no dato"},
        )

    lo = price_for_surplus(duel.role, duel.limit, 0.0)      # nuestro límite
    hi = price_for_surplus(duel.role, duel.limit, pie)      # límite del rival
    a, b = int(round(min(lo, hi))), int(round(max(lo, hi)))
    best: Optional[tuple[float, int]] = None
    for px in range(a, b + 1):
        u = surplus(duel.role, duel.limit, px)
        if u <= 0:
            continue
        p_ok = accept_prob(rival_surplus(duel.role, beliefs.rival_limit, px) / pie,
                           beliefs.firmness)
        ev = p_ok * u * pie_factor(d, 1)
        ev += (1 - p_ok) * beliefs.reply_prob * (fallback_share * pie) * pie_factor(d, 2)
        if best is None or ev > best[0]:
            best = (ev, px)

    ev, px_price = best if best else (0.0, int(round(lo)))
    px = float(px_price)
    if plan and plan["we_concede_time"]:
        # Ceder el día no es gratis: se cobra la pérdida y una parte del pastel
        # nuevo. Luego se comprueba que la oferta siga siendo tomable.
        px += price_shift(duel.role, plan["compensation"] + 0.25 * plan["created"])
        px = _make_takeable(duel, beliefs, px, day, pie_t)
    px = int(round(px))
    share_them = _their_total(duel, beliefs, px, day) / pie_t if pie_t > 0 else 0.0
    return Decision(
        action="open", price=px, days=day,
        text=_opening_text(duel, px, day),
        reason=(f"apertura tomable: les deja ~{share_them:.0%} del pastel; "
                f"EV {ev:.1f} frente a 0 por no contestar"),
        ev_accept=ev, ev_counter=ev,
        detail={"pie": pie, "pie_total": pie_t, "band": (a, b), "days_plan": plan,
                "their_share": share_them,
                "assumption": "rival_limit y rival_days_weight son creencias"},
    )


def _opening_text(duel: DuelView, price: int, day: Optional[int]) -> str:
    """Texto que acompaña la oferta. Las palabras no obligan: la estructura sí.

    Dos cosas que el texto debe hacer: invitar a aceptar YA (la merma es real
    para los dos) y preguntar por la prisa del otro, que es el único modo de
    convertir la suposición sobre su peso por día en información.
    """
    side = "te lo dejo" if duel.role == "seller" else "te lo compro"
    when = f", entrega el día {day}" if day is not None else ""
    out = (f"{side} a {price}{when}. Es una oferta que puedes tomar ya: cada "
           f"ronda que hablamos nos encoge el trato a los dos, y si ninguno "
           f"cierra, los dos nos vamos con cero. Si te sirve, acéptala.")
    if day is not None:
        out += (" Dime si el día te aprieta: si te corre más prisa que a mí, te "
                "lo adelanto y lo arreglamos en el precio.")
    return out


# -------------------------------------------------------------- regla de respuesta

def respond(
    duel: DuelView,
    beliefs: Beliefs,
    params: SessionParams,
    *,
    fallback_share: float = 0.35,
    max_counters: int = 1,
) -> Decision:
    """Qué hacer con lo que tenemos delante. NUNCA devuelve "callar".

    Orden de la lógica, que es el orden de la importancia:

    1. Sin oferta del rival → abrimos (`opening_offer`). El silencio da cero.
    2. Su oferta nos deja excedente y ya gastamos las contras permitidas, o
       mejorarla no paga la merma, o el reloj aprieta → **aceptar**.
    3. Su oferta nos deja excedente pero hay sitio para arrancar más que
       `min_worthwhile_gain` → **una** contraoferta, y luego cerrar.
    4. Su oferta está fuera de nuestro límite (excedente de precio <= 0) →
       nunca se acepta (resta puntos), pero se contraoferta en nuestro límite:
       es la única jugada que todavía puede acabar en trato.

    El límite se mide SOBRE EL PRECIO: `your_limit` es un coste o un valor
    monetario. Suposición: que el día no mueve ese límite.
    """
    if not duel.has_rival_offer:
        return opening_offer(duel, beliefs, params, fallback_share=fallback_share)

    d = params.decay
    px_in = float(duel.rival_price)
    u_price = surplus(duel.role, duel.limit, px_in)
    pie = pie_size(duel.role, duel.limit, beliefs.rival_limit)
    plan = _days_plan(duel, beliefs, params)
    day_in = duel.rival_days if plan else None
    pie_t = total_pie(pie, plan)

    # Utilidad total de SU oferta, con el día que trae.
    u_now = u_price + day_penalty(duel.days_weight, day_in)

    # ---- 4. fuera de límite: no se acepta, pero se contesta
    if u_price <= 0:
        want = 0.10 * pie if pie > 0 else 0.0
        px = price_for_surplus(duel.role, duel.limit, max(0.0, want))
        day = plan["day"] if plan else None
        if plan and plan["we_concede_time"]:
            px += price_shift(duel.role, plan["compensation"])
            px = _make_takeable(duel, beliefs, px, day, pie_t)
        px = int(round(px))
        return Decision(
            action="counter", price=px, days=day,
            text=(f"A {int(px_in)} pierdo puntos, no puedo firmarlo. {px} sí lo "
                  f"firmo ahora mismo"
                  + (f", entrega el día {day}" if day is not None else "")
                  + ". Cerrar algo nos vale a los dos más que irnos con cero."),
            reason="su oferta cae fuera de nuestro límite: aceptarla restaría puntos",
            ev_accept=u_price,
            ev_counter=max(0.0, want) * pie_factor(d, duel.rounds_used + 1),
            detail={"u_now": u_now, "pie": pie, "pie_total": pie_t, "days_plan": plan},
        )

    ev_accept = u_now * pie_factor(d, duel.rounds_used)

    # ---- objetivo de la contra, en excedente de PRECIO
    # No pedir la luna: tiene que seguir siendo tomable o la merma nos come.
    target = max(u_price, (1.0 - fallback_share) * pie) if pie > 0 else u_price
    ask_price_u = u_price + 0.5 * max(0.0, target - u_price)
    px_counter = price_for_surplus(duel.role, duel.limit, ask_price_u)

    day = plan["day"] if plan else None
    if plan and plan["we_concede_time"]:
        # Cedemos el día a quien más le urge y lo cobramos: compensación más
        # una parte del pastel nuevo. Esto es el excedente de Duels II.
        px_counter += price_shift(duel.role, plan["compensation"] + 0.25 * plan["created"])
    if plan:
        px_counter = _make_takeable(duel, beliefs, px_counter, day, pie_t)

    u_counter = total_utility(duel.role, duel.limit, duel.days_weight, px_counter, day)
    gain = u_counter - u_now
    needed = min_worthwhile_gain(u_now, d)

    counters_left = max(0, max_counters - duel.rounds_used)
    ticks_tight = duel.ticks_left is not None and duel.ticks_left <= 2

    p_ok = accept_prob(_their_total(duel, beliefs, px_counter, day) / pie_t
                       if pie_t > 0 else 0.0, beliefs.firmness)
    # Si rechazan y contestan, suponemos que volvemos a algo como su oferta
    # actual (conservador). Si no contestan, cero: eso es lo que nos disciplina.
    ev_counter = pie_factor(d, duel.rounds_used + 1) * (
        p_ok * u_counter + (1 - p_ok) * beliefs.reply_prob * u_now)

    # ---- 2. aceptar
    if counters_left <= 0 or ticks_tight or gain < needed or ev_counter <= ev_accept:
        why = ("sin contras disponibles" if counters_left <= 0 else
               "quedan <=2 ticks: el riesgo de no-trato (cero) domina" if ticks_tight else
               f"la mejora alcanzable ({gain:.1f}) no cubre la merma de una ronda "
               f"({needed:.1f} = {round_cost_fraction(d):.1%} de nuestro excedente)"
               if gain < needed else
               f"EV de contraofertar ({ev_counter:.1f}) no bate aceptar ({ev_accept:.1f})")
        return Decision(
            action="accept", price=int(px_in), days=day_in,
            text="Hecho, acepto.",
            reason=f"aceptar: {why}; y aceptar bate siempre a no contestar (0)",
            ev_accept=ev_accept, ev_counter=ev_counter,
            detail={"u_now": u_now, "pie": pie, "pie_total": pie_t,
                    "needed_gain": needed, "achievable_gain": gain,
                    "days_plan": plan},
        )

    # ---- 3. una sola contra
    px_counter = int(round(px_counter))
    return Decision(
        action="counter", price=px_counter, days=day,
        text=(f"Casi. {px_counter}"
              + (f" con entrega el día {day}" if day is not None else "")
              + " y lo firmo en este mismo turno; si no, acepto lo tuyo antes de "
                "que la charla nos coma el trato. Una sola vuelta, no más."),
        reason=(f"contra única: gana {gain:.1f} > {needed:.1f} que cuesta la ronda "
                f"({round_cost_fraction(d):.1%} del excedente)"
                + (f"; cedemos el día {day} y lo cobramos "
                   f"(+{plan['created']:.1f} de pastel nuevo)"
                   if plan and plan["we_concede_time"] and plan["created"] > 0 else "")),
        ev_accept=ev_accept, ev_counter=ev_counter,
        detail={"u_now": u_now, "pie": pie, "pie_total": pie_t,
                "needed_gain": needed, "achievable_gain": gain, "p_accept": p_ok,
                "days_plan": plan,
                "assumption": "pie y p_accept dependen de beliefs.rival_limit"},
    )


# ------------------------------------------------- reparto de atención por tick

def urgency(duel: DuelView, beliefs: Beliefs, params: SessionParams) -> float:
    """Cuánto se pierde por NO contestar este duelo en este tick.

    Con `max_concurrent` 6 (Duels II) y un presupuesto de mensajes por tick,
    el cuello de botella es la atención, no la política. Se prioriza por lo
    que está en juego, no por el número de duelo:

    - un duelo sin contestar vale 0, así que el primer mensaje de un duelo
      virgen vale todo su pastel: bonificación grande;
    - pocos ticks restantes = riesgo inminente de cerrar en no_deal;
    - pastel grande antes que pastel pequeño;
    - la merma de un tick perdido se paga en el pastel completo.
    """
    pie_t = max(0.0, total_pie(pie_size(duel.role, duel.limit, beliefs.rival_limit),
                               _days_plan(duel, beliefs, params)))
    at_stake = pie_t if pie_t > 0 else max(1.0, abs(duel.limit) * 0.1)
    score = at_stake * params.decay           # lo que quema un tick de silencio
    if not duel.has_rival_offer and duel.rounds_used == 0:
        score += at_stake                     # abrir o no abrir es 0 vs todo
    if duel.ticks_left is not None:
        # Un duelo a punto de expirar sin trato es un cero garantizado.
        score += at_stake / max(1.0, float(duel.ticks_left))
    return score


def triage(duels: list[DuelView], beliefs: dict[int, Beliefs], params: SessionParams,
           *, budget: Optional[int] = None) -> list[DuelView]:
    """Ordena los duelos por lo que cuesta ignorarlos, y corta por presupuesto.

    `beliefs` se indexa por `duel_id`; lo que falte usa un prior neutro
    (el propio límite como estimación del valor del rival, que es lo único
    que tenemos sin información).
    """
    def b(d: DuelView) -> Beliefs:
        return beliefs.get(d.duel_id) or Beliefs(rival_limit=d.limit)

    ordered = sorted(duels, key=lambda d: -urgency(d, b(d), params))
    if budget is None:
        budget = params.max_concurrent
    return ordered[:max(0, int(budget))]


# ------------------------------------------------------------------ chuleta 11:30

def cheat_sheet(params: SessionParams) -> str:
    """La política en siete líneas, para aplicarla a mano si falla el script."""
    c = round_cost_fraction(params.decay)
    return "\n".join([
        f"{params.name}: decay {params.decay:.0%}/ronda, {params.duel_ticks} ticks, "
        f"max {params.max_concurrent} a la vez, issues {list(params.issues)}.",
        "1. CONTESTA TODOS los duelos. Sin respuesta = 0 para los dos.",
        "2. Abre con una oferta tomable: quédate ~65 % del pastel que estimes, "
        "no el 95 %. Dilo en el texto: 'acéptala ya'.",
        f"3. Una ronda más cuesta {c:.1%} de TU excedente. Si no puedes arrancar "
        f"más que eso, ACEPTA.",
        "4. Nunca firmes fuera de tu límite (resta puntos): contraoferta en tu "
        "límite +10 % del pastel y deja que cierren ellos.",
        "5. Una contra como máximo; con <=2 ticks, acepta.",
        ("6. Días (0-10): cede el día al que más le urja y cóbralo en precio. "
         "Si tu peso por día es pequeño y el suyo grande, el día es suyo y la "
         "prima es tuya. Todo mensaje con precio LLEVA days o sale missing_days."
         if params.two_issue else
         "6. Esta sesión es sólo precio: no mandes days."),
        f"7. Con {params.max_concurrent} duelos a la vez, primero los que aún no "
        f"has contestado y los que van a expirar: ésos valen 0 si los dejas.",
    ])
