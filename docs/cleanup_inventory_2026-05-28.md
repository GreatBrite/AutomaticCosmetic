# Cleanup Inventory 2026-05-28

This document records the safe cleanup pass performed on 2026-05-28.
Nothing below was deleted; obsolete or dangerous items were moved to an ignored quarantine folder.

## Current Production Roots

- `/root/AutomaticCosmetic` is the current working project root.
- `/root/AutomaticCosmetic/src/freelance_leads_bot` contains the current Codex/Telegram control plane and Avito webhook code.
- Legacy runtime is no longer active as of 2026-05-29.
- Archived legacy location: `/root/AutomaticCosmetic_archives/20260529_full_migration`.

## Active Services Left In Place

- `freelance-leads-bot.service`
  - Root: `/root/AutomaticCosmetic`
  - Entry point: `python -m src.freelance_leads_bot.main serve`
- `yclients-avito-webhook.service`
  - Root: `/root/AutomaticCosmetic`
  - Entry point: `uvicorn src.freelance_leads_bot.integrations.avito_webhook:app`
- `yclients-yclients-integration.service`
  - Root: `/root/AutomaticCosmetic`
  - Entry point: `run_yclients_integration.sh`

`yclients-tg-client.service` was disabled, stopped, and masked on 2026-05-29 because it used the old non-Codex client runtime.

## Full Migration Update 2026-05-29

Moved out of the active project tree:

```text
/root/AutomaticCosmetic/.legacy_runtime
/root/AutomaticCosmetic/.quarantine
/root/AutomaticCosmetic/legacy_integrations
-> /root/AutomaticCosmetic_archives/20260529_full_migration/
```

The old `yclients-tg-client.service` unit file was archived under:

```text
/root/AutomaticCosmetic_archives/20260529_full_migration/systemd_units/
```

## Historical Legacy Runtime Move

Moved on 2026-05-28:

```text
/root/AutomaticCosmetic/legacy_integrations/yclients_avito_tg
-> /root/AutomaticCosmetic/.legacy_runtime/yclients_avito_tg
```

Systemd units were updated and daemon-reloaded after the move.

This was superseded on 2026-05-29 by the full migration archive at `/root/AutomaticCosmetic_archives/20260529_full_migration`.

## Historical Quarantine Location

Ignored by git via `.gitignore`:

```text
/root/AutomaticCosmetic/.quarantine/20260528-185619
```

This quarantine directory has since been moved into `/root/AutomaticCosmetic_archives/20260529_full_migration/.quarantine`.

## Quarantined Systemd Units

Moved out of `/etc/systemd/system/` and followed by `systemctl daemon-reload`.

- `yclients-tg-admin.service`
  - Reason: legacy admin bot used the same `TELEGRAM_BOT_TOKEN` as the current Codex control plane and caused Telegram `409 Conflict`.
- `yclients-avito-poller.service`
  - Reason: disabled/dead, pointed at stale `/root/YclientsAvitoTg`.
- `yclients-vk-bot.service`
  - Reason: disabled/dead, pointed at stale `/root/YclientsAvitoTg`.
- `yclients-asr.service`
  - Reason: enabled but stuck in restart loop; failed with `No module named 'src.presentation.asr'`.

## Quarantined Legacy Roots

- `/root/YclientsAvitoTg`
  - Reason: stale root, no active service should depend on it after cleanup.
- `/root/YclientsAvitoTg_fixed`
  - Reason: older fixed snapshot, not a live working directory.

## Quarantined Root Artifacts

Moved from `/root/AutomaticCosmetic` root into quarantine:

- `ChatExport_2026-05-26.zip`
- `freelance-leads-bot-portable.tar.gz`
- `freelance-leads-bot-portable.tar.gz (2).sha256`
- `yclients_avito_tg_migration_2026-05-20.tar.gz`
- `yclients_avito_tg_migration_2026-05-21-fixed.tar.gz`
- `bot.log`
- `client_bot.log`
- `asr.log`
- `yclients_integration.log`

Runtime files under `/root/AutomaticCosmetic/data/` were left in place.

## Restore Notes

To inspect the archived units, use:

```text
/root/AutomaticCosmetic_archives/20260529_full_migration/
```

Restoring a legacy service is intentionally a manual operation. Do not unmask `yclients-tg-client.service` unless you are deliberately restoring the old non-Codex Telegram client runtime.

```bash
systemctl unmask yclients-tg-client.service
```
