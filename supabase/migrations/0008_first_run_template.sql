-- OnePager — first-run CV template marker.
--
-- The first CV is an onboarding template, not an invariant that the account
-- must always have a CV. This marker lets the app seed once, then respect an
-- intentional last-CV deletion.

alter table public.profiles
  add column if not exists first_cv_seeded_at timestamptz;

create or replace function public.mark_first_cv_seen()
returns timestamptz
language plpgsql
security definer
set search_path = ''
as $$
declare
  marked_at timestamptz;
begin
  if auth.uid() is null then
    raise exception 'not authenticated';
  end if;

  insert into public.profiles (id)
  values (auth.uid())
  on conflict (id) do nothing;

  update public.profiles
     set first_cv_seeded_at = coalesce(first_cv_seeded_at, now()),
         updated_at = now()
   where id = auth.uid()
   returning first_cv_seeded_at into marked_at;

  return marked_at;
end;
$$;

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
begin
  if auth.uid() is null then
    raise exception 'not authenticated';
  end if;

  insert into public.profiles (id)
  values (auth.uid())
  on conflict (id) do nothing;

  select * into profile_row
    from public.profiles
   where id = auth.uid()
   for update;

  if profile_row.deletion_requested_at is not null then
    return null;
  end if;

  select id into existing_cv_id
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

  insert into public.documents (type, name, yaml_blob, accent, font, density)
  values (
    'cv',
    coalesce(nullif(trim(p_name), ''), 'Template CV - John Doe'),
    coalesce(p_yaml_blob, ''),
    p_accent,
    p_font,
    p_density
  )
  returning * into new_doc;

  update public.profiles
     set first_cv_seeded_at = now(),
         updated_at = now()
   where id = auth.uid();

  return new_doc;
end;
$$;

revoke execute on function public.mark_first_cv_seen() from public, anon;
revoke execute on function public.seed_first_cv_template(text, text, text, text, text) from public, anon;
grant execute on function public.mark_first_cv_seen() to authenticated;
grant execute on function public.seed_first_cv_template(text, text, text, text, text) to authenticated;

-- Clean up any historical document->job links that predate the ownership
-- policy below. Cross-owner links should never be visible or used.
update public.documents d
   set job_id = null,
       updated_at = now()
  from public.saved_jobs sj
 where d.job_id = sj.id
   and d.owner_id <> sj.owner_id;

-- Close the pending-erasure write window at the RLS layer too. Migration 0007
-- already requires documents.job_id to be null or owned; preserve that check and
-- add a profile-state check so a user in the 30-day deletion grace period cannot
-- create or update live documents.
drop policy if exists "saved_jobs_select_own" on public.saved_jobs;
create policy "saved_jobs_select_own" on public.saved_jobs
  for select to authenticated
  using (
    owner_id = (select auth.uid())
    and not exists (
      select 1 from public.profiles p
      where p.id = (select auth.uid())
        and p.deletion_requested_at is not null
    )
  );

drop policy if exists "saved_jobs_insert_own" on public.saved_jobs;
create policy "saved_jobs_insert_own" on public.saved_jobs
  for insert to authenticated
  with check (
    owner_id = (select auth.uid())
    and not exists (
      select 1 from public.profiles p
      where p.id = (select auth.uid())
        and p.deletion_requested_at is not null
    )
  );

drop policy if exists "saved_jobs_update_own" on public.saved_jobs;
create policy "saved_jobs_update_own" on public.saved_jobs
  for update to authenticated
  using (owner_id = (select auth.uid()))
  with check (
    owner_id = (select auth.uid())
    and not exists (
      select 1 from public.profiles p
      where p.id = (select auth.uid())
        and p.deletion_requested_at is not null
    )
  );

drop policy if exists "saved_jobs_delete_own" on public.saved_jobs;
create policy "saved_jobs_delete_own" on public.saved_jobs
  for delete to authenticated
  using (
    owner_id = (select auth.uid())
    and not exists (
      select 1 from public.profiles p
      where p.id = (select auth.uid())
        and p.deletion_requested_at is not null
    )
  );

