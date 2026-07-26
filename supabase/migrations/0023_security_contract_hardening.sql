-- CV Copilot — close authenticated write and worker-contract gaps.
--
-- This is a forward-only migration for databases that already applied the
-- earlier release ledger. It deliberately reissues the trigger-function
-- EXECUTE revocation that was once stored in the invalid 0001a filename.

revoke execute on function public.handle_new_user()
  from public, anon, authenticated;

-- Soft-deleted documents and every document owned by an account pending
-- erasure must disappear from the authenticated Data API. The cancellation
-- RPC is SECURITY DEFINER and can restore the exact deletion-stamped rows
-- without relying on this SELECT policy.
drop policy if exists "documents_select_own" on public.documents;
create policy "documents_select_own" on public.documents
  for select to authenticated
  using (
    owner_id = (select auth.uid())
    and deleted_at is null
    and not exists (
      select 1
        from public.profiles as p
       where p.id = (select auth.uid())
         and p.deletion_requested_at is not null
    )
  );

-- Managed-credit use is an authenticated, tenant-derived operation. Reject
-- unknown event kinds and pending-erasure accounts before touching either the
-- balance or the immutable usage ledger.
create or replace function public.consume_managed_credit(p_kind text)
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
declare
  caller_id uuid := auth.uid();
begin
  if caller_id is null then
    raise exception 'not authenticated';
  end if;

  if p_kind is null or p_kind not in (
    'cv_tailor',
    'ats_score',
    'cover_letter_generate',
    'polish_bullet',
    'polish_section',
    'extract'
  ) then
    raise exception 'unsupported usage kind'
      using errcode = '22023';
  end if;

  update public.profiles
     set managed_credits_remaining = managed_credits_remaining - 1,
         updated_at = statement_timestamp()
   where id = caller_id
     and deletion_requested_at is null
     and managed_credits_remaining > 0;

  if not found then
    return false;
  end if;

  insert into public.usage_events (
    owner_id,
    kind,
    used_managed_credit
  )
  values (
    caller_id,
    p_kind,
    true
  );

  return true;
end;
$$;

-- BYO usage is written through a narrow RPC. Browser roles cannot forge the
-- owner, managed-credit flag, timestamp, or arbitrary event/provider values.
create or replace function public.record_byo_usage(
  p_kind text,
  p_provider text
)
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
declare
  caller_id uuid := auth.uid();
begin
  if caller_id is null then
    raise exception 'not authenticated';
  end if;

  if p_kind is null or p_kind not in (
    'cv_tailor',
    'ats_score',
    'cover_letter_generate',
    'polish_bullet',
    'polish_section',
    'extract'
  ) then
    raise exception 'unsupported usage kind'
      using errcode = '22023';
  end if;

  if p_provider is null or p_provider not in (
    'anthropic',
    'gemini',
    'ollama',
    'openrouter',
    'deepseek',
    'groq',
    'mistral',
    'openai_compatible'
  ) then
    raise exception 'unsupported usage provider'
      using errcode = '22023';
  end if;

  if not exists (
    select 1
      from public.profiles as p
     where p.id = caller_id
       and p.deletion_requested_at is null
  ) then
    raise exception 'account is unavailable'
      using errcode = '55000';
  end if;

  insert into public.usage_events (
    owner_id,
    kind,
    used_managed_credit,
    provider
  )
  values (
    caller_id,
    p_kind,
    false,
    p_provider
  );

  return true;
end;
$$;

drop policy if exists "usage_events_insert_own" on public.usage_events;
revoke insert on table public.usage_events from authenticated;
revoke execute on function public.consume_managed_credit(text)
  from public, anon;
grant execute on function public.consume_managed_credit(text)
  to authenticated;
revoke execute on function public.record_byo_usage(text, text)
  from public, anon;
grant execute on function public.record_byo_usage(text, text)
  to authenticated;

-- First-run template creation receives browser-controlled strings. Keep the
-- database constraints aligned with the editor's input contract.
create or replace function public.seed_first_cv_template(
  p_name text,
  p_yaml_blob text,
  p_accent text default null,
  p_font text default null,
  p_density text default null
)
returns public.documents
language plpgsql
security definer
set search_path = ''
as $$
declare
  profile_row public.profiles%rowtype;
  existing_cv_id uuid;
  new_doc public.documents%rowtype;
  normalized_name text;
  normalized_accent text := nullif(btrim(p_accent), '');
  normalized_font text := nullif(lower(btrim(p_font)), '');
  normalized_density text := nullif(lower(btrim(p_density)), '');
