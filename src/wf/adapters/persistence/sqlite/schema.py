"""The board's SQLite schema, migrations, and connection."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
pragma foreign_keys = on;

create table if not exists project (
    key           text primary key,
    name          text not null,
    discovered_at integer not null
);

create table if not exists project_status (
    project_key text not null references project(key),
    name        text not null,
    category    text not null check (category in ('new', 'indeterminate', 'done')),
    source      text not null check (source in ('observed', 'workflow')),
    primary key (project_key, name)
);

create table if not exists stage_mapping (
    project_key text not null references project(key),
    stage       text not null,
    status_name text,
    reason      text not null,
    primary key (project_key, stage),
    foreign key (project_key, status_name) references project_status(project_key, name)
);

create table if not exists sprint (
    id            text primary key,
    board_id      text not null,
    name          text not null,
    state         text not null check (state in ('future', 'active', 'closed')),
    goal          text,
    start_date    integer,
    end_date      integer,
    complete_date integer,
    synced_at     integer not null
);
create index if not exists sprint_state on sprint (state);

create table if not exists sprint_event (
    id         integer primary key autoincrement,
    sprint_id  text not null references sprint(id),
    at         integer not null,
    from_state text,
    to_state   text not null,
    source     text not null check (source in ('sync', 'user'))
);

create table if not exists ticket (
    key                  text primary key,
    project_key          text not null references project(key),
    jira_id              text not null,
    summary              text not null,
    description          text,
    acceptance           text,
    issue_type           text,
    priority             text,
    points               real,
    assignee_id          text,
    jira_status          text not null,
    jira_status_category text,
    labels               text not null default '[]',
    sprint_id            text references sprint(id),
    stage                text,
    flags                text not null default '[]',
    jira_updated         integer,
    synced_at            integer not null,
    removed_at           integer
);
create index if not exists ticket_stage on ticket (stage);
create index if not exists ticket_project on ticket (project_key);
create index if not exists ticket_sprint_id on ticket (sprint_id);

create table if not exists ticket_event (
    id         integer primary key autoincrement,
    ticket_key text not null references ticket(key),
    at         integer not null,
    actor      text not null,
    kind       text not null,
    from_value text,
    to_value   text,
    detail     text
);
create index if not exists ticket_event_key on ticket_event (ticket_key, at);

create table if not exists ticket_comment (
    id              integer primary key autoincrement,
    ticket_key      text not null references ticket(key),
    origin          text not null check (origin in ('jira', 'user', 'agent')),
    jira_comment_id text unique,
    author          text not null,
    body            text not null,
    created_at      integer not null,
    updated_at      integer,
    synced_at       integer
);
create index if not exists ticket_comment_key on ticket_comment (ticket_key, created_at);

create table if not exists sync_run (
    id          integer primary key autoincrement,
    started_at  integer not null,
    finished_at integer,
    kind        text not null check (kind in ('active', 'past', 'backlog')),
    status      text not null check (status in ('running', 'ok', 'failed')),
    counts      text,
    error       text
);

-- Changes waiting to be pushed to Jira by the Jira agent. Each row is sent at most once.
create table if not exists outbox (
    id          integer primary key autoincrement,
    created_at  integer not null,
    ticket_key  text not null references ticket(key),
    kind        text not null check (kind in ('transition')),
    target_stage text not null,
    actor       text not null,
    status      text not null check (status in
                ('pending', 'sending', 'sent', 'skipped', 'blocked', 'conflict', 'failed', 'superseded')),
    attempts    integer not null default 0,
    detail      text,
    jira_response text,
    updated_at  integer
);
create index if not exists outbox_status on outbox (status);
create index if not exists outbox_ticket on outbox (ticket_key, status);

create view if not exists active_sprint_tickets as
select t.*, s.name as sprint_name
from ticket t
join sprint s on s.id = t.sprint_id
where s.state = 'active' and t.removed_at is null;

create view if not exists past_sprints as
select * from sprint where state = 'closed';

create view if not exists backlog_tickets as
select * from ticket where sprint_id is null and removed_at is null;
"""


LINK_SCHEMA = """
create table if not exists ticket_link (
    ticket_key    text not null,
    other_key     text not null,
    link_type     text not null,
    direction     text not null,
    other_summary text,
    other_status  text,
    primary key (ticket_key, other_key, link_type)
);
"""


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma foreign_keys = on")
    conn.executescript(SCHEMA)
    cols = [r[1] for r in conn.execute("pragma table_info(ticket)")]
    if "assignee_name" not in cols:
        conn.execute("alter table ticket add column assignee_name text")
    if "sort_order" not in cols:
        conn.execute("alter table ticket add column sort_order integer")
    # The needs_refinement stage was removed: its tickets and mapping rows move to in_refinement.
    conn.execute("update ticket set stage = 'in_refinement' where stage = 'needs_refinement'")
    conn.execute("delete from stage_mapping where stage = 'needs_refinement'")
    conn.commit()
    conn.executescript(LINK_SCHEMA)
    link_cols = [r[1] for r in conn.execute("pragma table_info(ticket_link)")]
    if "synced" not in link_cols:  # links from Jira are synced already; new local ones are not
        conn.execute("alter table ticket_link add column synced integer not null default 1")
    conn.execute(f"pragma user_version = {SCHEMA_VERSION}")
    return conn
