"""Configuration for the smart battery controller.

All tunables live here. Defaults are conservative; adjust the battery and
grid sections to match your hardware before running on real equipment.

Conventions used everywhere in the code base
--------------------------------------------
* Battery power, AC side:  +W = discharging into the house, -W = charging.
* Grid power:               +W = importing from the grid.
* Prices:                   EUR per kWh, all-in (what you actually pay).
* Time:                     naive local time (set the Pi's timezone to
                            Europe/Paris with `sudo raspi-config`).
"""
import json
from dataclasses import dataclass, field, fields


@dataclass
class Config:
    # ---- planning grid ---------------------------------------------------
    slot_minutes: int = 15          # must divide 60 (price granularity)
    horizon_slots: int = 96         # rolling horizon: 96 x 15 min = 24 h
    soc_levels: int = 41            # SoC discretisation of the optimiser

    # ---- battery (usable figures, AC side) ----------------------------------
    # PLACEHOLDERS: capacity and power limits are NOT in your repo. Put the
    # usable kWh of your pack(s) and the real AC limits of your device here.
    capacity_kwh: float = 1.92
    soc_min: float = 0.10           # = battery_limit_low  in your battery.py
    soc_max: float = 0.90           # = battery_limit_high in your battery.py
    soc_initial: float = 0.50       # used only if no SoC reading / no saved state
    max_charge_w: float = 800.0
    max_discharge_w: float = 800.0
    eff_charge: float = 0.95        # AC -> stored energy
    eff_discharge: float = 0.95     # stored energy -> AC
    cycle_cost_eur_kwh: float = 0.02  # wear cost per kWh moved through the cells

    # ---- Zendure SolarFlow, local API (values from your battery.py) -----------
    zendure_url: str = "http://192.168.1.144"
    zendure_sn: str = "EOC1NLNAN492379"   # or set the ZENDURE_SN environment variable
    zendure_ac_mode_charge: int = 1       # ASSUMED: acMode 1 = AC input (charge), the
    zendure_ac_mode_discharge: int = 2    # Zendure docs do not define it. Check with
                                          # `python3 main.py --battery-test`.
    zendure_smart_mode: int = 1     # 1 = parameters are not written to the device flash
                                    # (docs: recommended for frequent changes); None = don't send
    zendure_timeout_s: float = 3.0

    # ---- Linky TIC, standard mode (from your gateway.py) -----------------------
    tic_port: str = "/dev/serial0"  # gateway.py has "COM5" (Windows); on the Pi use
                                    # /dev/serial0, /dev/ttyAMA0 or /dev/ttyUSB0
    tic_baud: int = 9600
    tic_max_age_s: float = 10.0     # older SINSTS = "no data" -> fail-safe

    # ---- grid ----------------------------------------------------------------
    max_grid_w: float = 5000.0      # stay below subscribed power (e.g. 6 kVA)
    grid_margin_w: float = 300.0    # safety margin kept under max_grid_w
    grid_signed: bool = False       # True if export can be read as negative W

    # ---- prices ----------------------------------------------------------------
    sobry_url: str = "https://api.sobry.co/v2/user/daily-prices"
    sobry_params: dict = field(default_factory=lambda: {
        "taxMode": "ttc", "granularity": "15m"})   # ttc = tax included (as in prices.py)
    timezone: str = "Europe/Paris"  # to convert timestamps that carry a UTC offset
    price_scale: float = 1.0        # multiply raw API value to get EUR/kWh
                                    # (unit of the Sobry v2 response not verified: see --probe)
    price_adder_eur_kwh: float = 0.0  # constant add-on (taxes, grid fees...)
    price_prior_eur_kwh: float = 0.20  # used until prices have been learned
    price_refresh_min: int = 60     # re-fetch while the horizon isn't covered
    price_refresh_full_min: int = 360  # re-fetch when it is
    terminal_value_factor: float = 0.9  # value of energy left at horizon end,
                                        # as a fraction of the mean price

    # ---- forecasting / learning ---------------------------------------------
    load_prior_w: float = 400.0     # used until consumption has been learned
    load_alpha: float = 0.15        # EWMA rate of the load profiles
    price_alpha: float = 0.30       # EWMA rate of the price profiles
    hedge_eta: float = 0.1          # learning rate of the expert weights
    hedge_memory_slots: int = 288   # loss memory of the blend (~3 days of 15-min slots)

    # ---- real-time control ------------------------------------------------------
    control_period_s: float = 2.0   # the battery's HTTP API reacts in seconds: a 1 s
                                    # loop would only chase its own commands
    follow_gain: float = 0.4        # fraction of the grid-import error corrected per tick
                                    # (tune on the real device: lower if it oscillates)
    target_import_w: float = 30.0   # keep this much import so we never export
    min_change_w: float = 10.0      # don't re-send the setpoint for smaller changes
    command_refresh_s: float = 30.0  # re-send the setpoint at least this often
    discharge_budget_factor: float = 1.15  # allow 15 % above planned energy / slot
    charge_budget_factor: float = 1.02
    stale_s: float = 15.0           # no grid reading for this long -> battery idle
    soc_poll_s: float = 10.0        # how often to ask the battery for its SoC

    # ---- persistence ------------------------------------------------------------
    state_path: str = "state.json"  # None disables persistence
    save_every_s: float = 1800.0    # SD-card friendly

    @classmethod
    def from_file(cls, path):
        """Load overrides from a JSON file; unknown keys are rejected."""
        with open(path) as fh:
            data = json.load(fh)
        known = {f.name for f in fields(cls)}
        bad = set(data) - known
        if bad:
            raise ValueError("Unknown config keys: %s" % ", ".join(sorted(bad)))
        return cls(**data)
