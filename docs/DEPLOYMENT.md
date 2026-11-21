# Headless VPS deployment

This guide deploys the bot as one outbound-polling systemd service on a Linux VPS. It assumes a Debian/Ubuntu-style host, an authorized private storage channel, and a bot token created with BotFather. It does not open any inbound network port. Run commands from an SSH session with an account that can use `sudo`.

## 1. Install system tools and the application

Install Git, certificate roots, and the `uv` Python project manager using its official installation instructions. The project requires CPython 3.15; install that managed runtime under `/opt` so systemd's `ProtectHome=true` does not hide it from the service:

```sh
sudo apt update
sudo apt install -y ca-certificates git curl
# Install uv following https://docs.astral.sh/uv/getting-started/installation/
UV="$(command -v uv)"
sudo install -d -o root -g root -m 0755 /opt/uv/python
sudo env UV_PYTHON_INSTALL_DIR=/opt/uv/python "$UV" python install 3.15
```

Create the dedicated, non-login service account and clone the repository. Replace the clone URL with the repository URL if using a private fork; authenticate Git as your deployment operator rather than storing credentials in the checkout.

```sh
sudo useradd --system --no-create-home --home-dir /nonexistent \
  --shell /usr/sbin/nologin telegrambot
git clone https://github.com/Sushiyanti/telegram-bot.git /tmp/telegram-storage-bot
sudo install -d -o telegrambot -g telegrambot -m 0750 /opt/telegram-storage-bot
sudo cp -a /tmp/telegram-storage-bot/. /opt/telegram-storage-bot/
sudo chown -R telegrambot:telegrambot /opt/telegram-storage-bot
rm -rf /tmp/telegram-storage-bot
```

Create the locked production environment with the managed Python. The resulting virtual environment lives beside the source, while its base interpreter remains readable under `/opt/uv/python`:

```sh
UV="$(command -v uv)"
cd /opt/telegram-storage-bot
sudo env UV_PYTHON_INSTALL_DIR=/opt/uv/python "$UV" sync \
  --python 3.15 --locked --no-dev
sudo chown -R telegrambot:telegrambot /opt/telegram-storage-bot
```

If the repository is private, clone it with your approved deployment credentials; do not put a GitHub token in the bot environment file.

## 2. Configure the bot environment

The systemd unit reads `/etc/telegram-storage-bot.env`. Create it outside the repository with restrictive permissions:

```sh
sudo install -o root -g root -m 0600 /dev/null /etc/telegram-storage-bot.env
sudoedit /etc/telegram-storage-bot.env
```

Set at least these values, replacing every placeholder with your own configuration:

```dotenv
BOT_TOKEN=replace-with-the-BotFather-token
STORAGE_CHANNEL_ID=-1001234567890
ADMIN_IDS=123456789,987654321
DB_PATH=/var/lib/telegram-storage-bot/bot.db
ENV=prod
LOG_LEVEL=INFO
MAX_RELAY_FILE_BYTES=2147483648
DEFAULT_PROTECT_CONTENT=true
```

`ADMIN_IDS` must be comma-separated numeric Telegram user IDs. Configure only optional values you intend to use, such as `UPLOADER_IDS`, `UPLOADER_MODE`, `UPLOADER_REDEEM_CODE`, `REQUIRED_CHANNEL_ID`, and `VERIFIED_TRANSFER_MAX_FILE_BYTES`. Do not copy placeholder values from `.env.example`. The BotFather token is a credential: keep it out of shell history, Git, support logs, and backups that are not encrypted and access-controlled.

Before start-up, add the bot to the private storage channel as an administrator with permission to post. If `REQUIRED_CHANNEL_ID` is configured, the bot must also be able to call `getChatMember` for that channel. Keep exactly one polling instance active for this bot token.

## 3. Install and start the systemd service

Install the provided unit and enable it at boot:

```sh
sudo install -o root -g root -m 0644 \
  /opt/telegram-storage-bot/deploy/telegram-storage-bot.service \
  /etc/systemd/system/telegram-storage-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-storage-bot
```

Check service state and sanitized application logs:

```sh
sudo systemctl status telegram-storage-bot --no-pager
sudo journalctl -u telegram-storage-bot -n 100 --no-pager
```

The unit creates `/var/lib/telegram-storage-bot` as the service's private writable state directory. The `DB_PATH` value above stores the SQLite database there. The unit's exit-status-2 restart guard avoids a rapid restart loop for missing or invalid settings; correct the environment file and restart with `sudo systemctl restart telegram-storage-bot`. Admins can then use `/configcheck` for live bot, channel, database, and configuration checks.

To deploy an update, stop the service, update the checkout to the intended revision, run `uv sync --python 3.15 --locked --no-dev` from the project root using the same managed Python path, restore ownership to `telegrambot`, then start the service and inspect its logs. Do not run two polling processes during deployment.

## 4. Schedule database backups

The M1 backup command uses SQLite's online backup API, so it can create a consistent snapshot while the bot is running. It backs up metadata and Telegram message references, not the media bytes in Telegram. The destination must be a new path; the CLI creates backup files with mode `0600`.

Create a private local backup directory owned by the service account:

```sh
sudo install -d -o telegrambot -g telegrambot -m 0700 \
  /var/backups/telegram-storage-bot
```

Create a small root-owned helper script that runs as `telegrambot` from cron. Replace no paths unless your installation differs:

```sh
sudoedit /usr/local/sbin/telegram-storage-bot-backup
```

Use this script body:

```sh
#!/bin/sh
set -eu
umask 077
cd /opt/telegram-storage-bot
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
exec /opt/telegram-storage-bot/.venv/bin/python -m media_bot.db.backup backup \
  --database /var/lib/telegram-storage-bot/bot.db \
  --destination "/var/backups/telegram-storage-bot/bot-${stamp}.db"
```

Install it with owner root and executable permissions:

```sh
sudo chown root:root /usr/local/sbin/telegram-storage-bot-backup
sudo chmod 0755 /usr/local/sbin/telegram-storage-bot-backup
```

Test the exact backup path manually as the service account, then verify a new database file was created:

```sh
sudo -u telegrambot /usr/local/sbin/telegram-storage-bot-backup
sudo ls -l /var/backups/telegram-storage-bot
```

Schedule a daily 03:17 backup in the VPS's local timezone in the service account's crontab. Backup filenames still use UTC timestamps:

```sh
sudo crontab -u telegrambot -e
```

Add:

```cron
17 3 * * * /usr/local/sbin/telegram-storage-bot-backup
```

Ensure the host's cron service is enabled. Monitor cron output through the host's configured cron mail/logging and periodically test that backups exist and can be restored. Establish a retention policy and copy encrypted backups off-host; local snapshots do not protect against VPS or disk loss. Avoid deleting old backups until at least one recent backup has been validated.

To restore, stop the bot first and use the existing M1 restore command with explicit confirmation, then start the service again:

```sh
sudo systemctl stop telegram-storage-bot
sudo -u telegrambot /opt/telegram-storage-bot/.venv/bin/python \
  -m media_bot.db.backup restore \
  --backup /var/backups/telegram-storage-bot/bot-YYYYMMDDTHHMMSSZ.db \
  --database /var/lib/telegram-storage-bot/bot.db \
  --confirm-restore
sudo systemctl start telegram-storage-bot
```

Restore replaces the database and validates integrity, foreign keys, and the schema version. It does not restore deleted Telegram messages or media; the private storage channel requires its own recovery plan.
