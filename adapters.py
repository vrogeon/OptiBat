"""Hardware / API glue, written against the real OptiBat modules.

Why this file does not simply call your prototype modules:

* TicReader      Linky TIC reader (standard mode). gateway.decode_teleinfo()
                 opens the serial port on every call and loops until a SINSTS
                 line arrives: if the Linky goes silent it blocks forever,
                 which would freeze the control loop and disable the
                 "no data -> battery idle" fail-safe. Here a background thread
                 keeps the port open and the controller only asks for the
                 latest value and its age.
* ZendureBattery Same local HTTP API, IP and serial number as battery.py.
                 battery.set_power() only writes outputLimit (discharge);
                 charging from the grid also needs acMode + inputLimit.
* SobryPrices    Same endpoint as prices.py, but for today AND tomorrow
                 (`day` parameter), with the key taken from the environment.

NOT verified against real hardware (no access from here), see README:
  - the meaning of acMode 1 / 2 (the Zendure docs do not define them),
  - the JSON layout of GET /properties/report,
  - the JSON layout of the Sobry v2 response (tolerant parser below),
  - the TIC checksum on a real Linky (both known variants are accepted).
`python3 main.py --probe` prints what each part really returns.
"""
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

log = logging.getLogger("smart.io")


# =============================================================== Linky TIC
def parse_tic_line(raw):
    """One TIC standard-mode line (bytes) -> (label, [data fields]) or None.

    Layout: LABEL <HT> DATA [<HT> DATA ...] <HT> CHECKSUM. The checksum is
    (sum of the characters & 0x3F) + 0x20; depending on the document it covers
    the separator before the checksum or not, so both are accepted.
    """
    try:
        text = raw.strip(b"\x02\x03\r\n").decode("ascii")
    except UnicodeDecodeError:
        return None
    sep = "\t" if "\t" in text else " "
    fields = text.split(sep)
    if len(fields) < 3 or len(fields[-1]) != 1:
        return None
    body = sep.join(fields[:-1])
    s = sum(map(ord, body))
    chk = fields[-1]
    if chk not in (chr((s & 0x3F) + 0x20), chr(((s + ord(sep)) & 0x3F) + 0x20)):
        return None
    return fields[0], fields[1:-1]


class TicReader:
    """Keeps the latest SINSTS (withdrawn VA) and SINSTI (injected VA).

    Owns a thread instead of subclassing Thread: Thread has private attributes
    (_stop, _handle...) that a subclass can silently shadow and break.
    """

    def __init__(self, port, baud=9600, max_va=60000):
        self.port, self.baud, self.max_va = port, baud, max_va
        self._lock = threading.Lock()
        self._halt = threading.Event()
        self._vals = {"SINSTS": (None, 0.0), "SINSTI": (None, 0.0)}
        self._thread = threading.Thread(target=self._run, daemon=True, name="tic-reader")
        self.frames_ok = 0
        self.frames_bad = 0

    def start(self):
        self._thread.start()

    def _run(self):
        try:
            import serial
        except ImportError:
            log.error("pyserial missing: sudo apt install python3-serial")
            return
        while not self._halt.is_set():
            try:
                with serial.Serial(self.port, self.baud, bytesize=7,
                                   parity=serial.PARITY_EVEN, stopbits=1,
                                   timeout=2) as ser:
                    log.info("TIC port %s open", self.port)
                    while not self._halt.is_set():
                        self._on_line(ser.readline())
            except Exception as exc:           # SerialException, OSError...
                log.warning("TIC port error: %s (retry in 3 s)", exc)
                self._halt.wait(3.0)

    def _on_line(self, raw):
        if not raw:
            return
        parsed = parse_tic_line(raw)
        if parsed is None:
            self.frames_bad += 1
            return
        self.frames_ok += 1
        label, data = parsed
        if label in self._vals and data:
            try:
                v = int(data[-1])
            except ValueError:
                return
            if 0 <= v <= self.max_va:
                with self._lock:
                    self._vals[label] = (v, time.monotonic())

    def latest(self, max_age_s):
        """(sinsts, sinsti_or_None), or None if SINSTS is missing / too old."""
        now = time.monotonic()
        with self._lock:
            sinsts, t1 = self._vals["SINSTS"]
            sinsti, t2 = self._vals["SINSTI"]
        if sinsts is None or now - t1 > max_age_s:
            return None
        return sinsts, (sinsti if sinsti is not None and now - t2 <= max_age_s else None)

    def close(self):
        self._halt.set()


# ==================================================================== Zendure
REPORT_KEYS = ("electricLevel", "socSet", "minSoc", "acMode", "inputLimit",
               "outputLimit", "smartMode", "gridInputPower", "outputHomePower",
               "packInputPower", "outputPackPower", "solarInputPower")


