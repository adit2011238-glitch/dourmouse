# Dourmouse — Supabase backend

Project ref `hpmruavpiloegvhdvcib`, region `ap-northeast-2`, Postgres 17.6.

This exists to sync the things that genuinely need to be the same on more
than one machine — the memory store and the usage ledger — between the Mac
and the Windows desktop. Everything else in Dourmouse stays local SQLite.

Built to the project's Tier 1/2/3 architecture rules. Where the
implementation departs from those rules it says so, here and in the
migration comment, with the reason.

---

## Schema

| Table | Purpose | Notes |
|---|---|---|
| `public.profiles` | Public mirror of `auth.users` | Every FK points here, never at `auth.users` |
| `public.devices` | One row per syncing machine | `unique(user_id, name)` |
| `public.facts` | Cloud mirror of the local memory store | `unique(user_id, source, title)` |
| `public.usage_events` | Per-turn Claude/Ollama accounting | Append-only from a client |

`public.facts` mirrors the live local schema in `dourmouse/memory_store.py`
(`facts(id, source, title, body, created_at, updated_at)` with
`UNIQUE(source, title)`). The cloud form scopes that uniqueness per user, so
an upsert arriving from either machine converges on one row instead of
duplicating.

### Applied migrations

```
20260903193941  tier1_profiles_auth_isolation_and_verified_email_helper
20260903194030  tier1_2_dourmouse_sync_tables_devices_facts_usage
20260903194048  tier1_3_harden_definer_grants_and_isolate_realtime
20260903194129  move_email_verified_helper_out_of_rest_exposed_schema
20260903194213  tier3_atomic_fact_sync_rpc
20260903194310  grant_dml_to_authenticated_and_service_role
```

Each migration carries its full rationale as SQL comments. To pull the exact
DDL into `supabase/migrations/` locally:

```bash
supabase link --project-ref hpmruavpiloegvhdvcib && supabase db pull
```

That needs a `SUPABASE_ACCESS_TOKEN`, which is why the files are not already
checked in — the token was not available in the environment that built this.

---

## Two things worth knowing before you edit any policy

### 1. The spec's verified-email check does not work, and fails silently

Tier 1 rule 3 proposes this predicate:

```sql
AND auth.jwt()->>'email_confirmed_at' IS NOT NULL
```

There is no `email_confirmed_at` claim in a Supabase JWT. Per Supabase's own
JWT Claims Reference the complete set is
`iss/aud/exp/iat/sub/role/aal/session_id/email/phone/is_anonymous`, plus
optional `jti/nbf/app_metadata/user_metadata/amr`. So that expression is
`NULL` for every real user, `NULL IS NOT NULL` is false, and the policy
denies **everyone, permanently** — a silent total lockout rather than a
visible error.

The obvious substitute, `user_metadata->>'email_verified'`, is worse: a user
can set their own `user_metadata` via `auth.updateUser({ data: ... })`, so
any unverified account could assert its own verification and walk through.

What is actually used instead: `private.is_email_verified()`, a `STABLE`
`SECURITY DEFINER` function reading the authoritative, non-user-writable
`auth.users.email_confirmed_at`. It lives in the `private` schema
specifically so PostgREST does not expose it at `/rest/v1/rpc/`.

**Bootstrap consequence:** an account that has not confirmed its email cannot
register a device or sync anything. That is the intended reading of rule 3 —
verify first, then sync — and it fails visibly at the first sync attempt.

### 2. RLS is only half of access control

Found by a real negative test rather than by reading the schema: after the
tables were created, **no role held `SELECT`/`INSERT`/`UPDATE`/`DELETE` on any
of them** — only `REFERENCES`/`TRIGGER`/`TRUNCATE`. The policies were correct
but unreachable; `authenticated` would have been refused at the grant layer
with a `42501` before a policy ever ran, and the app would have shipped
completely non-functional.

The Supabase model needs both halves: a broad table grant to the role, and
RLS doing the per-row filtering. If you add a table, you must add its grants
too. The `rls_auto_enable` event trigger on this project turns RLS **on** for
new tables automatically; it does not grant anything.

`anon` holds no privilege on any of these four tables, deliberately. Every
row is one specific user's private data; there is no anonymous read path.

---

## Verified behaviour

Run as real transactions against the live database and rolled back:

| Scenario | Result |
|---|---|
| Insert into `auth.users` | Profile row auto-created by `on_auth_user_created` |
| Verified user, 2 users' facts present | Sees exactly their own 1 row |
| **Unverified user who owns a fact** | **Sees 0 rows** |
| `anon` reading `public.facts` | Hard-denied at the grant layer (`42501`) |
| `sync_facts`, first call | `synced=2 skipped=0` |
| `sync_facts`, stale replay (older `updated_at`) | `synced=0 skipped=1`, stored body unchanged |
| `sync_facts`, newer write | `synced=1`, body replaced |
| `sync_facts` with another user's `device_id` | Rejected — device not visible under RLS |

Security advisors: **0 findings**.

Performance advisors report five `unused_index` INFO lints. Those are
expected on a database that has never served a query — the indexes exist for
the FK and RLS-predicate columns that rule 8 requires. Re-check once there is
real traffic before removing any of them.

---

## Keys

`supabase/client.ts` reads `NEXT_PUBLIC_SUPABASE_URL` and
`NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY` and **throws at construction** if
handed a `sb_secret_...` key or a JWT whose payload says
`"role":"service_role"`. The service_role key bypasses every policy above, so
one leak into a client bundle hands full read/write on every row to anyone
who unzips it. It is server-side only, and it is not in this repository.

The publishable key is safe to ship in a client and is the one this app uses.

---

## Realtime

Only `public.facts` is in the `supabase_realtime` publication, and
`subscribeToOwnFacts()` filters it further to `user_id=eq.<uid>` before the
socket. `usage_events` is deliberately **not** published: it is append-only
and high-frequency, and the usage bar polls a rolled-up total instead, so
row-level push would burn bandwidth for no feature.
