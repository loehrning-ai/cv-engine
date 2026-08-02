-- CV Copilot -- make user-facing hard deletion atomic and lineage-preserving.
--
-- Direct Data API DELETE used to race with packet/attempt creation.  The
-- foreign keys intentionally use ON DELETE SET NULL, so winning that race
-- erased the parent identity from durable application history.  Authenticated
-- callers now delete only through the two functions below.  Each function:
--
--   * derives the tenant exclusively from auth.uid();
--   * locks the owned parent row for the short duration of this statement;
--   * checks every durable application-history relationship while that lock
--     prevents a concurrent FK insert/update from acquiring KEY SHARE; and
--   * deletes only when no protected relationship exists.
--
-- Account erasure remains separate: the service-role worker deletes auth.users
-- after external cleanup, and the existing ownership FKs cascade from there.

drop policy if exists "saved_jobs_delete_own" on public.saved_jobs;
drop policy if exists "documents_delete_own" on public.documents;

revoke delete on table public.saved_jobs from authenticated;
revoke delete on table public.documents from authenticated;
revoke insert on table public.documents from authenticated;

-- Apply-attempt evidence is authored only by the server after its readiness,
-- kill-switch, rate-limit, and confirmation checks. The earlier authenticated
-- RPC accepted caller-supplied status/readiness JSON and could forge evidence.
revoke execute on function public.record_application_apply_attempt(
  uuid,
  uuid,
  text,
  text,
  text,
  text,
  text,
  text,
  jsonb,
  jsonb,
  jsonb
) from public, anon, authenticated;

-- R2 is external to PostgreSQL, so document deletion cannot atomically remove
-- the object. Persist the canonical key before the document and its cascading
-- pdf_artifacts rows disappear. The application consumes this outbox
-- idempotently; account erasure still deletes the complete owner prefix.
alter table public.pdf_artifacts
  add column if not exists generation_id uuid;

-- Bounded library projections. Large document blobs are fetched only when a
-- document is opened; cover cards read these denormalized, server-maintained
-- fields instead of transferring every YAML body.
alter table public.documents
  add column if not exists summary_company text,
  add column if not exists summary_role text;

-- One document has one authoritative PDF, regardless of how many historical
-- content hashes exist. New objects use a unique generation id in their key,
-- so a delayed cleanup can never delete a later publication that happens to
-- have the same content hash.
with ranked_fresh_artifacts as (
  select
    id,
    row_number() over (
      partition by document_id
      order by generated_at desc nulls last, id desc
    ) as freshness_rank
  from public.pdf_artifacts
  where stale = false
)
update public.pdf_artifacts as artifact
   set stale = true
  from ranked_fresh_artifacts as ranked
 where artifact.id = ranked.id
   and ranked.freshness_rank > 1;

drop index if exists public.pdf_artifacts_one_fresh_content_idx;
create unique index if not exists pdf_artifacts_one_fresh_document_idx
  on public.pdf_artifacts (document_id)
  where stale = false;

create table if not exists public.pdf_artifact_cleanup_queue (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null references auth.users(id) on delete cascade,
  document_id uuid not null,
  r2_key text not null check (char_length(r2_key) between 1 and 500),
  content_hash text not null check (content_hash ~ '^[0-9a-f]{64}$'),
  generation_id uuid,
  attempt_count integer not null default 0 check (attempt_count >= 0),
  last_attempt_at timestamptz,
  claim_token uuid,
  claimed_at timestamptz,
  not_before timestamptz not null default now(),
  created_at timestamptz not null default now(),
  unique (owner_id, r2_key)
);

alter table public.pdf_artifact_cleanup_queue
  add column if not exists generation_id uuid;
alter table public.pdf_artifact_cleanup_queue
  add column if not exists not_before timestamptz not null default now();
alter table public.pdf_artifact_cleanup_queue
  add column if not exists claim_token uuid;
alter table public.pdf_artifact_cleanup_queue
  add column if not exists claimed_at timestamptz;

create index if not exists pdf_artifact_cleanup_queue_owner_created_idx
  on public.pdf_artifact_cleanup_queue (owner_id, created_at, id);
create index if not exists pdf_artifact_cleanup_queue_due_idx
  on public.pdf_artifact_cleanup_queue (
    not_before, claimed_at, attempt_count, created_at, id
  );

-- Move pre-existing stale canonical artifacts into the durable object-cleanup
-- outbox before retiring their metadata. Invalid legacy keys remain visible
-- for operator repair; deleting their only identity would orphan R2 bytes.
insert into public.pdf_artifact_cleanup_queue (
  owner_id, document_id, r2_key, content_hash, generation_id, not_before
)
select a.owner_id, a.document_id, a.r2_key, a.content_hash, a.generation_id,
       now() + interval '2 hours'
  from public.pdf_artifacts as a
 where a.stale = true
   and a.content_hash ~ '^[0-9a-f]{64}$'
   and (
     (
       a.generation_id is null
       and a.r2_key = (
         a.owner_id::text || '/' || a.document_id::text || '/' ||
         a.content_hash || '.pdf'
       )
     )
     or (
       a.generation_id is not null
       and a.r2_key = (
         a.owner_id::text || '/' || a.document_id::text || '/' ||
         a.content_hash || '/' || a.generation_id::text || '.pdf'
       )
     )
   )
on conflict (owner_id, r2_key) do update
  set document_id = excluded.document_id,
      content_hash = excluded.content_hash,
      generation_id = excluded.generation_id,
      not_before = greatest(
        public.pdf_artifact_cleanup_queue.not_before,
        excluded.not_before
      );

delete from public.pdf_artifacts as a
 where a.stale = true
   and exists (
     select 1
       from public.pdf_artifact_cleanup_queue as q
      where q.owner_id = a.owner_id
        and q.r2_key = a.r2_key
        and q.document_id = a.document_id
   );

-- Preserve the legacy scheduler entrypoint but route every retired metadata
-- row through the outbox. This removes the prior metadata-only R2 orphan path.
create or replace function public.purge_stale_pdf_artifacts(
  older_than_days int default 30
)
returns integer
language plpgsql
security definer
set search_path = ''
set lock_timeout = '2s'
as $$
declare
  deleted_count integer := 0;
begin
  perform a.id
    from public.pdf_artifacts as a
   where a.stale = true
      or a.generated_at < now() - make_interval(days => greatest(older_than_days, 0))
   order by a.owner_id, a.document_id, a.id
   for update;

  insert into public.pdf_artifact_cleanup_queue (
    owner_id, document_id, r2_key, content_hash, generation_id, not_before
  )
  select a.owner_id, a.document_id, a.r2_key, a.content_hash, a.generation_id,
         now() + interval '2 hours'
    from public.pdf_artifacts as a
   where (
       a.stale = true
       or a.generated_at < now() - make_interval(days => greatest(older_than_days, 0))
     )
     and a.content_hash ~ '^[0-9a-f]{64}$'
     and (
       (
         a.generation_id is null
         and a.r2_key = (
           a.owner_id::text || '/' || a.document_id::text || '/' ||
           a.content_hash || '.pdf'
         )
       )
       or (
         a.generation_id is not null
         and a.r2_key = (
           a.owner_id::text || '/' || a.document_id::text || '/' ||
           a.content_hash || '/' || a.generation_id::text || '.pdf'
         )
       )
     )
  on conflict (owner_id, r2_key) do update
    set document_id = excluded.document_id,
        content_hash = excluded.content_hash,
        generation_id = excluded.generation_id,
        not_before = greatest(
          public.pdf_artifact_cleanup_queue.not_before,
          excluded.not_before
        );

  with removed as (
    delete from public.pdf_artifacts as a
     where (
         a.stale = true
         or a.generated_at < now() - make_interval(days => greatest(older_than_days, 0))
       )
       and exists (
         select 1
           from public.pdf_artifact_cleanup_queue as q
          where q.owner_id = a.owner_id
            and q.r2_key = a.r2_key
            and q.document_id = a.document_id
       )
    returning 1
  )
  select count(*) into deleted_count from removed;
  return deleted_count;
end;
$$;

alter table public.pdf_artifact_cleanup_queue enable row level security;

revoke all on table public.pdf_artifact_cleanup_queue
  from public, anon, authenticated;

-- PDF metadata is server-authored evidence. Browser roles retain the existing
-- SELECT-own policy, but cannot forge cache rows, retarget object keys, or
-- remove the only evidence that an external object exists.
drop policy if exists "pdf_artifacts_insert_own" on public.pdf_artifacts;
drop policy if exists "pdf_artifacts_update_own" on public.pdf_artifacts;
drop policy if exists "pdf_artifacts_delete_own" on public.pdf_artifacts;
revoke insert, update, delete on table public.pdf_artifacts from authenticated;

-- Document content and render-affecting appearance move only through the
-- revisioned server RPC below. Browser roles retain harmless metadata updates
-- (name/slug/last-used timestamps) but cannot bypass artifact invalidation.
revoke update (
  yaml_blob, accent, font, density, photo_key, job_id
) on table public.documents from authenticated;

-- Atomically detach an owned document's artifact metadata into the durable
-- cleanup outbox. The application deletes the external objects after this
-- transaction, then acknowledges each queue row through the service role.
create or replace function public.detach_pdf_artifacts(
  p_owner_id uuid,
  p_document_id uuid
)
returns jsonb
language plpgsql
security definer
set search_path = ''
set lock_timeout = '2s'
as $$
declare
  locked_document_id uuid;
  artifact_snapshot jsonb := '[]'::jsonb;
