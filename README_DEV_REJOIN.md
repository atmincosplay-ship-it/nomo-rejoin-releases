NOMO Rejoin Dev
================

This is the clean/system rewrite test channel. It does not replace stable
`nomo` yet.

Install / Update
----------------

Run this in Termux:

```sh
curl -L https://raw.githubusercontent.com/atmincosplay-ship-it/nomo-rejoin-releases/main/install_dev.sh | sh
```

After that:

```sh
nomo-dev update
```

Open the dev menu:

```sh
nomo-dev
```

Safe First Tests
----------------

These should not stop or open Roblox:

```sh
  nomo-dev doctor
  nomo-dev init
  nomo-dev list
  nomo-dev menu
```

Expected:

- `doctor` says forbidden stop commands are `none`.
- `init` creates `/storage/emulated/0/Download/nomo_rejoin_clean/config.json`.
- `list` shows detected Roblox/Noka packages, alive/dead, state freshness,
  pet count, and the route decision.

If usernames are stale after account/cookie changes:

```sh
nomo-dev list --refresh-api
```

This reads each package cookie, asks Roblox's authenticated user API for the
username, and saves only username/user ID metadata locally. It does not write or
change cookies.

One Clone Stop/Open Test
------------------------

Only do this after `list` shows the clone name/package you expect.

```sh
nomo-dev test-rejoin clone1
nomo-dev stop clone1
nomo-dev restart clone1
```

Hard rule: the dev script uses exact PID stop only. It must not use broad
package stop commands.

Fleet Routing Test
------------------

This is still a dev skeleton. Only test after config and state look correct:

```sh
nomo-dev route clone1
```

Watch Loop Test
---------------

Dry-run first. This prints what the controller would do, but does not restart
or route anything:

```sh
nomo-dev watch --once
nomo-dev watch
```

Only after the dry-run output looks correct:

```sh
nomo-dev watch --apply
```

Stop a running watch from another Termux window:

```sh
nomo-dev stop-watch
```

The watch loop keeps exact-PID stop only and applies per-clone cooldowns plus a
fleet cooldown. After any clone is opened or restarted, the whole fleet waits
180 seconds before another real action. This prevents the controller from
opening every package at once when several clones are stale.
After a clone is opened, the monitor waits 180 seconds for fresh Lua state.
If the state is still stale, it retries only that same clone.
If a clone is alive but its state is already stale for 180 seconds, it also
retries that clone. The monitor performs at most one real action per cycle.

Stable stays separate:

```sh
nomo
nomo update
```
