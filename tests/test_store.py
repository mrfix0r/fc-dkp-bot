from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from store import RuleError, Store
from runtime import InstanceLock


class DKPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "dkp.sqlite3"
        self.now = 1000000
        self.db = Store(self.path, 123, lambda: self.now)
        for uid in (1, 2, 3):
            self.db.register(uid, f"Игрок{uid}")
            self.db.adjust(uid, 100, "Начальный баланс", 99, f"seed-{uid}")

    def test_event_awards_once_and_deduplicates_attendance(self):
        eid = self.db.create_event("ЧВ", 10, 99)
        self.db.attend(eid, [1, 1, 2], True, 99)
        self.db.attend(eid, [1], True, 1)
        event, people = self.db.event_snapshot(eid)
        self.assertEqual(len(people), 2)
        self.assertEqual(self.db.award_event(eid, event["revision"], 99), 2)
        with self.assertRaises(RuleError):
            self.db.award_event(eid, event["revision"], 99)
        self.assertEqual(self.db.balance(1)["total"], 110)
        self.assertEqual(self.db.balance(2)["total"], 110)

    def test_balance_reads_committed_state_while_another_writer_is_busy(self):
        aid = self.db.create_auction("Щит", 10, 1, 5, 99)
        self.db.bid(aid, 1, 30)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.db.transaction() as writer:
                writer.execute("INSERT INTO ledger(user_id,amount,reason,actor_id,op_key,created_at) VALUES(1,50,'test',99,'busy-writer',?)", (self.now,))
                writer.execute("UPDATE auctions SET highest_bid=40 WHERE id=?", (aid,))
                # The reader must finish while this uncommitted writer still holds its lock.
                state = pool.submit(self.db.balance, 1).result(timeout=2)
                self.assertEqual((state["total"], state["reserved"], state["available"]), (100, 30, 70))
        state = self.db.balance(1)
        self.assertEqual((state["total"], state["reserved"], state["available"]), (150, 40, 110))

    def test_changed_roster_invalidates_confirmation(self):
        eid = self.db.create_event("ПБ", 10, 99)
        self.db.attend(eid, [1], True, 99)
        snapshot, _ = self.db.event_snapshot(eid)
        self.db.attend(eid, [2], True, 2)
        with self.assertRaises(RuleError):
            self.db.award_event(eid, snapshot["revision"], 99)
        self.assertEqual(self.db.balance(1)["total"], 100)
        self.assertEqual(self.db.entity("events", eid)["status"], "open")

    def test_invalid_member_rolls_back_entire_batch(self):
        eid = self.db.create_event("ПБ", 10, 99)
        with self.assertRaises(RuleError):
            self.db.attend(eid, [1, 999], True, 99)
        self.assertEqual(self.db.attendees(eid), [])

    def test_reservations_across_auctions_and_outbid(self):
        a = self.db.create_auction("Меч", 10, 5, 10, 99)
        b = self.db.create_auction("Щит", 10, 1, 10, 99)
        self.db.bid(a, 1, 70)
        self.assertEqual(self.db.balance(1)["available"], 30)
        with self.assertRaises(RuleError):
            self.db.bid(b, 1, 40)
        self.db.bid(a, 1, 80)  # The bidder's own previous reservation is replaced.
        self.db.bid(a, 2, 90)
        self.assertEqual(self.db.balance(1)["available"], 100)
        self.db.bid(b, 1, 100)
        self.assertEqual(self.db.balance(1)["available"], 0)

    def test_adjustment_cannot_spend_reserved_points(self):
        a = self.db.create_auction("Меч", 10, 1, 10, 99)
        self.db.bid(a, 1, 90)
        with self.assertRaises(RuleError):
            self.db.adjust(1, -11, "Штраф", 99, "negative")
        self.db.adjust(1, -10, "Исправление", 99, "negative-ok")
        self.assertEqual(self.db.balance(1)["available"], 0)

    def test_ties_minimum_and_step(self):
        a = self.db.create_auction("Меч", 10, 5, 10, 99)
        for amount in (0, -10, 9):
            with self.assertRaises(RuleError):
                self.db.bid(a, 1, amount)
        self.db.bid(a, 1, 10)
        for amount in (10, 11, 14):
            with self.assertRaises(RuleError):
                self.db.bid(a, 2, amount)
        self.db.bid(a, 2, 15)

    def test_anti_sniping_and_expired_bid(self):
        a = self.db.create_auction("Меч", 10, 1, 1, 99)
        self.now += 55
        self.db.bid(a, 1, 10)
        self.assertEqual(self.db.entity("auctions", a)["ends_at"], self.now+30)
        self.now += 30
        with self.assertRaises(RuleError):
            self.db.bid(a, 2, 20)

    def test_restart_closes_expired_auction_exactly_once(self):
        a = self.db.create_auction("Меч", 10, 1, 1, 99)
        self.db.bid(a, 1, 30)
        self.now += 300
        reloaded = Store(self.path, 123, lambda: self.now)
        reloaded.close_due()
        reloaded.close_due()
        self.assertEqual(reloaded.balance(1)["total"], 70)
        self.assertEqual(reloaded.balance(1)["reserved"], 0)
        self.assertEqual(len(reloaded.rows("SELECT * FROM ledger WHERE op_key=?", (f"auction:{a}",))), 1)

    def test_cancel_releases_reservation_without_spending(self):
        a = self.db.create_auction("Щит", 10, 1, 10, 99)
        self.db.bid(a, 1, 100)
        self.db.cancel_auction(a, 99, "Ошибка предмета")
        self.assertEqual(self.db.balance(1)["total"], 100)
        self.assertEqual(self.db.balance(1)["available"], 100)
        with self.assertRaises(RuleError):
            self.db.bid(a, 2, 101)

    def test_due_auction_cannot_be_cancelled_to_avoid_payment(self):
        a = self.db.create_auction("Щит", 10, 1, 1, 99)
        self.db.bid(a, 1, 100)
        self.now += 60
        with self.assertRaises(RuleError):
            self.db.cancel_auction(a, 99, "Слишком поздно")
        self.db.close_due()
        self.assertEqual(self.db.balance(1)["total"], 0)

    def test_no_bids_no_charge(self):
        a = self.db.create_auction("Щит", 10, 1, 1, 99)
        self.now += 60
        self.db.close_due()
        self.assertEqual(self.db.entity("auctions", a)["status"], "closed")
        self.assertEqual(len(self.db.rows("SELECT * FROM ledger")), 3)

    def test_duplicate_adjustment_and_nickname_identity(self):
        self.db.adjust(1, 10, "Бонус", 99, "unique")
        with self.assertRaises(RuleError):
            self.db.adjust(1, 10, "Бонус", 99, "unique")
        with self.assertRaises(RuleError):
            self.db.register(5, "игрок1")
        with self.assertRaises(RuleError):
            self.db.register(1, "НовоеИмя")
        self.db.rename(1, "Fix0r", 99)
        self.assertEqual(self.db.balance(1)["total"], 110)
        self.assertEqual(self.db.balance(1)["nickname"], "Fix0r")

    def test_concurrent_bids_cannot_overspend(self):
        a = self.db.create_auction("Меч", 10, 1, 10, 99)
        b = self.db.create_auction("Щит", 10, 1, 10, 99)
        barrier = threading.Barrier(2)
        def bid(aid):
            barrier.wait()
            try:
                self.db.bid(aid, 1, 80)
                return True
            except RuleError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(bid, [a, b]))
        self.assertEqual(results.count(True), 1)
        self.assertEqual(self.db.balance(1)["available"], 20)

    def test_concurrent_close_charges_once(self):
        a = self.db.create_auction("Меч", 10, 1, 1, 99)
        self.db.bid(a, 1, 50)
        self.now += 60
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(self.db.close_auction, [a]*4))
        self.assertEqual(results.count(True), 1)
        self.assertEqual(self.db.balance(1)["total"], 50)

    def test_backup_guild_binding_and_immutable_ledger(self):
        copy = self.db.backup(Path(self.temp.name) / "backup.sqlite3")
        restored = Store(copy, 123, lambda: self.now)
        self.assertEqual(restored.balance(1)["total"], 100)
        with self.assertRaises(RuleError):
            Store(self.path, 456)
        for sql in ("DELETE FROM ledger", "UPDATE ledger SET amount=500", "DELETE FROM journal"):
            with self.assertRaises(sqlite3.IntegrityError), self.db.transaction() as conn:
                conn.execute(sql)

    def test_backup_closes_both_connections_on_success_and_failure(self):
        real_connect = sqlite3.connect

        class FailingBackup(sqlite3.Connection):
            def backup(self, *args, **kwargs):
                raise sqlite3.OperationalError("Simulated backup failure")

        for fail in (False, True):
            with self.subTest(fail=fail):
                connections = []

                def track_connect(*args, **kwargs):
                    if fail:
                        kwargs["factory"] = FailingBackup
                    connection = real_connect(*args, **kwargs)
                    connections.append(connection)
                    return connection

                try:
                    with patch("store.sqlite3.connect", side_effect=track_connect):
                        target = Path(self.temp.name) / f"closed-backup-{fail}.sqlite3"
                        if fail:
                            with self.assertRaises(sqlite3.OperationalError):
                                self.db.backup(target)
                        else:
                            self.db.backup(target)
                    self.assertEqual(len(connections), 2)
                    for connection in connections:
                        with self.assertRaises(sqlite3.ProgrammingError):
                            connection.execute("SELECT 1")
                finally:
                    for connection in connections:
                        connection.close()

    def test_pending_publication_survives_restart(self):
        pending = self.db.pending_journal()
        self.db.delivered(pending[0]["id"], 777)
        restarted = Store(self.path, 123)
        self.assertNotIn(pending[0]["id"], [r["id"] for r in restarted.pending_journal()])
        self.assertEqual(len(restarted.pending_journal()), len(pending)-1)
        eid = self.db.create_event("ЧВ", 10, 99)
        old_revision = self.db.entity("events", eid)["revision"]
        self.db.attend(eid, [1], True, 1)
        self.db.rendered("events", eid, old_revision, 778)
        self.assertEqual(len(self.db.dirty("events")), 1)

    def test_process_lock_releases(self):
        lockfile = Path(self.temp.name) / "bot.lock"
        with InstanceLock(lockfile):
            with self.assertRaises(RuntimeError):
                with InstanceLock(lockfile):
                    pass
        with InstanceLock(lockfile):
            pass


if __name__ == "__main__":
    unittest.main()
