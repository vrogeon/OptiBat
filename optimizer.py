"""Battery scheduling by dynamic programming.

Given a price vector and a load forecast over the horizon, find the
charge/discharge sequence that minimises the grid bill, including
conversion losses and a per-kWh wear cost. The state is the battery SoC,
discretised so that every action is an integer number of SoC steps: no
interpolation, exact energy bookkeeping.

Cost per slot  = grid_import_kWh * price + cycle_cost * kWh_through_cells
Constraints    : no export to the grid, import <= subscribed power,
                 SoC within [soc_min, soc_max], inverter power limits.

Complexity is O(horizon * soc_levels * actions) ~ 40k simple operations
for the defaults - a fraction of a second even on a Raspberry Pi 2.
"""
import time
from dataclasses import dataclass

INF = float("inf")


@dataclass
class PlanStep:
    d: int               # SoC steps moved (+ charge, - discharge)
    battery_w: float     # mean AC power over the slot (+ discharge, - charge)
    energy_ac_wh: float  # |AC energy| planned in the slot
    soc_end: float
    price: float
    load_w: float
    cost_eur: float      # grid energy + wear for this slot


@dataclass
class Plan:
    steps: list
    cost_eur: float      # planned cost over the horizon
    baseline_eur: float  # same horizon without battery
    solve_ms: float


class Optimizer:
    def __init__(self, cfg):
        self.cfg = cfg
        self.dt_h = cfg.slot_minutes / 60.0
        self.S = cfg.soc_levels
        window = (cfg.soc_max - cfg.soc_min) * cfg.capacity_kwh
        self.step_kwh = window / (self.S - 1)

        kc = int(cfg.max_charge_w / 1000.0 * self.dt_h * cfg.eff_charge / self.step_kwh)
        kd = int(cfg.max_discharge_w / 1000.0 * self.dt_h / cfg.eff_discharge / self.step_kwh)
        if kc < 1 or kd < 1:
            # one SoC step would already exceed what the inverter can move in a
            # slot: every planned action would break the power limit
            raise ValueError(
                "SoC step (%.0f Wh) is larger than the energy the battery can move "
                "in one %d-min slot: lower soc_levels or raise max_*_w" % (
                    self.step_kwh * 1000, cfg.slot_minutes))
        self.kc = kc
        self.kd = kd

        # actions: (d, ac_kwh, wear_eur). ac_kwh > 0: drawn from grid (charge),
        # < 0: delivered to the house (discharge). d = 0 first so ties -> idle.
        acts = []
        for d in sorted(range(-self.kd, self.kc + 1), key=lambda x: (abs(x), x)):
            e_dc = abs(d) * self.step_kwh
            if d > 0:
                ac = e_dc / cfg.eff_charge
            elif d < 0:
                ac = -e_dc * cfg.eff_discharge
            else:
                ac = 0.0
            acts.append((d, ac, e_dc * cfg.cycle_cost_eur_kwh))
        self.acts = acts
        self._ac = {d: ac for d, ac, _ in acts}
        self._wear = {d: w for d, _, w in acts}

    # -- SoC <-> index ----------------------------------------------------------
    def index_of(self, soc):
        c = self.cfg
        i = int(round((soc - c.soc_min) / (c.soc_max - c.soc_min) * (self.S - 1)))
        return min(max(i, 0), self.S - 1)

    def soc_of(self, idx):
        return self.cfg.soc_min + idx * self.step_kwh / self.cfg.capacity_kwh

    # -- solve --------------------------------------------------------------------
    def solve(self, soc0, prices, loads_kwh, terminal_value=0.0):
        """prices: EUR/kWh per slot; loads_kwh: forecast house energy per slot."""
        t_start = time.perf_counter()
        cfg = self.cfg
        S = self.S
        N = len(prices)
        glim = (cfg.max_grid_w - cfg.grid_margin_w) / 1000.0 * self.dt_h

        # value of the energy left in the battery when the horizon ends
        V = [terminal_value * i * self.step_kwh * cfg.eff_discharge for i in range(S)]
        policy = [None] * N

        for t in range(N - 1, -1, -1):
            p = prices[t]
            load = loads_kwh[t]
            best = [INF] * S
            pol = [0] * S
            for d, ac, wear in self.acts:
                imp = load + ac
                if d != 0 and (imp < 0.0 or imp > glim):
                    continue                      # no export / grid limit
                c = imp * p + wear
                lo = -d if d < 0 else 0
                hi = S - d if d > 0 else S
                for s in range(lo, hi):
                    v = c + V[s + d]
                    if v < best[s]:
                        best[s] = v
                        pol[s] = d
            V = best
            policy[t] = pol

        # forward pass from the measured SoC
        s = self.index_of(soc0)
        steps = []
        total = 0.0
        base = 0.0
        for t in range(N):
            d = policy[t][s]
            ac = self._ac[d]
            cost = (loads_kwh[t] + ac) * prices[t] + self._wear[d]
            total += cost
            base += loads_kwh[t] * prices[t]
            s += d
            steps.append(PlanStep(
                d=d,
                battery_w=-ac / self.dt_h * 1000.0,
                energy_ac_wh=abs(ac) * 1000.0,
                soc_end=self.soc_of(s),
                price=prices[t],
                load_w=loads_kwh[t] / self.dt_h * 1000.0,
                cost_eur=cost))
        return Plan(steps, total, base, (time.perf_counter() - t_start) * 1000.0)
