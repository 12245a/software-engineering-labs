PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('organizer', 'admin', 'participant', 'checker'))
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    organizer_id INTEGER NOT NULL REFERENCES users(id),
    title TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('draft', 'pending', 'published', 'unlisted', 'disabled', 'cancelled'))
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    event_id INTEGER NOT NULL REFERENCES events(id),
    venue TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT,
    cancelled_at TEXT,
    capacity INTEGER NOT NULL CHECK (capacity > 0)
);

CREATE TABLE IF NOT EXISTS ticket_types (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    name TEXT NOT NULL,
    price_cents INTEGER NOT NULL CHECK (price_cents >= 0),
    capacity INTEGER NOT NULL CHECK (capacity > 0),
    closed_at TEXT,
    UNIQUE (session_id, name),
    UNIQUE (id, session_id)
);

CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY,
    event_id INTEGER NOT NULL REFERENCES events(id),
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected')),
    reviewer_id INTEGER REFERENCES users(id),
    previous_status TEXT NOT NULL DEFAULT 'draft',
    reason TEXT NOT NULL DEFAULT ''
);

CREATE UNIQUE INDEX IF NOT EXISTS one_pending_review
ON reviews(event_id) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS change_proposals (
    review_id INTEGER PRIMARY KEY REFERENCES reviews(id),
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    ticket_type_id INTEGER NOT NULL,
    price_cents INTEGER NOT NULL CHECK (price_cents >= 0),
    ticket_capacity INTEGER NOT NULL CHECK (ticket_capacity > 0),
    session_capacity INTEGER NOT NULL CHECK (session_capacity > 0),
    starts_at TEXT NOT NULL,
    FOREIGN KEY (ticket_type_id, session_id) REFERENCES ticket_types(id, session_id)
);

CREATE TABLE IF NOT EXISTS closure_proposals (
    review_id INTEGER PRIMARY KEY REFERENCES reviews(id),
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    ticket_type_id INTEGER,
    FOREIGN KEY (ticket_type_id, session_id) REFERENCES ticket_types(id, session_id)
);

CREATE TABLE IF NOT EXISTS registrations (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    status TEXT NOT NULL CHECK (status IN ('registered', 'checked_in', 'absent', 'cancelled')),
    UNIQUE (id, session_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_registration
ON registrations(user_id, session_id) WHERE status <> 'cancelled';

CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY,
    registration_id INTEGER NOT NULL,
    session_id INTEGER NOT NULL,
    ticket_type_id INTEGER NOT NULL,
    holder_name TEXT NOT NULL,
    price_cents INTEGER NOT NULL CHECK (price_cents >= 0),
    code TEXT NOT NULL UNIQUE,
    checked_at TEXT,
    FOREIGN KEY (registration_id, session_id) REFERENCES registrations(id, session_id),
    FOREIGN KEY (ticket_type_id, session_id) REFERENCES ticket_types(id, session_id)
);

CREATE TABLE IF NOT EXISTS checker_assignments (
    checker_id INTEGER NOT NULL REFERENCES users(id),
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    PRIMARY KEY (checker_id, session_id)
);

CREATE TABLE IF NOT EXISTS operation_logs (
    id INTEGER PRIMARY KEY,
    actor_id INTEGER NOT NULL REFERENCES users(id),
    action TEXT NOT NULL,
    target_id INTEGER NOT NULL,
    target_type TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS tickets_by_type ON tickets(ticket_type_id);

CREATE TABLE IF NOT EXISTS payments (
    registration_id INTEGER PRIMARY KEY REFERENCES registrations(id),
    paid_at TEXT NOT NULL,
    refunded_at TEXT
);

CREATE TABLE IF NOT EXISTS participant_notices (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    event_id INTEGER NOT NULL REFERENCES events(id),
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS waitlist (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    position INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'offered', 'confirmed', 'closed')),
    deadline TEXT,
    UNIQUE (id, session_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS one_live_waitlist_entry
ON waitlist(user_id, session_id) WHERE status IN ('queued', 'offered');

CREATE INDEX IF NOT EXISTS waitlist_order ON waitlist(session_id, status, position);

CREATE TABLE IF NOT EXISTS waitlist_items (
    id INTEGER PRIMARY KEY,
    waitlist_id INTEGER NOT NULL,
    session_id INTEGER NOT NULL,
    ticket_type_id INTEGER NOT NULL,
    holder_name TEXT NOT NULL,
    FOREIGN KEY (waitlist_id, session_id) REFERENCES waitlist(id, session_id),
    FOREIGN KEY (ticket_type_id, session_id) REFERENCES ticket_types(id, session_id)
);