begin
  if p_owner_id is null or p_document_id is null then
    raise exception 'owner and document are required'
      using errcode = '22023';
  end if;

  perform p.id
    from public.profiles as p
   where p.id = p_owner_id
     and p.deletion_requested_at is null
   for share;
  if not found then
    return jsonb_build_object('status', 'blocked', 'code', 'account_unavailable');
  end if;

  select d.id
    into locked_document_id
    from public.documents as d
   where d.id = p_document_id
     and d.owner_id = p_owner_id
     and d.deleted_at is null
   for update;

  if locked_document_id is null then
    return jsonb_build_object('status', 'not_found', 'artifacts', '[]'::jsonb);
  end if;

  perform p.id
    from public.pdf_artifacts as p
   where p.document_id = locked_document_id
     and p.owner_id = p_owner_id
   for update;

  if exists (
    select 1
      from public.pdf_artifacts as p
     where p.document_id = locked_document_id
       and p.owner_id = p_owner_id
       and (
         p.content_hash !~ '^[0-9a-f]{64}$'
         or (
           p.generation_id is null
           and p.r2_key <> (
             p_owner_id::text || '/' || locked_document_id::text || '/' ||
             p.content_hash || '.pdf'
           )
         )
         or (
           p.generation_id is not null
           and p.r2_key <> (
             p_owner_id::text || '/' || locked_document_id::text || '/' ||
             p.content_hash || '/' || p.generation_id::text || '.pdf'
           )
         )
       )
  ) then
    return jsonb_build_object(
      'status', 'blocked',
      'code', 'document_artifact_invalid',
      'artifacts', '[]'::jsonb
    );
  end if;

  insert into public.pdf_artifact_cleanup_queue (
    owner_id, document_id, r2_key, content_hash, generation_id, not_before
  )
  select p_owner_id, p.document_id, p.r2_key, p.content_hash, p.generation_id,
         now() + interval '2 hours'
    from public.pdf_artifacts as p
   where p.document_id = locked_document_id
     and p.owner_id = p_owner_id
  on conflict (owner_id, r2_key) do update
    set document_id = excluded.document_id,
        content_hash = excluded.content_hash,
        generation_id = excluded.generation_id,
        not_before = greatest(
          public.pdf_artifact_cleanup_queue.not_before,
          excluded.not_before
        );

  select coalesce(
           jsonb_agg(
             jsonb_build_object(
               'cleanup_id', q.id,
               'document_id', q.document_id,
               'r2_key', q.r2_key,
               'content_hash', q.content_hash,
               'generation_id', q.generation_id,
               'not_before', q.not_before
             )
             order by q.id
           ),
           '[]'::jsonb
         )
    into artifact_snapshot
    from public.pdf_artifact_cleanup_queue as q
   where q.owner_id = p_owner_id
     and q.document_id = locked_document_id;

  delete from public.pdf_artifacts as p
   where p.document_id = locked_document_id
     and p.owner_id = p_owner_id;

  return jsonb_build_object(
    'status', 'detached',
    'document_id', locked_document_id,
    'artifacts', artifact_snapshot
  );
end;
$$;

-- A shared owner photo affects every CV render. Detach the complete owner-wide
-- CV artifact set in one transaction so a mutable application-side page walk
-- cannot miss an artifact while documents are concurrently created/deleted.
create or replace function public.detach_owner_cv_pdf_artifacts(
  p_owner_id uuid
)
returns jsonb
language plpgsql
security definer
set search_path = ''
set lock_timeout = '2s'
as $$
declare
  artifact_snapshot jsonb := '[]'::jsonb;
begin
  if p_owner_id is null then
    raise exception 'owner is required'
      using errcode = '22023';
  end if;

  perform p.id
    from public.profiles as p
   where p.id = p_owner_id
     and p.deletion_requested_at is null
   for share;
  if not found then
    return jsonb_build_object('status', 'blocked', 'code', 'account_unavailable');
  end if;

  perform d.id
    from public.documents as d
   where d.owner_id = p_owner_id
     and d.type = 'cv'
     and d.deleted_at is null
   order by d.id
   for update;

  perform a.id
    from public.pdf_artifacts as a
    join public.documents as d on d.id = a.document_id
   where a.owner_id = p_owner_id
     and d.owner_id = p_owner_id
     and d.type = 'cv'
     and d.deleted_at is null
   order by a.document_id, a.id
   for update of a;

  if exists (
    select 1
      from public.pdf_artifacts as a
      join public.documents as d on d.id = a.document_id
     where a.owner_id = p_owner_id
       and d.owner_id = p_owner_id
       and d.type = 'cv'
       and d.deleted_at is null
       and (
         a.content_hash !~ '^[0-9a-f]{64}$'
         or (
           a.generation_id is null
           and a.r2_key <> (
             p_owner_id::text || '/' || a.document_id::text || '/' ||
             a.content_hash || '.pdf'
           )
         )
         or (
           a.generation_id is not null
           and a.r2_key <> (
             p_owner_id::text || '/' || a.document_id::text || '/' ||
             a.content_hash || '/' || a.generation_id::text || '.pdf'
           )
         )
       )
  ) then
    return jsonb_build_object('status', 'blocked', 'code', 'document_artifact_invalid');
  end if;

  insert into public.pdf_artifact_cleanup_queue (
    owner_id, document_id, r2_key, content_hash, generation_id, not_before
  )
  select p_owner_id, a.document_id, a.r2_key, a.content_hash, a.generation_id, now()
    from public.pdf_artifacts as a
    join public.documents as d on d.id = a.document_id
   where a.owner_id = p_owner_id
     and d.owner_id = p_owner_id
     and d.type = 'cv'
     and d.deleted_at is null
  on conflict (owner_id, r2_key) do update
    set document_id = excluded.document_id,
        content_hash = excluded.content_hash,
        generation_id = excluded.generation_id,
        not_before = least(
          public.pdf_artifact_cleanup_queue.not_before,
          excluded.not_before
        );

  select coalesce(
           jsonb_agg(
             jsonb_build_object(
               'cleanup_id', q.id,
               'document_id', q.document_id,
               'r2_key', q.r2_key,
               'content_hash', q.content_hash,
               'generation_id', q.generation_id,
               'not_before', q.not_before
             )
             order by q.document_id, q.id
           ),
           '[]'::jsonb
         )
    into artifact_snapshot
    from public.pdf_artifact_cleanup_queue as q
    join public.pdf_artifacts as a
      on a.owner_id = q.owner_id
     and a.r2_key = q.r2_key
    join public.documents as d on d.id = a.document_id
   where q.owner_id = p_owner_id
     and d.owner_id = p_owner_id
     and d.type = 'cv'
     and d.deleted_at is null;

  delete from public.pdf_artifacts as a
   using public.documents as d
   where a.document_id = d.id
     and a.owner_id = p_owner_id
     and d.owner_id = p_owner_id
     and d.type = 'cv'
     and d.deleted_at is null;

  return jsonb_build_object(
    'status', 'detached',
    'artifacts', artifact_snapshot
  );
end;
$$;

-- Create every user-authored document while holding the same profile-row lock
-- used by account deletion. An insert therefore commits wholly before erasure
-- (and is included in its soft-delete snapshot) or wholly after it (blocked).
create or replace function public.insert_document_if_account_active(
  p_type text,
  p_name text,
  p_yaml_blob text,
  p_job_id uuid,
  p_accent text,
  p_font text,
  p_density text,
  p_summary_company text,
  p_summary_role text
)
returns jsonb
language plpgsql
security definer
set search_path = ''
set lock_timeout = '2s'
as $$
declare
  caller_id uuid := auth.uid();
  created_document public.documents%rowtype;
  normalized_name text := btrim(p_name);
  normalized_accent text := nullif(btrim(p_accent), '');
  normalized_font text := nullif(lower(btrim(p_font)), '');
  normalized_density text := nullif(lower(btrim(p_density)), '');
begin
  if caller_id is null then
    raise exception 'not authenticated'
      using errcode = '42501';
  end if;
  if p_type not in ('cv', 'cover_letter')
     or p_name is null or normalized_name = ''
     or char_length(normalized_name) > 200
     or p_yaml_blob is null then
    raise exception 'invalid document insert'
      using errcode = '22023';
  end if;
  if (p_type = 'cv' and octet_length(p_yaml_blob) > 524288)
     or (p_type = 'cover_letter' and octet_length(p_yaml_blob) > 65536) then
    raise exception 'document content is too large'
      using errcode = '22023';
  end if;
  if p_type = 'cv' then
    if normalized_accent is not null
       and normalized_accent !~ '^#[0-9A-Fa-f]{6}$' then
      raise exception 'invalid document accent'
        using errcode = '22023';
    end if;
    if normalized_font is not null
       and normalized_font not in ('serif', 'sans', 'mono') then
      raise exception 'invalid document font'
        using errcode = '22023';
    end if;
    if normalized_density is not null
       and normalized_density not in ('tight', 'normal', 'airy') then
      raise exception 'invalid document density'
        using errcode = '22023';
    end if;
  end if;

  perform p.id
    from public.profiles as p
   where p.id = caller_id
     and p.deletion_requested_at is null
   for share;
  if not found then
    return jsonb_build_object('status', 'blocked', 'code', 'account_unavailable');
  end if;

  if p_job_id is not null then
    perform j.id
      from public.saved_jobs as j
     where j.id = p_job_id
       and j.owner_id = caller_id
     for key share;
    if not found then
      return jsonb_build_object('status', 'blocked', 'code', 'job_unavailable');
    end if;
  end if;

  insert into public.documents (
    owner_id, type, name, yaml_blob, job_id,
    accent, font, density, summary_company, summary_role
  ) values (
    caller_id,
    p_type,
    normalized_name,
    p_yaml_blob,
    p_job_id,
    case when p_type = 'cv' then normalized_accent else null end,
    case when p_type = 'cv' then normalized_font else null end,
    case when p_type = 'cv' then normalized_density else null end,
    case when p_type = 'cover_letter' then left(p_summary_company, 500) else null end,
    case when p_type = 'cover_letter' then left(p_summary_role, 500) else null end
  )
  returning * into created_document;

  return jsonb_build_object(
    'status', 'created',
    'document', to_jsonb(created_document)
  );
end;
$$;

-- Advance a document revision and detach every prior PDF in the same locked
-- transaction. A build can linearize either before or after this mutation,
-- never between content change and artifact invalidation.
create or replace function public.update_document_with_artifact_detach(
  p_owner_id uuid,
  p_document_id uuid,
  p_document_type text,
  p_expected_revision timestamptz,
  p_yaml_blob text,
  p_updated_at timestamptz,
  p_accent text,
  p_font text,
  p_density text,
  p_summary_company text,
  p_summary_role text
)
returns jsonb
language plpgsql
security definer
set search_path = ''
set lock_timeout = '2s'
as $$
declare
  locked_document public.documents%rowtype;
  render_changed boolean;
  artifact_snapshot jsonb := '[]'::jsonb;
