"""Bounded past-only validation of the actual v4/analogue blend, 24–48h only."""
from datetime import timedelta
from statistics import mean, median

from .model import STEP, _quarter_start

CONFIGS = ((7, False), (14, False), (28, False), (14, True), (28, True))
WEIGHTS = (.25, .5, .75, 1.)


class AnalogIndex:
    """Cache levels per cutoff, rather than re-scan all prices per candidate."""
    def __init__(self, known, local_zone):
        self.rows = list(known.items())
        self.zone = local_zone
        self.slots, self.levels, self.values = {}, {}, {}
        for stamp, row in self.rows:
            local = stamp.astimezone(local_zone)
            self.slots.setdefault((local.hour, local.minute, local.weekday() >= 5), []).append((stamp, row))

    def price(self, target, cutoff, days, level):
        local = target.astimezone(self.zone)
        slot = (local.hour, local.minute, local.weekday() >= 5)
        key = (cutoff, days, level, slot)
        if key in self.values: return self.values[key]
        candidates = [r['price'] for t, r in self.slots.get(slot, [])
                      if cutoff - timedelta(days=days) <= t < cutoff and r['available_at'] <= cutoff]
        if len(candidates) < 2:
            self.values[key] = None
            return None
        value = median(candidates)
        if level:
            level_key = (cutoff, days)
            if level_key not in self.levels:
                recent = [r['price'] for t, r in self.rows if cutoff-timedelta(days=2) <= t < cutoff and r['available_at'] <= cutoff]
                prior = [r['price'] for t, r in self.rows if cutoff-timedelta(days=days) <= t < cutoff and r['available_at'] <= cutoff]
                self.levels[level_key] = median(recent)-median(prior) if len(recent) >= 96 and len(prior) >= 192 else 0.
            value += self.levels[level_key]
        self.values[key] = value
        return value


def validated_blend(known, issued_at, price_scale, base, local_zone):
    """Return selected configuration and its index, or None for cold start.

    Seven earlier daily origins; hourly validation samples in the 24–48h band.
    Every fold rebuilds its baseline from prices ended and available at its
    origin. No current weather is reused as a historical forecast. Candidates
    share identical validation targets, including the unmodified v4 baseline.
    """
    if not known or min(known) > issued_at-timedelta(days=14): return None
    index = AnalogIndex(known, local_zone)
    candidates = [(0, False, 0.)] + [(d, level, w) for d, level in CONFIGS for w in WEIGHTS]
    losses = {config: [] for config in candidates}
    midnight = _quarter_start(issued_at).replace(hour=0, minute=0, second=0, microsecond=0)
    for offset in range(7, 0, -1):
        cutoff = midnight-timedelta(days=offset)
        history = [{'start_utc': t, 'end_utc': r['end'], 'price': r['price'],
                    'published_at': r['published_at'], 'source': r.get('source')}
                   for t, r in known.items() if r['end'] <= cutoff and r['available_at'] <= cutoff]
        if len(history) < 192: continue
        run = base(history, issued_at=cutoff, weather=None, max_points=192, price_scale=price_scale)
        for point in run.points:
            target = point.start_utc
            actual = known.get(target)
            if (target.minute != 0 or not 24 <= (target-cutoff).total_seconds()/3600 < 48
                    or actual is None or actual['end'] > issued_at or actual['available_at'] > issued_at):
                continue
            analogues = {(d, level): index.price(target, cutoff, d, level) for d, level in CONFIGS}
            if any(value is None for value in analogues.values()): continue
            for config in candidates:
                days, level, weight = config
                prediction = point.price if not weight else (1-weight)*point.price+weight*analogues[days, level]
                losses[config].append(abs(prediction-actual['price']))
    # Never choose candidates scored on different or tiny target sets.
    if len(losses[candidates[0]]) < 48: return None
    chosen = min(candidates, key=lambda config: mean(losses[config]))
    return chosen, index, len(losses[chosen])
