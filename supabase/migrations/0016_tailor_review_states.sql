-- OnePager — durable encrypted Tailor review state.
--
-- Review tokens returned to the browser carry only ids, hashes, expiry, and a
-- digest. The reviewed YAML is encrypted before it is stored so multi-worker
-- runtimes can apply reviews without exposing YAML blobs in browser-readable
-- Supabase rows.

create table if not exists public.tailor_review_states (
  id text primary key,
  owner_id uuid not null references auth.users(id) on delete cascade default auth.uid(),
  cv_id uuid references public.documents(id) on delete cascade,
  job_id uuid references public.saved_jobs(id) on delete set null,
  source_hash text not null,
  jd_hash text not null,
  tailored_hash text not null,
  state_digest text not null,
  state_ciphertext text not null,
  applied_at timestamptz,
  expires_at timestamptz not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.tailor_review_states drop constraint if exists tailor_review_states_ciphertext_size_check;
alter table public.tailor_review_states add constraint tailor_review_states_ciphertext_size_check
  check (char_length(state_ciphertext) between 32 and 250000);

alter table public.tailor_review_states enable row level security;

drop policy if exists "tailor_review_states_select_own" on public.tailor_review_states;
create policy "tailor_review_states_select_own" on public.tailor_review_states
  for select to authenticated
  using (
    owner_id = (select auth.uid())
    and not exists (
      select 1 from public.profiles p
      where p.id = (select auth.uid())
        and p.deletion_requested_at is not null
    )
  );

drop policy if exists "tailor_review_states_insert_own" on public.tailor_review_states;
create policy "tailor_review_states_insert_own" on public.tailor_review_states
  for insert to authenticated
  with check (
    owner_id = (select auth.uid())
    and exists (
      select 1 from public.documents d
      where d.id = tailor_review_states.cv_id
        and d.owner_id = (select auth.uid())
        and d.type = 'cv'
    )
    and (
      job_id is null
      or exists (
        select 1 from public.saved_jobs sj
        where sj.id = tailor_review_states.job_id
          and sj.owner_id = (select auth.uid())
      )
    )
    and not exists (
      select 1 from public.profiles p
      where p.id = (select auth.uid())
        and p.deletion_requested_at is not null
    )
  );

drop policy if exists "tailor_review_states_update_own" on public.tailor_review_states;
create policy "tailor_review_states_update_own" on public.tailor_review_states
  for update to authenticated
  using (
    owner_id = (select auth.uid())
    and not exists (
      select 1 from public.profiles p
      where p.id = (select auth.uid())
        and p.deletion_requested_at is not null
    )
  )
  with check (
    owner_id = (select auth.uid())
    and not exists (
      select 1 from public.profiles p
      where p.id = (select auth.uid())
        and p.deletion_requested_at is not null
    )
  );

drop policy if exists "tailor_review_states_delete_own" on public.tailor_review_states;

revoke select on public.tailor_review_states from authenticated;
grant select (id, owner_id, cv_id, job_id, source_hash, jd_hash, tailored_hash, state_digest, applied_at, expires_at, created_at, updated_at)
  on public.tailor_review_states to authenticated;
revoke insert on public.tailor_review_states from authenticated;
revoke delete on public.tailor_review_states from authenticated;
revoke update on public.tailor_review_states from authenticated;

create index if not exists tailor_review_states_owner_expires_idx
  on public.tailor_review_states (owner_id, expires_at desc);

create index if not exists tailor_review_states_owner_cv_idx
  on public.tailor_review_states (owner_id, cv_id, created_at desc);