begin
  if p_owner_id is null or p_document_id is null
     or p_document_type not in ('cv', 'cover_letter')
     or p_expected_revision is null or p_updated_at is null
     or p_yaml_blob is null then
    raise exception 'invalid document update'
      using errcode = '22023';
  end if;

  perform p.id
    from public.profiles as p
   where p.id = p_owner_id
     and p.deletion_requested_at is null
   for share;
  if not found then
    return jsonb_build_object('status', 'blocked', 'code', 'account_unavailable');
  end if;

  select d.*
    into locked_document
    from public.documents as d
   where d.id = p_document_id
     and d.owner_id = p_owner_id
     and d.type = p_document_type
     and d.deleted_at is null
   for update;

  if locked_document.id is null then
    return jsonb_build_object('status', 'not_found');
  end if;
  if locked_document.updated_at is distinct from p_expected_revision then
    return jsonb_build_object(
      'status', 'revision_conflict',
      'document_revision', locked_document.updated_at
    );
  end if;

  render_changed := locked_document.yaml_blob is distinct from p_yaml_blob;
  if p_document_type = 'cv' then
    render_changed := render_changed
      or locked_document.accent is distinct from p_accent
      or locked_document.font is distinct from p_font
      or locked_document.density is distinct from p_density;
  end if;

  if render_changed then
    perform p.id
      from public.pdf_artifacts as p
     where p.document_id = locked_document.id
       and p.owner_id = p_owner_id
     for update;

    if exists (
      select 1
        from public.pdf_artifacts as p
       where p.document_id = locked_document.id
         and p.owner_id = p_owner_id
         and (
           p.content_hash !~ '^[0-9a-f]{64}$'
           or (
             p.generation_id is null
             and p.r2_key <> (
               p_owner_id::text || '/' || locked_document.id::text || '/' ||
               p.content_hash || '.pdf'
             )
           )
           or (
             p.generation_id is not null
             and p.r2_key <> (
               p_owner_id::text || '/' || locked_document.id::text || '/' ||
               p.content_hash || '/' || p.generation_id::text || '.pdf'
             )
           )
         )
    ) then
      return jsonb_build_object('status', 'blocked', 'code', 'document_artifact_invalid');
    end if;

    insert into public.pdf_artifact_cleanup_queue (
      owner_id, document_id, r2_key, content_hash, generation_id, not_before
    )
    select p_owner_id, p.document_id, p.r2_key, p.content_hash, p.generation_id,
           now() + interval '2 hours'
      from public.pdf_artifacts as p
     where p.document_id = locked_document.id
       and p.owner_id = p_owner_id
    on conflict (owner_id, r2_key) do update
      set document_id = excluded.document_id,
          content_hash = excluded.content_hash,
          generation_id = excluded.generation_id,
          not_before = greatest(
            public.pdf_artifact_cleanup_queue.not_before,
            excluded.not_before
          );

    select coalesce(
             jsonb_agg(
               jsonb_build_object(
                 'cleanup_id', q.id,
                 'document_id', q.document_id,
                 'r2_key', q.r2_key,
                 'content_hash', q.content_hash,
                 'generation_id', q.generation_id,
                 'not_before', q.not_before
               )
               order by q.id
             ),
             '[]'::jsonb
           )
      into artifact_snapshot
      from public.pdf_artifact_cleanup_queue as q
     where q.owner_id = p_owner_id
       and q.document_id = locked_document.id;

    delete from public.pdf_artifacts as p
     where p.document_id = locked_document.id
       and p.owner_id = p_owner_id;
  end if;

  update public.documents as d
     set yaml_blob = p_yaml_blob,
         updated_at = p_updated_at,
         accent = case when p_document_type = 'cv' then p_accent else d.accent end,
         font = case when p_document_type = 'cv' then p_font else d.font end,
         density = case when p_document_type = 'cv' then p_density else d.density end,
         summary_company = case
           when p_document_type = 'cover_letter' then left(p_summary_company, 500)
           else d.summary_company
         end,
         summary_role = case
           when p_document_type = 'cover_letter' then left(p_summary_role, 500)
           else d.summary_role
         end
   where d.id = locked_document.id
     and d.owner_id = p_owner_id;

  return jsonb_build_object(
    'status', 'saved',
    'document_id', locked_document.id,
    'document_revision', p_updated_at,
    'render_changed', render_changed,
    'artifacts', artifact_snapshot
  );
end;
$$;

-- Evict one metadata row whose R2 object failed HEAD. The exact artifact id
-- and document revision are both compared under locks; a concurrent build can
-- publish a replacement without that replacement being detached by repair.
create or replace function public.evict_broken_pdf_artifact(
  p_owner_id uuid,
  p_document_id uuid,
  p_document_revision timestamptz,
  p_artifact_id uuid
)
returns jsonb
language plpgsql
security definer
set search_path = ''
set lock_timeout = '2s'
as $$
declare
  locked_revision timestamptz;
  artifact_row public.pdf_artifacts%rowtype;
  cleanup_id uuid;
begin
  if p_owner_id is null or p_document_id is null
     or p_document_revision is null or p_artifact_id is null then
    raise exception 'invalid broken PDF eviction'
      using errcode = '22023';
  end if;

  perform p.id
    from public.profiles as p
   where p.id = p_owner_id
     and p.deletion_requested_at is null
   for share;
  if not found then
    return jsonb_build_object('status', 'blocked', 'code', 'account_unavailable');
  end if;

  select d.updated_at
    into locked_revision
    from public.documents as d
   where d.id = p_document_id
     and d.owner_id = p_owner_id
     and d.deleted_at is null
   for update;
  if locked_revision is null or locked_revision is distinct from p_document_revision then
    return jsonb_build_object('status', 'revision_conflict');
  end if;

  select p.*
    into artifact_row
    from public.pdf_artifacts as p
   where p.id = p_artifact_id
     and p.document_id = p_document_id
     and p.owner_id = p_owner_id
   for update;
  if artifact_row.id is null then
    return jsonb_build_object('status', 'already_replaced');
  end if;

  if artifact_row.content_hash !~ '^[0-9a-f]{64}$'
     or (
       artifact_row.generation_id is null
       and artifact_row.r2_key <> (
         p_owner_id::text || '/' || p_document_id::text || '/' ||
         artifact_row.content_hash || '.pdf'
       )
     )
     or (
       artifact_row.generation_id is not null
       and artifact_row.r2_key <> (
         p_owner_id::text || '/' || p_document_id::text || '/' ||
         artifact_row.content_hash || '/' || artifact_row.generation_id::text || '.pdf'
       )
     ) then
    return jsonb_build_object('status', 'blocked', 'code', 'document_artifact_invalid');
  end if;

  insert into public.pdf_artifact_cleanup_queue (
    owner_id, document_id, r2_key, content_hash, generation_id, not_before
  ) values (
    p_owner_id, artifact_row.document_id, artifact_row.r2_key,
    artifact_row.content_hash, artifact_row.generation_id, now()
  )
  on conflict (owner_id, r2_key) do update
    set document_id = excluded.document_id,
        content_hash = excluded.content_hash,
        generation_id = excluded.generation_id,
        not_before = least(
          public.pdf_artifact_cleanup_queue.not_before,
          excluded.not_before
        )
  returning id into cleanup_id;

  delete from public.pdf_artifacts as p
   where p.id = artifact_row.id
     and p.owner_id = p_owner_id;

  return jsonb_build_object(
    'status', 'evicted',
    'artifact', jsonb_build_object(
      'cleanup_id', cleanup_id,
      'document_id', artifact_row.document_id,
      'r2_key', artifact_row.r2_key,
      'content_hash', artifact_row.content_hash,
      'generation_id', artifact_row.generation_id,
      'not_before', now()
    )
  );
end;
$$;

-- Register a unique prospective object before R2 PUT. If the process crashes
-- or a PUT response is ambiguous, the outbox still owns the only key that
-- request could have written. Successful publication removes this candidate.
create or replace function public.register_pdf_upload_candidate(
  p_owner_id uuid,
  p_document_id uuid,
  p_document_revision timestamptz,
  p_content_hash text,
  p_generation_id uuid,
  p_dependency_document_id uuid,
  p_dependency_revision timestamptz
)
returns jsonb
language plpgsql
security definer
set search_path = ''
set lock_timeout = '2s'
as $$
declare
  target_revision timestamptz;
  dependency_revision timestamptz;
  candidate_key text;
  cleanup_id uuid;
begin
  if p_owner_id is null or p_document_id is null
     or p_document_revision is null or p_generation_id is null
     or p_content_hash !~ '^[0-9a-f]{64}$'
     or ((p_dependency_document_id is null) <> (p_dependency_revision is null))
  then
    raise exception 'invalid PDF upload candidate'
      using errcode = '22023';
  end if;

  perform p.id
    from public.profiles as p
   where p.id = p_owner_id
     and p.deletion_requested_at is null
   for share;
  if not found then
    return jsonb_build_object('status', 'blocked', 'code', 'account_unavailable');
  end if;

  perform d.id
    from public.documents as d
   where d.owner_id = p_owner_id
     and d.deleted_at is null
     and d.id in (p_document_id, p_dependency_document_id)
   order by d.id
   for share;

  select d.updated_at
    into target_revision
    from public.documents as d
   where d.id = p_document_id
     and d.owner_id = p_owner_id
     and d.type in ('cv', 'cover_letter')
     and d.deleted_at is null;
  if target_revision is null or target_revision is distinct from p_document_revision then
    return jsonb_build_object('status', 'revision_conflict');
  end if;

  if p_dependency_document_id is not null then
    select d.updated_at
      into dependency_revision
      from public.documents as d
     where d.id = p_dependency_document_id
       and d.owner_id = p_owner_id
       and d.type = 'cv'
       and d.deleted_at is null;
    if dependency_revision is null
       or dependency_revision is distinct from p_dependency_revision then
      return jsonb_build_object('status', 'revision_conflict');
    end if;
  end if;

  candidate_key := (
    p_owner_id::text || '/' || p_document_id::text || '/' ||
    p_content_hash || '/' || p_generation_id::text || '.pdf'
  );
  insert into public.pdf_artifact_cleanup_queue (
    owner_id, document_id, r2_key, content_hash, generation_id, not_before
  ) values (
    p_owner_id, p_document_id, candidate_key, p_content_hash,
    p_generation_id, now() + interval '2 hours'
  )
  on conflict (owner_id, r2_key) do update
    set document_id = excluded.document_id,
        content_hash = excluded.content_hash,
        generation_id = excluded.generation_id,
        not_before = greatest(
          public.pdf_artifact_cleanup_queue.not_before,
          excluded.not_before
        )
  returning id into cleanup_id;

  return jsonb_build_object(
    'status', 'registered',
    'cleanup_id', cleanup_id,
    'document_id', p_document_id,
    'r2_key', candidate_key,
    'content_hash', p_content_hash,
    'generation_id', p_generation_id
  );
end;
$$;

-- Resolve a cache hit at a linearization point that checks both the rendered
-- document and an optional source-CV dependency under row locks.
create or replace function public.resolve_pdf_artifact(
  p_owner_id uuid,
  p_document_id uuid,
  p_document_revision timestamptz,
  p_content_hash text,
  p_dependency_document_id uuid,
  p_dependency_revision timestamptz
)
returns jsonb
language plpgsql
security definer
set search_path = ''
set lock_timeout = '2s'
as $$
declare
  target_revision timestamptz;
  dependency_revision timestamptz;
  artifact_row public.pdf_artifacts%rowtype;
