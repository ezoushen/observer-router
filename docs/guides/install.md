# Install as a macOS service

The router is standard-library only, so any Python 3.11+ works. The reference install uses a
dedicated venv so the service cannot be disturbed by unrelated package installs.

1. Copy `com.ezou.observer-router.plist` into `~/Library/LaunchAgents/`.
2. Edit `Label`, both `ProgramArguments` paths, and `PATH` for your machine.
3. Copy `config.example.toml` to `~/.config/observer-router/config.toml` and edit it. A local
   instance's `model` must match the id the server advertises at `/v1/models`, or it answers 404
   and the chain loses its backstop.
4. Point claude-mem at the router, per [Use with claude-mem](claude-mem.md).
5. Load it and check health:

```bash
launchctl bootout gui/$UID/com.ezou.observer-router   # "no such process" is fine on first install
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.ezou.observer-router.plist
curl -fsS http://127.0.0.1:1244/health
```

Restart with `bootout`/`bootstrap`, never `launchctl stop`: stopping leaves the service
unloaded, and `KeepAlive` cannot bring back a job that is no longer registered.
