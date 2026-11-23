"""SQLite business operations for the desktop prototype."""

import csv
import secrets
import sqlite3
from collections import Counter
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock


PUBLIC_EVENT = (
    "(e.status = 'published' OR (e.status = 'pending' AND EXISTS ("
    "SELECT 1 FROM reviews r LEFT JOIN change_proposals p ON p.review_id = "
    "r.id "
    "LEFT JOIN closure_proposals c ON c.review_id = r.id "
    "WHERE r.event_id = e.id AND r.status = 'pending' AND (p.review_id IS NOT "
    "NULL OR c.review_id IS NOT NULL))))"
)

LEGACY_LOG_TARGETS = {
    "event": ("创建活动草稿", "提交发布审核", "提交变更审核", "审核通过", "审核驳回", "发布审核通过", "发布审核驳回",
              "变更审核通过", "变更审核驳回", "下架活动", "停用活动", "取消活动"),
    "session": ("添加场次", "编辑草稿场次", "检票授权"),
    "registration": ("报名出票", "登记线下缴费", "登记线下退款", "自助取消报名", "人工取消报名"),
    "waitlist": ("加入候补", "递补超时释放", "关闭无法满足的候补", "生成站内递补通知", "放弃候补", "确认递补"),
    "ticket": ("检票签到",),
}


class RuleError(ValueError):
    """A requested action violates a business rule."""


class SoldOut(RuleError):
    """The selected tickets cannot be issued in full."""


class InvalidTicketQuantity(RuleError):
    """A request exceeds a ticket type's total capacity."""


class AlreadyCheckedIn(RuleError):
    def __init__(self, checked_at):
        self.first_checked_at = checked_at.replace("T", " ")
        super().__init__(f"该票已签到，首次签到时间为 {self.first_checked_at}。")


@dataclass(frozen=True)
class TicketRequest:
    ticket_type_id: int
    holder_name: str


@dataclass(frozen=True)
class SessionDraft:
    venue: str
    starts_at: datetime
    capacity: int
    ticket_types: tuple[tuple[str, int, int], ...]
    ends_at: datetime | None = None


