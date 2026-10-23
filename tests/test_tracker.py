import threading
import unittest

import tracker
from tracker import SEQ_MODULUS, SenderStats, SequenceTracker, Verdict

A = ("192.0.2.1", 50001)
B = ("192.0.2.2", 50002)
NEW, LATE, DUP = Verdict.NEW, Verdict.LATE, Verdict.DUPLICATE


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class TrackerTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.tracker = SequenceTracker(clock=self.clock)

    def feed(self, sender, *seqs):
        return [self.tracker.observe(sender, s) for s in seqs]


class OrderingTests(TrackerTestCase):
    def test_first_frame_is_new_whatever_its_number(self):
        self.assertEqual(self.feed(A, 500), [NEW])
        self.assertEqual(self.tracker.stats(A), SenderStats(received=1))

    def test_in_order_stream(self):
        self.assertEqual(self.feed(A, 0, 1, 2, 3), [NEW] * 4)
        stats = self.tracker.stats(A)
        self.assertEqual((stats.received, stats.gaps, stats.duplicates), (4, 0, 0))

    def test_duplicate_of_latest_and_of_older(self):
        self.assertEqual(self.feed(A, 0, 1, 2, 2, 1, 0), [NEW, NEW, NEW, DUP, DUP, DUP])
        self.assertEqual(self.tracker.stats(A).duplicates, 3)
        self.assertEqual(self.tracker.stats(A).received, 3)

    def test_gap_is_counted(self):
        self.feed(A, 0, 4)
        stats = self.tracker.stats(A)
        self.assertEqual((stats.gaps, stats.missing), (3, 3))

    def test_late_arrival_fills_gap_once(self):
        self.assertEqual(self.feed(A, 0, 3, 2, 1, 2), [NEW, NEW, LATE, LATE, DUP])
        stats = self.tracker.stats(A)
        self.assertEqual((stats.gaps, stats.late, stats.missing, stats.duplicates), (2, 2, 0, 1))

    def test_senders_are_independent(self):
        # The core reason for per-sender tracking: both peers count from 0.
        self.assertEqual(self.feed(A, 0, 1), [NEW, NEW])
        self.assertEqual(self.feed(B, 0, 1), [NEW, NEW])
        self.assertEqual(self.tracker.totals()["duplicates"], 0)
        self.assertEqual(self.tracker.totals()["senders"], 2)

    def test_rejects_out_of_range_sequence(self):
        for seq in (-1, SEQ_MODULUS):
            with self.assertRaises(ValueError):
                self.tracker.observe(A, seq)


class WraparoundTests(TrackerTestCase):
    def test_wrap_from_max_to_zero_is_in_order(self):
        top = SEQ_MODULUS - 1
        self.assertEqual(self.feed(A, top - 1, top, 0, 1), [NEW] * 4)
        self.assertEqual(self.tracker.stats(A).gaps, 0)

    def test_gap_across_wrap(self):
        top = SEQ_MODULUS - 1
        self.feed(A, top - 1, 1)  # Skips top and 0.
        self.assertEqual(self.tracker.stats(A).gaps, 2)
        self.assertEqual(self.feed(A, top, 0), [LATE, LATE])

    def test_old_number_before_wrap_is_duplicate(self):
        top = SEQ_MODULUS - 1
        self.assertEqual(self.feed(A, top, 0, top), [NEW, NEW, DUP])


class RestartTests(TrackerTestCase):
    def test_backwards_after_idle_is_new_session(self):
        self.feed(A, 0, 1, 2)
        self.clock.now += tracker.IDLE_RESET_SECONDS
        self.assertEqual(self.feed(A, 0, 1), [NEW, NEW])
        stats = self.tracker.stats(A)
        self.assertEqual((stats.restarts, stats.duplicates), (1, 0))

    def test_repeat_of_highest_after_idle_is_new_session(self):
        self.feed(A, 0)
        self.clock.now += tracker.IDLE_RESET_SECONDS
        self.assertEqual(self.feed(A, 0), [NEW])

    def test_backwards_while_active_is_duplicate(self):
        self.feed(A, 0, 1, 2)
        self.clock.now += tracker.IDLE_RESET_SECONDS / 2
        self.assertEqual(self.feed(A, 1), [DUP])

    def test_restart_forgets_old_gaps(self):
        self.feed(A, 0, 5)
        self.clock.now += tracker.IDLE_RESET_SECONDS
        self.feed(A, 0)
        self.assertEqual(self.feed(A, 3), [NEW])  # Not "late" from the old session.


class BoundsTests(unittest.TestCase):
    def test_missing_set_is_bounded_but_gaps_still_counted(self):
        t = SequenceTracker(max_missing=10)
        t.observe(A, 0)
        t.observe(A, 101)  # 100 skipped; only the newest 10 remembered.
        self.assertEqual(t.stats(A).gaps, 100)
        self.assertEqual(t.observe(A, 100), LATE)
        self.assertEqual(t.observe(A, 50), DUP)  # Forgotten gap: treated as old.

    def test_sender_count_is_bounded_and_counts_survive_eviction(self):
        t = SequenceTracker(max_senders=2)
        for port in range(5):
            sender = ("192.0.2.9", port)
            t.observe(sender, 0)
            t.observe(sender, 0)  # One duplicate each.
        totals = t.totals()
        self.assertEqual(totals["senders"], 2)
        self.assertEqual((totals["received"], totals["duplicates"]), (5, 5))

    def test_least_recently_heard_sender_is_evicted(self):
        t = SequenceTracker(max_senders=2)
        t.observe(A, 0)
        t.observe(B, 0)
        t.observe(A, 1)  # A is now the most recent.
        t.observe(("192.0.2.3", 1), 0)  # Evicts B.
        self.assertEqual(t.stats(B), SenderStats())
        self.assertEqual(t.stats(A).received, 2)

    def test_reset_clears_everything(self):
        t = SequenceTracker()
        t.observe(A, 0)
        t.observe(A, 0)
        t.reset()
        self.assertEqual(t.totals()["duplicates"], 0)
        self.assertEqual(t.observe(A, 0), NEW)


class ThreadSafetyTests(unittest.TestCase):
    def test_concurrent_observe_and_totals(self):
        t = SequenceTracker()
        errors = []

        def writer(port):
            try:
                for seq in range(2000):
                    t.observe(("192.0.2.1", port), seq)
            except Exception as exc:  # pragma: no cover - only on failure
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(p,)) for p in range(4)]
        for thread in threads:
            thread.start()
        while any(thread.is_alive() for thread in threads):
            t.totals()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(t.totals()["received"], 8000)
        self.assertEqual(t.totals()["duplicates"], 0)


if __name__ == "__main__":
    unittest.main()