begin
  if p_owner_id is null or p_document_id is null
     or p_document_revision is null
     or p_content_hash !~ '^[0-9a-f]{64}$'
     or ((p_dependency_document_id is null) <> (p_dependency_revision is null))
  then
    raise exception 'invalid PDF artifact resolution'
      using errcode = '22023';
  end if;

  perform p.id
    from public.profiles as p
   where p.id = p_owner_id
     and p.deletion_requested_at is null
   for share;
  if not found then
    return jsonb_build_object('status', 'blocked', 'code', 'account_unavailable');
  end if;

  perform d.id
    from public.documents as d
   where d.owner_id = p_owner_id
     and d.deleted_at is null
     and d.id in (p_document_id, p_dependency_document_id)
   order by d.id
   for share;

  select d.updated_at
    into target_revision
    from public.documents as d
   where d.id = p_document_id
     and d.owner_id = p_owner_id
     and d.type in ('cv', 'cover_letter')
     and d.deleted_at is null;
  if target_revision is null or target_revision is distinct from p_document_revision then
    return jsonb_build_object('status', 'revision_conflict');
  end if;

  if p_dependency_document_id is not null then
    select d.updated_at
      into dependency_revision
      from public.documents as d
     where d.id = p_dependency_document_id
       and d.owner_id = p_owner_id
       and d.type = 'cv'
       and d.deleted_at is null;
    if dependency_revision is null
       or dependency_revision is distinct from p_dependency_revision then
      return jsonb_build_object('status', 'revision_conflict');
    end if;
  end if;

  select p.*
    into artifact_row
    from public.pdf_artifacts as p
   where p.document_id = p_document_id
     and p.owner_id = p_owner_id
     and p.content_hash = p_content_hash
     and p.stale = false
   limit 1;

  if artifact_row.id is null then
    return jsonb_build_object('status', 'miss');
  end if;
  if artifact_row.generation_id is null then
    if artifact_row.r2_key <> (
      p_owner_id::text || '/' || p_document_id::text || '/' ||
      p_content_hash || '.pdf'
    ) then
      return jsonb_build_object('status', 'blocked', 'code', 'document_artifact_invalid');
    end if;
  elsif artifact_row.r2_key <> (
    p_owner_id::text || '/' || p_document_id::text || '/' ||
    p_content_hash || '/' || artifact_row.generation_id::text || '.pdf'
  ) then
    return jsonb_build_object('status', 'blocked', 'code', 'document_artifact_invalid');
  end if;

  return jsonb_build_object(
    'status', 'hit',
    'artifact_id', artifact_row.id,
    'document_id', artifact_row.document_id,
    'r2_key', artifact_row.r2_key,
    'content_hash', artifact_row.content_hash,
    'generation_id', artifact_row.generation_id,
    'page_count', artifact_row.page_count
  );
end;
$$;

-- Linearization point for direct-stream and storage-fallback exports. The
-- caller renders a previously read revision only when both the account and
-- document are still active at this authorization transaction.
create or replace function public.authorize_document_export(
  p_owner_id uuid,
  p_document_id uuid,
  p_document_revision timestamptz
)
returns jsonb
language plpgsql
security definer
set search_path = ''
set lock_timeout = '2s'
as $$
declare
  current_revision timestamptz;
begin
  if p_owner_id is null or p_document_id is null or p_document_revision is null then
    raise exception 'invalid document export authorization'
      using errcode = '22023';
  end if;
  perform p.id
    from public.profiles as p
   where p.id = p_owner_id
     and p.deletion_requested_at is null
   for share;
  if not found then
    return jsonb_build_object('status', 'blocked', 'code', 'account_unavailable');
  end if;
  select d.updated_at
    into current_revision
    from public.documents as d
   where d.id = p_document_id
     and d.owner_id = p_owner_id
     and d.type in ('cv', 'cover_letter')
     and d.deleted_at is null
   for share;
  if current_revision is null then
    return jsonb_build_object('status', 'blocked', 'code', 'document_unavailable');
  end if;
  if current_revision is distinct from p_document_revision then
    return jsonb_build_object('status', 'revision_conflict');
  end if;
  return jsonb_build_object(
    'status', 'authorized',
    'document_id', p_document_id,
    'document_revision', current_revision
  );
end;
$$;

-- Publish a unique generation only if every rendered input revision is still
-- current. Same-hash concurrent builds keep the first winner and return its
-- key; the losing request can safely delete its never-reused generation key.
create or replace function public.publish_pdf_artifact(
  p_owner_id uuid,
  p_document_id uuid,
  p_document_revision timestamptz,
  p_content_hash text,
  p_generation_id uuid,
  p_page_count integer,
  p_dependency_document_id uuid,
  p_dependency_revision timestamptz
)
returns jsonb
language plpgsql
security definer
set search_path = ''
set lock_timeout = '2s'
as $$
declare
  locked_document_type text;
  locked_revision timestamptz;
  dependency_revision timestamptz;
  candidate_key text;
  existing_artifact public.pdf_artifacts%rowtype;
  published_artifact_id uuid;
  published_key text;
  published_generation_id uuid;
  published_page_count integer;
  cleanup_snapshot jsonb := '[]'::jsonb;
  candidate_cleanup_id uuid;
  candidate_claim_token uuid;
begin
  if p_owner_id is null or p_document_id is null
     or p_document_revision is null or p_generation_id is null
     or p_content_hash !~ '^[0-9a-f]{64}$'
     or p_page_count is null or p_page_count < 1
     or ((p_dependency_document_id is null) <> (p_dependency_revision is null))
  then
    raise exception 'invalid PDF artifact publication'
      using errcode = '22023';
  end if;

  perform p.id
    from public.profiles as p
   where p.id = p_owner_id
     and p.deletion_requested_at is null
   for share;
  if not found then
    return jsonb_build_object('status', 'blocked', 'code', 'account_unavailable');
  end if;

  perform d.id
    from public.documents as d
   where d.owner_id = p_owner_id
     and d.deleted_at is null
     and d.id in (p_document_id, p_dependency_document_id)
   order by d.id
   for update;

  select d.type, d.updated_at
    into locked_document_type, locked_revision
    from public.documents as d
   where d.id = p_document_id
     and d.owner_id = p_owner_id
     and d.type in ('cv', 'cover_letter')
     and d.deleted_at is null;
  if locked_revision is null or locked_revision is distinct from p_document_revision then
    return jsonb_build_object('status', 'revision_conflict');
  end if;

  if p_dependency_document_id is not null then
    select d.updated_at
      into dependency_revision
      from public.documents as d
     where d.id = p_dependency_document_id
       and d.owner_id = p_owner_id
       and d.type = 'cv'
       and d.deleted_at is null;
    if dependency_revision is null
       or dependency_revision is distinct from p_dependency_revision then
      return jsonb_build_object('status', 'revision_conflict');
    end if;
  end if;
  if locked_document_type = 'cv' and p_page_count <> 1 then
    raise exception 'CV artifact must contain exactly one page'
      using errcode = '22023';
  end if;

  candidate_key := (
    p_owner_id::text || '/' || p_document_id::text || '/' ||
    p_content_hash || '/' || p_generation_id::text || '.pdf'
  );

  -- The durable candidate row is the publication lease. Cleanup claims this
  -- exact row before external deletion; publication locks it first and refuses
  -- a claimed or missing candidate, so metadata can never be committed for an
  -- object a worker is concurrently deleting.
  select q.id, q.claim_token
    into candidate_cleanup_id, candidate_claim_token
    from public.pdf_artifact_cleanup_queue as q
   where q.owner_id = p_owner_id
     and q.document_id = p_document_id
     and q.r2_key = candidate_key
     and q.content_hash = p_content_hash
     and q.generation_id = p_generation_id
   for update;

  if candidate_cleanup_id is null then
    return jsonb_build_object('status', 'candidate_missing');
  end if;
  if candidate_claim_token is not null then
    return jsonb_build_object('status', 'candidate_expired');
  end if;

  perform p.id
    from public.pdf_artifacts as p
   where p.document_id = p_document_id
     and p.owner_id = p_owner_id
   for update;

  if exists (
    select 1
      from public.pdf_artifacts as p
     where p.document_id = p_document_id
       and p.owner_id = p_owner_id
       and (
         p.content_hash !~ '^[0-9a-f]{64}$'
         or (
           p.generation_id is null
           and p.r2_key <> (
             p_owner_id::text || '/' || p_document_id::text || '/' ||
             p.content_hash || '.pdf'
           )
         )
         or (
           p.generation_id is not null
           and p.r2_key <> (
             p_owner_id::text || '/' || p_document_id::text || '/' ||
             p.content_hash || '/' || p.generation_id::text || '.pdf'
           )
         )
       )
  ) then
    return jsonb_build_object('status', 'blocked', 'code', 'document_artifact_invalid');
  end if;

  select p.*
    into existing_artifact
    from public.pdf_artifacts as p
   where p.document_id = p_document_id
     and p.owner_id = p_owner_id
     and p.content_hash = p_content_hash
     and p.stale = false
   limit 1;

  insert into public.pdf_artifact_cleanup_queue (
    owner_id, document_id, r2_key, content_hash, generation_id, not_before
  )
  select p_owner_id, p.document_id, p.r2_key, p.content_hash, p.generation_id,
         now() + interval '2 hours'
    from public.pdf_artifacts as p
   where p.document_id = p_document_id
     and p.owner_id = p_owner_id
     and (existing_artifact.id is null or p.id <> existing_artifact.id)
  on conflict (owner_id, r2_key) do update
    set document_id = excluded.document_id,
        content_hash = excluded.content_hash,
        generation_id = excluded.generation_id,
        not_before = greatest(
          public.pdf_artifact_cleanup_queue.not_before,
          excluded.not_before
        );

  delete from public.pdf_artifacts as p
   where p.document_id = p_document_id
     and p.owner_id = p_owner_id
     and (existing_artifact.id is null or p.id <> existing_artifact.id);

  if existing_artifact.id is not null then
    published_artifact_id := existing_artifact.id;
    published_key := existing_artifact.r2_key;
    published_generation_id := existing_artifact.generation_id;
    published_page_count := existing_artifact.page_count;
  else
    insert into public.pdf_artifacts (
      document_id, owner_id, r2_key, content_hash,
      generation_id, page_count, stale
    ) values (
      p_document_id, p_owner_id, candidate_key, p_content_hash,
      p_generation_id, p_page_count, false
    )
    returning id into published_artifact_id;
    published_key := candidate_key;
    published_generation_id := p_generation_id;
    published_page_count := p_page_count;
  end if;

  -- An active generation can never remain cleanup-eligible. The autonomous
  -- worker also rechecks active metadata immediately before external deletion.
  delete from public.pdf_artifact_cleanup_queue as q
   where q.owner_id = p_owner_id
     and q.r2_key = published_key;

  select coalesce(
           jsonb_agg(
             jsonb_build_object(
               'cleanup_id', q.id,
               'document_id', q.document_id,
               'r2_key', q.r2_key,
               'content_hash', q.content_hash,
               'generation_id', q.generation_id,
               'not_before', q.not_before
             )
             order by q.id
           ),
           '[]'::jsonb
         )
    into cleanup_snapshot
    from public.pdf_artifact_cleanup_queue as q
   where q.owner_id = p_owner_id
     and q.document_id = p_document_id;

  return jsonb_build_object(
    'status', case when existing_artifact.id is null then 'published' else 'cached' end,
    'artifact_id', published_artifact_id,
    'document_id', p_document_id,
    'r2_key', published_key,
    'content_hash', p_content_hash,
    'generation_id', published_generation_id,
    'page_count', published_page_count,
    'discard_r2_key', case when existing_artifact.id is null then null else candidate_key end,
    'artifacts', cleanup_snapshot
  );
end;
$$;