drop policy if exists "documents_insert_own" on public.documents;
create policy "documents_insert_own" on public.documents
  for insert to authenticated
  with check (
    owner_id = (select auth.uid())
    and not exists (
      select 1 from public.profiles p
      where p.id = (select auth.uid())
        and p.deletion_requested_at is not null
    )
    and (
      job_id is null
      or exists (
        select 1 from public.saved_jobs sj
        where sj.id = documents.job_id
          and sj.owner_id = (select auth.uid())
      )
    )
  );

drop policy if exists "documents_delete_own" on public.documents;
create policy "documents_delete_own" on public.documents
  for delete to authenticated
  using (
    owner_id = (select auth.uid())
    and not exists (
      select 1 from public.profiles p
      where p.id = (select auth.uid())
        and p.deletion_requested_at is not null
    )
  );

drop policy if exists "pdf_artifacts_select_own" on public.pdf_artifacts;
create policy "pdf_artifacts_select_own" on public.pdf_artifacts
  for select to authenticated
  using (
    owner_id = (select auth.uid())
    and exists (
      select 1 from public.documents d
      where d.id = pdf_artifacts.document_id
        and d.owner_id = (select auth.uid())
    )
    and not exists (
      select 1 from public.profiles p
      where p.id = (select auth.uid())
        and p.deletion_requested_at is not null
    )
  );

drop policy if exists "pdf_artifacts_insert_own" on public.pdf_artifacts;
create policy "pdf_artifacts_insert_own" on public.pdf_artifacts
  for insert to authenticated
  with check (
    owner_id = (select auth.uid())
    and r2_key like ((select auth.uid())::text || '/%')
    and exists (
      select 1 from public.documents d
      where d.id = pdf_artifacts.document_id
        and d.owner_id = (select auth.uid())
    )
    and not exists (
      select 1 from public.profiles p
      where p.id = (select auth.uid())
        and p.deletion_requested_at is not null
    )
  );

drop policy if exists "pdf_artifacts_update_own" on public.pdf_artifacts;
create policy "pdf_artifacts_update_own" on public.pdf_artifacts
  for update to authenticated
  using (owner_id = (select auth.uid()))
  with check (
    owner_id = (select auth.uid())
    and r2_key like ((select auth.uid())::text || '/%')
    and exists (
      select 1 from public.documents d
      where d.id = pdf_artifacts.document_id
        and d.owner_id = (select auth.uid())
    )
    and not exists (
      select 1 from public.profiles p
      where p.id = (select auth.uid())
        and p.deletion_requested_at is not null
    )
  );

drop policy if exists "pdf_artifacts_delete_own" on public.pdf_artifacts;
create policy "pdf_artifacts_delete_own" on public.pdf_artifacts
  for delete to authenticated
  using (
    owner_id = (select auth.uid())
    and exists (
      select 1 from public.documents d
      where d.id = pdf_artifacts.document_id
        and d.owner_id = (select auth.uid())
    )
    and not exists (
      select 1 from public.profiles p
      where p.id = (select auth.uid())
        and p.deletion_requested_at is not null
    )
  );

-- Upgrade-safe reconciliation: older deployments could contain more than one
-- non-stale artifact for the same document/content pair. Keep the newest row
-- authoritative and mark every older duplicate stale before adding the unique
-- index. Never delete an artifact or its external object during migration.
with ranked_fresh_artifacts as (
  select
    id,
    row_number() over (
      partition by document_id, content_hash
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

create unique index if not exists pdf_artifacts_one_fresh_content_idx
  on public.pdf_artifacts (document_id, content_hash)
  where stale = false;

drop policy if exists "documents_update_own" on public.documents;
create policy "documents_update_own" on public.documents
  for update to authenticated
  using (owner_id = (select auth.uid()))
  with check (
    owner_id = (select auth.uid())
    and not exists (
      select 1 from public.profiles p
      where p.id = (select auth.uid())
        and p.deletion_requested_at is not null
    )
    and (
      job_id is null
      or exists (
        select 1 from public.saved_jobs sj
        where sj.id = documents.job_id
          and sj.owner_id = (select auth.uid())
      )
    )
  );
