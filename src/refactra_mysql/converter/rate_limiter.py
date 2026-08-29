"""
Rate Limiter — Manages API request pacing to stay within provider limits.

Implements a token-bucket style rate limiter that tracks:
- Requests per minute (RPM)
- Input tokens per minute
- Output tokens per minute

Automatically waits when approaching limits to prevent 429 errors.

Usage:
    limiter = RateLimiter(rpm=5, input_tpm=10000, output_tpm=4000)

    # Before each API call:
    limiter.wait_if_needed(estimated_input_tokens=3500)

    # After each API call:
    limiter.record_usage(input_tokens=3200, output_tokens=400)
"""
import time
import threading
from collections import deque
from dataclasses import dataclass

from refactra_mysql.config import (
    RATE_LIMIT_INPUT_TPM,
    RATE_LIMIT_OUTPUT_TPM,
    RATE_LIMIT_RPM,
    setup_logging,
)

logger = setup_logging("rate_limiter")


@dataclass
class UsageRecord:
    """A single API call usage record."""
    timestamp: float
    input_tokens: int
    output_tokens: int


class RateLimiter:
    """
    Token-bucket rate limiter for AI API calls.

    Tracks requests and token usage over a sliding 60-second window
    and blocks when limits would be exceeded.

    Args:
        rpm: Maximum requests per minute.
        input_tpm: Maximum input tokens per minute.
        output_tpm: Maximum output tokens per minute.
        safety_margin: Fraction of limit to use as buffer (0.0 to 1.0).
                       Default 0.2 means we target 80% of the limit.
    """

    def __init__(
        self,
        rpm: int = RATE_LIMIT_RPM,
        input_tpm: int = RATE_LIMIT_INPUT_TPM,
        output_tpm: int = RATE_LIMIT_OUTPUT_TPM,
        safety_margin: float = 0.2,
    ):
        self.rpm = rpm
        self.input_tpm = input_tpm
        self.output_tpm = output_tpm
        self.safety_margin = safety_margin

        # Effective limits with safety margin
        self._effective_rpm = max(1, int(rpm * (1 - safety_margin)))
        self._effective_input_tpm = max(1, int(input_tpm * (1 - safety_margin)))
        self._effective_output_tpm = max(1, int(output_tpm * (1 - safety_margin)))

        # Sliding window of recent usage
        self._history: deque[UsageRecord] = deque()
        self._lock = threading.RLock()  # RLock: reentrant, survives Ctrl+C

        # Statistics
        self.total_requests = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_wait_seconds = 0.0
        self.total_retries = 0

        logger.info(
            "Rate limiter initialized: %d RPM, %dK input TPM, %dK output TPM (%.0f%% safety margin)",
            rpm, input_tpm // 1000, output_tpm // 1000, safety_margin * 100,
        )

    def _cleanup_old_records(self) -> None:
        """Remove records older than 60 seconds from the sliding window."""
        cutoff = time.monotonic() - 60.0
        while self._history and self._history[0].timestamp < cutoff:
            self._history.popleft()

    def _current_usage(self) -> tuple[int, int, int]:
        """
        Get current usage in the sliding 60-second window.

        Returns:
            Tuple of (requests_count, input_tokens, output_tokens).
        """
        self._cleanup_old_records()
        requests = len(self._history)
        input_tokens = sum(r.input_tokens for r in self._history)
        output_tokens = sum(r.output_tokens for r in self._history)
        return requests, input_tokens, output_tokens

    def wait_if_needed(self, estimated_input_tokens: int = 0) -> float:
        """
        Block until it's safe to make another API call.

        Checks current usage against limits and sleeps if necessary.

        Args:
            estimated_input_tokens: Expected input tokens for the next call.

        Returns:
            Number of seconds waited (0.0 if no wait was needed).

        Raises:
            RuntimeError: If max wait time (300s) is exceeded.
        """
        MAX_WAIT_PER_CALL = 300.0  # 5 minutes max wait per single call
        STALL_THRESHOLD = 3  # If wait_time stays same for N iterations, force expire

        waited = 0.0
        stall_count = 0
        last_wait_time = None

        with self._lock:
            while True:
                requests, input_tokens, output_tokens = self._current_usage()

                # Check all three limits
                rpm_ok = requests < self._effective_rpm
                input_ok = (input_tokens + estimated_input_tokens) < self._effective_input_tpm
                output_ok = output_tokens < self._effective_output_tpm

                if rpm_ok and input_ok and output_ok:
                    break

                # Guard: prevent infinite loop
                if waited >= MAX_WAIT_PER_CALL:
                    logger.warning(
                        "Max wait time (%.0fs) exceeded, proceeding anyway",
                        MAX_WAIT_PER_CALL,
                    )
                    break

                # Calculate how long to wait
                if not rpm_ok:
                    reason = f"RPM limit ({requests}/{self._effective_rpm})"
                elif not input_ok:
                    reason = f"Input TPM ({input_tokens + estimated_input_tokens}/{self._effective_input_tpm})"
                else:
                    reason = f"Output TPM ({output_tokens}/{self._effective_output_tpm})"

                # Wait until oldest record expires from the window
                if self._history:
                    oldest = self._history[0].timestamp
                    wait_time = (oldest + 61.0) - time.monotonic()
                    if wait_time <= 0:
                        # Record should already be expired — force cleanup & retry
                        self._cleanup_old_records()
                        continue
                    wait_time = max(1.0, wait_time)
                else:
                    # No history but still over limit — should not happen
                    logger.warning("Rate limiter: no history but limits exceeded, resetting")
                    break

                # Stall detection: if wait_time is always ~5s, something is stuck
                if last_wait_time is not None and abs(wait_time - last_wait_time) < 0.5:
                    stall_count += 1
                    if stall_count >= STALL_THRESHOLD:
                        logger.warning(
                            "Rate limiter stall detected (%d iterations at %.1fs), "
                            "forcing oldest record expiry",
                            stall_count, wait_time,
                        )
                        if self._history:
                            self._history.popleft()
                        stall_count = 0
                        continue
                else:
                    stall_count = 0
                last_wait_time = wait_time

                # Cap wait time to avoid extremely long waits
                wait_time = min(wait_time, 65.0)
                # Don't exceed max total wait
                wait_time = min(wait_time, MAX_WAIT_PER_CALL - waited)

                logger.info(
                    "Rate limit approaching (%s), waiting %.1f seconds...",
                    reason, wait_time,
                )

                # Sleep WITHOUT releasing the lock (avoids release-unlocked-lock bug)
                # RLock is reentrant so this is safe for single-threaded usage
                time.sleep(wait_time)

                waited += wait_time
                self.total_wait_seconds += wait_time

            return waited

    def record_usage(self, input_tokens: int, output_tokens: int) -> None:
        """
        Record token usage from a completed API call.

        Args:
            input_tokens: Actual input tokens used.
            output_tokens: Actual output tokens used.
        """
        with self._lock:
            record = UsageRecord(
                timestamp=time.monotonic(),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
            self._history.append(record)

            self.total_requests += 1
            self.total_input_tokens += input_tokens
            self.total_output_tokens += output_tokens

    def get_stats(self) -> dict:
        """Return cumulative usage statistics."""
        return {
            "total_requests": self.total_requests,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_wait_seconds": round(self.total_wait_seconds, 1),
            "avg_wait_per_request": round(
                self.total_wait_seconds / max(1, self.total_requests), 2
            ),
        }