def find_key(obj, key):
    """First value stored under `key` anywhere in a nested dict/list."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = find_key(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_key(v, key)
            if r is not None:
                return r
    return None


class ZendureBattery:
    """Zendure SolarFlow local API: GET /properties/report, POST /properties/write.

    set_power(w): +W discharge to the house, -W charge from the grid, 0 idle.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.base = cfg.zendure_url.rstrip("/")
        self.sn = os.environ.get("ZENDURE_SN") or cfg.zendure_sn
        self.timeout = cfg.zendure_timeout_s

    def _call(self, path, body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.base + path, data=data,
            headers={"Content-Type": "application/json"} if data else {},
            method="POST" if data else "GET")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return resp.read().decode("utf-8", "replace")

    def report(self):
        return json.loads(self._call("/properties/report"))

    def get_soc(self):
        """0..1, or None if the report has no electricLevel."""
        v = find_key(self.report(), "electricLevel")
        return None if v is None else float(v) / 100.0

    def set_power(self, watts):
        c = self.cfg
        w = int(round(watts))
        if w > 0:
            props = {"acMode": c.zendure_ac_mode_discharge,
                     "outputLimit": min(w, int(c.max_discharge_w))}
        elif w < 0:
            props = {"acMode": c.zendure_ac_mode_charge,
                     "inputLimit": min(-w, int(c.max_charge_w))}
        else:
            props = {"outputLimit": 0, "inputLimit": 0}
        if c.zendure_smart_mode is not None:
            # 1 = do not write to the device flash (many writes per day)
            props["smartMode"] = c.zendure_smart_mode
        self._call("/properties/write", {"sn": self.sn, "properties": props})

    def check_limits(self):
        """Warn if the device's own SoC limits contradict our config."""
        try:
            rep = self.report()
        except Exception as exc:
            log.warning("battery report unavailable: %s", exc)
            return
        soc_set, min_soc = find_key(rep, "socSet"), find_key(rep, "minSoc")
        if soc_set is not None and float(soc_set) < self.cfg.soc_max * 100:
            log.warning("device socSet=%s%% is below soc_max=%.0f%%: the plan "
                        "will assume energy the battery will not accept",
                        soc_set, self.cfg.soc_max * 100)
        if min_soc is not None and float(min_soc) > self.cfg.soc_min * 100:
            log.warning("device minSoc=%s%% is above soc_min=%.0f%%: the plan "
                        "will assume energy the battery will not deliver",
                        min_soc, self.cfg.soc_min * 100)


# ===================================================================== prices
_TIME_KEYS = ("start", "startDate", "start_date", "from", "datetime",
              "date", "timestamp", "time", "startsAt")
_PRICE_KEYS = ("price", "value", "priceKwh", "price_kwh", "eurPerKwh",
               "priceEurKwh")


def _zone(name):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return None


def _first_key(rec, keys):
    for k in keys:
        if k in rec:
            return k
    return None


def _parse_time(v, tz=None):
    """ISO string or epoch (s / ms) -> naive local datetime."""
    if isinstance(v, (int, float)):
        if v > 1e11:                      # milliseconds
            v /= 1000.0
        return datetime.fromtimestamp(v, tz).replace(tzinfo=None)
    s = str(v).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is not None:
        dt = dt.astimezone(tz).replace(tzinfo=None)   # tz=None: system zone
    return dt


def _find_records(obj):
    if isinstance(obj, list):
        if (obj and isinstance(obj[0], dict)
                and _first_key(obj[0], _TIME_KEYS) and _first_key(obj[0], _PRICE_KEYS)):
            return obj
        for item in obj:
            r = _find_records(item)
            if r:
                return r
    elif isinstance(obj, dict):
        for v in obj.values():
            r = _find_records(v)
            if r:
                return r
    return None


def parse_price_records(data, tz=None):
    """UNVERIFIED parser for the Sobry v2 JSON -> [(naive local datetime, price)].

    Finds the first list of objects that carry a time field and a price field.
    If your response looks different (run `main.py --probe` and look at the
    sample it prints), fix this one function.
    """
    recs = _find_records(data)
    if not recs:
        raise ValueError("no price records found in API response")
    tk = _first_key(recs[0], _TIME_KEYS)
    pk = _first_key(recs[0], _PRICE_KEYS)
    out = [(_parse_time(r[tk], tz), float(r[pk])) for r in recs]
    out.sort()
    return out