create or replace function public.delete_saved_job_safely(p_job_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = ''
set lock_timeout = '2s'
as $$
declare
  caller_id uuid := auth.uid();
  locked_job_id uuid;
begin
  if caller_id is null then
    raise exception 'not authenticated'
      using errcode = '42501';
  end if;

  if exists (
    select 1
      from public.profiles as p
     where p.id = caller_id
       and p.deletion_requested_at is not null
  ) then
    return jsonb_build_object(
      'status', 'blocked',
      'code', 'account_unavailable'
    );
  end if;

  -- FOR UPDATE conflicts with the KEY SHARE lock taken by every FK write.
  -- Once acquired, no new linked document, packet, attempt, or review can be
  -- committed before the checks and DELETE below complete.
  select j.id
    into locked_job_id
    from public.saved_jobs as j
   where j.id = p_job_id
     and j.owner_id = caller_id
   for update;

  if locked_job_id is null then
    return jsonb_build_object(
      'status', 'not_found',
      'code', 'job_not_found'
    );
  end if;

  -- Only APPLICATION history blocks a delete. `job_import_results` is
  -- deliberately absent: its `saved_job_id` FK is ON DELETE SET NULL, so the
  -- import record (jd_text, provenance, verification status) survives the
  -- delete intact and stays owned by its `import_run_id`. Blocking on it would
  -- make every job saved from an import permanently undeletable while
  -- protecting nothing.
  if exists (
    select 1
      from public.documents as d
     where d.job_id = locked_job_id
       and d.owner_id = caller_id
  ) or exists (
    select 1
      from public.application_packets as p
     where p.job_id = locked_job_id
       and p.owner_id = caller_id
  ) or exists (
    select 1
      from public.application_apply_attempts as a
     where a.job_id = locked_job_id
       and a.owner_id = caller_id
  ) or exists (
    select 1
      from public.tailor_review_states as r
     where r.job_id = locked_job_id
       and r.owner_id = caller_id
  ) then
    return jsonb_build_object(
      'status', 'blocked',
      'code', 'job_has_application_history'
    );
  end if;

  delete from public.saved_jobs as j
   where j.id = locked_job_id
     and j.owner_id = caller_id;

  return jsonb_build_object(
    'status', 'deleted',
    'deleted', locked_job_id
  );
end;
$$;

create or replace function public.delete_document_safely(
  p_document_id uuid,
  p_document_type text
)
returns jsonb
language plpgsql
security definer
set search_path = ''
set lock_timeout = '2s'
as $$
declare
  caller_id uuid := auth.uid();
  locked_document_id uuid;
  artifact_snapshot jsonb := '[]'::jsonb;
begin
  if caller_id is null then
    raise exception 'not authenticated'
      using errcode = '42501';
  end if;

  if p_document_type is null
     or p_document_type not in ('cv', 'cover_letter') then
    raise exception 'unsupported document type'
      using errcode = '22023';
  end if;

  if exists (
    select 1
      from public.profiles as p
     where p.id = caller_id
       and p.deletion_requested_at is not null
  ) then
    return jsonb_build_object(
      'status', 'blocked',
      'code', 'account_unavailable'
    );
  end if;

  -- The row lock serializes this delete with packet/review FK writes.  An
  -- insert that starts later waits and then fails its FK after a successful
  -- delete; an insert that commits first is visible to the checks below.
  select d.id
    into locked_document_id
    from public.documents as d
   where d.id = p_document_id
     and d.owner_id = caller_id
     and d.type = p_document_type
     and d.deleted_at is null
   for update;

  if locked_document_id is null then
    return jsonb_build_object(
      'status', 'not_found',
      'code', 'document_not_found'
    );
  end if;

  if exists (
    select 1
      from public.application_packets as p
     where p.owner_id = caller_id
       and (
         p.source_cv_id = locked_document_id
         or p.tailored_cv_id = locked_document_id
         or p.cover_letter_id = locked_document_id
       )
  ) or exists (
    select 1
      from public.tailor_review_states as r
     where r.cv_id = locked_document_id
       and r.owner_id = caller_id
  ) or exists (
    -- Legacy standalone letters resolved their signer live and did not persist
    -- sender_name. Keep their source CV until the letter is regenerated into
    -- the immutable-snapshot format; otherwise deletion would strip its
    -- signature permanently.
    select 1
      from public.documents as cover
     where p_document_type = 'cv'
       and cover.owner_id = caller_id
       and cover.type = 'cover_letter'
       and cover.deleted_at is null
       and cover.yaml_blob ~ (
         '(?m)^source_cv_id:[[:space:]]*["'']?' ||
         locked_document_id::text || '["'']?[[:space:]]*$'
       )
       and cover.yaml_blob !~ '(?m)^sender_name:[[:space:]]*[^[:space:]]'
  ) then
    return jsonb_build_object(
      'status', 'blocked',
      'code', 'document_has_application_history'
    );
  end if;

  -- Lock existing artifact rows before validating them. The parent lock above
  -- blocks new FK inserts; these child locks block concurrent metadata edits.
  perform p.id
    from public.pdf_artifacts as p
   where p.document_id = locked_document_id
     and p.owner_id = caller_id
   for update;

  -- Refuse the delete before any external side effect when artifact metadata
  -- is not the canonical owner/document/hash key. The application performs
  -- the same validation, but the privileged RPC boundary must stand alone.
  if exists (
    select 1
      from public.pdf_artifacts as p
     where p.document_id = locked_document_id
       and p.owner_id = caller_id
       and (
         p.content_hash !~ '^[0-9a-f]{64}$'
         or (
           p.generation_id is null
           and p.r2_key <> (
             caller_id::text || '/' || locked_document_id::text || '/' ||
             p.content_hash || '.pdf'
           )
         )
         or (
           p.generation_id is not null
           and p.r2_key <> (
             caller_id::text || '/' || locked_document_id::text || '/' ||
             p.content_hash || '/' || p.generation_id::text || '.pdf'
           )
         )
       )
  ) then
    return jsonb_build_object(
      'status', 'blocked',
      'code', 'document_artifact_invalid'
    );
  end if;

  -- Lock and enqueue content-addressed object identities in the same
  -- transaction as the document delete. The parent lock prevents new
  -- artifact inserts; row locks prevent metadata drift. Queue rows survive
  -- the document FK cascade, including when a caller invokes this RPC directly
  -- and ignores its response.
  insert into public.pdf_artifact_cleanup_queue (
    owner_id, document_id, r2_key, content_hash, generation_id, not_before
  )
  select caller_id, p.document_id, p.r2_key, p.content_hash, p.generation_id,
         now()
    from public.pdf_artifacts as p
   where p.document_id = locked_document_id
     and p.owner_id = caller_id
  on conflict (owner_id, r2_key) do update
    set document_id = excluded.document_id,
        content_hash = excluded.content_hash,
        generation_id = excluded.generation_id,
        not_before = least(
          public.pdf_artifact_cleanup_queue.not_before,
          excluded.not_before
        );

  select coalesce(
           jsonb_agg(
             jsonb_build_object(
               'cleanup_id', q.id,
               'document_id', q.document_id,
               'r2_key', q.r2_key,
               'content_hash', q.content_hash,
               'generation_id', q.generation_id,
               'not_before', q.not_before
             )
             order by q.id
           ),
           '[]'::jsonb
         )
    into artifact_snapshot
    from public.pdf_artifact_cleanup_queue as q
   where q.owner_id = caller_id
     and q.document_id = locked_document_id;

  delete from public.documents as d
   where d.id = locked_document_id
     and d.owner_id = caller_id
     and d.type = p_document_type;

  return jsonb_build_object(
    'status', 'deleted',
    'deleted', locked_document_id,
    'document_type', p_document_type,
    'artifacts', artifact_snapshot
  );
end;
$$;

-- Atomically claim one due outbox row before touching R2. Publication locks
-- the same candidate row and refuses claimed candidates. This closes the
-- external-delete/publication race that an unlocked SELECT cannot close.
create or replace function public.claim_pdf_artifact_cleanup(
  p_owner_id uuid,
  p_cleanup_id uuid,
  p_claim_token uuid,
  p_force boolean
)
returns jsonb
language plpgsql
security definer
set search_path = ''
set lock_timeout = '2s'
as $$
declare
  queued public.pdf_artifact_cleanup_queue%rowtype;
begin
  if p_owner_id is null or p_cleanup_id is null or p_claim_token is null
     or p_force is null then
    raise exception 'owner, cleanup id, claim token, and force are required'
      using errcode = '22023';
  end if;

  select q.*
    into queued
    from public.pdf_artifact_cleanup_queue as q
   where q.id = p_cleanup_id
     and q.owner_id = p_owner_id
   for update;

  if queued.id is null then
    return jsonb_build_object('status', 'missing');
  end if;
  if not p_force and queued.not_before > now() then
    return jsonb_build_object(
      'status', 'pending',
      'not_before', queued.not_before
    );
  end if;
  if queued.claim_token is not null
     and queued.claimed_at >= now() - interval '1 hour' then
    return jsonb_build_object('status', 'busy');
  end if;

  if queued.content_hash !~ '^[0-9a-f]{64}$'
     or (
       queued.generation_id is null
       and queued.r2_key <> (
         queued.owner_id::text || '/' || queued.document_id::text || '/' ||
         queued.content_hash || '.pdf'
       )
     )
     or (
       queued.generation_id is not null
       and queued.r2_key <> (
         queued.owner_id::text || '/' || queued.document_id::text || '/' ||
         queued.content_hash || '/' || queued.generation_id::text || '.pdf'
       )
     ) then
    update public.pdf_artifact_cleanup_queue as q
       set attempt_count = q.attempt_count + 1,
           last_attempt_at = now()
     where q.id = queued.id
       and q.owner_id = queued.owner_id;
    return jsonb_build_object(
      'status', 'blocked',
      'code', 'cleanup_key_invalid',
      'failure_recorded', true
    );
  end if;

  -- A queue row that still names active metadata is stale queue state, not
  -- deletion authority. Remove the hazardous queue row without touching R2.
  if exists (
    select 1
      from public.pdf_artifacts as p
     where p.owner_id = queued.owner_id
       and p.document_id = queued.document_id
       and p.r2_key = queued.r2_key
       and p.content_hash = queued.content_hash
       and p.generation_id is not distinct from queued.generation_id
       and p.stale = false
  ) then
    delete from public.pdf_artifact_cleanup_queue as q
     where q.id = queued.id
       and q.owner_id = queued.owner_id;
    return jsonb_build_object('status', 'active');
  end if;

  update public.pdf_artifact_cleanup_queue as q
     set claim_token = p_claim_token,
         claimed_at = now()
   where q.id = queued.id
     and q.owner_id = queued.owner_id;

  return jsonb_build_object(
    'status', 'claimed',
    'cleanup_id', queued.id,
    'owner_id', queued.owner_id,
    'document_id', queued.document_id,
    'r2_key', queued.r2_key,
    'content_hash', queued.content_hash,
    'generation_id', queued.generation_id,
    'not_before', queued.not_before,
    'attempt_count', queued.attempt_count,
    'claim_token', p_claim_token
  );
end;
$$;

create or replace function public.complete_pdf_artifact_cleanup(
  p_owner_id uuid,
  p_cleanup_id uuid,
  p_claim_token uuid
)
returns boolean
language plpgsql
security definer
set search_path = ''
set lock_timeout = '2s'
as $$
declare
  removed_id uuid;
begin
  if p_owner_id is null or p_cleanup_id is null or p_claim_token is null then
    raise exception 'owner, cleanup id, and claim token are required'
      using errcode = '22023';
  end if;

  delete from public.pdf_artifact_cleanup_queue as q
   where q.id = p_cleanup_id
     and q.owner_id = p_owner_id
     and q.claim_token = p_claim_token
  returning q.id into removed_id;

  return removed_id is not null;
end;
$$;

create or replace function public.record_pdf_artifact_cleanup_failure(
  p_owner_id uuid,
  p_cleanup_id uuid,
  p_claim_token uuid
)
returns boolean
language plpgsql
security definer
set search_path = ''
set lock_timeout = '2s'
as $$
declare
  updated_id uuid;
begin
  if p_owner_id is null or p_cleanup_id is null or p_claim_token is null then
    raise exception 'owner, cleanup id, and claim token are required'
      using errcode = '22023';
  end if;

  update public.pdf_artifact_cleanup_queue as q
     set attempt_count = q.attempt_count + 1,
         last_attempt_at = now(),
         claim_token = null,
         claimed_at = null
   where q.id = p_cleanup_id
     and q.owner_id = p_owner_id
     and q.claim_token = p_claim_token
  returning q.id into updated_id;

  return updated_id is not null;
end;
$$;

-- Mark the account unavailable and detach every currently published PDF in
-- one transaction. PDF register/resolve/publish functions lock the same
-- profile row in SHARE mode before document work, so a build linearizes either
-- wholly before this transition (and is detached here) or wholly after it (and
-- is rejected). Prospective upload candidates retain their build lease: a PUT
-- already in flight may finish late, and its durable row cleans that unique key
-- after the lease instead of creating an orphan.
create or replace function public.request_account_deletion_with_artifact_detach()
returns jsonb
language plpgsql
security definer
set search_path = ''
set lock_timeout = '2s'
as $$
declare
  caller_id uuid := auth.uid();
  profile_row public.profiles%rowtype;
  requested_at timestamptz;
  artifact_snapshot jsonb := '[]'::jsonb;
  invalid_artifact_count integer := 0;
begin
  if caller_id is null then
    raise exception 'not authenticated'
      using errcode = '42501';
  end if;

  select p.*
    into profile_row
    from public.profiles as p
   where p.id = caller_id
   for update;
  if profile_row.id is null then
    raise exception 'profile not found'
      using errcode = 'P0002';
  end if;
  if exists (
    select 1
      from private.account_deletion_claims as c
     where c.owner_id = caller_id
  ) then
    raise exception 'account deletion is already being processed'
      using errcode = '55000';
  end if;
  if profile_row.deletion_requested_at is not null then
    return jsonb_build_object(
      'status', 'already_requested',
      'requested_at', profile_row.deletion_requested_at,
      'artifacts', '[]'::jsonb,
      'invalid_artifact_count', 0
    );
  end if;

  requested_at := statement_timestamp();
  update public.profiles as p
     set deletion_requested_at = requested_at,
         updated_at = requested_at
   where p.id = caller_id;

  perform p.id
    from public.pdf_artifacts as p
   where p.owner_id = caller_id
   order by p.document_id, p.id
   for update;

  select count(*)
    into invalid_artifact_count
    from public.pdf_artifacts as p
   where p.owner_id = caller_id
     and (
       p.content_hash !~ '^[0-9a-f]{64}$'
       or (
         p.generation_id is null
         and p.r2_key <> (
           caller_id::text || '/' || p.document_id::text || '/' ||
           p.content_hash || '.pdf'
         )
       )
       or (
         p.generation_id is not null
         and p.r2_key <> (
           caller_id::text || '/' || p.document_id::text || '/' ||
           p.content_hash || '/' || p.generation_id::text || '.pdf'
         )
       )
     );

  insert into public.pdf_artifact_cleanup_queue (
    owner_id, document_id, r2_key, content_hash, generation_id, not_before
  )
  select caller_id, p.document_id, p.r2_key, p.content_hash, p.generation_id,
         now()
    from public.pdf_artifacts as p
   where p.owner_id = caller_id
     and p.content_hash ~ '^[0-9a-f]{64}$'
     and (
       (
         p.generation_id is null
         and p.r2_key = (
           caller_id::text || '/' || p.document_id::text || '/' ||
           p.content_hash || '.pdf'
         )
       )
       or (
         p.generation_id is not null
         and p.r2_key = (
           caller_id::text || '/' || p.document_id::text || '/' ||
           p.content_hash || '/' || p.generation_id::text || '.pdf'
         )
       )
     )
  on conflict (owner_id, r2_key) do update
    set document_id = excluded.document_id,
        content_hash = excluded.content_hash,
        generation_id = excluded.generation_id,
        not_before = least(
          public.pdf_artifact_cleanup_queue.not_before,
          excluded.not_before
        );

  select coalesce(
           jsonb_agg(
             jsonb_build_object(
               'cleanup_id', q.id,
               'document_id', q.document_id,
               'r2_key', q.r2_key,
               'content_hash', q.content_hash,
               'generation_id', q.generation_id,
               'not_before', q.not_before
             )
             order by q.id
           ),
           '[]'::jsonb
         )
    into artifact_snapshot
    from public.pdf_artifact_cleanup_queue as q
    join public.pdf_artifacts as p
      on p.owner_id = q.owner_id
     and p.r2_key = q.r2_key
   where q.owner_id = caller_id;

  delete from public.pdf_artifacts as p
   where p.owner_id = caller_id;
  update public.documents as d
     set deleted_at = requested_at,
         updated_at = requested_at
   where d.owner_id = caller_id
     and d.deleted_at is null;

  return jsonb_build_object(
    'status', 'requested',
    'requested_at', requested_at,
    'artifacts', artifact_snapshot,
    'invalid_artifact_count', invalid_artifact_count
  );
end;
$$;

-- Replace the one active packet lineage row atomically. Regeneration cannot
-- archive in one HTTP request and insert in another: the unique active index,
-- version allocation, archive, and new pending row now share one transaction.
create or replace function public.replace_active_application_packet(
  p_owner_id uuid,
  p_payload jsonb,
  p_now timestamptz
)
returns jsonb
language plpgsql
security definer
set search_path = ''
set lock_timeout = '2s'
as $$
declare
  v_job_id uuid := nullif(p_payload ->> 'job_id', '')::uuid;
  v_source_cv_id uuid := nullif(p_payload ->> 'source_cv_id', '')::uuid;
  v_tailored_cv_id uuid := nullif(p_payload ->> 'tailored_cv_id', '')::uuid;
  v_cover_letter_id uuid := nullif(p_payload ->> 'cover_letter_id', '')::uuid;
  next_version integer;
  created_packet public.application_packets%rowtype;
  archived_ids jsonb := '[]'::jsonb;
begin
  if p_owner_id is null or p_payload is null or p_now is null
     or v_job_id is null or v_source_cv_id is null
     or v_tailored_cv_id is null or v_cover_letter_id is null then
    raise exception 'invalid application packet replacement'
      using errcode = '22023';
  end if;

  perform p.id
    from public.profiles as p
   where p.id = p_owner_id
     and p.deletion_requested_at is null
   for share;
  if not found then
    return jsonb_build_object('status', 'blocked', 'code', 'account_unavailable');
  end if;
  perform j.id
    from public.saved_jobs as j
   where j.id = v_job_id
     and j.owner_id = p_owner_id
   for share;
  if not found then
    return jsonb_build_object('status', 'blocked', 'code', 'job_not_found');
  end if;
  if not exists (
    select 1 from public.documents as d
     where d.id = v_source_cv_id and d.owner_id = p_owner_id
       and d.type = 'cv' and d.deleted_at is null
  ) or not exists (
    select 1 from public.documents as d
     where d.id = v_tailored_cv_id and d.owner_id = p_owner_id
       and d.type = 'cv' and d.job_id = v_job_id and d.deleted_at is null
  ) or not exists (
    select 1 from public.documents as d
     where d.id = v_cover_letter_id and d.owner_id = p_owner_id
       and d.type = 'cover_letter' and d.job_id = v_job_id and d.deleted_at is null
  ) then
    return jsonb_build_object('status', 'blocked', 'code', 'packet_document_invalid');
  end if;

  perform p.id
    from public.application_packets as p
   where p.owner_id = p_owner_id
     and p.job_id = v_job_id
     and p.source_cv_id = v_source_cv_id
     and p.archived_at is null
   order by p.id
   for update;

  select coalesce(max(p.packet_version), 0) + 1
    into next_version
    from public.application_packets as p
   where p.owner_id = p_owner_id
     and p.job_id = v_job_id
     and p.source_cv_id = v_source_cv_id;

  with archived as (
    update public.application_packets as p
       set status = 'archived',
           archived_at = p_now,
           updated_at = p_now
     where p.owner_id = p_owner_id
       and p.job_id = v_job_id
       and p.source_cv_id = v_source_cv_id
       and p.archived_at is null
    returning p.id
  )
  select coalesce(jsonb_agg(a.id order by a.id), '[]'::jsonb)
    into archived_ids
    from archived as a;

  insert into public.application_packets (
    owner_id, job_id, source_cv_id, tailored_cv_id, cover_letter_id,
    status, review_status, provider, model, prompt_version,
    score_system_id, score_snapshot, source_cv_hash, job_content_hash,
    tailored_cv_hash, cover_letter_hash, input_hash, output_hash,
    packet_version, profile_answers_complete, legal_answers_confirmed,
    visa_answers_confirmed, cover_letter_review_status,
    cover_letter_review_confirmed_at, cover_letter_review_id, updated_at
  ) values (
    p_owner_id, v_job_id, v_source_cv_id, v_tailored_cv_id, v_cover_letter_id,
    coalesce(p_payload ->> 'status', 'review_applied'),
    coalesce(p_payload ->> 'review_status', 'reviewed'),
    nullif(p_payload ->> 'provider', ''), nullif(p_payload ->> 'model', ''),
    nullif(p_payload ->> 'prompt_version', ''),
    coalesce(nullif(p_payload ->> 'score_system_id', ''), 'semantic_fit'),
    coalesce(p_payload -> 'score_snapshot', '{}'::jsonb),
    nullif(p_payload ->> 'source_cv_hash', ''),
    nullif(p_payload ->> 'job_content_hash', ''),
    nullif(p_payload ->> 'tailored_cv_hash', ''),
    nullif(p_payload ->> 'cover_letter_hash', ''),
    nullif(p_payload ->> 'input_hash', ''),
    nullif(p_payload ->> 'output_hash', ''),
    next_version,
    coalesce((p_payload ->> 'profile_answers_complete')::boolean, false),
    coalesce((p_payload ->> 'legal_answers_confirmed')::boolean, false),
    coalesce((p_payload ->> 'visa_answers_confirmed')::boolean, false),
    coalesce(p_payload ->> 'cover_letter_review_status', 'pending'),
    nullif(p_payload ->> 'cover_letter_review_confirmed_at', '')::timestamptz,
    nullif(p_payload ->> 'cover_letter_review_id', ''),
    p_now
  )
  returning * into created_packet;

  return jsonb_build_object(
    'status', 'created',
    'packet', to_jsonb(created_packet),
    'archived_ids', archived_ids
  );
end;
$$;

revoke execute on function public.delete_saved_job_safely(uuid)
  from public, anon, authenticated;
grant execute on function public.delete_saved_job_safely(uuid)
  to authenticated;

revoke execute on function public.delete_document_safely(uuid, text)
  from public, anon, authenticated;
grant execute on function public.delete_document_safely(uuid, text)
  to authenticated;

revoke execute on function public.request_account_deletion_with_artifact_detach()
  from public, anon, authenticated;
grant execute on function public.request_account_deletion_with_artifact_detach()
  to authenticated;
revoke execute on function public.request_account_deletion()
  from authenticated;

revoke execute on function public.replace_active_application_packet(
  uuid, jsonb, timestamptz
)
  from public, anon, authenticated;
grant execute on function public.replace_active_application_packet(
  uuid, jsonb, timestamptz
)
  to service_role;

revoke execute on function public.detach_pdf_artifacts(uuid, uuid)
  from public, anon, authenticated;
grant execute on function public.detach_pdf_artifacts(uuid, uuid)
  to service_role;

revoke execute on function public.detach_owner_cv_pdf_artifacts(uuid)
  from public, anon, authenticated;
grant execute on function public.detach_owner_cv_pdf_artifacts(uuid)
  to service_role;

revoke execute on function public.insert_document_if_account_active(
  text, text, text, uuid, text, text, text, text, text
)
  from public, anon, authenticated;
grant execute on function public.insert_document_if_account_active(
  text, text, text, uuid, text, text, text, text, text
)
  to authenticated;

revoke execute on function public.update_document_with_artifact_detach(
  uuid, uuid, text, timestamptz, text, timestamptz, text, text, text, text, text
)
  from public, anon, authenticated;
grant execute on function public.update_document_with_artifact_detach(
  uuid, uuid, text, timestamptz, text, timestamptz, text, text, text, text, text
)
  to service_role;

revoke execute on function public.evict_broken_pdf_artifact(
  uuid, uuid, timestamptz, uuid
)
  from public, anon, authenticated;
grant execute on function public.evict_broken_pdf_artifact(
  uuid, uuid, timestamptz, uuid
)
  to service_role;

revoke execute on function public.register_pdf_upload_candidate(
  uuid, uuid, timestamptz, text, uuid, uuid, timestamptz
)
  from public, anon, authenticated;
grant execute on function public.register_pdf_upload_candidate(
  uuid, uuid, timestamptz, text, uuid, uuid, timestamptz
)
  to service_role;

revoke execute on function public.resolve_pdf_artifact(
  uuid, uuid, timestamptz, text, uuid, timestamptz
)
  from public, anon, authenticated;
grant execute on function public.resolve_pdf_artifact(
  uuid, uuid, timestamptz, text, uuid, timestamptz
)
  to service_role;

revoke execute on function public.authorize_document_export(
  uuid, uuid, timestamptz
)
  from public, anon, authenticated;
grant execute on function public.authorize_document_export(
  uuid, uuid, timestamptz
)
  to service_role;

revoke execute on function public.publish_pdf_artifact(
  uuid, uuid, timestamptz, text, uuid, integer, uuid, timestamptz
)
  from public, anon, authenticated;
grant execute on function public.publish_pdf_artifact(
  uuid, uuid, timestamptz, text, uuid, integer, uuid, timestamptz
)
  to service_role;

revoke execute on function public.claim_pdf_artifact_cleanup(
  uuid, uuid, uuid, boolean
)
  from public, anon, authenticated;
grant execute on function public.claim_pdf_artifact_cleanup(
  uuid, uuid, uuid, boolean
)
  to service_role;

revoke execute on function public.complete_pdf_artifact_cleanup(uuid, uuid, uuid)
  from public, anon, authenticated;
grant execute on function public.complete_pdf_artifact_cleanup(uuid, uuid, uuid)
  to service_role;

revoke execute on function public.record_pdf_artifact_cleanup_failure(uuid, uuid, uuid)
  from public, anon, authenticated;
grant execute on function public.record_pdf_artifact_cleanup_failure(uuid, uuid, uuid)
  to service_role;

revoke execute on function public.purge_stale_pdf_artifacts(int)
  from public, anon, authenticated;

-- Bind every successful external-apply action to one locked, current packet
-- snapshot. The browser-facing service may preflight readiness, but only this
-- transaction can author success evidence after the account, target, packet,
-- documents, and persisted PDF artifacts have been revalidated together.
create or replace function public.finalize_application_apply_action(
  p_owner_id uuid,
  p_job_id uuid,
  p_packet_id uuid,
  p_action text,
  p_status text,
  p_reason_code text,
  p_target_url text,
  p_target_host text,
  p_target_classification text,
  p_expected jsonb,
  p_readiness jsonb,
  p_confirmations jsonb,
  p_metadata jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = ''
set lock_timeout = '2s'
as $$
declare
  job_row public.saved_jobs%rowtype;
  packet_row public.application_packets%rowtype;
  source_row public.documents%rowtype;
  tailored_row public.documents%rowtype;
  letter_row public.documents%rowtype;
  tailored_artifact public.pdf_artifacts%rowtype;
  letter_artifact public.pdf_artifacts%rowtype;
  inserted public.application_apply_attempts%rowtype;
  expected_job_updated_at timestamptz;
  expected_packet_updated_at timestamptz;
  expected_source_updated_at timestamptz;
  expected_tailored_updated_at timestamptz;
  expected_letter_updated_at timestamptz;
  expected_source_id uuid;
  expected_tailored_id uuid;
  expected_letter_id uuid;
  expected_tailored_artifact_id uuid;
  expected_letter_artifact_id uuid;
  expected_tailored_generation_id uuid;
  expected_letter_generation_id uuid;
  expected_tailored_page_count integer;
  expected_letter_page_count integer;
  expected_packet_version integer;
  expected_reason text;
begin
  if p_owner_id is null or p_job_id is null or p_packet_id is null then
    raise exception 'owner, job, and packet are required'
      using errcode = '22023';
  end if;
  if p_action is null or p_status is null
     or not (
       (p_action = 'manual_open' and p_status = 'manual_open_ready')
       or (p_action = 'dry_run' and p_status = 'dry_run_completed')
     ) then
    raise exception 'invalid successful application action'
      using errcode = '22023';
  end if;
  if p_target_classification is null
     or p_target_classification not in (
       'linkedin', 'known_ats', 'safe_fixture', 'unknown'
     )
     or p_target_url is null or btrim(p_target_url) = ''
     or char_length(p_target_url) > 2048
     or p_target_host is null or btrim(p_target_host) = ''
     or char_length(p_target_host) > 253 then
    raise exception 'invalid successful application target'
      using errcode = '22023';
  end if;
  if p_action = 'dry_run' and p_target_classification <> 'safe_fixture' then
    raise exception 'dry-run target is not a safe fixture'
      using errcode = '22023';
  end if;
  expected_reason := case p_target_classification
    when 'linkedin' then 'linkedin_manual_open_only'
    when 'known_ats' then 'known_ats_manual_open_only'
    when 'safe_fixture' then 'safe_fixture_host'
    when 'unknown' then 'unknown_manual_open_only'
    else null
  end;
  if p_reason_code is null
     or p_reason_code is distinct from expected_reason
     or p_reason_code !~ '^[a-z0-9_]{1,100}$' then
    raise exception 'invalid successful application reason'
      using errcode = '22023';
  end if;

  if p_expected is null
     or jsonb_typeof(p_expected) <> 'object'
     or pg_column_size(p_expected) > 131072
     or not p_expected ?& array[
       'job_updated_at', 'job_source_url', 'job_jd_text', 'job_content_hash',
       'packet_updated_at', 'prompt_version', 'packet_version',
       'source_cv_id', 'source_cv_updated_at', 'source_cv_hash',
       'tailored_cv_id', 'tailored_cv_updated_at', 'tailored_cv_hash',
       'tailored_cv_accent', 'tailored_cv_font',
       'cover_letter_id', 'cover_letter_updated_at', 'cover_letter_hash',
       'target_url', 'target_host', 'target_classification',
       'tailored_artifact_id', 'tailored_artifact_r2_key',
       'tailored_artifact_content_hash', 'tailored_artifact_generation_id',
       'tailored_artifact_page_count', 'letter_artifact_id',
       'letter_artifact_r2_key', 'letter_artifact_content_hash',
       'letter_artifact_generation_id', 'letter_artifact_page_count'
     ] then
    raise exception 'invalid expected application state'
      using errcode = '22023';
  end if;
  if p_readiness is null
     or jsonb_typeof(p_readiness) <> 'object'
     or pg_column_size(p_readiness) > 16384
     or p_readiness -> 'ready' is distinct from 'true'::jsonb
     or jsonb_typeof(p_readiness -> 'blockers') is distinct from 'array'
     or jsonb_array_length(p_readiness -> 'blockers') <> 0 then
    raise exception 'invalid successful application readiness'
      using errcode = '22023';
  end if;
  if p_confirmations is null
     or jsonb_typeof(p_confirmations) <> 'object'
     or pg_column_size(p_confirmations) > 16384
     or p_confirmations -> 'user_confirmed' is distinct from 'true'::jsonb
     or p_confirmations -> 'reviewed_packet_confirmed' is distinct from 'true'::jsonb
     or p_confirmations -> 'profile_answers_complete' is distinct from 'true'::jsonb
     or p_confirmations -> 'legal_answers_confirmed' is distinct from 'true'::jsonb
     or p_confirmations -> 'visa_answers_confirmed' is distinct from 'true'::jsonb then
    raise exception 'invalid successful application confirmations'
      using errcode = '22023';
  end if;
  if p_metadata is null
     or jsonb_typeof(p_metadata) <> 'object'
     or pg_column_size(p_metadata) > 16384 then
    raise exception 'invalid successful application metadata'
      using errcode = '22023';
  end if;

  begin
    expected_job_updated_at := nullif(p_expected ->> 'job_updated_at', '')::timestamptz;
    expected_packet_updated_at := nullif(p_expected ->> 'packet_updated_at', '')::timestamptz;
    expected_source_updated_at := nullif(p_expected ->> 'source_cv_updated_at', '')::timestamptz;
    expected_tailored_updated_at := nullif(p_expected ->> 'tailored_cv_updated_at', '')::timestamptz;
    expected_letter_updated_at := nullif(p_expected ->> 'cover_letter_updated_at', '')::timestamptz;
    expected_source_id := nullif(p_expected ->> 'source_cv_id', '')::uuid;
    expected_tailored_id := nullif(p_expected ->> 'tailored_cv_id', '')::uuid;
    expected_letter_id := nullif(p_expected ->> 'cover_letter_id', '')::uuid;
    expected_tailored_artifact_id := nullif(p_expected ->> 'tailored_artifact_id', '')::uuid;
    expected_letter_artifact_id := nullif(p_expected ->> 'letter_artifact_id', '')::uuid;
    expected_tailored_generation_id := nullif(
      p_expected ->> 'tailored_artifact_generation_id', ''
    )::uuid;
    expected_letter_generation_id := nullif(
      p_expected ->> 'letter_artifact_generation_id', ''
    )::uuid;
    expected_tailored_page_count := nullif(
      p_expected ->> 'tailored_artifact_page_count', ''
    )::integer;
    expected_letter_page_count := nullif(
      p_expected ->> 'letter_artifact_page_count', ''
    )::integer;
    expected_packet_version := nullif(p_expected ->> 'packet_version', '')::integer;
  exception when others then
    raise exception 'malformed expected application state'
      using errcode = '22023';
  end;
  if expected_job_updated_at is null or expected_packet_updated_at is null
     or expected_source_updated_at is null or expected_tailored_updated_at is null
     or expected_letter_updated_at is null or expected_source_id is null
     or expected_tailored_id is null or expected_letter_id is null
     or expected_tailored_artifact_id is null or expected_letter_artifact_id is null
     or expected_tailored_page_count is null or expected_letter_page_count is null
     or expected_packet_version is null
     or p_expected ->> 'job_content_hash' is null
     or p_expected ->> 'job_content_hash' !~ '^[0-9a-f]{64}$'
     or p_expected ->> 'source_cv_hash' is null
     or p_expected ->> 'source_cv_hash' !~ '^[0-9a-f]{64}$'
     or p_expected ->> 'tailored_cv_hash' is null
     or p_expected ->> 'tailored_cv_hash' !~ '^[0-9a-f]{64}$'
     or p_expected ->> 'cover_letter_hash' is null
     or p_expected ->> 'cover_letter_hash' !~ '^[0-9a-f]{64}$'
     or p_expected ->> 'tailored_artifact_content_hash' is null
     or p_expected ->> 'tailored_artifact_content_hash' !~ '^[0-9a-f]{64}$'
     or p_expected ->> 'letter_artifact_content_hash' is null
     or p_expected ->> 'letter_artifact_content_hash' !~ '^[0-9a-f]{64}$' then
    raise exception 'incomplete expected application state'
      using errcode = '22023';
  end if;
  if p_expected ->> 'target_url' is distinct from p_target_url
     or p_expected ->> 'target_host' is distinct from p_target_host
     or p_expected ->> 'target_classification' is distinct from p_target_classification then
    raise exception 'inconsistent expected application target'
      using errcode = '22023';
  end if;

  perform p.id
    from public.profiles as p
   where p.id = p_owner_id
     and p.deletion_requested_at is null
   for share;
  if not found then
    return jsonb_build_object('status', 'blocked', 'code', 'account_unavailable');
  end if;

  select j.* into job_row
    from public.saved_jobs as j
   where j.id = p_job_id
     and j.owner_id = p_owner_id
   for share;
  if job_row.id is null then
    return jsonb_build_object('status', 'blocked', 'code', 'job_unavailable');
  end if;
  if job_row.updated_at is distinct from expected_job_updated_at
     or coalesce(job_row.source_url, '') is distinct from (p_expected ->> 'job_source_url')
     or coalesce(job_row.jd_text, '') is distinct from (p_expected ->> 'job_jd_text')
     or coalesce(job_row.source_metadata ->> 'full_jd_status', '') = 'missing'
     or btrim(coalesce(job_row.jd_text, '')) = ''
     or pg_catalog.encode(
       pg_catalog.sha256(pg_catalog.convert_to(coalesce(job_row.jd_text, ''), 'UTF8')),
       'hex'
     ) is distinct from (p_expected ->> 'job_content_hash') then
    return jsonb_build_object('status', 'blocked', 'code', 'target_changed');
  end if;

  select p.* into packet_row
    from public.application_packets as p
   where p.id = p_packet_id
     and p.owner_id = p_owner_id
     and p.job_id = p_job_id
   for share;
  if packet_row.id is null then
    return jsonb_build_object('status', 'blocked', 'code', 'packet_changed');
  end if;
  if packet_row.updated_at is distinct from expected_packet_updated_at
     or packet_row.status <> 'packet_created'
     or packet_row.review_status <> 'reviewed'
     or packet_row.archived_at is not null
     or packet_row.prompt_version is distinct from 'jobs_packet.v1'
     or packet_row.prompt_version is distinct from (p_expected ->> 'prompt_version')
     or packet_row.packet_version < 1
     or packet_row.packet_version is distinct from expected_packet_version
     or packet_row.source_cv_id is distinct from expected_source_id
     or packet_row.tailored_cv_id is distinct from expected_tailored_id
     or packet_row.cover_letter_id is distinct from expected_letter_id
     or packet_row.source_cv_hash is distinct from (p_expected ->> 'source_cv_hash')
     or packet_row.job_content_hash is distinct from (p_expected ->> 'job_content_hash')
     or packet_row.tailored_cv_hash is distinct from (p_expected ->> 'tailored_cv_hash')
     or packet_row.cover_letter_hash is distinct from (p_expected ->> 'cover_letter_hash')
     or packet_row.profile_answers_complete is not true
     or packet_row.legal_answers_confirmed is not true
     or packet_row.visa_answers_confirmed is not true
     or packet_row.cover_letter_review_status <> 'confirmed'
     or packet_row.cover_letter_review_confirmed_at is null
     or nullif(packet_row.cover_letter_review_id, '') is null then
    return jsonb_build_object('status', 'blocked', 'code', 'packet_changed');
  end if;

  select d.* into source_row
    from public.documents as d
   where d.id = expected_source_id
     and d.owner_id = p_owner_id
   for share;
  if source_row.id is null or source_row.type <> 'cv'
     or source_row.deleted_at is not null
     or source_row.updated_at is distinct from expected_source_updated_at
     or pg_catalog.encode(
       pg_catalog.sha256(pg_catalog.convert_to(coalesce(source_row.yaml_blob, ''), 'UTF8')),
       'hex'
     ) is distinct from (p_expected ->> 'source_cv_hash') then
    return jsonb_build_object('status', 'blocked', 'code', 'document_changed');
  end if;

  select d.* into tailored_row
    from public.documents as d
   where d.id = expected_tailored_id
     and d.owner_id = p_owner_id
   for share;
  if tailored_row.id is null or tailored_row.type <> 'cv'
     or tailored_row.job_id is distinct from p_job_id
     or tailored_row.deleted_at is not null
     or tailored_row.updated_at is distinct from expected_tailored_updated_at
     or coalesce(tailored_row.accent, '') is distinct from (p_expected ->> 'tailored_cv_accent')
     or coalesce(tailored_row.font, '') is distinct from (p_expected ->> 'tailored_cv_font')
     or pg_catalog.encode(
       pg_catalog.sha256(pg_catalog.convert_to(coalesce(tailored_row.yaml_blob, ''), 'UTF8')),
       'hex'
     ) is distinct from (p_expected ->> 'tailored_cv_hash') then
    return jsonb_build_object('status', 'blocked', 'code', 'document_changed');
  end if;

  select d.* into letter_row
    from public.documents as d
   where d.id = expected_letter_id
     and d.owner_id = p_owner_id
   for share;
  if letter_row.id is null or letter_row.type <> 'cover_letter'
     or letter_row.job_id is distinct from p_job_id
     or letter_row.deleted_at is not null
     or letter_row.updated_at is distinct from expected_letter_updated_at
     or pg_catalog.encode(
       pg_catalog.sha256(pg_catalog.convert_to(coalesce(letter_row.yaml_blob, ''), 'UTF8')),
       'hex'
     ) is distinct from (p_expected ->> 'cover_letter_hash') then
    return jsonb_build_object('status', 'blocked', 'code', 'document_changed');
  end if;

  select a.* into tailored_artifact
    from public.pdf_artifacts as a
   where a.id = expected_tailored_artifact_id
     and a.owner_id = p_owner_id
   for share;
  if tailored_artifact.id is null
     or tailored_artifact.document_id is distinct from expected_tailored_id
     or tailored_artifact.stale is not false
     or tailored_artifact.r2_key is distinct from (p_expected ->> 'tailored_artifact_r2_key')
     or tailored_artifact.content_hash is distinct from (p_expected ->> 'tailored_artifact_content_hash')
     or tailored_artifact.generation_id is distinct from expected_tailored_generation_id
     or tailored_artifact.page_count is distinct from expected_tailored_page_count
     or tailored_artifact.page_count <> 1 then
    return jsonb_build_object('status', 'blocked', 'code', 'artifact_changed');
  end if;

  select a.* into letter_artifact
    from public.pdf_artifacts as a
   where a.id = expected_letter_artifact_id
     and a.owner_id = p_owner_id
   for share;
  if letter_artifact.id is null
     or letter_artifact.document_id is distinct from expected_letter_id
     or letter_artifact.stale is not false
     or letter_artifact.r2_key is distinct from (p_expected ->> 'letter_artifact_r2_key')
     or letter_artifact.content_hash is distinct from (p_expected ->> 'letter_artifact_content_hash')
     or letter_artifact.generation_id is distinct from expected_letter_generation_id
     or letter_artifact.page_count is distinct from expected_letter_page_count
     or letter_artifact.page_count < 1 then
    return jsonb_build_object('status', 'blocked', 'code', 'artifact_changed');
  end if;

  insert into public.application_apply_attempts (
    owner_id, job_id, packet_id, action, status, reason_code, target_url,
    target_host, target_classification, submitted, automation_performed,
    readiness, confirmations, metadata, created_at, updated_at
  ) values (
    p_owner_id, p_job_id, p_packet_id, p_action, p_status, p_reason_code,
    p_target_url, p_target_host, p_target_classification, false, false,
    p_readiness, p_confirmations, p_metadata,
    statement_timestamp(), statement_timestamp()
  )
  returning * into inserted;

  return jsonb_build_object(
    'status', 'recorded',
    'attempt', to_jsonb(inserted)
  );
end;
$$;

revoke execute on function public.finalize_application_apply_action(
  uuid, uuid, uuid, text, text, text, text, text, text,
  jsonb, jsonb, jsonb, jsonb
) from public, anon, authenticated;
grant execute on function public.finalize_application_apply_action(
  uuid, uuid, uuid, text, text, text, text, text, text,
  jsonb, jsonb, jsonb, jsonb
) to service_role;
