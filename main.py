"""Entry point: smart battery control on a Raspberry Pi.

Usage:
    export SOBRY_API_KEY=...                  # never hard-code it
    python3 main.py --probe [config.json]         # read-only: TIC, battery, prices
    python3 main.py --plan [config.json]          # read-only: the 24 h plan with live prices
    python3 main.py --battery-test [config.json]  # ~40 s, 100 W charge/discharge test
    python3 main.py --dry-run [config.json]       # plan + log, never touches the battery
    python3 main.py [config.json]                 # live
"""
import logging
import signal
from logging.handlers import RotatingFileHandler
import sys
import time
from datetime import datetime

import adapters
from config import Config
from controller import SmartController


class DryRunIO:
    """Wraps the real IO but only logs battery commands. The controller still
    learns consumption and prices, so a few days of dry run is a safe way to
    check forecasts and decisions (see the PLAN log lines) before going live."""
    dry_run = True

    def __init__(self, io):
        self._io = io

    def __getattr__(self, name):
        return getattr(self._io, name)

    def set_battery_power_w(self, watts):
        logging.getLogger("smart").debug("DRY RUN: battery command %d W", watts)


def setup_logging(cfg):
    """Console + rotating log file (cfg.log_path, None = console only)."""
    handlers = [logging.StreamHandler()]
    if cfg.log_path:
        try:
            handlers.append(RotatingFileHandler(
                cfg.log_path, maxBytes=cfg.log_max_bytes,
                backupCount=cfg.log_backups, encoding="utf-8"))
        except OSError as exc:        # unwritable folder: keep running on the console
            print("WARNING: cannot write the log file %s: %s" % (cfg.log_path, exc),
                  file=sys.stderr)
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(message)s")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    cfg = Config.from_file(args[0]) if args else Config()
    setup_logging(cfg)
    log = logging.getLogger("smart")

    if "--probe" in sys.argv:
        adapters.probe(cfg)
        return
    if "--plan" in sys.argv:
        adapters.show_plan(cfg)
        return
    if "--battery-test" in sys.argv:
        adapters.battery_test(cfg)
        return

    io = adapters.HardwareIO(cfg)
    io.startup_check()
    if "--dry-run" in sys.argv:
        io = DryRunIO(io)
        log.info("DRY RUN: the battery will not be commanded")
    ctl = SmartController(cfg, io)

    # systemd sends SIGTERM: leave the loop cleanly so the battery is zeroed
    # and the learned state is saved.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    log.info("smart battery controller started")
    try:
        while True:
            t0 = time.monotonic()
            try:
                ctl.step(datetime.now())
            except Exception:
                log.exception("control step failed; battery set to idle")
                try:
                    io.set_battery_power_w(0)
                except Exception:
                    pass
            time.sleep(max(0.0, cfg.control_period_s - (time.monotonic() - t0)))
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        ctl.shutdown()
        io.close()
        log.info("stopped")


if __name__ == "__main__":
    main()