class SobryPrices:
    def __init__(self, cfg):
        self.cfg = cfg
        self.tz = _zone(cfg.timezone)
        self._key = os.environ.get("SOBRY_API_KEY")
        if not self._key:
            raise RuntimeError("set the SOBRY_API_KEY environment variable "
                               "(and revoke the key that is in the public repo)")

    def fetch_raw(self, day):
        params = dict(self.cfg.sobry_params)
        params["day"] = day.isoformat()
        req = urllib.request.Request(
            "%s?%s" % (self.cfg.sobry_url, urllib.parse.urlencode(params)),
            headers={"Authorization": "Bearer " + self._key})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def curve(self):
        """Raw [(slot start, price)] for today and, once published, tomorrow."""
        today = datetime.now(self.tz).date() if self.tz else datetime.now().date()
        out = parse_price_records(self.fetch_raw(today), self.tz)  # failure -> raises
        try:
            out += parse_price_records(self.fetch_raw(today + timedelta(days=1)), self.tz)
        except (urllib.error.HTTPError, ValueError) as exc:
            log.info("tomorrow's prices not available yet (%s)", exc)
        out.sort()
        return out


# ================================================================== HardwareIO
class HardwareIO:
    """The small interface documented in controller.py."""
    grid_signed = False

    def __init__(self, cfg):
        self.cfg = cfg
        self.battery = ZendureBattery(cfg)
        self.prices = SobryPrices(cfg)
        self.tic = TicReader(cfg.tic_port, cfg.tic_baud)
        self.tic.start()

    def startup_check(self):
        self.battery.check_limits()

    def read_grid_w(self):
        """Net grid power in W (import > 0); None if the Linky is silent."""
        r = self.tic.latest(self.cfg.tic_max_age_s)
        if r is None:
            return None
        sinsts, sinsti = r
        if sinsti is not None:          # injection measured: signed value
            self.grid_signed = True
            return float(sinsts - sinsti)
        return float(sinsts)

    def get_soc(self):
        return self.battery.get_soc()

    def set_battery_power_w(self, watts):
        self.battery.set_power(watts)

    def get_price_curve(self):
        return self.prices.curve()

    def close(self):
        self.tic.close()


# ============================================================ diagnostics (CLI)
def _show_report(rep):
    for k in REPORT_KEYS:
        v = find_key(rep, k)
        if v is not None:
            print("   %-16s %s" % (k, v))


def probe(cfg):
    """Read-only checks: TIC, battery report, price response."""
    print("== Linky TIC on %s (waiting up to 15 s)" % cfg.tic_port)
    tic = TicReader(cfg.tic_port, cfg.tic_baud)
    tic.start()
    t0 = time.monotonic()
    r = None
    while r is None and time.monotonic() - t0 < 15:
        r = tic.latest(5.0)
        time.sleep(0.5)
    print("   SINSTS / SINSTI:", r, " frames ok/bad: %d/%d" % (tic.frames_ok, tic.frames_bad))
    if r is None:
        print("   -> nothing valid received: check port, 9600 baud 7E1, standard mode")
    tic.close()

    print("== Battery %s (GET /properties/report)" % cfg.zendure_url)
    try:
        _show_report(ZendureBattery(cfg).report())
    except Exception as exc:
        print("   failed:", exc)

    print("== Sobry prices (today)")
    try:
        sp = SobryPrices(cfg)
        data = sp.fetch_raw(datetime.now(sp.tz).date() if sp.tz else datetime.now().date())
        print("   raw response, first 1200 chars (the key is never printed):")
        print("  ", json.dumps(data)[:1200])
        recs = parse_price_records(data, sp.tz)
        print("   parsed %d slots, first %s, last %s" % (len(recs), recs[0], recs[-1]))
        vals = [p * cfg.price_scale + cfg.price_adder_eur_kwh for _, p in recs]
        print("   after price_scale/adder: min %.4f max %.4f (must be EUR/kWh)" % (min(vals), max(vals)))
    except Exception as exc:
        print("   failed:", exc)


def battery_test(cfg):
    """~40 s hardware test: charge 100 W, then discharge 100 W, then idle.

    Lets you check that acMode 1/2 mean what config.py assumes: during the
    'charge' step gridInputPower / outputPackPower should rise, during the
    'discharge' step outputHomePower should rise.
    """
    print("This sends REAL commands to the battery at %s: 100 W charge for 15 s, "
          "then 100 W discharge for 15 s, then stop." % cfg.zendure_url)
    if input("Type yes to continue: ").strip().lower() != "yes":
        print("aborted")
        return
    bat = ZendureBattery(cfg)
    try:
        for label, w in (("charge", -100), ("discharge", 100)):
            bat.set_power(w)
            print("== %s %d W, waiting 15 s" % (label, abs(w)))
            time.sleep(15)
            _show_report(bat.report())
    finally:
        bat.set_power(0)
        print("== stopped (outputLimit = inputLimit = 0)")
