-- CV Copilot — explicit Data API privilege allowlist.
--
-- RLS does not apply to TRUNCATE. Supabase's broad default table grants can
-- therefore turn an otherwise correct tenant policy into a whole-table
-- destructive path. Reset browser-role privileges and grant only operations
-- used by the application. RLS remains the row-level boundary for every
-- granted statement.

revoke all privileges on all tables in schema public from anon, authenticated;
revoke all privileges on all sequences in schema public from anon, authenticated;
revoke execute on all functions in schema public from public, anon, authenticated;

-- Table-level revocation does not remove explicit column grants. Clear those
-- too before rebuilding the allowlist.
do $privileges$
declare
  target record;
begin
  for target in
    select
      table_schema,
      table_name,
      string_agg(quote_ident(column_name), ', ' order by ordinal_position) as columns
    from information_schema.columns
    where table_schema = 'public'
    group by table_schema, table_name
  loop
    execute format(
      'revoke all privileges (%s) on table %I.%I from anon, authenticated',
      target.columns,
      target.table_schema,
      target.table_name
    );
  end loop;
end
$privileges$;

-- User-owned primary objects.
grant select on table public.profiles to authenticated;
grant update (
  display_name,
  default_language,
  email,
  updated_at,
  preferred_provider,
  preferred_model
) on table public.profiles to authenticated;

grant select, insert, delete on table public.documents to authenticated;
grant update (
  name,
  slug,
  yaml_blob,
  accent,
  font,
  density,
  photo_key,
  job_id,
  last_used_at,
  updated_at
) on table public.documents to authenticated;

grant select, insert, delete on table public.saved_jobs to authenticated;
grant update (
  title,
  company,
  jd_text,
  jd_json,
  source_url,
  source_metadata,
  content_hash,
  last_seen_at,
  expires_at,
  status,
  field,
  deadline,
  confidence,
  why_fit,
  experience_requirements,
  updated_at
) on table public.saved_jobs to authenticated;

grant select, insert, update, delete
  on table public.user_llm_keys
  to authenticated;
grant select, insert, update, delete
  on table public.pdf_artifacts
  to authenticated;
grant select, insert
  on table public.usage_events
  to authenticated;

-- Server-created workflow records expose only the user-safe read surface.
grant select on table public.job_import_runs to authenticated;
grant select on table public.job_import_results to authenticated;
grant select on table public.application_packets to authenticated;
grant select, insert on table public.application_apply_attempts to authenticated;
grant select (
  id,
  owner_id,
  cv_id,
  job_id,
  source_hash,
  jd_hash,
  tailored_hash,
  state_digest,
  applied_at,
  expires_at,
  created_at,
  updated_at
) on table public.tailor_review_states to authenticated;

-- Authenticated SECURITY DEFINER functions are deliberate, narrowly scoped
-- RPCs. Every function pins search_path and derives ownership from auth.uid().
grant execute on function public.consume_managed_credit(text)
  to authenticated;
grant execute on function public.request_account_deletion()
  to authenticated;
grant execute on function public.cancel_account_deletion()
  to authenticated;
grant execute on function public.mark_first_cv_seen()
  to authenticated;
grant execute on function public.seed_first_cv_template(
  text,
  text,
  text,
  text,
  text
) to authenticated;

-- Backend-only maintenance and irreversible-erasure RPCs.
grant execute on all functions in schema public to service_role;

-- New objects must fail closed until a later migration grants an intended API
-- surface explicitly.
alter default privileges for role postgres in schema public
  revoke all privileges on tables from anon, authenticated;
alter default privileges for role postgres in schema public
  revoke all privileges on sequences from anon, authenticated;
alter default privileges for role postgres
  revoke execute on functions from public;
alter default privileges for role postgres in schema public
  revoke execute on functions from anon, authenticated;
