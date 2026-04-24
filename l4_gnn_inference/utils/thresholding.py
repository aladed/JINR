"""Dynamic thresholding utilities for L4 inference."""

from __future__ import annotations

from collections import deque
from statistics import mean, stdev


class DynamicThreshold:
    """Compute anomaly threshold using a rolling Z-score estimate.

    The class tracks recent maximum anomaly scores from graph snapshots.
    Once enough history is collected, the threshold is estimated as:
    ``threshold = mu + z_multiplier * sigma``,
    where ``mu`` is the rolling mean and ``sigma`` is the sample standard
    deviation over the sliding window.
    """

    def __init__(
        self,
        window_size: int = 100,
        z_multiplier: float = 3.0,
        min_threshold: float = 0.65,
    ) -> None:
        """Initialize dynamic threshold parameters.

        Args:
            window_size: Number of recent snapshot scores in rolling history.
            z_multiplier: Z-score multiplier used in threshold formula.
            min_threshold: Lower bound used during warm-up and final clamping.

        Raises:
            ValueError: If input parameters are outside valid numeric ranges.
        """

        if window_size < 2:
            raise ValueError("window_size must be at least 2 for sample stdev.")
        if z_multiplier < 0.0:
            raise ValueError("z_multiplier must be non-negative.")
        if not 0.0 <= min_threshold <= 0.99:
            raise ValueError("min_threshold must be in range [0.0, 0.99].")

        self.window_size: int = window_size
        self.z_multiplier: float = z_multiplier
        self.min_threshold: float = min_threshold
        self.history: deque[float] = deque(maxlen=window_size)

    def update(self, max_score: float) -> None:
        """Append latest maximum anomaly score to rolling history.

        Args:
            max_score: Maximum anomaly score observed in current snapshot.
        """

        self.history.append(float(max_score))

    def get_threshold(self) -> float:
        """Return current dynamic threshold estimated from rolling history.

        Warm-up behavior:
            If history length is less than ``window_size``, returns
            ``min_threshold``.

        Standard behavior:
            Computes ``mu`` and ``sigma`` on the full window, then returns:
            ``min(max(mu + z_multiplier * sigma, min_threshold), 0.99)``.

        Returns:
            float: Threshold used to classify anomalous nodes.
        """

        if len(self.history) < self.window_size:
            return self.min_threshold

        mu: float = mean(self.history)
        sigma: float = stdev(self.history)
        calc_threshold: float = mu + self.z_multiplier * sigma
        final_threshold: float = max(calc_threshold, self.min_threshold)
        return min(final_threshold, 0.99)
