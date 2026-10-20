
"""
PAPER-TRADING EXECUTION V3.4 + HEBEL LEITER
Implementiert nach SYSTEM PROMPT: TURTLE V3.4 PAPER-TRADING EXECUTION + HEBEL LEITER

- Echte Sizes: 100k Budget, 3%*Leverage, Max Exposure 30%
- Auto-Sell Logik die NICHT zu früh abschiesst
- Hebel ist Zeit-Hebel
- CCXT paper, fee 0.04%, slippage 0.02%, market orders
- Loggt: HOLD_TOO_EARLY, TREND_STILL_OK_HOLD, REBOUND_SIGNAL, TIME_STOP, EQUITY_SAFE_MAX_DD

Integration: Wird von app_v34.py via monitor_positions aufgerufen
"""

import json
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dataclasses import dataclass
from typing import Tuple, Dict, Optional, Literal
import math

# Load Config
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "../config_V3_4.json")
# Fallback wenn nicht gefunden
DEFAULT_CONFIG = {
    "budget": 100000.0,
    "order_size_base": {"position_usd": 6000.0},
    "max_positions": 5,
    "max_exposure_pct": 30.0,
    "equity_safe": {"max_dd_per_hold_pct": -1.5},
    "fees": {"taker_fee_pct": 0.04, "slippage_pct": 0.02, "be_extra_pct": 0.1},
    "auto_sell_stages": {
        "stage_2_hold_respekt": {
            "KURZ": {"min_hold_hours": 2, "time_stop_hours_base": 4},
            "MITTEL": {"min_hold_hours": 4, "time_stop_hours_base": 24},
            "LANG": {"min_hold_hours": 12, "time_stop_hours_base": 168}
        }
    },
    "hebel_leiter": {
        "2x_standard": {"leverage": 2},
        "3x_high_conviction": {"leverage": 3},
        "4x_treasure": {"leverage": 4}
    }
}

try:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        CONFIG = json.load(f)
except:
    CONFIG = DEFAULT_CONFIG

HoldMode = Literal["KURZ","MITTEL","LANG"]

@dataclass
class PaperPosition:
    symbol: str
    entry_price: float
    current_price: float
    leverage: int
    hold_mode: HoldMode
    entry_time: datetime
    be_set: bool = False
    partial_closed_pct: float = 0.0
    # Für Bewertung
    trend_bed1_ok: bool = True
    moneyflow_bed4_ok: bool = True
    rsi_1h: float = 50.0
    vol_spike_pct: float = 0.0
    bb_expansion_up: bool = False

