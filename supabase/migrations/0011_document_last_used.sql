-- OnePager — document open recency for the sidebar library.
--
-- Last-used recency is intentionally separate from updated_at. Opening a CV or
-- cover letter should move it into the quick library even when no edit occurs.

alter table public.documents
  add column if not exists last_used_at timestamptz default now();

update public.documents
   set last_used_at = coalesce(last_used_at, updated_at, created_at, now())
 where last_used_at is null;

create index if not exists documents_owner_type_last_used_idx
  on public.documents (owner_id, type, last_used_at desc, updated_at desc, created_at desc)
  where deleted_at is null;

revoke update on public.documents from authenticated;
grant update (name, slug, yaml_blob, accent, font, density, photo_key, job_id, last_used_at, updated_at)
  on public.documents to authenticated;
