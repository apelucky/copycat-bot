"""Group Buy detection: 2+ distinct tracked wallets buying the same token within a window.
Funding-source overlap collapses wallets funded by the same wallet into one 'cluster'."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class GroupBuy:
    chain: str
    token: str
    first_ts: int
    buys: list[dict[str, Any]] = field(default_factory=list)   # ordered: wallet, ts, native, funder
    alerted_at: int = 0          # effective count at last alert
    exited: bool = False

    @property
    def wallets(self) -> list[str]:
        return [b["wallet"] for b in self.buys]

    def clusters(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for b in self.buys:
            out.setdefault(b.get("funder") or b["wallet"], []).append(b["wallet"])
        return out

    @property
    def effective_count(self) -> int:
        return len(self.clusters())

    def shared_funders(self) -> dict[str, list[str]]:
        return {f: ws for f, ws in self.clusters().items() if len(ws) > 1}

    def earliest(self, n: int = 2) -> list[str]:
        return self.wallets[:n]

    def to_dict(self) -> dict[str, Any]:
        return {"chain": self.chain, "token": self.token, "first_ts": self.first_ts, "buys": self.buys,
                "alerted_at": self.alerted_at, "exited": self.exited}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "GroupBuy":
        return cls(d["chain"], d["token"], d["first_ts"], d.get("buys", []), d.get("alerted_at", 0), d.get("exited", False))


class GroupBuyDetector:
    def __init__(self, window_s: int = 600, entry_at: int = 2, join_window_s: int | None = None):
        self.window_s = window_s
        self.entry_at = entry_at
        self.join_window_s = join_window_s or window_s * 6
        self.groups: dict[str, GroupBuy] = {}

    @staticmethod
    def key(chain: str, token: str) -> str:
        return f"{chain}:{token}"

    def prune(self, now: int) -> None:
        for k in [k for k, g in self.groups.items() if now - g.first_ts > self.join_window_s and (g.effective_count < self.entry_at or g.exited)]:
            del self.groups[k]

    def on_buy(self, chain: str, token: str, wallet: str, ts: int, native: float, funder: str | None) -> tuple[GroupBuy, str | None]:
        """Returns (group, event) where event is 'formed' when the entry threshold is first reached,
        'joined' when another distinct cluster joins an already-formed group, else None."""
        self.prune(ts)
        k = self.key(chain, token)
        g = self.groups.get(k)
        if g is None or (ts - g.first_ts > self.window_s and g.effective_count < self.entry_at):
            g = GroupBuy(chain, token, ts)
            self.groups[k] = g
        if g.exited or wallet in g.wallets:
            return g, None
        if ts - g.first_ts > self.join_window_s:
            return g, None
        before = g.effective_count
        g.buys.append({"wallet": wallet, "ts": ts, "native": native, "funder": funder})
        after = g.effective_count
        if after == before:
            return g, None  # same funder cluster: no new independent signal
        if after >= self.entry_at and before < self.entry_at:
            return g, "formed"
        if after > self.entry_at:
            return g, "joined"
        return g, None

    def on_sell(self, chain: str, token: str, wallet: str) -> tuple[GroupBuy | None, bool]:
        """(group, early_exit) – early_exit when one of the earliest buyers sells a formed group."""
        g = self.groups.get(self.key(chain, token))
        if not g or g.exited or g.effective_count < self.entry_at:
            return g, False
        if wallet in g.earliest(self.entry_at):
            g.exited = True
            return g, True
        return g, False

    def dump(self) -> list[dict[str, Any]]:
        return [g.to_dict() for g in self.groups.values()]

    def load(self, rows: list[dict[str, Any]]) -> None:
        self.groups = {self.key(r["chain"], r["token"]): GroupBuy.from_dict(r) for r in rows or []}


def scale_out_steps(effective_count: int, entry_at: int, spike: bool, max_steps: int = 3) -> int:
    """Target number of scale-out steps: one per extra cluster beyond entry (3rd/4th/5th), +1 on X spike."""
    steps = max(0, effective_count - entry_at) + (1 if spike else 0)
    return min(max_steps, steps)
