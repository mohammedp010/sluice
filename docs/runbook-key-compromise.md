# Runbook: a key, a tool server or the gateway may be compromised

For when you suspect a provider key, a virtual key or a tool server has been exposed: a
platform breach like Railway's in October 2026, a leaked `.env`, a rug-pulled tool, or a
`sluice.security` alert you can't explain. It's written from what Railway, LiteLLM's users and
Hugging Face had to do. Commands assume the policy is in `$SLUICE_POLICY` (as in
docker compose); add `--policy <file>` otherwise.

Speed matters more than completeness in the first ten minutes. Steps 1 and 2 stop the damage;
the rest find out how big it was and make sure it can't continue.

## 1. Lock down (one command)

```bash
python -m sluice.admin lockdown            # every key suspended; paid providers off everywhere
python -m sluice.admin lockdown --team X   # or only one team's keys (paid stays as it was)
```

- Suspension is reversible, so a lockdown you didn't need costs minutes, not data.
- Every replica sees it at once: keys and the paid-off flag live in the shared database.
- Requests fail with the same 401 an unknown key gets.

## 2. Revoke provider keys at the provider

Revoke them in the provider's console (OpenAI, Anthropic, AWS...), not only in Sluice. Sluice
never hands provider keys to clients or tool servers (ADR 0010), but if the host or its
environment leaked, a stolen copy keeps working until the provider kills it. Rotating the key
in Sluice's environment does nothing to a copy someone else already has.

## 3. Issue new credentials

- **Provider keys:** create new ones, put them in the secret source the gateway reads, and
  restart the gateway.
- **Virtual keys of services you trust:** `rotate-key` gives a new secret with the same grants
  and lets the old one work for the grace period. The new key starts active even if the old
  one is suspended.
  ```bash
  python -m sluice.admin rotate-key <id> --grace 1h
  ```
- **Anything you can't vouch for:** revoke it.
  ```bash
  python -m sluice.admin revoke --team X                   # every key of a team
  python -m sluice.admin revoke --created-before 2026-10-21 # every key older than the breach
  ```

## 4. Work out what was exposed

```bash
python -m sluice.report --security --since 2026-10-20   # suspensions, refused tool calls,
                                                        # secrets seen, held tools
python -m sluice.report --by key --since 2026-10-20     # spend per key
python -m sluice.report --mcp                           # tool calls by key, tool and outcome
python -m sluice.admin tools list                       # pinned tools; quarantined ones changed
```

Look for:
- keys used from new networks: the `R3_new_network` alerts in the gateway's logs, and the
  admin API's `/v1/sluice/admin/security`
- spend that jumped (R2)
- tool calls refused, blocked for carrying a secret (`egress_blocked`), or rate limited
- tools whose definition changed (`quarantined`): see `tools diff <tool>`.

The alerts are in memory and in the `sluice.security` log lines. Keep the logs: the in-memory
list doesn't survive a restart.

## 5. Check the audit log wasn't edited, then unlock team by team

```bash
python -m sluice.report --verify-audit   # exits 1 and names the row if the chain is broken
```

Compare the head hash it prints with one you recorded before the incident. The chain catches
edits and deletions, but someone with write access to the database could rebuild the whole
chain, so only a head hash kept elsewhere proves that didn't happen.

Then bring traffic back gradually:

```bash
python -m sluice.admin lockdown --lift --team search   # one team at a time; watch the alerts
python -m sluice.admin lockdown --lift                 # everyone; paid providers follow
                                                       # SLUICE_ALLOW_PAID again
```

`--lift` only undoes what lockdown suspended. Keys suspended by the watch or by hand stay
suspended; check them one by one with `unsuspend-key <id>`.

## 6. Rotate again once the cause is fixed

Railway rotated everything twice: once during the incident, and again after the intruder was
confirmed gone. A key issued while the attacker might still have been inside isn't clean. Do
steps 2 and 3 again.

## Afterwards

- Write down what the watch caught and what it missed. If a rule fired too late, tighten it
  (`watch:` in the policy); if it fired on legitimate traffic, loosen it or set `enforce:
  false` while you measure.
- If keys had no expiry, set `auth.key_ttl`. If they weren't bound to networks, consider
  `--cidrs` for services with fixed egress.
- If a tool server was the way in, switch `mcp.pin_mode` to `manual` and pin the server's
  package to an exact version.
