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
        self._rt_reason = ""              # "plan" / "price": why the battery is active
        self._fw_sum, self._fw_n, self._fw_at, self._fw_used = 0.0, 0, None, False
        self._grid_w = 0.0                # last raw meter reading (for the logs)
        self._mode = "idle"
        self._mode_logged_at = None
        self._status_at = None
        self._st = [0, 0.0, 0.0, 0.0]     # n, grid, battery cmd, load sums since last STATUS
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
        self._grid_w = grid_w
        self._apply(self._realtime_target(now, grid_w, load_w), now)
        self._log_activity(now, grid_w, load_w)
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
        provisional = getattr(self.io, "prices_provisional", False)  # "estimated" prices
        every = (cfg.price_refresh_full_min if full and not provisional
                 else cfg.price_refresh_min)
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
        if not vals:
            log.warning("price fetch returned no records")
            return False
        pv = [v for _, v in vals]
        kmin, kmax = min(k for k, _ in vals), max(k for k, _ in vals)
        n_slots = len({k for k, _ in vals})
        log.info("prices fetched: %d records, %d distinct slots (%s -> %s), "
                 "%.3f..%.3f EUR/kWh", len(vals), n_slots, kmin.strftime("%a %H:%M"),
                 kmax.strftime("%a %H:%M"), min(pv), max(pv))
        if n_slots < 0.5 * len(vals) or (len(pv) > 4 and max(pv) - min(pv) < 0.02):
            log.warning("prices look wrong (duplicate timestamps or no spread at all): "
                        "the planner will not charge. Run `python3 main.py --probe` "
                        "and check the sample it prints.")
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
        known = sum(1 for t in slots if t in self.known_prices)
        log.info("PLAN %s soc %.0f%% -> %s %.0f W | prices %.3f..%.3f (%d/%d published) "
                 "| horizon cost %.2f EUR (no battery %.2f) | %.0f ms",
                 slot_start.strftime("%a %H:%M"), self.soc * 100,
                 "charge" if s0.d > 0 else "discharge" if s0.d < 0 else "idle",
                 abs(s0.battery_w), min(prices), max(prices), known, N,
                 self.plan.cost_eur, self.plan.baseline_eur, self.plan.solve_ms)

    # --------------------------------------------------------------- real time
    def _worth_discharging(self):
        """Price now, net of losses and wear, vs. the value of keeping the energy.

        The value of stored energy comes from the plan's cost-to-go at the
        current SoC: it rises as the battery empties, so this switches off by
        itself before the energy needed for better slots is touched.
        """
        cfg = self.cfg
        if not cfg.value_based_discharge:
            return False
        lam = self.opt.marginal_value(self.plan, self.soc)
        if lam is None:
            return False
        gain = self.plan.steps[0].price * cfg.eff_discharge - cfg.cycle_cost_eur_kwh
        return gain > lam + cfg.value_margin_eur_kwh

    def _follow(self, now, sp, grid_w):
        """Adjust the discharge command from the meter, with smoothing.

        The meter readings are averaged over `follow_interval_s` and the command
        is changed only once per interval, and only if the average import is
        off target by more than `deadband_w`. Acting on every raw reading made
        the command chase every appliance cycle and the battery's own response
        lag; one decision per interval also lets the previous command take effect
        before the next one is judged.
        """
        cfg = self.cfg
        self._fw_used = True
        self._fw_sum += grid_w
        self._fw_n += 1
        if (self._fw_at is not None and
                (now - self._fw_at).total_seconds() < cfg.follow_interval_s - 1e-6):
            return sp                                        # hold the command
        g = self._fw_sum / self._fw_n
        self._fw_sum, self._fw_n, self._fw_at = 0.0, 0, now
        if not self._signed() and g < 1.0:                   # meter at 0: over-covering
            return sp - max(25.0, 0.5 * sp) if sp > 0.0 else sp
        err = g - cfg.target_import_w
        if abs(err) < cfg.deadband_w:
            return sp
        # from idle the meter shows the whole load: go straight to it
        return sp + (1.0 if sp <= 0.0 else cfg.follow_gain) * err

    def _realtime_target(self, now, grid_w, load_w):
        self._fw_used = False
        target = self._target(now, grid_w, load_w)
        if not self._fw_used:                  # not following the load: start afresh
            self._fw_sum, self._fw_n, self._fw_at = 0.0, 0, None
        return target

    def _target(self, now, grid_w, load_w):
        cfg = self.cfg
        if self.plan is None:
            return 0.0
        s0 = self.plan.steps[0]
        # Beyond the plan: if the price now (net of losses and wear) beats what
        # the same energy is worth later, cover as much of the real consumption
        # as possible, whatever the forecast said. Never in a charging slot.
        worth = s0.d <= 0 and self._worth_discharging()
        if s0.d == 0 and not worth:
            return 0.0

        if s0.d > 0:                                          # ---- charge
            self._rt_reason = "plan"
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

        # ---- discharge: follow the load. Inside the plan's energy budget, or
        # without budget while discharging is worth more than keeping the energy.
        if self.soc <= cfg.soc_min:
            return 0.0
        budget = s0.energy_ac_wh * cfg.discharge_budget_factor - self._slot_discharged_wh
        in_plan = s0.d < 0 and budget > 0.0
        if not in_plan and not worth:
            return 0.0
        self._rt_reason = "plan" if in_plan else "price"
        sp = self._follow(now, self.setpoint if self.setpoint > 0 else 0.0, grid_w)
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
        if cfg.log_commands and sp != self._last_sent:
            log.info("CMD%s %+d W (was %+d W) | %s | grid %.0f W load %.0f W soc %.0f%% "
                     "price %.3f", " [dry-run]" if getattr(self.io, "dry_run", False) else "",
                     sp, self._last_sent or 0, self._rt_reason or "-", self._grid_w,
                     self._load_w, self.soc * 100, self._slot_price)
        self._last_sent = sp
        self._last_sent_t = now
        self.setpoint = float(sp)

    # ----------------------------------------------------------------- activity log
    def _log_activity(self, now, grid_w, load_w):
        """ACTION on every change idle/charge/discharge, STATUS once per minute."""
        cfg = self.cfg
        st = self._st
        st[0] += 1
        st[1] += grid_w
        st[2] += self.setpoint
        st[3] += load_w
        dry = " [dry-run]" if getattr(self.io, "dry_run", False) else ""
        s0 = self.plan.steps[0] if self.plan else None

        mode = "charge" if self.setpoint < 0 else "discharge" if self.setpoint > 0 else "idle"
        if mode != self._mode and (self._mode_logged_at is None or
                                   (now - self._mode_logged_at).total_seconds() >= 15.0):
            log.info("ACTION%s %s -> %s %.0f W (%s) | price %.3f soc %.0f%% load %.0f W "
                     "grid %.0f W", dry, self._mode, mode, abs(self.setpoint),
                     self._rt_reason or "-", self._slot_price, self.soc * 100,
                     load_w, grid_w)
            self._mode, self._mode_logged_at = mode, now

        if not cfg.log_status_s:
            return
        if self._status_at is None:
            self._status_at = now
        elif (now - self._status_at).total_seconds() >= cfg.log_status_s and st[0]:
            t = self.totals
            plan = ("charge" if s0.d > 0 else "discharge" if s0.d < 0 else "idle") if s0 else "-"
            log.info("STATUS%s plan=%s | battery cmd %+.0f W | grid %.0f W | load %.0f W | "
                     "price %.3f | soc %.0f%% | saved %.2f EUR since start", dry, plan,
                     st[2] / st[0], st[1] / st[0], st[3] / st[0], self._slot_price,
                     self.soc * 100, t["baseline"] - t["cost"] - t["wear"])
            self._status_at = now
            self._st = [0, 0.0, 0.0, 0.0]

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
