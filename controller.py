"""Smart battery controller: forecast -> plan -> real-time tracking.

Two layers
----------
1. Planner (once per 15-min slot, and when new prices arrive)
   Forecasts house load and unknown prices from what it has learned, then
   solves the 24 h schedule with dynamic programming (optimizer.py).
2. Real-time layer (every tick, ~1 s)
   Executes the first step of the plan:
     charge    -> constant grid charging, throttled to respect the subscribed
                  power, catching up if it was throttled earlier in the slot;
     discharge -> closed loop that follows the house load so the meter reads
                  just above zero (never exports), within the slot's energy
                  budget;
     idle      -> battery off.

Learning
--------
At the end of each slot the measured house load (grid + battery) trains the
load forecaster; published prices train the price forecaster (used to fill
the part of the horizon whose prices are not yet known). Both blend several
experts with multiplicative weights, so they keep adapting. State is
persisted (rarely, to spare the SD card) and restored on restart.

IO contract (see adapters.py)
-----------------------------
    io.read_grid_w()            -> float | None   net grid power, import > 0; None = no
                                                  fresh measurement (fail-safe: idle)
    io.get_soc()                -> float | None   0..1, None if unavailable
    io.set_battery_power_w(w)   -> None           + discharge / - charge
    io.get_price_curve()        -> [(datetime, eur_per_kwh_raw), ...]
    io.grid_signed (optional)   -> bool
"""
import json
import logging
import os
from datetime import timedelta

from forecast import HedgeForecaster
from optimizer import Optimizer

log = logging.getLogger("smart")


