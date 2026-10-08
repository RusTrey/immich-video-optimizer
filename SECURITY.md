# Security

Immich Video Optimizer is an administrator tool with filesystem write access.
It does not provide authentication, authorization, TLS, or tenant isolation.

- Bind the web UI to localhost or a trusted private network.
- Do not expose port 8090 directly to the public internet.
- Keep `REPLACEMENT_ENABLED=false` until mounts and rollback have been tested.
- Back up valuable media independently; optimizer backups are rollback copies,
  not a disaster-recovery system.
- Never mount more host storage than the service requires.

Do not include media files, database contents, credentials, private paths, or
personal metadata in public security reports. Report vulnerabilities through
GitHub private vulnerability reporting when it is enabled for the repository.
