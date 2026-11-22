# rclone for backups

The backup DAGs read `deploy/rclone/rclone.conf` (gitignored) and expect a
remote named `gdrive-crypt`: an encrypted (crypt) remote on top of a Google
Drive remote.

One-time setup on a machine with a browser:

    rclone config
      n) new remote  name: gdrive   type: drive   scope: drive.file
         (sign in with the Google account that should own the backups)
      n) new remote  name: gdrive-crypt   type: crypt   remote: gdrive:birddog-backups
         (let rclone generate the password and salt; keep a copy of them --
          without them the backups can't be decrypted)

Then copy the resulting config (`rclone config file` shows where it is) to
`deploy/rclone/rclone.conf` on the server. Google service accounts can't store
files in a personal My Drive, so use a regular account (or a Workspace shared
drive).
