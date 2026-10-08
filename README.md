# Backup Monitoring

Dashboard for the PX server at `http://localhost:8090` on the server or `http://192.168.10.8:8090` elsewhere on the LAN.

## What it does

- Creates backup jobs for folders on `C:`, `E:`, and `F:`.
- Runs them daily, weekly, or monthly at a chosen server-local time.
- Copies each job to `H:\BackupMonitoring\jobs\<job-id>\data` on the drive labeled `EXTERNAL_USB`.
- Keeps existing destination files. Removed source files are not deleted from the backup.
- Records start and finish times, Robocopy exit codes, and a detailed log for every run.
- Runs at server startup through the `BackupMonitoringDashboard` Windows scheduled task.

The dashboard verifies the USB volume label before every copy. A job's `source.json` file on the USB identifies its source folder. To restore, copy files from that job's `data` directory back to the desired location.

## Administration

- Application: `F:\website\backup-monitoring\server.py`
- Settings and history: `F:\website\backup-monitoring\data`
- Run logs: `F:\website\backup-monitoring\data\logs`
- Windows task: `BackupMonitoringDashboard`
- Listening address: `192.168.10.8:8090`, the existing public dashboard port for `backups.primexeu.com`

Sign-in uses a separate dashboard password. `data\config.json` stores only its salted hash. Changing the password requires generating a new scrypt hash and updating that file, then restarting the task. Keep a copy of the dashboard password in your normal password manager.

The site currently uses localhost and a LAN IP address. A custom domain needs a DNS name and a reverse proxy or IIS binding supplied by the domain owner. Do not point an existing backup site at this folder without reviewing its current configuration.
