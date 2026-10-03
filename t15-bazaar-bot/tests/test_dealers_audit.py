"""Leaders' audit rules for dealer haggling (offline)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bazaar.strategy import dealers as SD  # noqa: E402


def th(dealer, price, final=False, topic=None):
    return {"id": 1, "with": dealer, "topic": topic or {"buy": {"card": "RET-08"}}, "messages": [],
            "standing_offers": [{"id": 7, "maker": dealer, "status": "open", "final": final,
                                 "want": {"cash": price}, "give": {}}]}


def main():
    # Chato final → one counter at final − 1, then accept the final
    conv = {"reservation": 35, "sent": [24], "rarity": "uncommon"}
    out = SD.haggle(th("chato", 27, final=True), conv, None, 10)
    assert [(i.kind, i.args.get("price")) for i in out] == [("say", 26)], out
    out = SD.haggle(th("chato", 27, final=True), conv, None, 11)
    assert [i.kind for i in out] == ["accept"], out
    # Abuela final: no counter (never observed), accept directly
    conv = {"reservation": 30, "sent": [18], "rarity": "uncommon"}
    assert [i.kind for i in SD.haggle(th("abuela", 22, final=True), conv, None, 10)] == ["accept"]
    # step mirroring: Chato uncommon +3, Abuela uncommon/pack +1; Chato rare anchor 0.70
    conv = {"reservation": 35, "sent": [20], "rarity": "uncommon"}
    assert SD.haggle(th("chato", 33), conv, None, 10)[0].args["price"] == 23
    conv = {"reservation": 30, "sent": [16], "rarity": None}
    assert SD.haggle(th("abuela", 25, topic={"buy": {"pack": "sobre_barrio"}}), conv, None, 10)[0].args["price"] == 17
    conv = {"reservation": 110, "rarity": "rare"}
    assert SD.haggle(th("chato", 97), conv, None, 10)[0].args["price"] == 68
    # routing: uncommons cheaper at Abuela than at Chato
    SD.PARAMS["FINALS"] = {"abuela|buy|uncommon": {"n": 8, "median": 22}, "chato|buy|uncommon": {"n": 4, "median": 27}}
    assert SD.cheaper_elsewhere("chato", "uncommon", 27) and not SD.cheaper_elsewhere("abuela", "uncommon", 22)
    SD.PARAMS["FINALS"] = {}
    print("OK — dealer audit rules (counter final−1, step mirroring, anchors, routing)")


if __name__ == "__main__":
    main()
