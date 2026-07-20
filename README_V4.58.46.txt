NOMO REJOIN V4.58.46 — DEVICE WEBHOOK + SINGLE CLONE

Default Discord webhook
https://discord.com/api/webhooks/1520291358223634522/2xoV1O0qPdg8QOFxVpBfFQiAyDGpVHpOhhJ5m70E86HdgeyXKWsGyiINK_CUSHJqkUpU

Webhook message
Delta key link — <device name>
https://auth.platorelay.com/a?d=...

No /bypass prefix is sent.

Device name
- Option 19 -> Settings -> 23 sets a custom name.
- Blank uses Android device_name, Bluetooth name, hostname, or model.

Single-clone workflow
- One Redfinger/device uses one shared Delta key.
- One worker clone performs bootstrap, panel detection, Copy Link, and ticket work.
- Option 19 -> Settings -> 24 selects the worker clone.
- After applying the license, only the source clone restarts.
- Option 19 -> 8 still manually restarts every enabled clone.
- Option 19 -> Settings -> 25 toggles source_only/all_enabled.

Safety
- Exact selected-package PID only.
- No am force-stop, killall, or pkill.
