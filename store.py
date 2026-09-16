"""Transactional DKP ledger. No Discord dependency; one database per guild."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
import unicodedata


class RuleError(Exception):
    """A user-visible validation error; no partial mutation was committed."""


def clean(value: str, maximum: int, label: str) -> str:
    value = unicodedata.normalize("NFKC", value).strip()
    if not value or len(value) > maximum or any(unicodedata.category(c).startswith("C") for c in value):
        raise RuleError(f"{label}: от 1 до {maximum} символов, без переносов и скрытых символов.")
    return value


class Store:
    def __init__(self, path: str | Path, guild_id: int, clock=time.time):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS settings(
                    id INTEGER PRIMARY KEY CHECK(id=1), channel_id INTEGER NOT NULL,
                    officer_role_id INTEGER NOT NULL, member_role_id INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS members(
                    user_id INTEGER PRIMARY KEY, nickname TEXT NOT NULL,
                    nickname_key TEXT NOT NULL UNIQUE, created_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS ledger(
                    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES members(user_id),
                    amount INTEGER NOT NULL, reason TEXT NOT NULL, actor_id INTEGER NOT NULL,
                    op_key TEXT NOT NULL UNIQUE, created_at INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS ledger_user ON ledger(user_id,id);
                CREATE TABLE IF NOT EXISTS events(
                    id INTEGER PRIMARY KEY, title TEXT NOT NULL, points INTEGER NOT NULL CHECK(points>0),
                    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','awarded','cancelled')),
                    creator_id INTEGER NOT NULL, created_at INTEGER NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1, rendered_revision INTEGER NOT NULL DEFAULT 0,
                    message_id INTEGER);
                CREATE TABLE IF NOT EXISTS attendance(
                    event_id INTEGER NOT NULL REFERENCES events(id),
                    user_id INTEGER NOT NULL REFERENCES members(user_id),
                    PRIMARY KEY(event_id,user_id));
                CREATE TABLE IF NOT EXISTS auctions(
                    id INTEGER PRIMARY KEY, item TEXT NOT NULL,
                    minimum INTEGER NOT NULL CHECK(minimum>0), step INTEGER NOT NULL CHECK(step>0),
                    ends_at INTEGER NOT NULL, highest_user INTEGER REFERENCES members(user_id),
                    highest_bid INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed','cancelled')),
                    creator_id INTEGER NOT NULL, created_at INTEGER NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1, rendered_revision INTEGER NOT NULL DEFAULT 0,
                    message_id INTEGER);
                CREATE INDEX IF NOT EXISTS auction_reserves ON auctions(status,highest_user);
                CREATE TABLE IF NOT EXISTS bids(
                    id INTEGER PRIMARY KEY, auction_id INTEGER NOT NULL REFERENCES auctions(id),
                    user_id INTEGER NOT NULL REFERENCES members(user_id),
                    amount INTEGER NOT NULL, created_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS journal(
                    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, actor_id INTEGER NOT NULL,
                    data TEXT NOT NULL, created_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS deliveries(
                    journal_id INTEGER PRIMARY KEY REFERENCES journal(id), message_id INTEGER NOT NULL);
                CREATE TRIGGER IF NOT EXISTS ledger_no_update BEFORE UPDATE ON ledger
                    BEGIN SELECT RAISE(ABORT,'Ledger is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS ledger_no_delete BEFORE DELETE ON ledger
                    BEGIN SELECT RAISE(ABORT,'Ledger is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS journal_no_update BEFORE UPDATE ON journal
                    BEGIN SELECT RAISE(ABORT,'Journal is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS journal_no_delete BEFORE DELETE ON journal
                    BEGIN SELECT RAISE(ABORT,'Journal is append-only'); END;
            """)
        with self.transaction() as db:
            guild = db.execute("SELECT value FROM meta WHERE key='guild_id'").fetchone()
            if guild and guild[0] != str(guild_id):
                raise RuleError("Эта база принадлежит другому Discord-серверу. Верните прежний GUILD_ID или используйте другую папку DATA_DIR.")
            db.execute("INSERT OR IGNORE INTO meta VALUES('guild_id',?)", (str(guild_id),))
            db.execute("INSERT OR IGNORE INTO meta VALUES('schema_version','1')")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.rollback()
                raise
            else:
                db.commit()

    def now(self):
        return int(self.clock())

    def rows(self, sql, args=()):
        with self.connection() as db:
            return [dict(row) for row in db.execute(sql, args).fetchall()]

    def settings(self):
        rows = self.rows("SELECT * FROM settings WHERE id=1")
        return rows[0] if rows else None

    def configure(self, channel_id, officer_role_id, member_role_id, actor_id):
        with self.transaction() as db:
            old = db.execute("SELECT * FROM settings WHERE id=1").fetchone()
            if old and old["channel_id"] != channel_id:
                raise RuleError("Канал уже настроен. В первой версии перенос канала не поддерживается, чтобы сохранить карточки и журнал.")
            db.execute("INSERT INTO settings VALUES(1,?,?,?) ON CONFLICT(id) DO UPDATE SET officer_role_id=excluded.officer_role_id, member_role_id=excluded.member_role_id", (channel_id, officer_role_id, member_role_id))
            self._journal(db, "settings", actor_id, channel_id=channel_id, officer_role_id=officer_role_id, member_role_id=member_role_id)

    def _journal(self, db, kind, actor_id, **data):
        return db.execute("INSERT INTO journal(kind,actor_id,data,created_at) VALUES(?,?,?,?)", (kind, actor_id, json.dumps(data, ensure_ascii=False), self.now())).lastrowid

    def _member(self, db, user_id):
        row = db.execute("SELECT * FROM members WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            raise RuleError(f"Участник <@{user_id}> ещё не привязал ник: /dkp register.")
        return row

    def register(self, user_id, nickname):
        nickname = clean(nickname, 32, "Ник")
        with self.transaction() as db:
            old = db.execute("SELECT nickname FROM members WHERE user_id=?", (user_id,)).fetchone()
            if old:
                raise RuleError("Ник уже привязан. Для изменения обратитесь к офицеру (/dkp rename).")
            self._set_nick(db, user_id, nickname, user_id)

    def _set_nick(self, db, user_id, nickname, actor_id):
        old = db.execute("SELECT nickname FROM members WHERE user_id=?", (user_id,)).fetchone()
        try:
            db.execute("INSERT INTO members VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET nickname=excluded.nickname, nickname_key=excluded.nickname_key", (user_id, nickname, nickname.casefold(), self.now()))
        except sqlite3.IntegrityError as exc:
            raise RuleError("Этот ник уже привязан к другому Discord-аккаунту.") from exc
        self._journal(db, "nickname", actor_id, user_id=user_id, nickname=nickname, previous=old[0] if old else None)

    def rename(self, user_id, nickname, actor_id):
        nickname = clean(nickname, 32, "Ник")
        with self.transaction() as db:
            self._member(db, user_id)
            self._set_nick(db, user_id, nickname, actor_id)

    def _balance(self, db, user_id, exclude_auction=0):
        member = self._member(db, user_id)
        total = db.execute("SELECT COALESCE(SUM(amount),0) FROM ledger WHERE user_id=?", (user_id,)).fetchone()[0]
        reserved = db.execute("SELECT COALESCE(SUM(highest_bid),0) FROM auctions WHERE status='open' AND highest_user=? AND id!=?", (user_id, exclude_auction)).fetchone()[0]
        return dict(user_id=user_id, nickname=member["nickname"], total=total, reserved=reserved, available=total-reserved)

    def balance(self, user_id):
        # A consistent read snapshot does not need to reserve SQLite's writer lock.
        with self.connection() as db:
            db.execute("BEGIN")
            return self._balance(db, user_id)

    def leaderboard(self, page=1):
        return self.rows("""SELECT m.user_id,m.nickname,
            COALESCE((SELECT SUM(amount) FROM ledger WHERE user_id=m.user_id),0) total,
            COALESCE((SELECT SUM(highest_bid) FROM auctions WHERE status='open' AND highest_user=m.user_id),0) reserved
            FROM members m ORDER BY total DESC,m.nickname_key LIMIT 20 OFFSET ?""", ((page-1)*20,))

    def adjust(self, user_id, amount, reason, actor_id, request_id):
        if not isinstance(amount, int) or not -1000000 <= amount <= 1000000 or amount == 0:
            raise RuleError("Изменение должно быть целым числом от −1 000 000 до 1 000 000, кроме нуля.")
        reason = clean(reason, 200, "Причина")
        key = f"adjust:{request_id}"
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM ledger WHERE op_key=?", (key,)).fetchone():
                raise RuleError("Это изменение уже проведено.")
            state = self._balance(db, user_id)
            if state["available"] + amount < 0:
                raise RuleError(f"Недостаточно свободных ДКП: {state['available']}. Резерв ставок списывать нельзя.")
            db.execute("INSERT INTO ledger(user_id,amount,reason,actor_id,op_key,created_at) VALUES(?,?,?,?,?,?)", (user_id, amount, reason, actor_id, key, self.now()))
            self._journal(db, "adjust", actor_id, user_id=user_id, amount=amount, reason=reason)

    def create_event(self, title, points, actor_id):
        title = clean(title, 100, "Название")
        if not 1 <= points <= 100000:
            raise RuleError("Награда: от 1 до 100 000 ДКП.")
        with self.transaction() as db:
            event_id = db.execute("INSERT INTO events(title,points,creator_id,created_at) VALUES(?,?,?,?)", (title, points, actor_id, self.now())).lastrowid
            self._journal(db, "event_created", actor_id, event_id=event_id, title=title, points=points)
            return event_id

    def _entity(self, db, table, entity_id):
        assert table in ("events", "auctions")
        row = db.execute(f"SELECT * FROM {table} WHERE id=?", (entity_id,)).fetchone()
        if not row:
            raise RuleError("Событие или аукцион с таким ID не найден.")
        return row

    def entity(self, table, entity_id):
        with self.connection() as db:
            return dict(self._entity(db, table, entity_id))

    def attend(self, event_id, user_ids, present, actor_id):
        user_ids = sorted(set(user_ids))
        if not user_ids:
            raise RuleError("Нет участников для добавления.")
        with self.transaction() as db:
            event = self._entity(db, "events", event_id)
            if event["status"] != "open":
                raise RuleError("Список уже закрыт.")
            for user_id in user_ids:
                self._member(db, user_id)
            changed = []
            for user_id in user_ids:
                if present:
                    cur = db.execute("INSERT OR IGNORE INTO attendance VALUES(?,?)", (event_id, user_id))
                else:
                    cur = db.execute("DELETE FROM attendance WHERE event_id=? AND user_id=?", (event_id, user_id))
                if cur.rowcount:
                    changed.append(user_id)
            if changed:
                db.execute("UPDATE events SET revision=revision+1 WHERE id=?", (event_id,))
                self._journal(db, "attendance", actor_id, event_id=event_id, present=present, user_ids=changed)
            return len(changed)

    def attendees(self, event_id):
        return self.rows("SELECT m.user_id,m.nickname FROM attendance a JOIN members m USING(user_id) WHERE event_id=? ORDER BY m.nickname_key", (event_id,))

    def event_snapshot(self, event_id):
        with self.transaction() as db:
            event = dict(self._entity(db, "events", event_id))
            people = [dict(r) for r in db.execute("SELECT m.user_id,m.nickname FROM attendance a JOIN members m USING(user_id) WHERE event_id=? ORDER BY m.nickname_key", (event_id,))]
            return event, people

    def award_event(self, event_id, expected_revision, actor_id):
        with self.transaction() as db:
            event = self._entity(db, "events", event_id)
            if event["status"] != "open":
                raise RuleError("Событие уже обработано. Повторного начисления не будет.")
            if event["revision"] != expected_revision:
                raise RuleError("Список изменился после проверки. Откройте подтверждение заново.")
            people = [r[0] for r in db.execute("SELECT user_id FROM attendance WHERE event_id=? ORDER BY user_id", (event_id,))]
            if not people:
                raise RuleError("В списке нет участников.")
            for uid in people:
                db.execute("INSERT INTO ledger(user_id,amount,reason,actor_id,op_key,created_at) VALUES(?,?,?,?,?,?)", (uid, event["points"], f"Событие #{event_id}: {event['title']}", actor_id, f"event:{event_id}:{uid}", self.now()))
            db.execute("UPDATE events SET status='awarded', revision=revision+1 WHERE id=?", (event_id,))
            self._journal(db, "event_awarded", actor_id, event_id=event_id, title=event["title"], points=event["points"], user_ids=people)
            return len(people)

    def cancel_event(self, event_id, actor_id):
        with self.transaction() as db:
            event = self._entity(db, "events", event_id)
            if event["status"] != "open":
                raise RuleError("Отменить можно только открытое событие. Начисления исправляются отдельной корректировкой.")
            db.execute("UPDATE events SET status='cancelled', revision=revision+1 WHERE id=?", (event_id,))
            self._journal(db, "event_cancelled", actor_id, event_id=event_id)

    def create_auction(self, item, minimum, step, minutes, actor_id):
        item = clean(item, 150, "Предмет")
        if not 1 <= minimum <= 1000000 or not 1 <= step <= 1000000 or not 1 <= minutes <= 10080:
            raise RuleError("Старт и шаг: 1–1 000 000. Время: 1–10 080 минут.")
        with self.transaction() as db:
            auction_id = db.execute("INSERT INTO auctions(item,minimum,step,ends_at,creator_id,created_at) VALUES(?,?,?,?,?,?)", (item, minimum, step, self.now()+minutes*60, actor_id, self.now())).lastrowid
            self._journal(db, "auction_created", actor_id, auction_id=auction_id, item=item)
            return auction_id

    def bid(self, auction_id, user_id, amount):
        if not isinstance(amount, int) or not 1 <= amount <= 1000000000:
            raise RuleError("Ставка должна быть целым положительным числом, не больше 1 000 000 000.")
        with self.transaction() as db:
            auction = self._entity(db, "auctions", auction_id)
            if auction["status"] != "open" or auction["ends_at"] <= self.now():
                raise RuleError("Приём ставок завершён.")
            required = auction["minimum"] if auction["highest_user"] is None else auction["highest_bid"] + auction["step"]
            if amount < required:
                raise RuleError(f"Минимальная следующая ставка: {required} ДКП.")
            state = self._balance(db, user_id, exclude_auction=auction_id)
            if amount > state["available"]:
                raise RuleError(f"На этот аукцион доступно {state['available']} ДКП с учётом других ставок.")
            ends_at = max(auction["ends_at"], self.now()+30)
            db.execute("UPDATE auctions SET highest_user=?,highest_bid=?,ends_at=?,revision=revision+1 WHERE id=?", (user_id, amount, ends_at, auction_id))
            db.execute("INSERT INTO bids(auction_id,user_id,amount,created_at) VALUES(?,?,?,?)", (auction_id, user_id, amount, self.now()))
            self._journal(db, "bid", user_id, auction_id=auction_id, amount=amount)

    def close_auction(self, auction_id):
        with self.transaction() as db:
            auction = self._entity(db, "auctions", auction_id)
            if auction["status"] != "open":
                return False
            if auction["ends_at"] > self.now():
                raise RuleError("Время аукциона ещё не истекло.")
            if auction["highest_user"] is not None:
                db.execute("INSERT INTO ledger(user_id,amount,reason,actor_id,op_key,created_at) VALUES(?,?,?,?,?,?)", (auction["highest_user"], -auction["highest_bid"], f"Аукцион #{auction_id}: {auction['item']}", 0, f"auction:{auction_id}", self.now()))
            db.execute("UPDATE auctions SET status='closed',revision=revision+1 WHERE id=?", (auction_id,))
            self._journal(db, "auction_closed", 0, auction_id=auction_id, item=auction["item"], user_id=auction["highest_user"], amount=auction["highest_bid"])
            return True

    def close_due(self):
        for row in self.rows("SELECT id FROM auctions WHERE status='open' AND ends_at<=?", (self.now(),)):
            try:
                self.close_auction(row["id"])
            except RuleError:
                pass  # A concurrent accepted bid may have extended this auction.

    def cancel_auction(self, auction_id, actor_id, reason):
        reason = clean(reason, 200, "Причина")
        with self.transaction() as db:
            auction = self._entity(db, "auctions", auction_id)
            if auction["status"] != "open" or auction["ends_at"] <= self.now():
                raise RuleError("Аукцион завершён: отмена недоступна.")
            db.execute("UPDATE auctions SET status='cancelled',revision=revision+1 WHERE id=?", (auction_id,))
            self._journal(db, "auction_cancelled", actor_id, auction_id=auction_id, reason=reason)

    def dirty(self, table):
        assert table in ("events", "auctions")
        return self.rows(f"SELECT * FROM {table} WHERE rendered_revision<revision ORDER BY id LIMIT 20")

    def rendered(self, table, entity_id, revision, message_id):
        assert table in ("events", "auctions")
        with self.transaction() as db:
            db.execute(f"UPDATE {table} SET rendered_revision=?,message_id=? WHERE id=?", (revision, message_id, entity_id))

    def pending_journal(self):
        return self.rows("SELECT j.* FROM journal j LEFT JOIN deliveries d ON d.journal_id=j.id WHERE d.journal_id IS NULL ORDER BY j.id LIMIT 30")

    def delivered(self, journal_id, message_id):
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO deliveries VALUES(?,?)", (journal_id, message_id))

    def history(self, user_id, page):
        return self.rows("SELECT * FROM ledger WHERE user_id=? ORDER BY id DESC LIMIT 10 OFFSET ?", (user_id, (page-1)*10))

    def journal(self, page):
        return self.rows("SELECT * FROM journal ORDER BY id DESC LIMIT 10 OFFSET ?", ((page-1)*10,))

    def backup(self, target):
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as source:
            dest = sqlite3.connect(target, timeout=5)
            try:
                deadline = time.monotonic() + 30
                def progress(status, remaining, total):
                    if time.monotonic() > deadline:
                        raise RuleError("Резервная копия не завершена: база занята слишком долго. Повторите позже.")
                source.backup(dest, pages=256, progress=progress, sleep=0.05)
            finally:
                # Connection.__exit__ commits/rolls back but does NOT close the file.
                dest.close()
        return target
