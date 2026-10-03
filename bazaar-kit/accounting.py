"""Contabilidad canónica del coordinador: cada operación liquidada se cuenta UNA sola vez.

Claves canónicas (evidencia del servidor, estables entre reinicios):
  dealer-buy:<hilo>     una compra a un vendedor se liquida UNA vez por hilo, la haya cerrado la contraoferta nuestra
                        (`dealer_counter`) o nuestra aceptación (`dealer_accept`): son DOS acciones, UN solo pago.
  dealer-sell:<hilo>    ídem para ventas a un vendedor.
  settlement:<id>       liquidación pública del feed (`settlement`), vista desde varias acciones.
  offer:<id>            oferta liquidada de una negociación entre equipos (campaña).

Definiciones (todas separadas, ninguna se reinicia ni se amplía en silencio):
  gasto bruto acumulado   = suma de lo pagado, una vez por clave canónica (incluye comisiones que pagamos nosotros);
  ingresos confirmados    = suma de lo cobrado, una vez por clave canónica (neto de comisiones que pagamos);
  presupuesto             = límite EXPLÍCITO del operador (`--max-spend`); modo `gross` (por defecto, como siempre) o
                            `net` (gasto bruto − ingresos confirmados), elegido con `--budget-mode`;
  comisiones por cobrar   = prometidas (p. ej. v10) y NUNCA contadas como efectivo ni como ingreso hasta liquidarse.
"""
from __future__ import annotations

import copy
import json
import time
from pathlib import Path

DEALER_BUY = ("dealer_accept", "dealer_counter")
DEALER_SELL = ("dealer_sell_accept", "dealer_sell_counter")


def key_dealer_buy(thread) -> str:
    return f"dealer-buy:{thread}"


def key_dealer_sell(thread) -> str:
    return f"dealer-sell:{thread}"


def key_settlement(sid) -> str:
    return f"settlement:{sid}"


def key_offer(oid) -> str:
    return f"offer:{oid}"


def counted(led: dict) -> dict:
    return led.setdefault("counted", {})


def count(led: dict, key: str, side: str, amount: int, tick: int, by: str = "") -> bool:
    """Registra el pago/cobro de una operación. Devuelve True solo la primera vez; las repeticiones no suman."""
    reg = counted(led)
    if key in reg:
        return False
    reg[key] = {"side": side, "amount": int(amount), "tick": tick, "by": by}
    if side == "spend":
        led["spent_confirmed"] = led.get("spent_confirmed", 0) + int(amount)
    else:
        led["cash_received"] = led.get("cash_received", 0) + int(amount)
    return True


def duplicates(led: dict) -> list:
    """Acciones del registro que cobran/pagan la MISMA operación que otra (legado anterior a las claves canónicas).
    Exacto para vendedores (el importe pagado consta en la acción); para el mercado se avisa por settlement repetido."""
    out, seen = [], {}
    for a in led.get("actions", []):
        if a.get("status") != "settled" or a.get("duplicate_of"):
            continue  # ya marcada como repetición de otra operación
        if a["type"] in DEALER_BUY and a.get("paid"):
            key, side, amt = key_dealer_buy(a.get("thread")), "spend", int(a["paid"])
        elif a["type"] in DEALER_SELL and a.get("received"):
            key, side, amt = key_dealer_sell(a.get("thread")), "income", int(a["received"])
        elif a.get("settlement") is not None and a["type"] in ("accept", "list", "bid", "swap_list", "team_accept"):
            key, side, amt = key_settlement(a["settlement"]), "market", 0
        else:
            continue
        if key in seen:
            out.append({"key": key, "side": side, "amount": amt, "type": a["type"], "tick": a.get("tick"),
                        "thread": a.get("thread"), "first": seen[key]})
        else:
            seen[key] = a["type"]
    return out


def audit(led: dict) -> dict:
    dups = duplicates(led)
    over_spend = sum(d["amount"] for d in dups if d["side"] == "spend")
    over_income = sum(d["amount"] for d in dups if d["side"] == "income")
    return {"spent_recorded": led.get("spent_confirmed", 0), "income_recorded": led.get("cash_received", 0),
            "double_counted_spend": over_spend, "double_counted_income": over_income,
            "spent_canonical": led.get("spent_confirmed", 0) - over_spend,
            "income_canonical": led.get("cash_received", 0) - over_income,
            "duplicates": dups, "market_duplicates": [d for d in dups if d["side"] == "market"]}


def repair(led: dict, path: Path | None = None) -> dict:
    """Corrección EXPLÍCITA (solo con --repair-accounting): resta el doble conteo exacto de vendedores y lo deja
    registrado en `led["accounting_log"]`. Hace copia de seguridad del registro antes. No toca límites ni reinicia."""
    rep = audit(led)
    if not (rep["double_counted_spend"] or rep["double_counted_income"]):
        return {**rep, "repaired": False}
    if path is not None and Path(path).exists():
        Path(str(path) + f".bak-{int(time.time())}").write_text(Path(path).read_text())
    firsts = {}
    for a in led.get("actions", []):
        if a.get("status") == "settled" and not a.get("duplicate_of"):
            k = (key_dealer_buy(a.get("thread")) if a["type"] in DEALER_BUY and a.get("paid") else
                 key_dealer_sell(a.get("thread")) if a["type"] in DEALER_SELL and a.get("received") else None)
            if k is None:
                continue
            if k in firsts:
                a["duplicate_of"] = k  # repetición: no vuelve a contarse ni en métricas
            else:
                firsts[k] = a
    reg = counted(led)
    for d in rep["duplicates"]:
        if d["side"] != "market":
            reg.setdefault(d["key"], {"side": d["side"], "amount": d["amount"], "tick": d["tick"], "by": "legado"})
    led["spent_confirmed"] = rep["spent_canonical"]
    led["cash_received"] = rep["income_canonical"]
    led.setdefault("accounting_log", []).append({
        "ts": round(time.time()), "action": "repair", "removed_spend": rep["double_counted_spend"],
        "removed_income": rep["double_counted_income"], "before": [rep["spent_recorded"], rep["income_recorded"]],
        "after": [rep["spent_canonical"], rep["income_canonical"]], "duplicates": rep["duplicates"]})
    return {**rep, "repaired": True}


def budget_state(led: dict, limit: int, mode: str = "gross") -> dict:
    """Presupuesto del operador. `limit` es SU límite (no se modifica); `used` según el modo elegido."""
    gross, income = led.get("spent_confirmed", 0), led.get("cash_received", 0)
    used = gross if mode == "gross" else max(0, gross - income)
    why = (f"gasto bruto {gross} P" if mode == "gross" else f"gasto bruto {gross} P − ingresos confirmados {income} P")
    return {"mode": mode, "limit": limit, "gross_spent": gross, "income_confirmed": income, "used": used,
            "remaining": limit - used, "explain": f"{why} frente al límite de {limit} P",
            "receivable_not_cash": "las comisiones por cobrar (p. ej. v10) no cuentan como efectivo ni como ingreso"}


def to_json(x) -> str:
    return json.dumps(x, ensure_ascii=False, indent=1, default=str)