begin
  if auth.uid() is null then
    raise exception 'not authenticated';
  end if;

  normalized_name := coalesce(nullif(btrim(p_name), ''), 'Template CV - John Doe');

  if char_length(normalized_name) > 200 then
    raise exception 'template name is too long'
      using errcode = '22023';
  end if;

  if octet_length(coalesce(p_yaml_blob, '')) > 524288 then
    raise exception 'template yaml exceeds 512 KiB'
      using errcode = '22023';
  end if;

  if normalized_accent is not null
     and normalized_accent !~ '^#[0-9A-Fa-f]{6}$' then
    raise exception 'invalid template accent'
      using errcode = '22023';
  end if;

  if normalized_font is not null
     and normalized_font not in ('serif', 'sans', 'mono') then
    raise exception 'invalid template font'
      using errcode = '22023';
  end if;

  if normalized_density is not null
     and normalized_density not in ('tight', 'normal', 'airy') then
    raise exception 'invalid template density'
      using errcode = '22023';
  end if;

  insert into public.profiles (id)
  values (auth.uid())
  on conflict (id) do nothing;

  select *
    into profile_row
    from public.profiles
   where id = auth.uid()
   for update;

  if profile_row.deletion_requested_at is not null then
    return null;
  end if;

  select id
    into existing_cv_id
    from public.documents
   where owner_id = auth.uid()
     and type = 'cv'
     and deleted_at is null
   limit 1;

  if existing_cv_id is not null then
    update public.profiles
       set first_cv_seeded_at = coalesce(first_cv_seeded_at, now()),
           updated_at = now()
     where id = auth.uid();
    return null;
  end if;

  if profile_row.first_cv_seeded_at is not null then
    return null;
  end if;

  insert into public.documents (
    type,
    name,
    yaml_blob,
    accent,
    font,
    density
  )
  values (
    'cv',
    normalized_name,
    coalesce(p_yaml_blob, ''),
    normalized_accent,
    normalized_font,
    normalized_density
  )
  returning * into new_doc;

  update public.profiles
     set first_cv_seeded_at = now(),
         updated_at = now()
   where id = auth.uid();

  return new_doc;
end;
$$;

revoke execute on function public.seed_first_cv_template(
  text,
  text,
  text,
  text,
  text
) from public, anon;
grant execute on function public.seed_first_cv_template(
  text,
  text,
  text,
  text,
  text
) to authenticated;

-- Application attempts are audit records. Derive ownership and authoritative
-- flags/timestamps inside the database instead of allowing direct inserts.
create or replace function public.record_application_apply_attempt(
  p_job_id uuid,
  p_packet_id uuid,
  p_action text,
  p_status text,
  p_reason_code text,
  p_target_url text,
  p_target_host text,
  p_target_classification text,
  p_readiness jsonb,
  p_confirmations jsonb,
  p_metadata jsonb
)
returns public.application_apply_attempts
language plpgsql
security definer
set search_path = ''
as $$
declare
  caller_id uuid := auth.uid();
  inserted public.application_apply_attempts%rowtype;
begin
  if caller_id is null then
    raise exception 'not authenticated';
  end if;

  if not exists (
    select 1
      from public.profiles as p
     where p.id = caller_id
       and p.deletion_requested_at is null
  ) then
    raise exception 'account is unavailable'
      using errcode = '55000';
  end if;

  if p_action is null or p_action not in ('manual_open', 'dry_run') then
    raise exception 'unsupported application action'
      using errcode = '22023';
  end if;

  if p_status is null or p_status not in (
    'blocked',
    'manual_open_ready',
    'dry_run_completed'
  ) then
    raise exception 'unsupported application status'
      using errcode = '22023';
  end if;

  if p_reason_code is null
     or p_reason_code !~ '^[a-z0-9_]{1,100}$' then
    raise exception 'invalid application reason code'
      using errcode = '22023';
  end if;

  if p_target_url is not null and char_length(p_target_url) > 2048 then
    raise exception 'application target url is too long'
      using errcode = '22023';
  end if;

  if p_target_host is not null and char_length(p_target_host) > 253 then
    raise exception 'application target host is too long'
      using errcode = '22023';
  end if;

  if p_target_classification is null
     or p_target_classification not in (
       'invalid',
       'linkedin',
       'known_ats',
       'safe_fixture',
       'unknown'
     ) then
    raise exception 'invalid application target classification'
      using errcode = '22023';
  end if;

  if p_readiness is null
     or jsonb_typeof(p_readiness) <> 'object'
     or pg_column_size(p_readiness) > 16384 then
    raise exception 'invalid application readiness payload'
      using errcode = '22023';
  end if;

  if p_confirmations is null
     or jsonb_typeof(p_confirmations) <> 'object'
     or pg_column_size(p_confirmations) > 16384 then
    raise exception 'invalid application confirmation payload'
      using errcode = '22023';
  end if;

  if p_metadata is null
     or jsonb_typeof(p_metadata) <> 'object'
     or pg_column_size(p_metadata) > 16384 then
    raise exception 'invalid application metadata payload'
      using errcode = '22023';
  end if;

  if p_job_id is not null
     and not exists (
       select 1
         from public.saved_jobs as j
        where j.id = p_job_id
          and j.owner_id = caller_id
     ) then
    raise exception 'application job is unavailable'
      using errcode = '23503';
  end if;

  if p_packet_id is not null
     and not exists (
       select 1
         from public.application_packets as p
        where p.id = p_packet_id
          and p.owner_id = caller_id
          and p.job_id is not distinct from p_job_id
     ) then
    raise exception 'application packet is unavailable'
      using errcode = '23503';
  end if;

  insert into public.application_apply_attempts (
    owner_id,
    job_id,
    packet_id,
    action,
    status,
    reason_code,
    target_url,
    target_host,
    target_classification,
    submitted,
    automation_performed,
    readiness,
    confirmations,
    metadata,
    created_at,
    updated_at
  )
  values (
    caller_id,
    p_job_id,
    p_packet_id,
    p_action,
    p_status,
    p_reason_code,
    p_target_url,
    coalesce(p_target_host, ''),
    p_target_classification,
    false,
    false,
    p_readiness,
    p_confirmations,
    p_metadata,
    statement_timestamp(),
    statement_timestamp()
  )
  returning * into inserted;

  return inserted;