class PaperTradingExecutionV34:
    def __init__(self, config: dict = None):
        self.config = config or CONFIG
        self.budget = self.config.get("budget", 100000.0)
        self.fee_pct = self.config.get("fees", {}).get("taker_fee_pct", 0.04) / 100.0
        self.slippage_pct = self.config.get("fees", {}).get("slippage_pct", 0.02) / 100.0
        self.be_extra_pct = self.config.get("fees", {}).get("be_extra_pct", 0.1) / 100.0
        
        # Min Hold Map aus Config
        stages = self.config.get("auto_sell_stages", {}).get("stage_2_hold_respekt", {})
        self.min_hold_map = {
            "KURZ": stages.get("KURZ", {}).get("min_hold_hours", 2),
            "MITTEL": stages.get("MITTEL", {}).get("min_hold_hours", 4),
            "LANG": stages.get("LANG", {}).get("min_hold_hours", 12)
        }
        self.base_time_stop_map = {
            "KURZ": stages.get("KURZ", {}).get("time_stop_hours_base", 4),
            "MITTEL": stages.get("MITTEL", {}).get("time_stop_hours_base", 24),
            "LANG": stages.get("LANG", {}).get("time_stop_hours_base", 168)
        }
        
        self.logs = []

    def log(self, symbol: str, code: str, detail: str = ""):
        entry = {
            "time": datetime.now(ZoneInfo("Europe/Berlin")).isoformat(),
            "symbol": symbol,
            "code": code,
            "detail": detail
        }
        self.logs.append(entry)
        print(f"[{code}] {symbol} {detail}")

    def calculate_position_size(self, leverage: int, score: int = 7) -> Tuple[float, float]:
        """
        Echte Sizes nach Hebel Leiter
        2x = 6% = 6000 USD
        3x = 9% = 9000 USD
        4x = 12% = 12000 USD
        """
        base_pct = 3.0  # 3%
        pos_pct = base_pct * leverage  # 6%, 9%, 12%
        pos_usd = self.budget * pos_pct / 100.0
        return pos_pct, pos_usd

    def get_leverage_for_score(self, score: int, bed1_perfect: bool, bed4_perfect: bool, usdt_dom_up: bool, funding_neg: bool) -> Tuple[int, HoldMode]:
        """
        Zeit-Bewertung vor Entry nach Log:
        Score 7-8/10 = 2x + Mittel 12-24h
        Score 9/10 + Risk-Off = 3x + Kurz/Mittel 4-12h
        Score 10/10 + USDT up + Funding neg = 4x + Kurz 2-4h
        """
        # Hebel Verbot: Lang-Hold niemals über 2x
        if score >= 10 and usdt_dom_up and funding_neg and bed1_perfect and bed4_perfect:
            return 4, "KURZ"
        elif score >= 9 and bed4_perfect:
            # 3x = High Conviction
            # Hold verkürzt: Mittel statt Lang, max 12-16h
            return 3, "MITTEL"  # wird später auf 16h gekürzt via Formel
        else:
            return 2, "MITTEL"

    def get_time_stop_hours(self, hold_mode: HoldMode, leverage: int) -> float:
        """
        Formel aus Log: Max Hold = Basis Zeit / (Hebel/2)
        Beispiel: 24h / 1.5 = 16h bei 3x
        """
        base = self.base_time_stop_map.get(hold_mode, 24)
        factor = leverage / 2.0
        if factor <= 0:
            factor = 1.0
        adjusted = base / factor
        # Für 4x Treasure: Nur Kurz 2-4h max, erzwingen
        if leverage >= 4:
            adjusted = min(adjusted, 4.0)
        return adjusted

    def should_auto_sell(self, position: dict) -> Tuple[bool, str, dict]:
        """
        Kernlogik - DARF NICHT ZU FRÜH ABSCHIESSEN
        Input position dict:
        {
          pnl_percent, pnl_usd, hold_mode, hours_held, leverage, be_set,
          trend_bed1_ok, moneyflow_bed4_ok, rsi_1h, vol_spike, bb_expansion_up,
          symbol, entry_price, current_price
        }
        Returns: (should_sell: bool, reason_code: str, meta: dict)
        """
        symbol = position.get("symbol", "UNKNOWN")
        pnl = float(position.get("pnl_percent", 0.0))
        hold_mode = position.get("hold_mode", "MITTEL")
        hours_held = float(position.get("hours_held", 0.0))
        leverage = int(position.get("leverage", 2))
        be_set = bool(position.get("be_set", False))
        trend_ok = bool(position.get("trend_bed1_ok", True))
        mf_ok = bool(position.get("moneyflow_bed4_ok", True))
        rsi = float(position.get("rsi_1h", 50.0))
        vol_spike = float(position.get("vol_spike", 0.0))
        bb_exp_up = bool(position.get("bb_expansion_up", False))

        # --- STAGE 3 HARD EQUITY SAFE zuerst ---
        # MaxDD -1.0% bei 4x, sonst -1.5% - Regel aus Log
        max_dd = -1.0 if leverage >= 4 else -1.5
        if pnl <= max_dd:
            self.log(symbol, "EQUITY_SAFE_MAX_DD", f"PnL {pnl:.2f}% <= {max_dd}% Hard Stop | Lev {leverage}x Hold {hold_mode} {hours_held:.1f}h")
            return True, "EQUITY_SAFE_MAX_DD", {"pnl": pnl, "max_dd": max_dd, "action": "100% Market Close"}

        # --- STAGE 1 BE PROTECTION ---
        # BE Trigger: 1.0% bei Leverage>=3, sonst 1.5%
        be_trigger = 1.0 if leverage >= 3 else 1.5
        if pnl >= be_trigger and not be_set:
            # Move SL to Entry + 0.1% Fee - wird in execution.py gemacht
            self.log(symbol, "BE_PROTECTION_TRIGGER", f"PnL {pnl:.2f}% >= {be_trigger}% -> Move SL to Entry+0.1% | Lev {leverage}x")
            # Kein Sell, nur BE setzen - aber signalisiere dass BE gesetzt werden soll
            # Wir return False, aber mit Meta dass BE gesetzt werden soll
            # Für diese Funktion: False = nicht verkaufen, aber BE Flag wird extern gesetzt
            # Hier loggen wir nur
            pass

        # --- STAGE 2 HOLD RESPEKT - KERN LOGIK - Unter Mindestzeit NIE verkaufen ---
        min_hold = self.min_hold_map.get(hold_mode, 4)
        if hours_held < min_hold:
            self.log(symbol, "HOLD_TOO_EARLY", f"{hours_held:.1f}h < {min_hold}h Min Hold {hold_mode} | PnL {pnl:.2f}% egal -> HALTEN! | Lev {leverage}x")
            return False, "HOLD_TOO_EARLY", {"hours_held": hours_held, "min_hold": min_hold}

        # Ab hier: Mindest-Hold erreicht, jetzt Bewertung

        # KURZ 2-4h Logik
        if hold_mode == "KURZ":
            time_stop = self.get_time_stop_hours("KURZ", leverage)
            # TimeStop 4h (adjusted bei 3x/4x) => Market Close
            if hours_held >= time_stop:
                self.log(symbol, "TIME_STOP", f"KURZ {hours_held:.1f}h >= {time_stop:.1f}h TimeStop -> Close | PnL {pnl:.2f}%")
                return True, "TIME_STOP", {"hold_mode": hold_mode, "time_stop": time_stop}
            # Early Exit NUR wenn MoneyFlow dreht auf Risk-On + Vol +40% bei Binance/Coinbase
            # vol_spike >40 und mf_ok=False = Risk-On
            if not mf_ok and vol_spike > 40:
                self.log(symbol, "EARLY_EXIT_MONEYFLOW_RISK_ON", f"MoneyFlow Risk-On + Vol {vol_spike:.1f}% -> Early Exit KURZ | PnL {pnl:.2f}%")
                return True, "EARLY_EXIT_MONEYFLOW_RISK_ON", {"vol_spike": vol_spike, "mf_ok": mf_ok}

        # MITTEL 12-24h (HYPE Beispiel - WICHTIGSTER MODUS)
        elif hold_mode == "MITTEL":
            time_stop = self.get_time_stop_hours("MITTEL", leverage)  # bei 3x = 16h statt 24h
            # Unter 4h NIE verkaufen auch bei -0.7% - schon durch min_hold abgedeckt
            # Alle 4h Bewertung
            eval_every = 4
            # Nur bewerten wenn wir bei 4h, 8h, 12h etc sind (Toleranz 0.5h)
            should_evaluate = (hours_held % eval_every) < 0.5 or hours_held >= time_stop
            
            if should_evaluate or hours_held >= time_stop:
                if hours_held >= time_stop:
                    self.log(symbol, "TIME_STOP", f"MITTEL {hours_held:.1f}h >= {time_stop:.1f}h TimeStop (Lev {leverage}x Formel Basis/(Lev/2)) -> Close")
                    return True, "TIME_STOP", {"hold_mode": hold_mode, "time_stop": time_stop}
                
                # Check Bed1 + Bed4 noch OK?
                if trend_ok and mf_ok:
                    self.log(symbol, "TREND_STILL_OK_HOLD", f"Bed1 fallend={trend_ok} + Bed4 Risk-Off={mf_ok} -> HOLD nicht verkaufen | {hours_held:.1f}h PnL {pnl:.2f}%")
                    return False, "TREND_STILL_OK_HOLD", {"trend_ok": trend_ok, "mf_ok": mf_ok}
                else:
                    # NEIN => 50% Teilverkauf, Rest auf BE
                    self.log(symbol, "PARTIAL_50_BED_FAIL", f"Bed1={trend_ok} Bed4={mf_ok} fail -> 50% Teilverkauf Rest BE | {hours_held:.1f}h PnL {pnl:.2f}%")
                    return False, "PARTIAL_50_BED_FAIL", {"action": "50% close, rest BE", "trend_ok": trend_ok, "mf_ok": mf_ok}

        # LANG 3-7d
        elif hold_mode == "LANG":
            # Hebel Verbot: Lang niemals über 2x!
            if leverage > 2:
                self.log(symbol, "LEVERAGE_VERBOT_LANG", f"LANG Hold darf niemals über 2x! Aktuell {leverage}x -> Force 2x oder Close")
                # Erzwinge Close oder reduziere auf 2x - hier Close
                return True, "LEVERAGE_VERBOT_LANG", {"leverage": leverage}

            time_stop = self.get_time_stop_hours("LANG", leverage)  # 168h Basis
            if hours_held >= time_stop:
                self.log(symbol, "TIME_STOP", f"LANG {hours_held:.1f}h >= {time_stop:.1f}h TimeStop -> Close")
                return True, "TIME_STOP", {}

            # Bewertung alle 12h
            eval_every = 12
            should_evaluate = (hours_held % eval_every) < 0.5

            if should_evaluate:
                # Bei -1.0% NICHT verkaufen, erst -1.5% Hard (schon oben abgefangen)
                if pnl <= -1.0 and pnl > -1.5:
                    self.log(symbol, "HOLD_MINUS_1_LANG", f"LANG bei -1.0% NICHT verkaufen, erst -1.5% Hard | PnL {pnl:.2f}% {hours_held:.1f}h -> HALTEN")
                    return False, "HOLD_MINUS_1_LANG", {"pnl": pnl}
                
                if trend_ok and mf_ok:
                    self.log(symbol, "TREND_STILL_OK_HOLD", f"LANG Bed1={trend_ok} Bed4={mf_ok} -> HOLD")
                    return False, "TREND_STILL_OK_HOLD", {}
                else:
                    self.log(symbol, "PARTIAL_50_BED_FAIL", f"LANG Bed fail -> 50% Teilverkauf")
                    return False, "PARTIAL_50_BED_FAIL", {}

        # --- STAGE 4 SMART PROFIT (Kein fixer TP mehr!) ---
        # Rebound Signal: RSI1h >65 + Vol Spike >50% + BB Ausdehnung oben
        if rsi > 65 and vol_spike > 50 and bb_exp_up:
            self.log(symbol, "REBOUND_SIGNAL", f"RSI {rsi:.1f}>65 + Vol {vol_spike:.1f}% >50% + BB Expansion oben -> Close 75% Rest Trailing BE+0.5% | PnL {pnl:.2f}%")
            return False, "REBOUND_SIGNAL", {"action": "Close 75%, rest trailing BE+0.5%", "rsi": rsi, "vol_spike": vol_spike}

        # Wenn kein Signal bis TimeStop => TimeStop Close (schon oben behandelt)

        # Zeit-Bewertung mit Hebel während Hold alle 4h Check (Zusatzregel)
        # Wenn Hebel 3x+ und PnL -0.8% nach 2h => Früher raus
        # Wenn Hebel 2x und PnL -0.8% nach 4h => Weiter halten
        if leverage >= 3 and pnl <= -0.8 and hours_held >= 2 and hours_held < 4:
            self.log(symbol, "HEBEL_3X_EARLY_EXIT", f"Leverage {leverage}x + PnL {pnl:.2f}% nach {hours_held:.1f}h -> Früher raus (bei 2x würde halten)")
            return True, "HEBEL_3X_EARLY_EXIT", {"leverage": leverage, "pnl": pnl}
        elif leverage == 2 and pnl <= -0.8 and hours_held >= 4 and hours_held < 6:
            self.log(symbol, "HEBEL_2X_HOLD", f"Leverage 2x + PnL {pnl:.2f}% nach {hours_held:.1f}h -> Weiter halten (bei 3x wäre raus)")
            return False, "HEBEL_2X_HOLD", {}

        # Default: halten
        return False, "HOLD_DEFAULT", {"pnl": pnl, "hours": hours_held}

    def evaluate_paper_distribution(self, trades_history: list) -> dict:
        """
        Paper Verteilung zum Testen:
        70% Trades 2x = Training
        25% Trades 3x = High Conviction
        5% Trades 4x = Treasure
        Teste: Bringt 3x wirklich +50% PnL oder nur +50% Stress?
        """
        total = len(trades_history)
        if total == 0:
            return {"2x": 0, "3x": 0, "4x": 0}
        
        count_2x = sum(1 for t in trades_history if t.get("leverage",2)==2)
        count_3x = sum(1 for t in trades_history if t.get("leverage",2)==3)
        count_4x = sum(1 for t in trades_history if t.get("leverage",2)==4)
        
        pnl_2x = sum(t.get("pnl_usd",0) for t in trades_history if t.get("leverage",2)==2)
        pnl_3x = sum(t.get("pnl_usd",0) for t in trades_history if t.get("leverage",2)==3)
        pnl_4x = sum(t.get("pnl_usd",0) for t in trades_history if t.get("leverage",2)==4)
        
        return {
            "distribution": {
                "2x": f"{count_2x}/{total} ({count_2x/total*100:.1f}%) Ziel 70%",
                "3x": f"{count_3x}/{total} ({count_3x/total*100:.1f}%) Ziel 25%",
                "4x": f"{count_4x}/{total} ({count_4x/total*100:.1f}%) Ziel 5%"
            },
            "pnl": {
                "2x": pnl_2x,
                "3x": pnl_3x,
                "4x": pnl_4x,
                "total": pnl_2x+pnl_3x+pnl_4x,
                "3x_vs_2x_per_trade": (pnl_3x/max(count_3x,1)) / (pnl_2x/max(count_2x,1)) if count_2x>0 and count_3x>0 else 0
            },
            "question": "Bringt 3x wirklich +50% PnL oder nur +50% Stress?",
            "recommendation": "Wenn 3x PnL/trade <1.3x von 2x, dann 3x Stress nicht wert -> nur 2x traden"
        }

    def calculate_fees(self, position_usd: float) -> float:
        """Fee 0.04% + Slippage 0.02%"""
        return position_usd * (self.fee_pct + self.slippage_pct)

    def simulate_market_order(self, symbol: str, side: str, amount_usd: float, price: float) -> dict:
        """Paper Market Order mit Fee + Slippage"""
        fee = self.calculate_fees(amount_usd)
        slippage_price = price * (1 + self.slippage_pct if side=="BUY" else 1 - self.slippage_pct)
        return {
            "symbol": symbol,
            "side": side,
            "requested_usd": amount_usd,
            "executed_price": slippage_price,
            "fee_usd": fee,
            "net_usd": amount_usd - fee,
            "timestamp": datetime.now(ZoneInfo("Europe/Berlin")).isoformat()
        }

