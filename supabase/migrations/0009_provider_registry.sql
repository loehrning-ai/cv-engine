-- OnePager — plan 055: provider registry expansion.
--
-- Widen the profile provider preference to the registry-backed providers.
-- Keep the existing column-scoped UPDATE grant: users may update only safe
-- profile fields and provider/model preferences, never managed credits or
-- deletion state.

alter table public.profiles drop constraint if exists profiles_preferred_provider_check;
alter table public.profiles add constraint profiles_preferred_provider_check
  check (
    preferred_provider in (
      'anthropic',
      'gemini',
      'ollama',
      'openrouter',
      'deepseek',
      'groq',
      'mistral',
      'openai_compatible'
    )
  );

revoke update on public.profiles from authenticated;
grant update (display_name, default_language, email, updated_at,
              preferred_provider, preferred_model)
  on public.profiles to authenticated;
