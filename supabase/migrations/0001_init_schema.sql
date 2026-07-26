-- OnePager — Stage 1: multi-tenant schema + Row-Level Security
-- Self-hosted migration for the operator-selected Supabase project and region.
--
-- Tenant isolation is the #1 risk. Every user-owned row is RLS-scoped to
-- auth.uid(). No table is exposed to the `anon` role. `org_id` on documents
-- is intentionally dormant (nullable, no FK) so a future teams/org layer
-- needs no second migration.
--
-- Table order matters: saved_jobs is created before documents because
-- documents.job_id has a FK to saved_jobs(id).

-- =====================================================================
-- profiles — one row per auth user, auto-provisioned on signup (trigger below)
-- =====================================================================
create table public.profiles (
  id                       uuid primary key references auth.users (id) on delete cascade,
  email                    text,
  display_name             text,
  default_language         text default 'en',
  managed_credits_remaining integer not null default 3,
  created_at               timestamptz default now(),
  updated_at               timestamptz default now()
);

-- =====================================================================
-- saved_jobs — pasted/URL job descriptions a user works against
-- (created before documents: documents.job_id references it)
-- =====================================================================
create table public.saved_jobs (
  id          uuid primary key default gen_random_uuid(),
  owner_id    uuid not null references auth.users (id) on delete cascade default auth.uid(),
  title       text,
  company     text,
  jd_text     text,
  jd_json     jsonb,
  source_url  text,
  status      text not null default 'saved',
  created_at  timestamptz default now(),
  updated_at  timestamptz default now()
);

-- =====================================================================
-- documents — CVs and cover letters, stored per-user as yaml_blob
-- org_id is dormant (nullable, NO FK yet) for a future teams layer
-- =====================================================================
create table public.documents (
  id          uuid primary key default gen_random_uuid(),
  owner_id    uuid not null references auth.users (id) on delete cascade default auth.uid(),
  org_id      uuid,
  type        text not null check (type in ('cv', 'cover_letter')),
  name        text not null,
  slug        text,
  yaml_blob   text not null default '',
  accent      text,
  font        text,
  density     text,
  photo_key   text,
  job_id      uuid references public.saved_jobs (id) on delete set null,
  created_at  timestamptz default now(),
  updated_at  timestamptz default now(),
  deleted_at  timestamptz
);

create index documents_owner_type_idx on public.documents (owner_id, type);
create index documents_owner_updated_idx on public.documents (owner_id, updated_at desc);

-- =====================================================================
-- user_llm_keys — BYO LLM API keys, AES-256-GCM envelope-encrypted.
-- Only ciphertext lives here; plaintext keys are never stored.
-- =====================================================================
create table public.user_llm_keys (
  id          uuid primary key default gen_random_uuid(),
  owner_id    uuid not null references auth.users (id) on delete cascade default auth.uid(),
  provider    text not null default 'anthropic',
  ciphertext  bytea not null,
  nonce       bytea not null,
  wrapped_dek bytea not null,
  dek_nonce   bytea not null,
  last4       text,
  created_at  timestamptz default now(),
  updated_at  timestamptz default now(),
  unique (owner_id, provider)
);

-- =====================================================================
-- pdf_artifacts — rendered PDFs in R2, content-hash cached.
-- owner_id is denormalized so RLS needs no join to documents.
-- =====================================================================
create table public.pdf_artifacts (
  id           uuid primary key default gen_random_uuid(),
  document_id  uuid not null references public.documents (id) on delete cascade,
  owner_id     uuid not null references auth.users (id) on delete cascade default auth.uid(),
  r2_key       text not null,
  content_hash text not null,
  page_count   integer,
  stale        boolean not null default false,
  generated_at timestamptz default now()
);

create index pdf_artifacts_document_idx on public.pdf_artifacts (document_id);
create index pdf_artifacts_content_hash_idx on public.pdf_artifacts (content_hash);

-- =====================================================================
-- usage_events — immutable audit log of AI/credit usage.
-- No update/delete policies: append-only.
-- =====================================================================
create table public.usage_events (
  id                  uuid primary key default gen_random_uuid(),
  owner_id            uuid not null references auth.users (id) on delete cascade default auth.uid(),
  kind                text not null,
  used_managed_credit boolean not null default false,
  provider            text,
  meta                jsonb,
  created_at          timestamptz default now()
);