# === Integration Helper für app_v34.py ===

def create_paper_position_from_splinter(symbol: str, price: float, splinter_decision, score: int, bed1_perfect: bool, bed4_perfect: bool, usdt_dom_up: bool, funding_neg: bool, leverage_override: int = None):
    """
    Erstellt Paper Position mit korrektem Hebel nach Score + Bed
    """
    executor = PaperTradingExecutionV34()
    
    if leverage_override:
        leverage = leverage_override
        # Hold Mode aus Splinter
        hold_mode = splinter_decision.hold_mode if hasattr(splinter_decision, 'hold_mode') else "MITTEL"
    else:
        leverage, hold_mode = executor.get_leverage_for_score(score, bed1_perfect, bed4_perfect, usdt_dom_up, funding_neg)
        # Wenn Splinter schon Hold Mode hat, respektiere aber kürze bei 3x/4x
        if hasattr(splinter_decision, 'hold_mode'):
            # Hebel Verbot: Lang niemals über 2x
            if splinter_decision.hold_mode == "LANG" and leverage > 2:
                leverage = 2
                hold_mode = "LANG"
            elif leverage >=4:
                hold_mode = "KURZ"  # 4x nur Kurz
    
    pos_pct, pos_usd = executor.calculate_position_size(leverage, score)
    
    # TimeStop nach Formel
    time_stop = executor.get_time_stop_hours(hold_mode, leverage)
    
    return {
        "symbol": symbol,
        "entry": price,
        "current_price": price,
        "leverage": leverage,
        "hold_mode": hold_mode,
        "position_pct": pos_pct,
        "position_usd": pos_usd,
        "time_stop_hours": time_stop,
        "score": score,
        "entry_time": datetime.now(ZoneInfo("Europe/Berlin")).isoformat(),
        "bed1_perfect": bed1_perfect,
        "bed4_perfect": bed4_perfect,
        "usdt_dom_up": usdt_dom_up,
        "funding_neg": funding_neg
    }
