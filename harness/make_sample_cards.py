"""Render sample trade / PnL cards with clearly fake example data."""
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copycat.bot.cards import pnl_card, trade_card  # noqa: E402

out = Path(sys.argv[1] if len(sys.argv) > 1 else "/workspace/copycat-mockup/cards")
out.mkdir(parents=True, exist_ok=True)
tz = ZoneInfo("Africa/Lagos")
t0 = 1793000000
trade_card({"chain": "solana", "symbol": "BONKZILLA", "source": "copy", "trader": "lunadegen", "cost": 0.5007, "pnl": 0.284,
            "entry": 0.0000418, "exit": 0.0000655, "slippage": 1.46, "fees": 0.0114, "opened_at": t0, "closed_at": t0 + 2460},
           str(out / "trade_card_solana_win.png"), tz, "Example data · not a real trade")
trade_card({"chain": "base", "symbol": "FROGWIF", "source": "group", "trader": None, "cost": 0.0150, "pnl": -0.0041,
            "entry": 0.00000231, "exit": 0.00000168, "slippage": None, "fees": 0.00021, "opened_at": t0, "closed_at": t0 + 840},
           str(out / "trade_card_base_loss.png"), tz, "Example data · not a real trade")
trade_card({"chain": "robinhood", "symbol": "HOODCAT", "source": "manual", "trader": None, "cost": 0.0200, "pnl": 0.0093,
            "entry": 0.00000052, "exit": 0.00000077, "slippage": None, "fees": 0.00025, "opened_at": t0, "closed_at": t0 + 7300},
           str(out / "trade_card_robinhood_manual.png"), tz, "Example data · not a real trade")
pnl_card("last 7 days", [
    {"chain": "solana", "net": 2.48, "pct": 24.8, "n": 41, "win_rate": 0.634, "best_sym": "BONKZILLA", "best": 0.62, "worst_sym": "RUGDOG", "worst": -0.21, "avg_slip": 1.2},
    {"chain": "base", "net": 0.031, "pct": 6.2, "n": 9, "win_rate": 0.556, "best_sym": "BASEDPEPE", "best": 0.018, "worst_sym": "FROGWIF", "worst": -0.0041, "avg_slip": 0.9},
    {"chain": "ethereum", "net": -0.012, "pct": -2.4, "n": 4, "win_rate": 0.25, "best_sym": "MOG2", "best": 0.004, "worst_sym": "GASBAG", "worst": -0.009, "avg_slip": 2.1},
], str(out / "pnl_card_7d.png"), "Example data · simulated (paper) results")
print(sorted(p.name for p in out.iterdir()))
