# Backups

Logos data is protected in two ways.

| What | How | Offsite |
| --- | --- | --- |
| Everything on VM 110 (agents, conversations, Matrix, calendars, dashboard) | Nightly 22:00 vzdump to TrueNAS (3 copies) | The newest VM archive goes to B2 every 28 days |
| The small, irreplaceable parts (below) | `scripts/logos-backup-export`, nightly 01:30 | The export folder is in the nightly restic run to B2 |

## The nightly export

`scripts/logos-backup-export` writes to `$LOGOS_BACKUP_DIR/latest`. The default is `~/backup-export`; on prod it is set in `~/.logos/.env` to a TrueNAS share that restic sends to B2. It contains:

- `calendar.bundle`: the calendars, as a git bundle with their whole history.
- `calendar-etc.tar.gz`: Radicale's settings and people (bcrypt hashes). The calendar service's password is left out; it is recreated on the next `up`.
- `brain-<agent>.bundle`: each agent's brain, a git bundle with its whole history.
- `agent-<agent>.tar.gz`: each agent's memories and scheduled jobs (`cron/jobs.json`).
- `manifest.json`: when it ran, plus each file's size and sha256.

It also writes `logos_backup_export.prom` next to `latest`, for the node_exporter textfile collector. The alert fires on `logos_backup_export_last_success_timestamp_seconds`.

Installed as a systemd user timer: `infra/systemd/logos-backup-export.{service,timer}`.

## Restoring

```
git clone calendar.bundle collections            # then copy into the logos-calendar volume
git clone brain-greg.bundle brain                # then into logos-brain-greg
tar -xzf agent-greg.tar.gz                       # memories/ and cron/jobs.json into logos-home-greg
```
