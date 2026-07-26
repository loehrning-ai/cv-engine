-- OnePager — plan 002 stage 4: provider/model preference on profiles.
-- Additive + idempotent. Lets a user pick their default LLM provider
-- (Anthropic | Gemini) and model; keys themselves live in user_llm_keys.
--
-- CRITICAL — the profiles UPDATE grant is COLUMN-SCOPED (migration 0002 revoked
-- the blanket grant and re-granted only display_name/default_language/email/
-- updated_at). New columns are therefore UN-WRITABLE for the `authenticated`
-- role unless we re-issue the column grant INCLUDING them. A naive `add column`
-- alone would leave the preference silently un-saveable (PostgREST 403 / no-op).
-- managed_credits_remaining + deletion_requested_at stay EXCLUDED so the
-- credit-tamper + erasure protections from 0002/0004 are preserved.

-- 1) New preference columns. Both default to the Anthropic/global default so
--    existing rows stay valid and behaviour is unchanged until a user opts in.
alter table public.profiles
  add column if not exists preferred_provider text not null default 'anthropic';
alter table public.profiles
  add column if not exists preferred_model text;   -- null → resolve to provider default

-- 2) Constrain the provider to the supported set (app validates too; this is
--    the DB backstop). Nullable disallowed (NOT NULL above) so only the two
--    canonical values are ever stored.
alter table public.profiles drop constraint if exists profiles_preferred_provider_check;
alter table public.profiles add  constraint profiles_preferred_provider_check
  check (preferred_provider in ('anthropic', 'gemini'));

-- 3) Re-issue the column-scoped UPDATE grant from 0002, now INCLUDING the two
--    preference columns. Still EXCLUDES managed_credits_remaining +
--    deletion_requested_at (tamper-proofing preserved).
revoke update on public.profiles from authenticated;
grant update (display_name, default_language, email, updated_at,
              preferred_provider, preferred_model)
  on public.profiles to authenticated;