class Ticketing:
    def __init__(self, path, clock=datetime.now):
        self.path = path
        self.clock = clock
        self._write_lock = Lock()
        schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        with self.connection() as db:
            self.migrate_schema(db)
            db.executescript(schema)
            db.execute("BEGIN IMMEDIATE")
            if "ends_at" not in {row["name"] for row in db.execute("PRAGMA table_info(sessions)")}:
                db.execute("ALTER TABLE sessions ADD COLUMN ends_at TEXT")
            db.commit()

    @staticmethod
    def migrate_schema(db):
        """Upgrade the first prototype's database while keeping ticket references."""
        db.execute("PRAGMA foreign_keys = OFF")
        try:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            existing = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = "
                "'registrations'"
            ).fetchone()
            if version < 3 and existing:
                db.execute(
                    "CREATE TABLE registrations_v3 ("
                    "id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), "
                    "session_id INTEGER NOT NULL REFERENCES sessions(id), "
                    "status TEXT NOT NULL "
                    "CHECK (status IN ('registered', 'checked_in', 'absent', 'cancelled')), "
                    "UNIQUE (id, session_id))"
                )
                db.execute(
                    "INSERT INTO registrations_v3(id, user_id, session_id, "
                    "status) "
                    "SELECT id, user_id, session_id, status FROM registrations"
                )
                db.execute("DROP TABLE registrations")
                db.execute("ALTER TABLE registrations_v3 RENAME TO registrations")
                db.execute(
                    "CREATE UNIQUE INDEX one_active_registration "
                    "ON registrations(user_id, session_id) WHERE status <> "
                    "'cancelled'"
                )
                if db.execute("PRAGMA foreign_key_check").fetchone():
                    raise sqlite3.IntegrityError("数据库升级未通过外键检查。")
            if version == 0 and existing:
                db.execute("ALTER TABLE operation_logs ADD COLUMN reason TEXT NOT NULL DEFAULT ''")
            if version < 2 and existing:
                db.execute(
                    "CREATE TABLE events_v2 (id INTEGER PRIMARY KEY, organizer_id INTEGER "
                    "NOT NULL REFERENCES users(id), title TEXT NOT NULL, status TEXT NOT NULL "
                    "CHECK (status IN ('draft', 'pending', 'published', "
                    "'unlisted', 'disabled', 'cancelled')))"
                )
                db.execute(
                    "INSERT INTO events_v2 SELECT id, organizer_id, title, "
                    "status FROM events"
                )
                db.execute("DROP TABLE events")
                db.execute("ALTER TABLE events_v2 RENAME TO events")
                db.execute(
                    "ALTER TABLE reviews ADD COLUMN previous_status TEXT NOT NULL DEFAULT 'draft'"
                )
                if db.execute("PRAGMA foreign_key_check").fetchone():
                    raise sqlite3.IntegrityError("活动数据库升级未通过外键检查。")
            if version < 4 and existing:
                db.execute(
                    "ALTER TABLE operation_logs ADD COLUMN target_type "
                    "TEXT NOT NULL DEFAULT ''"
                )
                for target_type, actions in LEGACY_LOG_TARGETS.items():
                    placeholders = ",".join("?" for _ in actions)
                    db.execute(
                        "UPDATE operation_logs SET target_type = ? WHERE "
                        f"action IN ({placeholders})",
                        (target_type, *actions),
                    )
            if version < 5 and existing:
                db.execute("ALTER TABLE sessions ADD COLUMN cancelled_at TEXT")
                db.execute("ALTER TABLE ticket_types ADD COLUMN closed_at TEXT")
            if version < 5:
                db.execute("PRAGMA user_version = 5")
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.execute("PRAGMA foreign_keys = ON")

    @contextmanager
    def connection(self, *, write=False):
        # SQLite permits one writer at a time. Queue local writers before they open connections.
        with self._write_lock if write else nullcontext():
            db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            db.row_factory = sqlite3.Row
            try:
                db.execute("PRAGMA foreign_keys = ON")
                if write:
                    db.execute("BEGIN IMMEDIATE")
                yield db
                if write:
                    db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

    @staticmethod
    def require_role(db, user_id, role):
        user = db.execute(
            "SELECT role FROM users WHERE id = ?", (user_id,),
        ).fetchone()
        if user is None or user["role"] != role:
            raise RuleError("当前账号无权执行此操作。")

    def log(self, db, actor, action, target, reason="", *, target_type):
        db.execute(
            "INSERT INTO operation_logs(actor_id, action, target_id, "
            "target_type, created_at, reason) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (actor, action, target, target_type, self.clock().isoformat(timespec="seconds"), reason),
        )

    def seed_demo_users(self):
        users = [
            (1, "演示组织者", "organizer"),
            (2, "演示管理员", "admin"),
            (3, "演示参与者", "participant"),
            (4, "演示检票员", "checker"),
            (5, "第二位参与者", "participant"),
        ]
        with self.connection(write=True) as db:
            db.executemany(
                "INSERT OR IGNORE INTO users(id, name, role) VALUES (?, ?, "
                "?)", users
            )

    def users(self):
        with self.connection() as db:
            return db.execute("SELECT id, name, role FROM users ORDER BY id").fetchall()

    def create_event(
        self, actor, title, venue, starts_at, capacity, ticket_types, *,
        ends_at=None,
    ):
        """Create a draft and its first session."""
        draft = SessionDraft(
            venue, starts_at, capacity, tuple(ticket_types), ends_at,
        )
        event, (session,) = self.create_event_with_sessions(actor, title, (draft,))
        return event, session

    def create_event_with_sessions(
        self, actor, title, sessions, *, submit=False,
    ):
        title = title.strip()
        if not title:
            raise RuleError("请填写活动名称。")
        sessions = tuple(sessions)
        if not sessions:
            raise RuleError("请至少添加一个场次。")
        with self.connection(write=True) as db:
            self.require_role(db, actor, "organizer")
            event = db.execute(
                "INSERT INTO events(organizer_id, title, status) VALUES (?, "
                "?, 'draft')",
                (actor, title),
            ).lastrowid
            session_ids = tuple(self._insert_session(
                db, event, session,
            ) for session in sessions)
            self.log(db, actor, "创建活动草稿", event, target_type="event")
            if submit:
                self._queue_publish_review(db, actor, event, "draft")
        return event, session_ids

    def add_session(
        self, actor, event, venue, starts_at, capacity, ticket_types, *,
        ends_at=None,
    ):
        with self.connection(write=True) as db:
            self.require_role(db, actor, "organizer")
            draft = db.execute(
                "SELECT 1 FROM events WHERE id = ? AND organizer_id = ? AND "
                "status = 'draft'",
                (event, actor),
            ).fetchone()
            if draft is None:
                raise RuleError("只能为本人创建的活动草稿添加场次。")
            draft = SessionDraft(
                venue, starts_at, capacity, tuple(ticket_types), ends_at,
            )
            session = self._insert_session(db, event, draft)
            self.log(db, actor, "添加场次", session, target_type="session")
        return session

    def _owned_draft_session(self, db, actor, session):
        self.require_role(db, actor, "organizer")
        row = db.execute(
            "SELECT s.* FROM sessions s JOIN events e ON e.id = s.event_id "
            "WHERE s.id = ? AND e.organizer_id = ? AND e.status = "
            "'draft'", (session, actor),
        ).fetchone()
        if row is None:
            raise RuleError("只能编辑本人活动草稿的场次。")
        return row

    def draft_session(self, actor, session):
        with self.connection() as db:
            row = self._owned_draft_session(db, actor, session)
            types = tuple(tuple(ticket) for ticket in db.execute(
                "SELECT name, price_cents, capacity FROM ticket_types WHERE "
                "session_id = ? ORDER BY id", (session,),
            ))
            ends_at = datetime.fromisoformat(row["ends_at"]) if row["ends_at"] else None
            return SessionDraft(
                row["venue"], datetime.fromisoformat(row["starts_at"]),
                row["capacity"], types, ends_at,
            )

    def edit_draft_session(self, actor, session, draft):
        with self.connection(write=True) as db:
            self._owned_draft_session(db, actor, session)
            types = self.validate_session(draft)
            ends_at = draft.ends_at.isoformat(timespec="seconds") if draft.ends_at else None
            db.execute(
                "UPDATE sessions SET venue = ?, starts_at = ?, ends_at = ?, "
                "capacity = ? WHERE id = ?",
                (draft.venue.strip(), draft.starts_at.isoformat(timespec="seconds"), ends_at, draft.capacity, session),
            )
            db.execute(
                "DELETE FROM ticket_types WHERE session_id = ?", (session,),
            )
            self._insert_ticket_types(db, session, types)
            self.log(db, actor, "编辑草稿场次", session, target_type="session")

    def validate_session(self, draft):
        if not draft.venue.strip():
            raise RuleError("请填写场次地点。")
        if draft.starts_at <= self.clock():
            raise RuleError("场次开始时间必须晚于当前时间。")
        if draft.ends_at is not None and draft.ends_at <= draft.starts_at:
            raise RuleError("场次结束时间必须晚于开始时间。")
        types = [(name.strip(), price, quota) for name, price, quota in draft.ticket_types]
        if not types or any(
            not name or price < 0 or quota < 1 for name, price, quota in types
        ):
            raise RuleError("请填写有效的票种、价格和名额。")
        if len({name for name, _, _ in types}) != len(types):
            raise RuleError("同一场次的票种名称不能重复。")
        if draft.capacity < 1 or sum(
            quota for _, _, quota in types
        ) > draft.capacity:
            raise RuleError("票种名额合计不能超过场次容量。")
        return types

    def _insert_session(self, db, event, draft):
        types = self.validate_session(draft)
        ends_at = draft.ends_at.isoformat(timespec="seconds") if draft.ends_at else None
        session = db.execute(
            "INSERT INTO sessions(event_id, venue, starts_at, ends_at, "
            "capacity) "
            "VALUES (?, ?, ?, ?, ?)",
            (event, draft.venue.strip(), draft.starts_at.isoformat(timespec="seconds"), ends_at, draft.capacity),
        ).lastrowid
        self._insert_ticket_types(db, session, types)
        return session

    @staticmethod
    def _insert_ticket_types(db, session, types):
        db.executemany(
            "INSERT INTO ticket_types(session_id, name, price_cents, "
            "capacity) "
            "VALUES (?, ?, ?, ?)",
            ((session, name, price, quota) for name, price, quota in types),
        )

    def submit_review(self, actor, event_id):
        with self.connection(write=True) as db:
            self.require_role(db, actor, "organizer")
            event = db.execute(
                "SELECT status FROM events WHERE id = ? AND organizer_id = "
                "?", (event_id, actor),
            ).fetchone()
            if event is None or event["status"] not in ("draft", "unlisted"):
                raise RuleError("只能提交本人的草稿或已下架活动。")
            self._queue_publish_review(db, actor, event_id, event["status"])

    def _queue_publish_review(self, db, actor, event_id, previous_status):
        db.execute(
            "UPDATE events SET status = 'pending' WHERE id = ?", (event_id,),
        )
        db.execute(
            "INSERT INTO reviews(event_id, status, previous_status) VALUES "
            "(?, 'pending', ?)",
            (event_id, previous_status),
        )
        self.log(db, actor, "提交发布审核", event_id, target_type="event")

    def change_options(self, actor, session_id):
        with self.connection() as db:
            self.require_role(db, actor, "organizer")
            return db.execute(
                "SELECT tt.id, tt.name, tt.price_cents, tt.capacity, "
                "s.starts_at, "
                "s.capacity AS session_capacity FROM ticket_types tt "
                "JOIN sessions s ON s.id = tt.session_id JOIN events e ON "
                "e.id = s.event_id "
                "WHERE s.id = ? AND e.organizer_id = ? AND e.status = "
                "'published' "
                "AND s.cancelled_at IS NULL AND tt.closed_at IS NULL ORDER BY "
                "tt.id",
                (session_id, actor),
            ).fetchall()

    def submit_change(
        self, actor, session_id, ticket_type_id, price, quota, capacity,
        starts_at,
    ):
        with self.connection(write=True) as db:
            self.require_role(db, actor, "organizer")
            event = db.execute(
                "SELECT e.id FROM events e JOIN sessions s ON s.event_id = "
                "e.id "
                "WHERE s.id = ? AND e.organizer_id = ? AND e.status = "
                "'published'",
                (session_id, actor),
            ).fetchone()
            if event is None:
                raise RuleError("只能提交本人已发布且没有待审变更的活动。")
            review_id = db.execute(
                "INSERT INTO reviews(event_id, status) VALUES (?, "
                "'pending')", (event["id"],),
            ).lastrowid
            proposal = {
                "session_id": session_id, "ticket_type_id": ticket_type_id,
                "price_cents": price, "ticket_capacity": quota,
                "session_capacity": capacity,
                "starts_at": starts_at.isoformat(timespec="seconds"),
            }
            self.validate_change(db, proposal)
            db.execute(
                "INSERT INTO change_proposals(review_id, session_id, "
                "ticket_type_id, price_cents, "
                "ticket_capacity, session_capacity, starts_at) VALUES (?, ?, "
                "?, ?, ?, ?, ?)",
                (
                    review_id, session_id, ticket_type_id,
                    price, quota, capacity, proposal["starts_at"],
                ),
            )
            db.execute(
                "UPDATE events SET status = 'pending' WHERE id = ?",
                (event["id"],),
            )
            self.log(db, actor, "提交变更审核", event["id"], target_type="event")

    def closure_options(self, actor, session):
        with self.connection() as db:
            self._published_session_owner(db, actor, session)
            return db.execute(
                "SELECT id, name FROM ticket_types WHERE session_id = ? AND "
                "closed_at IS NULL ORDER BY id", (session,),
            ).fetchall()

    def _published_session_owner(self, db, actor, session):
        self.require_role(db, actor, "organizer")
        row = db.execute(
            "SELECT s.event_id FROM sessions s JOIN events e ON e.id = "
            "s.event_id "
            "WHERE s.id = ? AND s.cancelled_at IS NULL AND e.organizer_id = ? "
            "AND e.status = 'published'",
            (session, actor),
        ).fetchone()
        if row is None:
            raise RuleError("只能为本人已发布、仍有效且没有待审申请的场次提交变更。")
        return row["event_id"]

    def submit_closure(self, actor, session, *, ticket_type=None):
        with self.connection(write=True) as db:
            event = self._published_session_owner(db, actor, session)
            self.validate_closure(db, session, ticket_type)
            review = db.execute(
                "INSERT INTO reviews(event_id, status) VALUES (?, "
                "'pending')", (event,),
            ).lastrowid
            db.execute(
                "INSERT INTO closure_proposals(review_id, session_id, "
                "ticket_type_id) VALUES (?, ?, ?)",
                (review, session, ticket_type),
            )
            db.execute(
                "UPDATE events SET status = 'pending' WHERE id = ?", (event,),
            )
            action, target, kind = self.closure_target(session, ticket_type)
            self.log(db, actor, f"提交{action}审核", target, target_type=kind)

    @staticmethod
    def closure_target(session, ticket_type):
        if ticket_type is None:
            return "取消场次", session, "session"
        return "关闭票种", ticket_type, "ticket_type"

    def validate_closure(self, db, session, ticket_type):
        row = db.execute(
            "SELECT s.event_id, s.starts_at, s.venue, e.title FROM sessions s "
            "JOIN events e ON e.id = s.event_id "
            "WHERE s.id = ? AND s.cancelled_at IS NULL", (session,),
        ).fetchone()
        if row is None:
            raise RuleError("该场次已取消或不存在。")
        if ticket_type is None:
            if datetime.fromisoformat(row["starts_at"]) <= self.clock():
                raise RuleError("场次已开始，不能取消场次。")
        elif db.execute(
            "SELECT 1 FROM ticket_types WHERE id = ? AND session_id = ? AND "
            "closed_at IS NULL",
            (ticket_type, session),
        ).fetchone() is None:
            raise RuleError("该票种已关闭或不属于所选场次。")
        elif db.execute(
            "SELECT 1 FROM tickets t JOIN registrations r ON r.id = "
            "t.registration_id "
            "WHERE t.ticket_type_id = ? AND r.status <> "
            "'cancelled'", (ticket_type,),
        ).fetchone():
            raise RuleError("该票种已有有效电子票，不能关闭。需要调整名额时请提交场次及票价变更审核。")
        return row

    def validate_change(self, db, proposal):
        session_id = proposal["session_id"]
        session = db.execute(
            "SELECT starts_at, ends_at, capacity FROM sessions WHERE id = ? "
            "AND cancelled_at IS NULL", (session_id,),
        ).fetchone()
        if session is None:
            raise RuleError("该场次已取消或不存在。")
        old_start = datetime.fromisoformat(session["starts_at"])
        starts = datetime.fromisoformat(proposal["starts_at"])
        now = self.clock()
        if min(starts, old_start) <= now:
            raise RuleError("只能变更未开始的场次，新开始时间必须晚于当前时间。")
        types = {
            row["id"]: row for row in db.execute(
                self.stock_query(protect_queue=True)
                + "WHERE tt.session_id = ? AND tt.closed_at IS NULL",
                (self.queue_cutoff(now), session_id),
            )
        }
        old = types.get(proposal["ticket_type_id"])
        if old is None:
            raise RuleError("票种不属于所选场次。")
        quota, capacity = proposal["ticket_capacity"], proposal["session_capacity"]
        if proposal["price_cents"] < 0 or quota < 1 or capacity < 1:
            raise RuleError("价格不得为负数，容量至少为 1。")
        if sum(row["capacity"] for row in types.values()) - old["capacity"] + quota > capacity:
            raise RuleError("票种容量之和不能超过场次容量。")
        if old["capacity"] - old["available"] > quota:
            raise RuleError("票种新容量低于已出票与预留的名额。")
        if quota < old["capacity"] and db.execute(
            "SELECT 1 FROM waitlist_items i JOIN waitlist w ON w.id = "
            "i.waitlist_id "
            "WHERE i.ticket_type_id = ? AND w.status = 'queued' "
            "GROUP BY w.id HAVING COUNT(*) > ?", (old["id"], quota),
        ).fetchone():
            raise RuleError("票种新容量不足以满足已有候补的单笔需求。")
        cutoff = (starts - timedelta(hours=2)).isoformat()
        if db.execute(
            "SELECT 1 FROM waitlist WHERE session_id = ? AND status = "
            "'offered' "
            "AND deadline > ? AND deadline > ?",
            (session_id, now.isoformat(), cutoff),
        ).fetchone():
            raise RuleError("新时间会缩短仍有效的递补确认期限。")
        ends_at = session["ends_at"]
        if ends_at:
            duration = datetime.fromisoformat(ends_at) - old_start
            ends_at = (starts + duration).isoformat(timespec="seconds")
        return quota < old["capacity"] or capacity < session["capacity"], ends_at

    def pending_review(self, actor, event_id):
        with self.connection() as db:
            self.require_role(db, actor, "admin")
            row = db.execute(
                "SELECT r.id AS review_id, p.*, tt.name, tt.price_cents AS "
                "old_price, "
                "tt.capacity AS old_quota, s.capacity AS old_capacity, s.starts_at AS old_start, "
                "c.session_id AS closure_session, c.ticket_type_id AS closure_ticket, "
                "cs.venue AS closure_venue, cs.starts_at AS closure_start, "
                "ct.name AS closure_name "
                "FROM reviews r LEFT JOIN change_proposals p ON p.review_id = "
                "r.id "
                "LEFT JOIN sessions s ON s.id = p.session_id "
                "LEFT JOIN ticket_types tt ON tt.id = p.ticket_type_id "
                "LEFT JOIN closure_proposals c ON c.review_id = r.id "
                "LEFT JOIN sessions cs ON cs.id = c.session_id LEFT JOIN "
                "ticket_types ct ON ct.id = c.ticket_type_id "
                "WHERE r.event_id = ? AND r.status = 'pending'", (event_id,),
            ).fetchone()
            if row is None:
                raise RuleError("该活动没有待审核的申请。")
            return row

    def review(
        self, actor, event_id, approved, reason="", *, confirm_reduction=False,
        review_id=None,
    ):
        if not approved and not reason.strip():
            raise RuleError("驳回时需要填写原因。")
        with self.connection(write=True) as db:
            self.require_role(db, actor, "admin")
            request = db.execute(
                "SELECT r.id AS request_id, r.previous_status, p.*, "
                "c.session_id AS closure_session, c.ticket_type_id AS closure_ticket "
                "FROM reviews r LEFT JOIN change_proposals p ON p.review_id = "
                "r.id "
                "LEFT JOIN closure_proposals c ON c.review_id = r.id "
                "WHERE r.event_id = ? AND r.status = 'pending'",
                (event_id,),
            ).fetchone()
            if request is None or (review_id is not None and review_id != request["request_id"]):
                raise RuleError("待审申请已变动，请刷新后重新审核。")
            closure_session = request["closure_session"]
            closure_ticket = request["closure_ticket"]
            is_change = request["session_id"] is not None or closure_session is not None
            if approved and closure_session is not None:
                self.apply_closure(db, closure_session, closure_ticket)
            elif approved and is_change:
                session = request["session_id"]
                now = self.clock()
                old_deadline = self.booking_deadline(db, session)
                self.advance_waitlist(
                    db, session, now, old_deadline, offer_new=False,
                )
                reduction, ends_at = self.validate_change(db, request)
                if reduction and not confirm_reduction:
                    raise RuleError("缩减容量前需要确认变更。")
                db.execute(
                    "UPDATE sessions SET starts_at = ?, ends_at = ?, capacity "
                    "= ? WHERE id = ?",
                    (request["starts_at"], ends_at, request["session_capacity"], session),
                )
                db.execute(
                    "UPDATE ticket_types SET price_cents = ?, capacity = ? "
                    "WHERE id = ?",
                    (request["price_cents"], request["ticket_capacity"], request["ticket_type_id"]),
                )
            db.execute(
                "UPDATE reviews SET status = ?, reviewer_id = ?, reason = ? "
                "WHERE id = ?",
                (
                    "approved" if approved else "rejected",
                    actor, reason.strip(), request["request_id"],
                ),
            )
            status = "published" if approved or is_change else request["previous_status"]
            db.execute(
                "UPDATE events SET status = ? WHERE id = ?",
                (status, event_id),
            )
            if closure_session is not None:
                action, target, kind = self.closure_target(
                    closure_session, closure_ticket,
                )
            else:
                action, target, kind = ("变更" if is_change else "发布"), event_id, "event"
            result = "审核通过" if approved else "审核驳回"
            self.log(
                db, actor, action + result, target, reason.strip(),
                target_type=kind,
            )
            resume_session = closure_session if closure_ticket is not None else request["session_id"]
            if approved and resume_session is not None:
                self.advance_waitlist(
                    db, resume_session, self.clock(),
                    self.booking_deadline(db, resume_session),
                )

    def apply_closure(self, db, session, ticket_type):
        row = self.validate_closure(db, session, ticket_type)
        timestamp = self.clock().isoformat(timespec="seconds")
        label = f"「{row['title']}」{row['starts_at'].replace('T', ' ')} · {row['venue']}"
        if ticket_type is None:
            db.execute(
                "UPDATE sessions SET cancelled_at = ? WHERE id = ?",
                (timestamp, session),
            )
            self.cancel_event_bookings(
                db, row["event_id"], label, session=session,
            )
            return
        ticket = db.execute(
            "SELECT name FROM ticket_types WHERE id = ?", (ticket_type,),
        ).fetchone()
        db.execute(
            "UPDATE ticket_types SET closed_at = ? WHERE id = ?",
            (timestamp, ticket_type),
        )
        affected = (
            "WHERE status IN ('queued', 'offered') AND id IN ("
            "SELECT waitlist_id FROM waitlist_items WHERE ticket_type_id = ?)"
        )
        users = db.execute(
            f"SELECT user_id FROM waitlist {affected}", (ticket_type,),
        ).fetchall()
        db.execute(
            f"UPDATE waitlist SET status = 'closed', deadline = NULL {affected}",
            (ticket_type,),
        )
        self.notify_participants(
            db, users, row["event_id"],
            f"{label}的{ticket['name']}已关闭，相关候补已取消。",
        )

    def set_event_status(self, actor, event_id, status, reason=""):
        actions = {"unlisted": "下架活动", "disabled": "停用活动", "cancelled": "取消活动"}
        if status not in actions or not reason.strip():
            raise RuleError("请选择活动操作并填写原因。")
        with self.connection(write=True) as db:
            event = db.execute(
                f"SELECT e.organizer_id, e.title, {PUBLIC_EVENT} AS "
                "is_public, "
                "u.role FROM events e JOIN users u ON u.id = ? WHERE e.id = ?",
                (actor, event_id),
            ).fetchone()
            if event is None or not event["is_public"]:
                raise RuleError("只能操作仍对外开放的活动。")
            owner = event["role"] == "organizer" and event["organizer_id"] == actor
            admin = event["role"] == "admin"
            if not ({"unlisted": owner, "disabled": admin, "cancelled": owner or admin}[status]):
                raise RuleError("当前账号无权执行此操作。")
            db.execute(
                "UPDATE events SET status = ? WHERE id = ?",
                (status, event_id),
            )
            db.execute(
                "UPDATE reviews SET status = 'rejected', reason = ? "
                "WHERE event_id = ? AND status = 'pending'",
                (f"活动已执行{actions[status]}，申请关闭。", event_id),
            )
            if status == "cancelled":
                self.mark_absences(db, self.clock())
                self.cancel_event_bookings(db, event_id, event["title"])
            self.log(
                db, actor, actions[status], event_id, reason.strip(),
                target_type="event",
            )

    def cancel_event_bookings(self, db, event_id, title, *, session=None):
        session_ids = "SELECT id FROM sessions WHERE event_id = ?"
        parameters = (event_id,)
        if session is not None:
            session_ids += " AND id = ?"
            parameters += (session,)
        users = db.execute(
            "SELECT user_id FROM registrations WHERE session_id IN "
            f"({session_ids}) "
            "AND status IN ('registered', 'checked_in') UNION "
            "SELECT user_id FROM waitlist WHERE session_id IN "
            f"({session_ids}) "
            "AND status IN ('queued', 'offered')", parameters * 2,
        ).fetchall()
        db.execute(
            "UPDATE registrations SET status = 'cancelled' WHERE session_id "
            f"IN ({session_ids}) "
            "AND status = 'registered'", parameters,
        )
        db.execute(
            f"UPDATE waitlist SET status = 'closed', deadline = NULL "
            f"WHERE session_id IN ({session_ids}) AND status IN ('queued', "
            "'offered')",
            parameters,
        )
        label = f"「{title}」" if session is None else f"{title}场次"
        self.notify_participants(
            db, users, event_id, f"{label}已取消。已缴费的用户请联系组织者处理退款。",
        )

    def notify_participants(self, db, users, event_id, message):
        db.executemany(
            "INSERT INTO participant_notices(user_id, event_id, message, "
            "created_at) "
            "VALUES (?, ?, ?, ?)",
            [(row["user_id"], event_id, message, self.clock().isoformat(timespec="seconds")) for row in users],
        )

    def my_notices(self, actor):
        with self.connection() as db:
            self.require_role(db, actor, "participant")
            return db.execute(
                "SELECT created_at, message FROM participant_notices WHERE "
                "user_id = ? "
                "ORDER BY id DESC", (actor,),
            ).fetchall()

    def list_sessions(
        self, actor, *, keyword="", venue="", date="", ticket="",
        available=False,
    ):
        if date:
            date = datetime.strptime(date, "%Y-%m-%d").date().isoformat()
        if available:
            self.process_waitlists()
        with self.connection() as db:
            user = db.execute(
                "SELECT role FROM users WHERE id = ?", (actor,),
            ).fetchone()
            if user is None:
                raise RuleError("账号不存在。")
            filters = {
                "organizer": ("e.organizer_id = ?", (actor,)),
                "admin": ("1 = 1", ()),
                "participant": (f"{PUBLIC_EVENT} AND s.cancelled_at IS NULL AND s.starts_at > ?", (
                    self.clock().isoformat(timespec="seconds"),
                )),
                "checker": (
                    f"{PUBLIC_EVENT} AND s.cancelled_at IS NULL AND EXISTS "
                    "(SELECT 1 FROM checker_assignments a "
                    "WHERE a.session_id = s.id AND a.checker_id = "
                    "?)", (actor,),
                ),
            }
            condition, parameters = filters[user["role"]]
            if any((venue, date, ticket, available)) and user["role"] != "participant":
                raise RuleError("组合筛选仅用于参与者查询。")
            if ticket or available:
                condition += (
                    " AND EXISTS (SELECT 1 FROM (" + self.stock_query(protect_queue=True) +
                    "WHERE tt.session_id = s.id AND tt.closed_at IS NULL) AS "
                    "stock "
                    "WHERE (? = '' OR instr(stock.name, ?) > 0) AND (? = 0 OR "
                    "stock.available > 0))"
                )
                parameters += (self.queue_cutoff(self.clock()), ticket, ticket, available)
            if available:
                condition += " AND s.starts_at > ?"
                parameters += ((self.clock() + timedelta(hours=2)).isoformat(timespec="seconds"),)
            return db.execute(
                "SELECT e.id AS event_id, e.title, e.status, s.id AS "
                "session_id, "
                f"{PUBLIC_EVENT} AS is_public, "
                "s.venue, s.starts_at, s.ends_at, s.cancelled_at, "
                "(SELECT COUNT(*) FROM ticket_types tt WHERE tt.session_id = "
                "s.id AND tt.closed_at IS NULL) "
                "AS open_ticket_types, "
                "(SELECT reason FROM reviews r WHERE r.event_id = e.id "
                "ORDER BY r.id DESC LIMIT 1) AS review_reason "
                "FROM events e JOIN sessions s ON s.event_id = e.id "
                f"WHERE ({condition}) AND (? = '' OR instr(e.title, ?) > 0) "
                "AND (? = '' OR instr(s.venue, ?) > 0) "
                "AND (? = '' OR substr(s.starts_at, 1, 10) = ?) "
                "ORDER BY s.starts_at, s.id",
                (*parameters, keyword, keyword, venue, venue, date, date),
            ).fetchall()

    def participant_session_stock(self, actor, event):
        self.process_waitlists()
        with self.connection() as db:
            self.require_role(db, actor, "participant")
            rows = db.execute(
                "SELECT session_id, SUM(available) AS available FROM "
                "(" + self.stock_query(protect_queue=True) +
                "JOIN sessions s ON s.id = tt.session_id JOIN events e ON "
                "e.id = s.event_id "
                f"WHERE s.event_id = ? AND {PUBLIC_EVENT} AND s.cancelled_at "
                "IS NULL "
                "AND tt.closed_at IS NULL) GROUP BY session_id",
                (self.queue_cutoff(self.clock()), event),
            )
            return {row["session_id"]: row["available"] for row in rows}

    def ticket_types(self, actor, session):
        self.process_waitlists()
        with self.connection() as db:
            self.require_role(db, actor, "participant")
            return db.execute(
                self.stock_query(protect_queue=True) +
                "JOIN sessions s ON s.id = tt.session_id JOIN events e ON "
                "e.id = s.event_id "
                f"WHERE tt.session_id = ? AND {PUBLIC_EVENT} AND "
                "s.cancelled_at IS NULL "
                "AND tt.closed_at IS NULL ORDER BY tt.id",
                (self.queue_cutoff(self.clock()), session),
            ).fetchall()

    @staticmethod
    def queue_cutoff(now):
        return (now + timedelta(hours=2, minutes=30)).isoformat()

    @staticmethod
    def stock_query(*, protect_queue=False):
        queued_demand = ""
        if protect_queue:
            queued_demand = (
                "- (SELECT COUNT(*) FROM waitlist_items wi WHERE "
                "wi.ticket_type_id = tt.id "
                "AND wi.waitlist_id = (SELECT w.id FROM waitlist w "
                "JOIN sessions qs ON qs.id = w.session_id WHERE w.session_id "
                "= tt.session_id "
                "AND w.status = 'queued' AND qs.starts_at > ? ORDER BY "
                "w.position LIMIT 1)) "
            )
        return (
            "SELECT tt.id, tt.session_id, tt.name, tt.price_cents, "
            "tt.capacity, tt.closed_at, MAX(0, tt.capacity "
            "- (SELECT COUNT(*) FROM tickets t JOIN registrations r ON r.id = "
            "t.registration_id "
            "WHERE t.ticket_type_id = tt.id AND r.status <> 'cancelled') "
            "- (SELECT COUNT(*) FROM waitlist_items wi JOIN waitlist w ON "
            "w.id = wi.waitlist_id "
            "WHERE wi.ticket_type_id = tt.id AND w.status = 'offered') "
            f"{queued_demand}) AS available FROM ticket_types tt "
        )

    def stock(self, db, session_id, requests, *, protect_queue=False):
        quantities = Counter(item.ticket_type_id for item in requests)
        parameters = (self.queue_cutoff(self.clock()), session_id) if protect_queue else (session_id,)
        stocks = {
            row["id"]: row for row in db.execute(
                self.stock_query(protect_queue=protect_queue) + "WHERE tt.session_id = ?",
                parameters,
            )
        }
        if quantities.keys() - stocks.keys():
            raise RuleError("票种不属于所选场次。")
        if any(stocks[kind]["closed_at"] is not None for kind in quantities):
            raise RuleError("所选票种已关闭，请重新选择。")
        if any(
            qty > stocks[kind]["capacity"] for kind, qty in quantities.items()
        ):
            raise InvalidTicketQuantity("所选票数超过对应票种的总名额。")
        prices = {kind: stocks[kind]["price_cents"] for kind in quantities}
        fits = all(
            stocks[kind]["available"] >= qty for kind,
            qty in quantities.items()
        )
        return fits, prices

    @staticmethod
    def validate_requests(requests):
        requests = tuple(requests)
        if not 1 <= len(requests) <= 2:
            raise RuleError("一次只能报名一至两张票。")
        if any(not item.holder_name.strip() for item in requests):
            raise RuleError("请填写每张票的持票人。")
        return requests

    def booking_deadline(self, db, session_id):
        session = db.execute(
            "SELECT s.starts_at FROM sessions s "
            "JOIN events e ON s.event_id = e.id WHERE s.id = ? AND "
            f"{PUBLIC_EVENT} AND s.cancelled_at IS NULL",
            (session_id,),
        ).fetchone()
        if session is None:
            raise RuleError("该场次尚未开放报名。")
        return datetime.fromisoformat(session["starts_at"]) - timedelta(hours=2)

    @staticmethod
    def require_no_registration(db, actor, session_id):
        if db.execute(
            "SELECT 1 FROM registrations WHERE user_id = ? AND session_id = ? "
            "AND status <> 'cancelled'", (actor, session_id),
        ).fetchone():
            raise RuleError("同一账号只能对该场次保留一条有效报名。")

    def issue(self, db, actor, session, requests, prices):
        order = db.execute(
            "INSERT INTO registrations(user_id, session_id, status) VALUES "
            "(?, ?, 'registered')",
            (actor, session),
        ).lastrowid
        codes = []
        for item in requests:
            code = secrets.token_hex(12)
            db.execute(
                "INSERT INTO tickets(registration_id, session_id, "
                "ticket_type_id, "
                "holder_name, price_cents, code) VALUES (?, ?, ?, ?, ?, ?)",
                (order, session, item.ticket_type_id, item.holder_name.strip(),
                 prices[item.ticket_type_id], code),
            )
            codes.append(code)
        self.log(db, actor, "报名出票", order, target_type="registration")
        return codes

    @staticmethod
    def require_current_prices(prices, expected_prices):
        if expected_prices is not None and any(
            expected_prices.get(kind) != price for kind,
            price in prices.items()
        ):
            raise RuleError("票价已更新，请重新打开报名窗口核对价格后再提交。")

    def register(self, actor, session_id, requests, *, expected_prices=None):
        requests = self.validate_requests(requests)
        self.process_waitlists()
        with self.connection(write=True) as db:
            self.require_role(db, actor, "participant")
            if self.clock() >= self.booking_deadline(db, session_id):
                raise RuleError("报名已截止。报名在开场前两小时关闭。")
            self.require_no_registration(db, actor, session_id)
            if db.execute(
                "SELECT 1 FROM waitlist WHERE user_id = ? AND session_id = ? "
                "AND status IN ('queued', 'offered')",
                (actor, session_id),
            ).fetchone():
                raise RuleError("已有候补记录，请打开「我的候补」。")
            fits, prices = self.stock(
                db, session_id, requests, protect_queue=True,
            )
            self.require_current_prices(prices, expected_prices)
            if not fits:
                raise SoldOut("名额不足，整单未出票。")
            return self.issue(db, actor, session_id, requests, prices)

    @staticmethod
    def next_position(db, session):
        return db.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 FROM waitlist WHERE "
            "session_id = ?",
            (session,),
        ).fetchone()[0]

    @staticmethod
    def waitlist_requests(db, entry):
        return [
            TicketRequest(row["ticket_type_id"], row["holder_name"])
            for row in db.execute(
                "SELECT ticket_type_id, holder_name FROM waitlist_items "
                "WHERE waitlist_id = ? ORDER BY id", (entry,),
            )
        ]

    def join_waitlist(
        self, actor, session, requests, *, accept_no_offer=False,
        expected_prices=None,
    ):
        requests = self.validate_requests(requests)
        with self.connection(write=True) as db:
            self.require_role(db, actor, "participant")
            self.require_no_registration(db, actor, session)
            deadline = self.booking_deadline(db, session)
            now = self.clock()
            if now >= deadline:
                raise RuleError("候补已截止。")
            existing = db.execute(
                "SELECT id FROM waitlist WHERE user_id = ? AND session_id = ? "
                "AND status IN ('queued', 'offered')", (actor, session),
            ).fetchone()
            if existing:
                return existing["id"]
            if now >= deadline - timedelta(minutes=30) and not accept_no_offer:
                raise RuleError("当前时段不会发送新递补通知。请先确认候补风险。")
            fits, prices = self.stock(
                db, session, requests, protect_queue=True,
            )
            self.require_current_prices(prices, expected_prices)
            if fits:
                raise RuleError("名额已恢复，请重新提交报名。")
            entry = db.execute(
                "INSERT INTO waitlist(user_id, session_id, position, status) "
                "VALUES (?, ?, ?, 'queued')",
                (actor, session, self.next_position(db, session)),
            ).lastrowid
            db.executemany(
                "INSERT INTO waitlist_items(waitlist_id, session_id, "
                "ticket_type_id, holder_name) "
                "VALUES (?, ?, ?, ?)",
                [
                    (entry, session, item.ticket_type_id, item.holder_name.strip())
                    for item in requests
                ],
            )
            self.log(db, actor, "加入候补", entry, target_type="waitlist")
        return entry

    def advance_waitlist(self, db, session, now, deadline, *, offer_new=True):
        entries = db.execute(
            "SELECT id, user_id, status, deadline FROM waitlist WHERE "
            "session_id = ? "
            "AND status IN ('queued', 'offered') ORDER BY "
            "position", (session,),
        ).fetchall()
        expired = set()
        for row in entries:
            if now >= deadline:
                db.execute(
                    "UPDATE waitlist SET status = 'closed' WHERE id = ?",
                    (row["id"],),
                )
            elif row["status"] == "offered" and now >= datetime.fromisoformat(row["deadline"]):
                expired.add(row["id"])
                db.execute(
                    "UPDATE waitlist SET status = 'queued', deadline = NULL, "
                    "position = ? "
                    "WHERE id = ?",
                    (self.next_position(db, session), row["id"]),
                )
                self.log(
                    db, row["user_id"], "递补超时释放", row["id"],
                    target_type="waitlist",
                )
        if not offer_new or now >= deadline - timedelta(minutes=30):
            return
        queue = db.execute(
            "SELECT id, user_id FROM waitlist WHERE session_id = ? AND status "
            "= 'queued' "
            "ORDER BY position", (session,),
        ).fetchall()
        for row in queue:
            if row["id"] in expired:
                break
            try:
                fits, _ = self.stock(
                    db, session, self.waitlist_requests(db, row["id"]),
                )
            except InvalidTicketQuantity:
                db.execute(
                    "UPDATE waitlist SET status = 'closed' WHERE id = ?",
                    (row["id"],),
                )
                self.log(
                    db, row["user_id"], "关闭无法满足的候补", row["id"],
                    target_type="waitlist",
                )
                continue
            if not fits:
                break
            db.execute(
                "UPDATE waitlist SET status = 'offered', deadline = ? WHERE "
                "id = ?",
                ((now + timedelta(minutes=30)).isoformat(), row["id"]),
            )
            self.log(
                db, row["user_id"], "生成站内递补通知", row["id"],
                target_type="waitlist",
            )

    def process_waitlists(self):
        with self.connection(write=True) as db:
            active = db.execute(
                "SELECT DISTINCT s.id, s.starts_at, "
                f"{PUBLIC_EVENT} AS is_public FROM waitlist w "
                "JOIN sessions s ON s.id = w.session_id JOIN events e ON e.id "
                "= s.event_id "
                "WHERE w.status IN ('queued', 'offered')"
            ).fetchall()
            now = self.clock()
            for row in active:
                deadline = datetime.fromisoformat(row["starts_at"]) - timedelta(hours=2)
                self.advance_waitlist(
                    db, row["id"], now, deadline, offer_new=row["is_public"],
                )
            self.mark_absences(db, now)

    @staticmethod
    def mark_absences(db, now):
        db.execute(
            "UPDATE registrations SET status = 'absent' WHERE status = "
            "'registered' "
            "AND session_id IN (SELECT id FROM sessions WHERE ends_at <= ?) "
            "AND NOT EXISTS (SELECT 1 FROM tickets t WHERE t.registration_id "
            "= registrations.id "
            "AND t.checked_at IS NOT NULL)",
            (now.isoformat(timespec="seconds"),),
        )

    def my_waitlist(self, actor):
        self.process_waitlists()
        with self.connection() as db:
            self.require_role(db, actor, "participant")
            return db.execute(
                "SELECT w.id, e.title, s.starts_at, s.venue, w.status, "
                "w.deadline, "
                "(SELECT GROUP_CONCAT(tt.name, '、') FROM waitlist_items i "
                "JOIN ticket_types tt ON tt.id = i.ticket_type_id WHERE "
                "i.waitlist_id = w.id) "
                "AS types, (SELECT SUM(tt.price_cents) FROM waitlist_items i "
                "JOIN ticket_types tt ON tt.id = i.ticket_type_id WHERE "
                "i.waitlist_id = w.id) AS amount "
                "FROM waitlist w JOIN sessions s ON s.id = w.session_id "
                "JOIN events e ON e.id = s.event_id WHERE w.user_id = ? ORDER "
                "BY w.id DESC",
                (actor,),
            ).fetchall()

    def respond_to_offer(
        self, actor, entry, *, accept, expected_deadline=None,
        expected_amount=None,
    ):
        with self.connection(write=True) as db:
            self.require_role(db, actor, "participant")
            row = db.execute(
                "SELECT session_id, status, deadline FROM waitlist WHERE id = "
                "? AND user_id = ?",
                (entry, actor),
            ).fetchone()
            if row is None or row["status"] not in ("queued", "offered"):
                raise RuleError("找不到可操作的候补记录。")
            session = row["session_id"]
            deadline = self.booking_deadline(db, session)
            now = self.clock()
            if not accept:
                db.execute(
                    "UPDATE waitlist SET status = 'closed' WHERE id = ?",
                    (entry,),
                )
                self.log(db, actor, "放弃候补", entry, target_type="waitlist")
                self.advance_waitlist(db, session, now, deadline)
                return []
            if row["status"] != "offered" or row["deadline"] != expected_deadline or now >= min(
                datetime.fromisoformat(row["deadline"]), deadline,
            ):
                raise RuleError("当前没有可确认的递补名额。")
            self.require_no_registration(db, actor, session)
            requests = self.waitlist_requests(db, entry)
            db.execute(
                "UPDATE waitlist SET status = 'confirmed' WHERE id = ?",
                (entry,),
            )
            fits, prices = self.stock(db, session, requests)
            if not fits:
                raise RuleError("预留名额异常，未生成电子票。")
            if (
                expected_amount is not None
                and sum(prices[item.ticket_type_id] for item in requests)
                != expected_amount
            ):
                raise RuleError("票价已更新，请刷新候补列表，核对金额后再确认递补。")
            codes = self.issue(db, actor, session, requests, prices)
            self.log(db, actor, "确认递补", entry, target_type="waitlist")
            return codes

    def my_tickets(self, actor):
        with self.connection(write=True) as db:
            self.require_role(db, actor, "participant")
            self.mark_absences(db, self.clock())
            return db.execute(
                "SELECT e.title, e.status AS event_status, s.starts_at, "
                "s.ends_at, s.cancelled_at, s.venue, "
                "tt.name, t.holder_name, t.price_cents, t.code, "
                "t.checked_at, r.id AS registration_id, r.status AS registration_status, "
                "p.paid_at, p.refunded_at, SUM(t.price_cents) OVER (PARTITION BY r.id) AS amount "
                "FROM tickets t JOIN registrations r ON r.id = "
                "t.registration_id "
                "JOIN sessions s ON s.id = t.session_id JOIN events e ON e.id "
                "= s.event_id "
                "JOIN ticket_types tt ON tt.id = t.ticket_type_id "
                "LEFT JOIN payments p ON p.registration_id = r.id "
                "WHERE r.user_id = ? ORDER BY t.id DESC", (actor,),
            ).fetchall()

    def session_report(self, actor, session):
        """Return an organizer's per-ticket roster and capacity snapshot."""
        with self.connection(write=True) as db:
            self.require_role(db, actor, "organizer")
            details = db.execute(
                "SELECT e.title, s.starts_at, s.ends_at, s.venue, "
                "s.cancelled_at FROM sessions s "
                "JOIN events e ON e.id = s.event_id "
                f"WHERE s.id = ? AND e.organizer_id = ? AND {PUBLIC_EVENT}",
                (session, actor),
            ).fetchone()
            if details is None:
                raise RuleError("只能查看本人已发布场次的统计及报名名单。")
            now = self.clock()
            self.mark_absences(db, now)
            end = details["ends_at"]
            cancelled = details["cancelled_at"] is not None
            finished = not cancelled and end is not None and now >= datetime.fromisoformat(end)
            if not cancelled:
                deadline = datetime.fromisoformat(details["starts_at"]) - timedelta(hours=2)
                self.advance_waitlist(db, session, now, deadline)
            types = db.execute(
                self.stock_query(protect_queue=True) + "WHERE tt.session_id = ? ORDER BY tt.id",
                (self.queue_cutoff(now), session),
            ).fetchall()
            roster = db.execute(
                "SELECT r.id AS registration_id, u.name AS registrant, tt.id "
                "AS ticket_type_id, "
                "tt.name AS ticket_type, t.holder_name, t.price_cents, t.checked_at "
                "FROM tickets t JOIN registrations r ON r.id = "
                "t.registration_id "
                "JOIN users u ON u.id = r.user_id JOIN ticket_types tt ON "
                "tt.id = t.ticket_type_id "
                "WHERE t.session_id = ? AND r.status <> 'cancelled' AND (? = "
                "0 OR t.checked_at IS NOT NULL) "
                "ORDER BY r.id, t.id", (session, cancelled),
            ).fetchall()
            issued = Counter(row["ticket_type_id"] for row in roster)
            checked = Counter(row["ticket_type_id"] for row in roster if row["checked_at"])
            statistics = []
            for row in types:
                count = issued[row["id"]]
                attendance = checked[row["id"]]
                closed = row["closed_at"] is not None
                statistics.append({
                    "name": row["name"],
                    "closed": closed,
                    "capacity": row["capacity"],
                    "issued": count,
                    "reserved": 0 if cancelled or closed else row["capacity"] - row["available"] - count,
                    "available": 0 if cancelled or closed else row["available"],
                    "checked": attendance,
                    "absent": count - attendance if finished else None,
                })
            return details, statistics, roster, finished

    def export_roster(self, actor, session, path):
        _, _, roster, finished = self.session_report(actor, session)
        with Path(path).open("w", encoding="utf-8-sig", newline="") as target:
            writer = csv.writer(target)
            writer.writerow(("报名编号", "报名人", "脱敏手机号", "持票人", "票种", "票价（元）", "状态"))
            for row in roster:
                status = "已签到" if row["checked_at"] else ("缺席" if finished else "尚未签到")
                writer.writerow((
                    row["registration_id"], self.csv_cell(row["registrant"]),
                    "",
                    self.csv_cell(row["holder_name"]),
                    self.csv_cell(row["ticket_type"]),
                    f"{row['price_cents'] / 100:.2f}", status,
                ))
        return len(roster)

    @staticmethod
    def csv_cell(value):
        if value.startswith(("\t", "\r", "\n")) or value.lstrip().startswith(("=", "+", "-", "@")):
            return "'" + value
        return value

    def session_registrations(self, actor, session):
        with self.connection() as db:
            return db.execute(
                "SELECT r.id, u.name, r.status FROM registrations r "
                "JOIN users u ON u.id = r.user_id JOIN sessions s ON s.id = "
                "r.session_id "
                "JOIN events e ON e.id = s.event_id JOIN users operator ON "
                "operator.id = ? "
                "WHERE r.session_id = ? AND (operator.role = 'admin' OR "
                "(operator.role = 'organizer' AND e.organizer_id = "
                "operator.id)) "
                "ORDER BY r.id DESC", (actor, session),
            ).fetchall()

    def session_payments(self, actor, session):
        with self.connection() as db:
            self.require_role(db, actor, "organizer")
            belongs = db.execute(
                "SELECT 1 FROM sessions s JOIN events e ON e.id = s.event_id "
                "WHERE s.id = ? AND e.organizer_id = ?", (session, actor),
            ).fetchone()
            if belongs is None:
                raise RuleError("只能查看本人活动的缴费记录。")
            return db.execute(
                "SELECT r.id, r.status, u.name, SUM(t.price_cents) AS amount, "
                "p.paid_at, p.refunded_at FROM registrations r "
                "JOIN tickets t ON t.registration_id = r.id JOIN users u ON "
                "u.id = r.user_id "
                "LEFT JOIN payments p ON p.registration_id = r.id "
                "WHERE r.session_id = ? GROUP BY r.id ORDER BY r.id "
                "DESC", (session,),
            ).fetchall()

    def record_payment(self, actor, order, *, refund):
        with self.connection(write=True) as db:
            self.require_role(db, actor, "organizer")
            registration = db.execute(
                "SELECT r.status, p.paid_at, p.refunded_at, "
                "(SELECT SUM(t.price_cents) FROM tickets t WHERE "
                "t.registration_id = r.id) "
                "AS amount FROM registrations r JOIN sessions s ON s.id = "
                "r.session_id "
                "JOIN events e ON e.id = s.event_id LEFT JOIN payments p "
                "ON p.registration_id = r.id WHERE r.id = ? AND "
                "e.organizer_id = ?",
                (order, actor),
            ).fetchone()
            if registration is None:
                raise RuleError("只能登记本人活动的缴费或退款。")
            now = self.clock().isoformat(timespec="seconds")
            if refund:
                if registration["status"] != "cancelled" or not registration["paid_at"]:
                    raise RuleError("只可登记已缴费且已取消的整笔报名退款。")
                if registration["refunded_at"]:
                    return
                db.execute(
                    "UPDATE payments SET refunded_at = ? WHERE registration_id = ?",
                           (now, order))
                action = "登记线下退款"
            else:
                if registration["status"] == "cancelled" or not registration["amount"]:
                    raise RuleError("不能为已取消或免费的报名登记缴费。")
                if registration["paid_at"]:
                    return
                db.execute(
                    "INSERT INTO payments(registration_id, paid_at) VALUES (?, ?)",
                           (order, now))
                action = "登记线下缴费"
            self.log(db, actor, action, order, target_type="registration")

    def operation_history(
        self, actor, *, action="", operator="", target_type="", target="",
        date="",
    ):
        if date:
            try:
                date = datetime.strptime(date, "%Y-%m-%d").date().isoformat()
            except ValueError as exc:
                raise RuleError("请输入有效日期，格式为年-月-日。") from exc
        with self.connection() as db:
            self.require_role(db, actor, "admin")
            return db.execute(
                "WITH log_entries AS (SELECT l.*, CASE WHEN l.action IN "
                "('递补超时释放', '关闭无法满足的候补', '生成站内递补通知') THEN '系统' "
                "ELSE u.name END AS operator_name FROM operation_logs l JOIN "
                "users u ON u.id = l.actor_id) "
                "SELECT l.action, l.target_type, l.target_id, l.created_at, "
                "l.operator_name AS name, "
                "e.id AS event_id, e.title AS event_title, "
                "CASE WHEN l.action = '添加场次' AND l.reason = '活动 #' || "
                "s.event_id THEN '' "
                "ELSE l.reason END AS reason "
                "FROM log_entries l "
                "LEFT JOIN registrations r ON l.target_type = 'registration' "
                "AND r.id = l.target_id "
                "LEFT JOIN tickets t ON l.target_type = 'ticket' AND t.id = "
                "l.target_id "
                "LEFT JOIN ticket_types tt ON l.target_type = 'ticket_type' "
                "AND tt.id = l.target_id "
                "LEFT JOIN waitlist w ON l.target_type = 'waitlist' AND w.id "
                "= l.target_id "
                "LEFT JOIN sessions s ON s.id = CASE WHEN l.target_type = "
                "'session' THEN l.target_id "
                "ELSE COALESCE(r.session_id, t.session_id, w.session_id, tt.session_id) END "
                "LEFT JOIN events e ON e.id = CASE WHEN l.target_type = "
                "'event' THEN l.target_id ELSE s.event_id END "
                "WHERE (? = '' OR instr(l.action, ?) > 0) "
                "AND (? = '' OR instr(l.operator_name, ?) > 0) "
                "AND (? = '' OR l.target_type = ?) "
                "AND (? = '' OR CAST(l.target_id AS TEXT) = ?) "
                "AND (? = '' OR substr(l.created_at, 1, 10) = ?) "
                "ORDER BY l.id DESC LIMIT 500",
                (action, action, operator, operator, target_type, target_type, target, target, date, date),
            ).fetchall()

    def cancel_registration(self, actor, registration_id, *, reason=""):
        with self.connection(write=True) as db:
            row = db.execute(
                "SELECT r.user_id, r.status, s.starts_at, s.id AS session_id, "
                "e.organizer_id, u.role, "
                f"{PUBLIC_EVENT} AS is_public "
                "FROM registrations r JOIN sessions s ON s.id = r.session_id "
                "JOIN events e ON e.id = s.event_id JOIN users u ON u.id = ? "
                "WHERE r.id = ?",
                (actor, registration_id),
            ).fetchone()
            if row is None:
                raise RuleError("找不到报名记录。")
            self_service = row["role"] == "participant" and row["user_id"] == actor
            assisted = row["role"] == "admin" or (
                row["role"] == "organizer" and row["organizer_id"] == actor
            )
            if not (self_service or assisted):
                raise RuleError("当前账号无权取消这笔报名。")
            if row["status"] == "cancelled":
                return
            if row["status"] == "checked_in":
                raise RuleError("已有票签到，无法取消整单。")
            remaining = datetime.fromisoformat(row["starts_at"]) - self.clock()
            if remaining <= timedelta(0):
                raise RuleError("场次已开始，无法取消报名。")
            if self_service and remaining <= timedelta(hours=24):
                raise RuleError("距开场不足或等于 24 小时，请联系组织者代取消。")
            if assisted:
                if remaining > timedelta(hours=24):
                    raise RuleError("距开场超过 24 小时，请由报名人自助取消。")
                if not reason.strip():
                    raise RuleError("请填写代取消原因。")
            db.execute(
                "UPDATE registrations SET status = 'cancelled' WHERE id = ?",
                (registration_id,),
            )
            action = "自助取消报名" if self_service else "人工取消报名"
            self.log(
                db, actor, action, registration_id,
                reason.strip() if assisted else "", target_type="registration",
            )
            deadline = datetime.fromisoformat(row["starts_at"]) - timedelta(hours=2)
            self.advance_waitlist(
                db, row["session_id"], self.clock(), deadline,
                offer_new=row["is_public"],
            )

    def assign_checker(self, actor, checker, session):
        with self.connection(write=True) as db:
            self.require_role(db, actor, "organizer")
            self.require_role(db, checker, "checker")
            ownership = db.execute(
                "SELECT 1 FROM sessions s JOIN events e ON e.id = s.event_id "
                "WHERE s.id = ? AND e.organizer_id = ? AND s.cancelled_at IS "
                "NULL", (session, actor),
            ).fetchone()
            if ownership is None:
                raise RuleError("只能为自己组织的场次授权。")
            result = db.execute(
                "INSERT OR IGNORE INTO checker_assignments(checker_id, "
                "session_id) VALUES (?, ?)",
                (checker, session),
            )
            if result.rowcount:
                self.log(db, actor, "检票授权", session, target_type="session")

    def _checkable_ticket(self, db, actor, session_id, code, now):
        self.require_role(db, actor, "checker")
        assigned = db.execute(
            "SELECT s.ends_at FROM checker_assignments a "
            "JOIN sessions s ON s.id = a.session_id "
            "JOIN events e ON e.id = s.event_id "
            f"WHERE a.checker_id = ? AND a.session_id = ? AND {PUBLIC_EVENT} "
            "AND s.cancelled_at IS NULL",
            (actor, session_id),
        ).fetchone()
        if assigned is None:
            raise RuleError("当前账号无权检票或该场次未发布。")
        ticket = db.execute(
            "SELECT t.id, t.registration_id, t.holder_name, t.checked_at, "
            "r.status, "
            "tt.name AS ticket_type "
            "FROM tickets t JOIN registrations r ON r.id = t.registration_id "
            "JOIN ticket_types tt ON tt.id = t.ticket_type_id "
            "WHERE t.code = ? AND t.session_id = ?",
            (code.strip(), session_id),
        ).fetchone()
        if ticket is None:
            raise RuleError("找不到本场次的对应票据。")
        if ticket["status"] == "cancelled":
            raise RuleError("这笔报名已取消，票据已失效。")
        if ticket["checked_at"]:
            raise AlreadyCheckedIn(ticket["checked_at"])
        if assigned["ends_at"] and now >= datetime.fromisoformat(assigned["ends_at"]):
            raise RuleError("场次已结束，不能再签到。")
        return ticket

    def preview_check_in(self, actor, session_id, code):
        with self.connection() as db:
            ticket = self._checkable_ticket(
                db, actor, session_id, code, self.clock(),
            )
            return ticket["holder_name"], ticket["ticket_type"]

    def check_in(self, actor, session_id, code):
        with self.connection(write=True) as db:
            now = self.clock()
            ticket = self._checkable_ticket(db, actor, session_id, code, now)
            checked_at = now.isoformat(timespec="seconds")
            db.execute(
                "UPDATE tickets SET checked_at = ? WHERE id = ?",
                (checked_at, ticket["id"]),
            )
            db.execute(
                "UPDATE registrations SET status = 'checked_in' WHERE id = ?",
                (ticket["registration_id"],),
            )
            self.log(db, actor, "检票签到", ticket["id"], target_type="ticket")
            return ticket["holder_name"], checked_at
