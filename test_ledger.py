import unittest

from ledger import DAY, Ledger, allocate, split_stars

T0 = 1_800_000_000


class SplitTests(unittest.TestCase):
    def test_split_sums_exactly(self):
        for total in (0, 1, 2, 100, 101, 499, 500, 9999):
            parts = split_stars(total, {"a": 1, "b": 1, "c": 1})
            self.assertEqual(sum(parts.values()), total)

    def test_uneven_weights(self):
        self.assertEqual(split_stars(100, {"a": 2, "b": 1, "c": 1}), {"a": 50, "b": 25, "c": 25})

    def test_remainder_goes_to_first_on_tie(self):
        self.assertEqual(split_stars(100, {"a": 1, "b": 1, "c": 1}), {"a": 34, "b": 33, "c": 33})

    def test_house_cut(self):
        parts = allocate(500, {"a": 1}, house_cut_percent=10)
        self.assertEqual(parts, {"a": 450, "house": 50})
        parts = allocate(333, {"a": 1, "b": 1, "c": 1}, house_cut_percent=10)
        self.assertEqual(sum(parts.values()), 333)
        self.assertEqual(allocate(100, {"house": 1}, house_cut_percent=10), {"house": 100})


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.l = Ledger(":memory:")

    def buy(self, charge, user=1, item="alex-sunsets", stars=250, alloc=None, when=T0, kind="bundle"):
        return self.l.record_purchase(
            charge_id=charge, user_id=user, username="u", item_type=kind, item_id=item,
            stars=stars, allocations=alloc or {"alex": stars}, now=when,
        )

    def test_duplicate_charge_ignored(self):
        self.assertTrue(self.buy("c1"))
        self.assertFalse(self.buy("c1"))
        self.assertEqual(self.l.balance("alex", now=T0).earned, 250)

    def test_allocations_must_match(self):
        with self.assertRaises(ValueError):
            self.buy("c1", stars=250, alloc={"alex": 200})

    def test_hold_and_payout(self):
        self.buy("c1", when=T0)
        self.buy("c2", when=T0 + 10 * DAY)
        b = self.l.balance("alex", now=T0 + 22 * DAY)
        self.assertEqual((b.earned, b.eligible, b.pending, b.owed_now), (500, 250, 250, 250))
        self.l.record_payout("alex", 250, now=T0 + 22 * DAY)
        b = self.l.balance("alex", now=T0 + 32 * DAY)
        self.assertEqual((b.eligible, b.paid, b.owed_now), (500, 250, 250))

    def test_refund_removes_earnings_and_ownership(self):
        self.buy("c1")
        self.assertTrue(self.l.owns_bundle(1, "alex-sunsets"))
        self.assertTrue(self.l.mark_refunded("c1"))
        self.assertFalse(self.l.mark_refunded("c1"))
        self.assertFalse(self.l.owns_bundle(1, "alex-sunsets"))
        self.assertEqual(self.l.balance("alex", now=T0 + 30 * DAY).earned, 0)

    def test_premium_split_and_lifecycle(self):
        alloc = split_stars(500, {"alex": 1, "bea": 1, "cam": 1})
        self.buy("p1", user=7, kind="premium", item="premium", stars=500, alloc=alloc)
        self.l.grant_premium(7, expires_at=T0 + 30 * DAY, invite_link="https://t.me/+x")
        self.assertTrue(self.l.is_premium_active(7, now=T0 + DAY))
        self.assertEqual(alloc, {"alex": 167, "bea": 167, "cam": 166})
        self.assertEqual(self.l.balance("cam", now=T0).earned, 166)
        # renewal extends and keeps link
        self.l.grant_premium(7, expires_at=T0 + 60 * DAY)
        self.assertEqual(self.l.premium(7)["invite_link"], "https://t.me/+x")
        self.l.mark_joined(7, now=T0 + DAY)
        self.assertIsNone(self.l.premium(7)["invite_link"])
        self.assertEqual(self.l.expired_members(now=T0 + 45 * DAY), [])
        self.assertEqual(self.l.expired_members(now=T0 + 62 * DAY), [7])
        self.l.mark_removed(7)
        self.assertFalse(self.l.is_premium_active(7, now=T0 + DAY))
        self.assertEqual(self.l.expired_members(now=T0 + 99 * DAY), [])

    def test_lifetime_never_expires(self):
        self.l.grant_premium(9, expires_at=None)
        self.assertTrue(self.l.is_premium_active(9, now=T0 + 9999 * DAY))
        self.assertEqual(self.l.expired_members(now=T0 + 9999 * DAY), [])


if __name__ == "__main__":
    unittest.main()
