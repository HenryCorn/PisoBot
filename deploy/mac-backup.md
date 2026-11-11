# Mac-pull encrypted backup

Run `deploy/mac-pull-backup.sh` on the Mac. It connects over SSH, asks the
`pisobot` account to make online SQLite `.backup` copies, copies the atomically
written JSON state, streams a tar archive back, compresses and encrypts it with
`age`, then decrypts into a mode-700 temporary directory and verifies hashes,
JSON, SQLite integrity and table row counts before publishing the archive.
Preferences and profile settings are in the SQLite databases. The live service
is not stopped, and this tool does not create or inspect the Pi migration
startup fence. It never reads the service environment file.

Each SQLite database is backed up consistently. Separate databases and JSON
files are not one cross-file transaction; JSON copies are individually complete
but can reflect a nearby point in time. SQLite's read-only online backup can
create empty WAL/SHM sidecars; the script never removes or changes live sidecars.
If any export, encryption or verification step fails, no final backup is kept.

## One-time Mac setup

1. Install `age` and ensure `ssh`, `gzip`, `shasum`, and Python 3.11 or newer are available.
2. Configure SSH host-key verification for the VPS and a host alias. The SSH
   account needs non-interactive `sudo -n -u pisobot python3 -` access; do not
   put credentials in the command or shell history.
3. Create an age key with `age-keygen`. Keep the identity private on the Mac
   with mode 600. Put only its public `age1...` recipient in a separate
   recipients file.
4. Choose a private local backup directory, for example
   `$HOME/Backups/pisobot` (the script creates it with the current umask).

## Run and restore-check

From the repository on the Mac:

```sh
deploy/mac-pull-backup.sh VPS_HOST "$HOME/Backups/pisobot" \
  "$HOME/.config/age/pisobot-recipients.txt" \
  "$HOME/.config/age/pisobot-identity.txt" 14
```

The final argument retains backups for 14 days; only matching PisoBot backup
archives and their checksum sidecars are removed. A successful run prints the
restore-verification counts and leaves an encrypted `.tar.gz.age` plus a
SHA-256 sidecar. The temporary plaintext restore used for verification is
private and removed on exit. To check a saved archive later, verify its
checksum, then decrypt it only into a private mode-700 directory and run the
same checks before using its contents for recovery.

This is an offline-prepared operator tool. Do not test it against production
until the owner has installed and checked the SSH host key and age identity.
