# NUC-Local Anki Curation Rollout

This runbook moves the complete Anki curation workflow onto the Windows NUC.
The Mac is a normal Anki client only: it receives completed cards through
AnkiWeb and never runs Study Hub curation code.

## Required NUC setup

Use the same interactive Windows account for the scheduled Study Hub task and
Anki Desktop. Install and open:

1. Anki Desktop.
2. AnkiConnect v6 or newer.
3. The AnkiHub add-on.

In AnkiConnect, bind only to `127.0.0.1` on port `8766`. Do not expose the port
to the LAN or any private overlay network. Sign in to both AnkiHub and AnkiWeb
inside Anki. In the AnkiHub add-on configuration, set:

```yaml
auto_sync: "on_ankiweb_sync"
```

This causes AnkiHub's work to run as part of Anki's normal sync path before the
AnkiWeb sync completes.

Study Hub expects:

- Source deck: `Anking Step Deck`
- Generated-card note type: `AnKingOverhaul (OMS_II_Extra/JCBrooks)`
- Required note-type fields: `Text` and `Extra`

The generated-card field list is discovered at runtime. Existing AnKing notes
may receive only the Study Hub-owned lecture tag; they are never edited, moved,
suspended, deleted, or assigned a different note type.

## Study Hub configuration

Set these values in the NUC's `.env`:

```text
OMS_HUB_ANKI_ENABLED=true
OMS_HUB_ANKI_CONNECT_URL=http://127.0.0.1:8766
OMS_HUB_ANKI_EXECUTABLE_PATH=C:\Users\conbr\AppData\Local\Programs\Anki\anki.exe
OMS_HUB_ANKI_STARTUP_TIMEOUT_SECONDS=60
OMS_HUB_ANKI_STARTUP_POLL_SECONDS=1
```

The executable path must be absolute and must point to the installed
`Anki.exe`. The scheduled task must use an interactive logon so Anki can open
in that user's desktop session.

## Preflight

With the NUC logged into its interactive Windows session, run:

```powershell
.\.venv\Scripts\oms-hub.exe validate-config
.\.venv\Scripts\oms-hub.exe anki-doctor
.\.venv\Scripts\oms-hub.exe anki-snapshot --full
```

The doctor reports only the AnkiConnect version, source-note count, and required
field availability. The snapshot command writes the accepted snapshot beneath
the configured NUC Anki data directory.

## Live acceptance

Use a small lecture with one approved existing AnKing note and one generated
card.

1. Curate and approve the envelope.
2. Confirm the existing note receives only the owned lecture tag.
3. Confirm the generated card uses the configured custom deck and note type.
4. Confirm approved media exists under its deterministic filename.
5. Confirm the single sync completes with both AnkiHub and AnkiWeb signed in.
6. Confirm Study Hub reports post-sync verification as successful.
7. On the Mac, run normal AnkiWeb sync and confirm the tagged note and generated
   card arrive.

No AnkiHub sync is required before curation. Both AnkiHub and AnkiWeb must sync
after curation, before Study Hub marks the envelope complete.

## Restart recovery

Interrupt a test apply after generated-card creation but before its receipt is
recorded, restart Study Hub, and retry. The operation marker must recover the
existing card without creating a duplicate. Replaying a completed envelope
must not add notes or invoke sync again.

If AnkiConnect or sync is unavailable, the envelope remains retryable. If source
note hashes changed after indexing or post-sync verification fails, the
envelope is not complete and must be reviewed.

## Rollback

1. Stop the Study Hub scheduled task.
2. Restore the application version and the pre-install `hub.db` backup.
3. Keep the Anki collection intact; do not delete generated cards as part of an
   application rollback.
4. Set `OMS_HUB_ANKI_ENABLED=false` if curation must remain disabled.
5. Restart Study Hub and verify its ordinary health endpoint.

The schema migration preserves curation jobs, index data, artifacts, envelopes,
and receipts. It removes only the obsolete remote-agent state and command
tables.

## Network boundary

Anki curation uses only the NUC loopback connection between Study Hub and
AnkiConnect. Tailscale is not used by Anki curation. There is no Mac service,
shared bearer credential, remote agent API, or cross-machine Anki curation
route. Cloudflare remote access to the Study Hub dashboard is independent of
the AnkiConnect boundary.
