"""Backtest / benchmark on a synthetic house, no hardware needed.

    python3 simulate.py [--days 35] [--tick 15] [--seed 1]

Compares, over the same synthetic prices and consumption:
  * no battery
  * a naive price-threshold strategy (no forecasting, no learning)
  * the smart controller, starting with ZERO knowledge (cold start)
  * an oracle that knows every future price and load (upper bound)

The data is synthetic (French-style price shape: morning/evening peaks,
midday solar dip, cheap nights; a house with morning/evening peaks and
random appliance bursts). Real savings depend on your real price spread
and consumption: replay your own logs through this script to check.
"""
import argparse
import logging
import math
import random
import resource
import time
from datetime import datetime, timedelta

from config import Config
from controller import SmartController
from optimizer import Optimizer


def g(x, mu, sig):
    return math.exp(-0.5 * ((x - mu) / sig) ** 2)


# ----------------------------------------------------------------------- world
class World:
    def __init__(self, start, days, tick_s, seed):
        rng = random.Random(seed)
        self.start, self.days, self.tick = start, days, tick_s
        self.prices = []                       # [day][slot] EUR/kWh
        off = 0.0
        for d in range(days + 2):
            weekend = (start + timedelta(days=d)).weekday() >= 5
            off = 0.7 * off + rng.gauss(0, 0.02)
            sun = rng.random() ** 0.7
            amp = rng.uniform(0.7, 1.3)
            row = []
            for s in range(96):
                h = s / 4.0
                p = 0.20 + off
                p += amp * 0.10 * g(h, 8.0, 1.4) * (0.5 if weekend else 1.0)
                p += amp * 0.14 * g(h, 19.5, 1.8)
                p -= 0.12 * sun * g(h, 13.5, 2.6) * (1.3 if weekend else 1.0)
                p -= 0.04 * g(h, 3.5, 2.5)
                p += rng.gauss(0, 0.008)
                row.append(round(p, 4))
            self.prices.append(row)

        self.loads = []                        # one value per tick (W)
        per_day = int(86400 / tick_s)
        for d in range(days):
            weekend = (start + timedelta(days=d)).weekday() >= 5
            scale = rng.uniform(0.85, 1.15)
            events = []
            for _ in range(rng.randint(2, 5)):
                st = rng.uniform(6, 22) * 3600
                events.append((st, st + rng.uniform(10, 90) * 60, rng.uniform(1200, 2200)))
            for k in range(per_day):
                t = k * tick_s
                h = t / 3600.0
                w = (220 + 480 * g(h, 7.8, 1.0) * (0.6 if weekend else 1.0)
                     + 220 * g(h, 12.5, 1.6) * (1.6 if weekend else 1.0)
                     + 1000 * g(h, 20.0, 2.0)) * scale * math.exp(rng.gauss(0, 0.12))
                for a, b, pw in events:
                    if a <= t < b:
                        w += pw
                self.loads.append(w)
        self.ticks = len(self.loads)

    def price_at(self, now):
        d = (now.date() - self.start.date()).days
        return self.prices[d][(now.hour * 60 + now.minute) // 15]

    def curve(self, now):
        """Published prices: today, plus tomorrow once it is 13:00 (day-ahead)."""
        d0 = (now.date() - self.start.date()).days
        out = []
        days = (d0, d0 + 1) if now.hour >= 13 else (d0,)
        for d in days:
            base = self.start + timedelta(days=d)
            for s in range(96):
                out.append((base + timedelta(minutes=15 * s), self.prices[d][s]))
        return out


class SimIO:
    grid_signed = False                        # worst case: meter only reads import

    def __init__(self, world, cfg, soc0=0.5):
        self.w, self.cfg = world, cfg
        self.now = world.start
        self.soc = soc0
        self.cmd = 0.0
        self.reading = 0.0
        self.cost = 0.0
        self.throughput_dc = 0.0
        self.import_kwh = 0.0

    def read_grid_w(self):
        return self.reading

    def get_soc(self):
        return self.soc

    def set_battery_power_w(self, w):
        self.cmd = w

    def get_price_curve(self):
        return self.w.curve(self.now)

    def physics(self, load_w):
        c = self.cfg
        cmd = max(-c.max_charge_w, min(c.max_discharge_w, self.cmd))
        if cmd > 0 and self.soc <= c.soc_min:
            cmd = 0.0                           # BMS protection
        if cmd < 0 and self.soc >= c.soc_max:
            cmd = 0.0
        dt_h = self.w.tick / 3600.0
        if cmd > 0:
            dc = cmd / 1000 * dt_h / c.eff_discharge
            self.soc -= dc / c.capacity_kwh
            self.throughput_dc += dc
        elif cmd < 0:
            dc = -cmd / 1000 * dt_h * c.eff_charge
            self.soc += dc / c.capacity_kwh
            self.throughput_dc += dc
        imp = max(0.0, load_w - cmd)            # exported energy is simply lost
        self.reading = imp
        kwh = imp / 1000 * dt_h
        self.import_kwh += kwh
        self.cost += kwh * self.w.price_at(self.now)


class ThresholdController:
    """Naive baseline: charge in the cheapest 30 % of known prices, follow the
    load in the dearest 30 %. No forecasting, no learning."""

    def __init__(self, cfg, io):
        self.cfg, self.io = cfg, io
        self.sp = 0.0
        self._t = None
        self._lo = self._hi = 0.0

    def step(self, now):
        c = self.cfg
        if self._t is None or (now - self._t).total_seconds() > 1800:
            self._t = now
            ps = sorted(p for t, p in self.io.get_price_curve() if t >= now)
            if ps:
                self._lo = ps[int(0.3 * (len(ps) - 1))]
                self._hi = ps[int(0.7 * (len(ps) - 1))]
        grid = self.io.read_grid_w()
        soc = self.io.get_soc()
        price = self.io.w.price_at(now)
        load = grid + self.sp
        if price <= self._lo and soc < c.soc_max:
            room = c.max_grid_w - c.grid_margin_w - load
            self.sp = -max(0.0, min(c.max_charge_w, room))
        elif price >= self._hi and soc > c.soc_min:
            if grid >= 1.0:
                self.sp += c.follow_gain * (grid - c.target_import_w)
            else:
                self.sp -= max(25.0, 0.25 * self.sp)
            self.sp = min(max(self.sp, 0.0), c.max_discharge_w)
        else:
            self.sp = 0.0
        self.io.set_battery_power_w(self.sp)


# ------------------------------------------------------------------------ runs
def run(world, cfg, strategy):
    io = SimIO(world, cfg)
    ctl = None
    if strategy == "smart":
        ctl = SmartController(cfg, io)
    elif strategy == "threshold":
        ctl = ThresholdController(cfg, io)
    weekly = []                                 # (cost, baseline, wear, load_err)
    per_week = int(7 * 86400 / world.tick)
    wk_cost0 = wk_wear0 = wk_base = 0.0
    wear_rate = cfg.cycle_cost_eur_kwh
    for k in range(world.ticks):
        now = world.start + timedelta(seconds=k * world.tick)
        io.now = now
        if ctl is not None:
            ctl.step(now)
        load = world.loads[k]
        io.physics(load)
        wk_base += load / 1000 * world.tick / 3600 * world.price_at(now)
        if (k + 1) % per_week == 0:
            err = ctl.load_fc.rel_err if strategy == "smart" else None
            wear = io.throughput_dc * wear_rate
            weekly.append((io.cost - wk_cost0, wk_base, wear - wk_wear0, err))
            wk_cost0, wk_wear0, wk_base = io.cost, wear, 0.0
    total_base = sum(w[1] for w in weekly)
    return {"cost": io.cost, "wear": io.throughput_dc * wear_rate,
            "base": total_base, "weekly": weekly, "ctl": ctl}


def oracle(world, cfg):
    """Perfect-information optimum over the whole period (slot model)."""
    spd = 96
    per_slot = int(900 / world.tick)
    n_slots = world.days * spd
    prices, loads = [], []
    for i in range(n_slots):
        d, s = divmod(i, spd)
        prices.append(world.prices[d][s])
        seg = world.loads[i * per_slot:(i + 1) * per_slot]
        loads.append(sum(seg) / len(seg) / 1000.0 * 0.25)
    opt = Optimizer(cfg)
    plan = opt.solve(0.5, prices, loads, 0.0)
    weekly = []
    for w in range(world.days // 7):
        seg = plan.steps[w * 7 * spd:(w + 1) * 7 * spd]
        base = sum(st.load_w / 1000 * 0.25 * st.price for st in seg)
        weekly.append((sum(st.cost_eur for st in seg), base))
    return plan, weekly


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=35)
    ap.add_argument("--tick", type=int, default=15, help="simulation step in s")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--capacity-kwh", type=float, default=None, help="override config")
    ap.add_argument("--power-w", type=float, default=None,
                    help="override max charge and discharge power")
    args = ap.parse_args()
    logging.disable(logging.CRITICAL)
    days = args.days - args.days % 7

    over = {}
    if args.capacity_kwh:
        over["capacity_kwh"] = args.capacity_kwh
    if args.power_w:
        over["max_charge_w"] = over["max_discharge_w"] = args.power_w
    cfg = Config(state_path=None, **over)
    start = datetime(2026, 10, 12)              # a Monday
    t0 = time.time()
    world = World(start, days, args.tick, args.seed)
    print("world generated: %d days, %.1f kWh/day average load  (%.0fs)" % (
        days, sum(world.loads) / 1000 * args.tick / 3600 / days, time.time() - t0))

    res_thr = run(world, cfg, "threshold")
    t0 = time.time()
    res_smart = run(world, cfg, "smart")
    print("smart controller simulated in %.0fs" % (time.time() - t0))
    plan, orc_weekly = oracle(world, cfg)

    base = res_thr["base"]
    def net(r): return base - r["cost"] - r["wear"]
    orc_net = base - plan.cost_eur

    print("\n=== %d days, battery %.1f kWh / %.1f kW ===" % (
        days, cfg.capacity_kwh, cfg.max_discharge_w / 1000))
    print("%-34s %9s %9s" % ("", "net EUR", "per year"))
    for name, v in (("no battery (bill)", None),
                    ("naive threshold", net(res_thr)),
                    ("smart controller (learned)", net(res_smart)),
                    ("oracle (perfect foresight)", orc_net)):
        if v is None:
            print("%-34s %9.2f" % (name, base))
        else:
            print("%-34s %9.2f %9.0f" % (name, v, v * 365 / days))
    print("smart captures %.0f%% of the oracle's savings, naive %.0f%%" % (
        100 * net(res_smart) / orc_net, 100 * net(res_thr) / orc_net))

    print("\nSmart controller, week by week (it starts knowing nothing):")
    print("%-6s %10s %10s %14s %12s" % ("week", "net EUR", "of oracle", "load fcst err", "naive net"))
    for i, (w, o, t) in enumerate(zip(res_smart["weekly"], orc_weekly, res_thr["weekly"])):
        n_s = w[1] - w[0] - w[2]
        n_o = o[1] - o[0]
        n_t = t[1] - t[0] - t[2]
        print("%-6d %10.2f %9.0f%% %13.0f%% %12.2f" % (i + 1, n_s, 100 * n_s / n_o,
                                                         100 * (w[3] or 0), n_t))
    c = res_smart["ctl"]
    print("\nlearned expert weights (week/daytype/lag): load %s  price %s" % (
        [round(x, 2) for x in c.load_fc.w], [round(x, 2) for x in c.price_fc.w]))

    # ---- footprint on the target hardware -----------------------------------
    opt = Optimizer(cfg)
    prices = [0.2 + 0.1 * math.sin(i / 8.0) for i in range(cfg.horizon_slots)]
    loads = [0.6] * cfg.horizon_slots           # 2.4 kW: every action feasible = worst case
    ts = []
    for _ in range(20):
        ts.append(opt.solve(0.5, prices, loads, 0.18).solve_ms)
    ts.sort()
    print("\nplanning time on this machine: median %.0f ms (24 h horizon, %d SoC levels, "
          "%d actions)" % (ts[len(ts) // 2], cfg.soc_levels, len(opt.acts)))
    print("peak memory of this whole simulation process: %.0f MB" % (
        resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0))


if __name__ == "__main__":
    main()