create index usage_events_owner_created_idx on public.usage_events (owner_id, created_at desc);

-- =====================================================================
-- Row-Level Security — enable on ALL six tables, then per-table policies.
-- Policies target the `authenticated` role only; `anon` gets nothing.
-- =====================================================================
alter table public.profiles      enable row level security;
alter table public.saved_jobs    enable row level security;
alter table public.documents     enable row level security;
alter table public.user_llm_keys enable row level security;
alter table public.pdf_artifacts enable row level security;
alter table public.usage_events  enable row level security;

-- profiles: a user manages only their own profile row (no delete).
create policy "profiles_select_own" on public.profiles
  for select to authenticated
  using (id = auth.uid());

create policy "profiles_insert_own" on public.profiles
  for insert to authenticated
  with check (id = auth.uid());

create policy "profiles_update_own" on public.profiles
  for update to authenticated
  using (id = auth.uid())
  with check (id = auth.uid());

-- saved_jobs: full CRUD scoped to the owner.
create policy "saved_jobs_select_own" on public.saved_jobs
  for select to authenticated
  using (owner_id = auth.uid());

create policy "saved_jobs_insert_own" on public.saved_jobs
  for insert to authenticated
  with check (owner_id = auth.uid());

create policy "saved_jobs_update_own" on public.saved_jobs
  for update to authenticated
  using (owner_id = auth.uid())
  with check (owner_id = auth.uid());

create policy "saved_jobs_delete_own" on public.saved_jobs
  for delete to authenticated
  using (owner_id = auth.uid());

-- documents: full CRUD scoped to the owner.
create policy "documents_select_own" on public.documents
  for select to authenticated
  using (owner_id = auth.uid());

create policy "documents_insert_own" on public.documents
  for insert to authenticated
  with check (owner_id = auth.uid());

create policy "documents_update_own" on public.documents
  for update to authenticated
  using (owner_id = auth.uid())
  with check (owner_id = auth.uid());

create policy "documents_delete_own" on public.documents
  for delete to authenticated
  using (owner_id = auth.uid());

-- user_llm_keys: full CRUD scoped to the owner.
create policy "user_llm_keys_select_own" on public.user_llm_keys
  for select to authenticated
  using (owner_id = auth.uid());

create policy "user_llm_keys_insert_own" on public.user_llm_keys
  for insert to authenticated
  with check (owner_id = auth.uid());

create policy "user_llm_keys_update_own" on public.user_llm_keys
  for update to authenticated
  using (owner_id = auth.uid())
  with check (owner_id = auth.uid());

create policy "user_llm_keys_delete_own" on public.user_llm_keys
  for delete to authenticated
  using (owner_id = auth.uid());

-- pdf_artifacts: full CRUD scoped to the owner (denormalized owner_id).
create policy "pdf_artifacts_select_own" on public.pdf_artifacts
  for select to authenticated
  using (owner_id = auth.uid());

create policy "pdf_artifacts_insert_own" on public.pdf_artifacts
  for insert to authenticated
  with check (owner_id = auth.uid());

create policy "pdf_artifacts_update_own" on public.pdf_artifacts
  for update to authenticated
  using (owner_id = auth.uid())
  with check (owner_id = auth.uid());

create policy "pdf_artifacts_delete_own" on public.pdf_artifacts
  for delete to authenticated
  using (owner_id = auth.uid());

-- usage_events: append-only audit log. Read + insert your own; no update/delete.
create policy "usage_events_select_own" on public.usage_events
  for select to authenticated
  using (owner_id = auth.uid());

create policy "usage_events_insert_own" on public.usage_events
  for insert to authenticated
  with check (owner_id = auth.uid());

-- =====================================================================
-- Auto-provision a profile row when a new auth user is created.
-- security definer + pinned empty search_path (satisfies the advisor).
-- =====================================================================
create function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  insert into public.profiles (id, email)
  values (new.id, new.email);
  return new;
end;
$$;

create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();

-- handle_new_user is fired ONLY by the auth.users trigger above; it must never
-- be callable as a public RPC (/rest/v1/rpc/handle_new_user). Revoke EXECUTE
-- from the API roles so neither anon nor authenticated can invoke it directly.
revoke execute on function public.handle_new_user() from public, anon, authenticated;
