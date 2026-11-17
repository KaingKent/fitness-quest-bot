create table if not exists players (
 discord_user_id bigint primary key,
 display_name text not null,
 weekly_target integer not null default 3 check (weekly_target between 1 and 14),
 created_at timestamptz not null default now()
);
create table if not exists workouts (
 id uuid primary key,
 discord_user_id bigint not null references players(discord_user_id),
 workout_date date not null,
 workout_type text not null,
 duration_minutes integer not null check (duration_minutes between 1 and 1440),
 notes text not null default '',
 visibility text not null check (visibility in ('private','public')),
 image_storage_path text,
 image_discord_url text,
 public_message_id bigint,
 created_at timestamptz not null default now()
);
create index if not exists workouts_user_date on workouts(discord_user_id,workout_date);
create table if not exists sync_outbox (
 id bigserial primary key,
 workout_id uuid not null references workouts(id) on delete cascade,
 event_type text not null,
 created_at timestamptz not null default now(),
 processed_at timestamptz,
 unique(workout_id,event_type)
);