end;
$$;

drop policy if exists "application_apply_attempts_insert_own"
  on public.application_apply_attempts;
revoke insert on table public.application_apply_attempts from authenticated;
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
) from public, anon;
grant execute on function public.record_application_apply_attempt(
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
) to authenticated;

-- Reissue the repaired claim function with explicit NULL rejection. NULL
-- LIMIT or lease controls must never turn into implicit unbounded behavior.
create or replace function public.claim_accounts_for_deletion(
  p_batch_size integer default 20,
  p_lease_minutes integer default 30
)
returns table (owner_id uuid, claim_token uuid)
language plpgsql
security definer
set search_path = ''
as $$
declare
  claimed_at timestamptz := statement_timestamp();
begin
  if p_batch_size is null
     or p_batch_size < 1
     or p_batch_size > 100 then
    raise exception 'batch size must be between 1 and 100'
      using errcode = '22023';
  end if;

  if p_lease_minutes is null
     or p_lease_minutes < 5
     or p_lease_minutes > 1440 then
    raise exception 'lease minutes must be between 5 and 1440'
      using errcode = '22023';
  end if;

  return query
  with candidates as (
    select p.id
      from public.profiles as p
      left join private.account_deletion_claims as c
        on c.owner_id = p.id
     where p.deletion_requested_at is not null
       and p.deletion_requested_at <= claimed_at - interval '30 days'
       and (c.owner_id is null or c.lease_expires_at <= claimed_at)
     order by p.deletion_requested_at, p.id
     for update of p skip locked
     limit p_batch_size
  ),
  claimed as (
    insert into private.account_deletion_claims as c (
      owner_id,
      claim_token,
      first_claimed_at,
      last_claimed_at,
      lease_expires_at
    )
    select candidates.id,
           gen_random_uuid(),
           claimed_at,
           claimed_at,
           claimed_at + make_interval(mins => p_lease_minutes)
      from candidates
    on conflict on constraint account_deletion_claims_pkey do update
      set claim_token = excluded.claim_token,
          last_claimed_at = excluded.last_claimed_at,
          lease_expires_at = excluded.lease_expires_at
      where c.lease_expires_at <= claimed_at
    returning c.owner_id, c.claim_token
  )
  select claimed.owner_id, claimed.claim_token
    from claimed;
end;
$$;

-- Exact worker compatibility is a versioned contract. A table-existence probe
-- cannot detect a stale or runtime-broken claim implementation.
drop function if exists public.account_deletion_worker_ready();
create or replace function public.account_deletion_worker_contract_version()
returns integer
language sql
immutable
security definer
set search_path = ''
as $$
  select 2;
$$;

revoke execute on function public.claim_accounts_for_deletion(integer, integer)
  from public, anon, authenticated;
grant execute on function public.claim_accounts_for_deletion(integer, integer)
  to service_role;
revoke execute on function public.account_deletion_worker_contract_version()
  from public, anon, authenticated;
grant execute on function public.account_deletion_worker_contract_version()
  to service_role;
