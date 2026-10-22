"""Light-weight online forecasters (pure Python, no numpy).

Each forecaster keeps three simple "experts" for a quantity that repeats
daily/weekly (house load, electricity price):

  * week     : running mean per (weekday, time-of-day slot)
  * daytype  : running mean per (weekday / saturday / sunday, slot) - learns
               faster because it pools more samples
  * lag      : the last value seen in the same slot (yesterday's value)

The experts are blended with the Hedge algorithm (multiplicative weights,
with a ~3-day memory): after each observation, experts that predicted badly
lose weight. So the
forecaster keeps improving as it sees data, and automatically switches
strategy when habits change (holidays, new appliance, season...).
A short-lived bias term corrects the nearest slots when the current hour
runs above/below the usual pattern.

Memory: ~1000 floats per forecaster. CPU: microseconds per call.
"""
import math


def slot_of(dt, slot_minutes):
    return (dt.hour * 60 + dt.minute) // slot_minutes


def day_type(dt):
    wd = dt.weekday()
    return 0 if wd < 5 else (1 if wd == 5 else 2)


class _Grid:
    """rows x cols table of running means with per-cell sample counts."""

    def __init__(self, rows, cols):
        self.m = [[None] * cols for _ in range(rows)]
        self.n = [[0] * cols for _ in range(rows)]

    def get(self, r, c):
        return self.m[r][c]

    def add(self, r, c, y, alpha):
        old = self.m[r][c]
        n = self.n[r][c]
        if old is None:
            self.m[r][c] = y
        else:
            a = max(alpha, 1.0 / (n + 1))   # plain average at first, EWMA later
            self.m[r][c] = old + a * (y - old)
        self.n[r][c] = n + 1

    def dump(self):
        return {"m": [[None if v is None else round(v, 5) for v in row]
                      for row in self.m], "n": self.n}

    def load(self, d):
        self.m = d["m"]
        self.n = d["n"]


class HedgeForecaster:
    def __init__(self, slot_minutes, prior, alpha=0.15, eta=0.1,
                 memory_slots=288, min_weight=0.03, scale_floor=100.0,
                 use_bias=True, bias_tau=4.0, floor_value=None, loss_power=2.0,
                 loss_clip=4.0):
        self.sm = slot_minutes
        self.cols = 24 * 60 // slot_minutes
        self.prior = prior
        self.alpha = alpha
        self.eta = eta
        self.gamma = 1.0 - 1.0 / memory_slots   # forgetting factor of the losses
        self.loss_power = loss_power            # 2: squared error (targets the mean)
        self.loss_clip = loss_clip              # caps the influence of a single freak slot
        self.min_weight = min_weight
        self.scale_floor = scale_floor
        self.use_bias = use_bias
        self.bias_tau = bias_tau
        self.floor_value = floor_value

        self.week = _Grid(7, self.cols)
        self.dtype = _Grid(3, self.cols)
        self.lag = [None] * self.cols
        self.level = None               # slow EWMA of everything seen
        self.w = [1.0 / 3] * 3          # expert weights
        self.loss = [0.0] * 3           # discounted cumulative loss per expert
        self.bias = 0.0
        self.rel_err = None             # EWMA of relative error (diagnostic)
        self.n_updates = 0

    # -- prediction -------------------------------------------------------
    def _experts(self, dt):
        s = slot_of(dt, self.sm)
        fallback = self.level if self.level is not None else self.prior
        vals = (self.week.get(dt.weekday(), s),
                self.dtype.get(day_type(dt), s),
                self.lag[s])
        return [fallback if v is None else v for v in vals]

    def predict(self, dt, h=0):
        """Forecast for the slot containing `dt`, `h` slots ahead of now."""
        e = self._experts(dt)
        v = self.w[0] * e[0] + self.w[1] * e[1] + self.w[2] * e[2]
        if self.use_bias:
            v += self.bias * math.exp(-h / self.bias_tau)
        if self.floor_value is not None and v < self.floor_value:
            v = self.floor_value
        return v

    # -- learning -----------------------------------------------------------
    def update(self, dt, y):
        """Feed the observed slot value `y` for the slot starting at `dt`."""
        s = slot_of(dt, self.sm)
        preds = self._experts(dt)               # what each expert said
        mix = self.w[0] * preds[0] + self.w[1] * preds[1] + self.w[2] * preds[2]
        ref = max(abs(self.level) if self.level is not None else abs(y),
                  self.scale_floor)

        if self.n_updates > 0:                  # nothing to judge on day 0
            # Hedge with forgetting: weight ~ exp(-eta * recent cumulative loss).
            # Squared error is used on purpose: it rewards the expert that
            # tracks the MEAN (what the planner needs), whereas absolute /
            # tightly clipped errors favour 'typical value' predictors that
            # ignore bursty consumption (measured: worse forecasts).
            # Forgetting (~3 days) keeps weights stable against one-off events
            # yet lets them follow lasting changes in habits.
            for i in range(3):
                err = (abs(preds[i] - y) / ref) ** self.loss_power
                self.loss[i] = self.gamma * self.loss[i] + min(self.loss_clip, err)
            m = min(self.loss)
            raw = [math.exp(-self.eta * (x - m)) for x in self.loss]
            tot = sum(raw)
            w = [max(self.min_weight, r / tot) for r in raw]
            tot = sum(w)
            self.w = [x / tot for x in w]
            rel = abs(mix - y) / ref
            self.rel_err = rel if self.rel_err is None else 0.98 * self.rel_err + 0.02 * rel
            if self.use_bias:
                self.bias += 0.4 * ((y - mix) - self.bias)

        self.week.add(dt.weekday(), s, y, self.alpha)
        self.dtype.add(day_type(dt), s, y, self.alpha)
        self.lag[s] = y
        self.level = y if self.level is None else 0.97 * self.level + 0.03 * y
        self.n_updates += 1

    # -- persistence --------------------------------------------------------
    def to_dict(self):
        return {"cols": self.cols, "week": self.week.dump(),
                "dtype": self.dtype.dump(),
                "lag": [None if v is None else round(v, 5) for v in self.lag],
                "level": self.level, "w": self.w, "loss": self.loss, "bias": self.bias,
                "rel_err": self.rel_err, "n": self.n_updates}

    def from_dict(self, d):
        if d.get("cols") != self.cols:
            return False                        # slot size changed: start fresh
        self.week.load(d["week"])
        self.dtype.load(d["dtype"])
        self.lag = d["lag"]
        self.level = d["level"]
        self.w = d["w"]
        self.loss = d.get("loss", [0.0] * 3)
        self.bias = d["bias"]
        self.rel_err = d["rel_err"]
        self.n_updates = d["n"]
        return True