class SmartController:
    STATE_VERSION = 1

    def __init__(self, cfg, io):
        self.cfg = cfg
        self.io = io
        self.sm = cfg.slot_minutes
        self.dt_slot_h = cfg.slot_minutes / 60.0

        self.load_fc = HedgeForecaster(
            cfg.slot_minutes, prior=cfg.load_prior_w, alpha=cfg.load_alpha,
            eta=cfg.hedge_eta, memory_slots=cfg.hedge_memory_slots,
            scale_floor=100.0, use_bias=True, floor_value=0.0)
        self.price_fc = HedgeForecaster(
            cfg.slot_minutes, prior=cfg.price_prior_eur_kwh, alpha=cfg.price_alpha,
            eta=cfg.hedge_eta, memory_slots=cfg.hedge_memory_slots,
            scale_floor=0.05, use_bias=False)
        self.opt = Optimizer(cfg)

        self.soc = cfg.soc_initial
        self.plan = None
        self.known_prices = {}            # slot start -> EUR/kWh (all-in)
        self.setpoint = 0.0               # last command actually sent (+ discharge)
        self.totals = {"cost": 0.0, "baseline": 0.0, "wear": 0.0}

        self._last_now = None
        self._last_grid_t = None
        self._load_w = cfg.load_prior_w
        self._slot_start = None
        self._slot_price = cfg.price_prior_eur_kwh
        self._acc_load_ws = 0.0
        self._acc_t = 0.0
        self._slot_charged_wh = 0.0
        self._slot_discharged_wh = 0.0
        self._last_sent = None
        self._last_sent_t = None
        self._cmd_retry_at = None
        self._price_fetch_at = None
        self._price_trained_until = None
        self._prices_dirty = False
        self._soc_polled_at = None
        self._last_saved = None
        self._day_marker = None

        self.load_state()

    # ------------------------------------------------------------------ helpers
    def _floor(self, dt):
        return dt.replace(minute=(dt.minute // self.sm) * self.sm,
                          second=0, microsecond=0)

    def _price_for(self, slot_start, h=0):
        p = self.known_prices.get(slot_start)
        return p if p is not None else self.price_fc.predict(slot_start, h)

    def _signed(self):
        return getattr(self.io, "grid_signed", self.cfg.grid_signed)

    def _battery_w(self):
        """Battery power actually flowing (0 in dry-run, where nothing is sent)."""
        return 0.0 if getattr(self.io, "dry_run", False) else self.setpoint

    # ----------------------------------------------------------------- main tick
    def step(self, now):
        """Call every control period (~1 s) with the current local time."""
        cfg = self.cfg
        dt = 1.0 if self._last_now is None else \
            min(max((now - self._last_now).total_seconds(), 0.0), 60.0)
        self._last_now = now

        grid_w = self.io.read_grid_w()
        if grid_w is None:                                   # no fresh measurement
            if self._last_grid_t is None:
                self._last_grid_t = now
            elif (now - self._last_grid_t).total_seconds() > cfg.stale_s:
                self._apply(0.0, now)                        # fail safe: idle
            return
        self._last_grid_t = now

        self._update_soc(now, dt)
        load_w = self._estimate_load(grid_w)

        slot_start = self._floor(now)
        if slot_start != self._slot_start:
            self._rollover(now, slot_start)
        elif self._refresh_prices(now) or self._prices_dirty:
            self._prices_dirty = False
            self._replan(slot_start)

        self._account(grid_w, load_w, dt)
        self._apply(self._realtime_target(now, grid_w, load_w), now)
        self._maybe_save(now)

    def shutdown(self):
        """Call on exit: stop the battery and persist what was learned."""
        try:
            self.io.set_battery_power_w(0)
        except Exception:
            log.exception("could not zero the battery on shutdown")
        self.save_state()

    # ------------------------------------------------------- measurements / SoC
    def _update_soc(self, now, dt):
        cfg = self.cfg
        p = self._battery_w()                   # power applied during this tick
        if p > 0:
            e_ac = p * dt / 3600.0              # Wh delivered
            dc = e_ac / cfg.eff_discharge / 1000.0
            self.soc -= dc / cfg.capacity_kwh
            self._slot_discharged_wh += e_ac
            self.totals["wear"] += dc * cfg.cycle_cost_eur_kwh
        elif p < 0:
            e_ac = -p * dt / 3600.0
            dc = e_ac * cfg.eff_charge / 1000.0
            self.soc += dc / cfg.capacity_kwh
            self._slot_charged_wh += e_ac
            self.totals["wear"] += dc * cfg.cycle_cost_eur_kwh
        self.soc = min(max(self.soc, 0.0), 1.0)

        if (self._soc_polled_at is None or
                (now - self._soc_polled_at).total_seconds() >= cfg.soc_poll_s):
            self._soc_polled_at = now
            try:
                meas = self.io.get_soc()
            except Exception:
                meas = None
            if meas is not None:                # trust the BMS over our estimate
                self.soc = min(max(float(meas), 0.0), 1.0)

    def _estimate_load(self, grid_w):
        batt = self._battery_w()
        if not self._signed() and grid_w < 1.0 and batt > 0:
            # meter reads 0: battery may have been over-covering, load is censored
            load = min(batt, self._load_w)
        else:
            load = grid_w + batt
        self._load_w = max(0.0, load)
        return self._load_w

    def _account(self, grid_w, load_w, dt):
        price = self._slot_price
        self.totals["cost"] += max(grid_w, 0.0) * dt / 3.6e6 * price
        self.totals["baseline"] += load_w * dt / 3.6e6 * price
        self._acc_load_ws += load_w * dt
        self._acc_t += dt

    # -------------------------------------------------------------- slot handling
    def _rollover(self, now, slot_start):
        if self._slot_start is not None:
            self._finish_slot()
        self._slot_start = slot_start
        self._acc_load_ws = 0.0
        self._acc_t = 0.0
        self._slot_charged_wh = 0.0
        self._slot_discharged_wh = 0.0
        self._refresh_prices(now)
        self._prices_dirty = False
        self._train_prices(slot_start)
        self._slot_price = self._price_for(slot_start)
        self._replan(slot_start)

        day = slot_start.date()
        if self._day_marker is not None and day != self._day_marker:
            t = self.totals
            log.info("DAY SUMMARY %s: bill %.2f EUR (no battery %.2f), wear %.2f, "
                     "net saving %.2f EUR", self._day_marker, t["cost"],
                     t["baseline"], t["wear"], t["baseline"] - t["cost"] - t["wear"])
        self._day_marker = day

    def _finish_slot(self):
        if self._acc_t >= 0.5 * self.sm * 60.0:
            mean_w = self._acc_load_ws / self._acc_t
            self.load_fc.update(self._slot_start, mean_w)
            log.info("slot %s load %.0f W price %.3f soc %.0f%% | load err %.0f%% "
                     "weights %s", self._slot_start.strftime("%a %H:%M"), mean_w,
                     self._slot_price, self.soc * 100,
                     100 * (self.load_fc.rel_err or 0),
                     ",".join("%.2f" % w for w in self.load_fc.w))

    # ------------------------------------------------------------------- prices
    def _refresh_prices(self, now):
        """Fetch prices when due. Returns True if new data arrived."""
        cfg = self.cfg
        slot_start = self._floor(now)
        horizon_end = slot_start + timedelta(minutes=self.sm * cfg.horizon_slots)
        covered = max(self.known_prices) if self.known_prices else None
        full = covered is not None and covered >= horizon_end - timedelta(minutes=self.sm)
        every = cfg.price_refresh_full_min if full else cfg.price_refresh_min
        if (self._price_fetch_at is not None and
                (now - self._price_fetch_at).total_seconds() < every * 60):
            return False
        self._price_fetch_at = now              # even on failure: don't hammer the API
        try:
            curve = self.io.get_price_curve()
        except Exception as exc:
            log.warning("price fetch failed: %s", exc)
            return False
        vals = [(self._floor(t), raw * cfg.price_scale + cfg.price_adder_eur_kwh)
                for t, raw in curve]
        if any(not (-0.5 <= v <= 3.0) for _, v in vals):
            log.error("price outside -0.5..3 EUR/kWh: check price_scale "
                      "(is the API in c/kWh or EUR/MWh?). Fetch ignored.")
            return False
        changed = False
        for key, val in vals:
            if self.known_prices.get(key) != val:
                self.known_prices[key] = val
                changed = True
        cutoff = slot_start - timedelta(days=2)
        for k in [k for k in self.known_prices if k < cutoff]:
            del self.known_prices[k]
        return changed

    def _train_prices(self, slot_start):
        """Teach the price forecaster every published price of a past slot."""
        done = self._price_trained_until
        for t in sorted(k for k in self.known_prices if k < slot_start):
            if done is None or t > done:
                self.price_fc.update(t, self.known_prices[t])
                done = t
        self._price_trained_until = done

    # --------------------------------------------------------------------- plan
    def _replan(self, slot_start):
        cfg = self.cfg
        N = cfg.horizon_slots
        slots = [slot_start + timedelta(minutes=self.sm * i) for i in range(N)]
        prices = [self._price_for(t, i) for i, t in enumerate(slots)]
        loads = [self.load_fc.predict(t, i) / 1000.0 * self.dt_slot_h
                 for i, t in enumerate(slots)]
        tv = cfg.terminal_value_factor * sum(prices) / N
        self.plan = self.opt.solve(self.soc, prices, loads, tv)
        s0 = self.plan.steps[0]
        log.info("PLAN %s soc %.0f%% -> %s %.0f W | horizon cost %.2f EUR "
                 "(no battery %.2f) | %.0f ms", slot_start.strftime("%a %H:%M"),
                 self.soc * 100,
                 "charge" if s0.d > 0 else "discharge" if s0.d < 0 else "idle",
                 abs(s0.battery_w), self.plan.cost_eur, self.plan.baseline_eur,
                 self.plan.solve_ms)

    # --------------------------------------------------------------- real time
    def _realtime_target(self, now, grid_w, load_w):
        cfg = self.cfg
        if self.plan is None:
            return 0.0
        s0 = self.plan.steps[0]
        if s0.d == 0:
            return 0.0

        if s0.d > 0:                                          # ---- charge
            if self.soc >= cfg.soc_max:
                return 0.0
            budget = s0.energy_ac_wh * cfg.charge_budget_factor - self._slot_charged_wh
            if budget <= 0.0:
                return 0.0
            left_s = max((self._slot_start + timedelta(minutes=self.sm) - now)
                         .total_seconds(), 60.0)
            want = budget * 3600.0 / left_s                  # catches up if throttled
            room = cfg.max_grid_w - cfg.grid_margin_w - load_w
            return -max(0.0, min(want, cfg.max_charge_w, room))

        # ---- discharge: follow the load, within the slot's energy budget
        if self.soc <= cfg.soc_min:
            return 0.0
        budget = s0.energy_ac_wh * cfg.discharge_budget_factor - self._slot_discharged_wh
        if budget <= 0.0:
            return 0.0
        sp = self.setpoint if self.setpoint > 0 else 0.0
        if self._signed() or grid_w >= 1.0:
            sp += cfg.follow_gain * (grid_w - cfg.target_import_w)
        else:                                                # meter at 0: back off fast
            sp -= max(25.0, 0.25 * sp)
        return min(max(sp, 0.0), cfg.max_discharge_w)

    def _apply(self, target_w, now):
        cfg = self.cfg
        sp = int(round(target_w))
        due = (self._last_sent is None
               or abs(sp - self._last_sent) >= cfg.min_change_w
               or (sp == 0 and self._last_sent != 0)
               or (now - self._last_sent_t).total_seconds() >= cfg.command_refresh_s)
        if not due:
            return
        if self._cmd_retry_at is not None and now < self._cmd_retry_at:
            return                              # device unreachable: back off
        try:
            self.io.set_battery_power_w(sp)
        except Exception as exc:
            log.warning("battery command %d W failed: %s (retry in 10 s)", sp, exc)
            self._cmd_retry_at = now + timedelta(seconds=10)
            return
        self._cmd_retry_at = None
        self._last_sent = sp
        self._last_sent_t = now
        self.setpoint = float(sp)

    # ------------------------------------------------------------- persistence
    def _maybe_save(self, now):
        if self._last_saved is None:
            self._last_saved = now
        elif (now - self._last_saved).total_seconds() >= self.cfg.save_every_s:
            self._last_saved = now
            self.save_state()

    def save_state(self):
        path = self.cfg.state_path
        if not path:
            return
        data = {
            "version": self.STATE_VERSION,
            "load_fc": self.load_fc.to_dict(),
            "price_fc": self.price_fc.to_dict(),
            "soc": self.soc,
            "totals": self.totals,
            "price_trained_until": (self._price_trained_until.isoformat()
                                    if self._price_trained_until else None),
        }
        tmp = path + ".tmp"
        try:
            with open(tmp, "w") as fh:
                json.dump(data, fh)
            os.replace(tmp, path)               # atomic: no corrupt file on power loss
        except OSError:
            log.exception("could not save state")

    def load_state(self):
        path = self.cfg.state_path
        if not path or not os.path.exists(path):
            return
        try:
            with open(path) as fh:
                data = json.load(fh)
            if data.get("version") != self.STATE_VERSION:
                return
            ok1 = self.load_fc.from_dict(data["load_fc"])
            ok2 = self.price_fc.from_dict(data["price_fc"])
            if ok1 and ok2:
                self.soc = data.get("soc", self.soc)
                self.totals = data.get("totals", self.totals)
                tu = data.get("price_trained_until")
                if tu:
                    from datetime import datetime
                    self._price_trained_until = datetime.fromisoformat(tu)
                log.info("restored learned state from %s", path)
        except Exception:
            log.exception("state file unreadable, starting fresh")
