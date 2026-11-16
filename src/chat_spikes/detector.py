"""Option A: rule-based spike detection. No AI, no cost.

Messages are grouped into fixed windows (default 10 s). Each window gets a
score; a window is a spike when its score is well above the stream's recent
normal level. Neighbouring spike windows are merged into one moment.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter, deque
from dataclasses import dataclass, field

# Words and emoji that usually mean "something just happened".
REACTION_RE = re.compile(
    r"(\b(lol+|lmao+|lmfao|rofl|omg+|wtf|no way|w+|l|bruh+|clip it|clip that|"
    r"pog+|kekw|lul|holy|insane|crazy|wha+t{2,})\b|😂|🤣|💀|😱|🔥|‼|!!)",
    re.IGNORECASE,
)


@dataclass
class Rules:
    window: int = 10            # seconds per window
    threshold: float = 2.5      # spike = score >= threshold x normal level
    min_score: float = 8.0      # ignore tiny streams where 3 messages look like a spike
    history: int = 30           # windows used to measure the normal level (30 x 10 s = 5 min)
    per_user_cap: int = 2       # one person spamming counts at most this many times per window
    reaction_bonus: float = 0.5 # extra score for each message that looks like a reaction
    lead: int = 20              # clip starts this many seconds before the spike (chat reacts late)
    tail: int = 15              # clip ends this many seconds after the spike
    merge_gap: int = 30         # spikes closer than this become one moment
    ignore_users: set[str] = field(default_factory=set)


@dataclass
class Window:
    start: float
    raw: int = 0          # every message
    counted: int = 0      # after the rules
    users: int = 0        # different people talking
    reactions: int = 0
    score: float = 0.0
    baseline: float = 0.0
    is_spike: bool = False
    samples: list[str] = field(default_factory=list)
    _react_samples: list[str] = field(default_factory=list, repr=False)
    _other_samples: list[str] = field(default_factory=list, repr=False)

    @property
    def ratio(self) -> float:
        return self.score / self.baseline if self.baseline else 0.0


@dataclass
class Spike:
    start: float          # first spike window start
    end: float            # last spike window end
    peak: Window
    clip_start: float
    clip_end: float


def keep_message(author: str, text: str, rules: Rules) -> bool:
    """Option A filters: drop bots, commands and empty messages."""
    if not text or not text.strip():
        return False
    if text.lstrip().startswith("!"):
        return False
    if author.lower() in rules.ignore_users:
        return False
    return True


class Detector:
    """Feed messages in time order; get finished windows back.

    Works the same for a finished stream and a live one.
    """

    def __init__(self, rules: Rules):
        self.rules = rules
        self.current: Window | None = None
        self._per_user: Counter[str] = Counter()
        self._recent: deque[float] = deque(maxlen=rules.history)
        self.windows: list[Window] = []

    def _close(self) -> Window:
        w = self.current
        assert w is not None
        r = self.rules
        w.users = len(self._per_user)
        w.counted = sum(min(n, r.per_user_cap) for n in self._per_user.values())
        w.score = w.counted + r.reaction_bonus * w.reactions
        # Normal level = median of recent windows (median ignores earlier spikes).
        w.baseline = statistics.median(self._recent) if len(self._recent) >= 3 else 0.0
        floor = max(w.baseline, 1.0)
        w.is_spike = len(self._recent) >= 3 and w.score >= r.min_score and w.score >= r.threshold * floor
        self._recent.append(w.score)
        self.windows.append(w)
        self._per_user = Counter()
        self.current = None
        return w

    def add(self, t: float, author: str, text: str) -> list[Window]:
        """Add one message at time t (seconds). Returns windows that finished."""
        r = self.rules
        done: list[Window] = []
        start = (t // r.window) * r.window
        if self.current is None:
            self.current = Window(start=start)
        # Close the current window and any empty windows in between.
        while start >= self.current.start + r.window:
            nxt = self.current.start + r.window
            done.append(self._close())
            self.current = Window(start=nxt)
        w = self.current
        w.raw += 1
        if keep_message(author, text, r):
            self._per_user[author] += 1
            is_reaction = bool(REACTION_RE.search(text))
            if self._per_user[author] <= r.per_user_cap and is_reaction:
                w.reactions += 1
            if self._per_user[author] == 1:
                line = f"{author}: {text.strip()[:80]}"
                # Show what people reacted with: reactions go first, ordinary chat fills the rest.
                if is_reaction:
                    w._react_samples.append(line)
                else:
                    w._other_samples.append(line)
                w.samples = (w._react_samples + w._other_samples)[:5]
        return done

    def advance(self, now: float) -> list[Window]:
        """Live mode: close windows whose time has passed, even if chat is silent."""
        r = self.rules
        done: list[Window] = []
        if self.current is None:
            self.current = Window(start=(now // r.window) * r.window)
            return done
        while now >= self.current.start + r.window:
            nxt = self.current.start + r.window
            done.append(self._close())
            self.current = Window(start=nxt)
        return done

    def flush(self) -> list[Window]:
        return [self._close()] if self.current is not None else []


def group_spikes(windows: list[Window], rules: Rules) -> list[Spike]:
    """Merge nearby spike windows into moments and add clip times."""
    spikes: list[Spike] = []
    for w in windows:
        if not w.is_spike:
            continue
        end = w.start + rules.window
        if spikes and w.start - spikes[-1].end <= rules.merge_gap:
            s = spikes[-1]
            s.end = end
            if w.score > s.peak.score:
                s.peak = w
            s.clip_end = end + rules.tail
        else:
            spikes.append(Spike(start=w.start, end=end, peak=w,
                                clip_start=max(0.0, w.start - rules.lead),
                                clip_end=end + rules.tail))
    return spikes
