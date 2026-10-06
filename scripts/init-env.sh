#!/usr/bin/env sh
# Create .env from .env.example with fresh random secrets. Never overwrites an existing .env.
set -eu
cd "$(dirname "$0")/.."

if [ -e .env ]; then
  echo ".env already exists; leaving it alone." >&2
  exit 1
fi

secret() {
  # 48 hex characters: letters and digits only, safe inside connection URLs
  od -An -N24 -tx1 /dev/urandom | tr -d ' \n'
}

umask 077
sed \
  -e "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(secret)/" \
  -e "s/^APP_SECRET_KEY=.*/APP_SECRET_KEY=$(secret)/" \
  -e "s/^EVOLUTION_API_KEY=.*/EVOLUTION_API_KEY=$(secret)/" \
  -e "s/^ADMIN_PASSWORD=.*/ADMIN_PASSWORD=$(secret | cut -c1-20)/" \
  .env.example > .env

echo "Created .env with new secrets (readable only by you)."
echo "The setup page's password is ADMIN_PASSWORD in .env."
echo "Now fill in CMS_ENROLLMENT, CMS_PASSWORD, WHATSAPP_NUMBER (or DISCORD_WEBHOOK_URL) and PUBLIC_URL."
