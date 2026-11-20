"""
Sales ledger for the photo shop bot.

Stdlib only (sqlite3). Every Stars payment is recorded once, keyed by
Telegram's charge id, and split into per-photographer allocations at the
moment of sale, so later price/split changes never rewrite history.

All amounts are in Telegram Stars (integers).
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

DAY = 86_400
STARS_HOLD_DAYS = 21  # Telegram's hold before Stars become withdrawable

SCHEMA = """
CREATE TABLE IF NOT EXISTS purchases (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    charge_id     TEXT    NOT NULL UNIQUE,   -- telegram_payment_charge_id
    user_id       INTEGER NOT NULL,
    username      TEXT,
    item_type     TEXT    NOT NULL,          -- 'bundle' | 'premium'
    item_id       TEXT    NOT NULL,
    stars         INTEGER NOT NULL,
    is_recurring  INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL,
    refunded_at   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_purchases_user ON purchases(user_id);

CREATE TABLE IF NOT EXISTS allocations (
    purchase_id   INTEGER NOT NULL REFERENCES purchases(id),
    photographer  TEXT    NOT NULL,          -- photographer key, or 'house'
    stars         INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alloc_ph ON allocations(photographer);

CREATE TABLE IF NOT EXISTS payouts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    photographer  TEXT    NOT NULL,
    stars         INTEGER NOT NULL,
    note          TEXT,
    recorded_by   INTEGER,
    created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS premium_members (
    user_id       INTEGER PRIMARY KEY,
    expires_at    INTEGER,                   -- NULL = lifetime
    invite_link   TEXT,
    joined_at     INTEGER,
    removed_at    INTEGER
);
"""


def split_stars(total: int, weights: dict[str, float]) -> dict[str, int]:
    """Split an integer amount by weights; remainder goes by largest fraction
    (ties broken by key order) so the parts always sum to `total`."""
    if total < 0:
        raise ValueError("total must be >= 0")
    weights = {k: w for k, w in weights.items() if w > 0}
    if not weights:
        raise ValueError("need at least one positive weight")
    wsum = sum(weights.values())
    exact = {k: total * w / wsum for k, w in weights.items()}
    parts = {k: int(v) for k, v in exact.items()}
    leftover = total - sum(parts.values())
    order = sorted(weights, key=lambda k: (-(exact[k] - parts[k]), list(weights).index(k)))
    for k in order[:leftover]:
        parts[k] += 1
    return parts


def allocate(stars: int, owners: dict[str, float], house_cut_percent: float = 0) -> dict[str, int]:
    """House cut first (rounded down), the rest split among owners."""
    house = int(stars * house_cut_percent / 100) if house_cut_percent else 0
    parts = split_stars(stars - house, owners)
    if house:
        parts["house"] = parts.get("house", 0) + house
    return parts


@dataclass
class Balance:
    photographer: str
    earned: int          # all non-refunded allocations
    eligible: int        # of that, older than the 21-day hold
    paid: int            # recorded payouts

    @property
    def pending(self) -> int:
        return self.earned - self.eligible

    @property
    def owed_now(self) -> int:
        """What can be paid out today (may be negative if over-paid / refunds)."""
        return self.eligible - self.paid


class Ledger:
    def __init__(self, path: str = "shop.db"):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ---------- purchases ----------

    def record_purchase(
        self,
        *,
        charge_id: str,
        user_id: int,
        username: str | None,
        item_type: str,
        item_id: str,
        stars: int,
        allocations: dict[str, int],
        is_recurring: bool = False,
        now: int | None = None,
    ) -> bool:
        """Returns False if this charge was already recorded (duplicate update)."""
        if sum(allocations.values()) != stars:
            raise ValueError("allocations must sum to the paid amount")
        now = int(now or time.time())
        try:
            with self.db:
                cur = self.db.execute(
                    "INSERT INTO purchases (charge_id,user_id,username,item_type,item_id,stars,is_recurring,created_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (charge_id, user_id, username, item_type, item_id, stars, int(is_recurring), now),
                )
                pid = cur.lastrowid
                self.db.executemany(
                    "INSERT INTO allocations (purchase_id, photographer, stars) VALUES (?,?,?)",
                    [(pid, ph, s) for ph, s in allocations.items() if s],
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def get_purchase(self, charge_id: str):
        return self.db.execute("SELECT * FROM purchases WHERE charge_id=?", (charge_id,)).fetchone()

    def mark_refunded(self, charge_id: str, now: int | None = None) -> bool:
        with self.db:
            cur = self.db.execute(
                "UPDATE purchases SET refunded_at=? WHERE charge_id=? AND refunded_at IS NULL",
                (int(now or time.time()), charge_id),
            )
        return cur.rowcount == 1

    def owned_bundles(self, user_id: int) -> list[str]:
        rows = self.db.execute(
            "SELECT DISTINCT item_id FROM purchases"
            " WHERE user_id=? AND item_type='bundle' AND refunded_at IS NULL ORDER BY created_at",
            (user_id,),
        ).fetchall()
        return [r["item_id"] for r in rows]

    def owns_bundle(self, user_id: int, bundle_id: str) -> bool:
        return bundle_id in self.owned_bundles(user_id)

    def recent_sales(self, since: int, photographer: str | None = None):
        if photographer:
            return self.db.execute(
                "SELECT p.*, a.stars AS share FROM purchases p JOIN allocations a ON a.purchase_id=p.id"
                " WHERE p.created_at>=? AND a.photographer=? ORDER BY p.created_at DESC",
                (since, photographer),
            ).fetchall()
        return self.db.execute(
            "SELECT * FROM purchases WHERE created_at>=? ORDER BY created_at DESC", (since,)
        ).fetchall()

    # ---------- payouts / balances ----------

    def record_payout(self, photographer: str, stars: int, note: str = "", recorded_by: int | None = None,
                      now: int | None = None) -> None:
        if stars <= 0:
            raise ValueError("payout must be positive")
        with self.db:
            self.db.execute(
                "INSERT INTO payouts (photographer, stars, note, recorded_by, created_at) VALUES (?,?,?,?,?)",
                (photographer, stars, note, recorded_by, int(now or time.time())),
            )

    def balance(self, photographer: str, now: int | None = None) -> Balance:
        now = int(now or time.time())
        cutoff = now - STARS_HOLD_DAYS * DAY
        row = self.db.execute(
            "SELECT"
            "  COALESCE(SUM(a.stars),0) AS earned,"
            "  COALESCE(SUM(CASE WHEN p.created_at<=? THEN a.stars END),0) AS eligible"
            " FROM allocations a JOIN purchases p ON p.id=a.purchase_id"
            " WHERE a.photographer=? AND p.refunded_at IS NULL",
            (cutoff, photographer),
        ).fetchone()
        paid = self.db.execute(
            "SELECT COALESCE(SUM(stars),0) FROM payouts WHERE photographer=?", (photographer,)
        ).fetchone()[0]
        return Balance(photographer, row["earned"], row["eligible"], paid)

    # ---------- premium membership ----------

    def grant_premium(self, user_id: int, expires_at: int | None, invite_link: str | None = None) -> None:
        """Create or extend membership. expires_at=None means lifetime."""
        with self.db:
            self.db.execute(
                "INSERT INTO premium_members (user_id, expires_at, invite_link, removed_at) VALUES (?,?,?,NULL)"
                " ON CONFLICT(user_id) DO UPDATE SET expires_at=excluded.expires_at,"
                " invite_link=COALESCE(excluded.invite_link, premium_members.invite_link), removed_at=NULL",
                (user_id, expires_at, invite_link),
            )

    def set_invite(self, user_id: int, link: str | None) -> None:
        with self.db:
            self.db.execute("UPDATE premium_members SET invite_link=? WHERE user_id=?", (link, user_id))

    def premium(self, user_id: int):
        return self.db.execute("SELECT * FROM premium_members WHERE user_id=?", (user_id,)).fetchone()

    def is_premium_active(self, user_id: int, now: int | None = None) -> bool:
        m = self.premium(user_id)
        if not m or m["removed_at"] is not None:
            return False
        return m["expires_at"] is None or m["expires_at"] > int(now or time.time())

    def mark_joined(self, user_id: int, now: int | None = None) -> None:
        with self.db:
            self.db.execute(
                "UPDATE premium_members SET joined_at=?, invite_link=NULL WHERE user_id=?",
                (int(now or time.time()), user_id),
            )

    def expired_members(self, grace_seconds: int = DAY, now: int | None = None) -> list[int]:
        now = int(now or time.time())
        rows = self.db.execute(
            "SELECT user_id FROM premium_members"
            " WHERE removed_at IS NULL AND expires_at IS NOT NULL AND expires_at < ?",
            (now - grace_seconds,),
        ).fetchall()
        return [r["user_id"] for r in rows]

    def mark_removed(self, user_id: int, now: int | None = None) -> None:
        with self.db:
            self.db.execute(
                "UPDATE premium_members SET removed_at=?, invite_link=NULL WHERE user_id=?",
                (int(now or time.time()), user_id),
            )
