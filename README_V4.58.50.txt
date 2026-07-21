NOMO REJOIN V4.58.50 — DELTA REJOIN TAKEOVER FIX

What the screenshot revealed
- The expired Delta panel was visible while Option 1 kept queueing old-state
  package recoveries.
- delta_key_auto_monitor_tick was called only after the queue was built.
- Because safe_to_act was false, the Delta worker could never start.
- The normal queue then killed/reopened clones again, creating a deadlock.

Fix
- Market, Hatcher/Booster, and Rejoin Only now clear/defer their normal package
  queue whenever Delta renewal is active.
- On the next five-second tick, the one configured worker clone can open the
  bootstrap place, detect the panel, capture the URL, send Discord, poll the
  API, apply the shared key, and restart itself.
- Normal rejoin resumes afterward.

Expiry detection fix
- The old default API refresh interval is reduced from 600s to 60s.
- Existing configs using exactly the old 600s default are migrated to 60s.
- If the API returns exp-claim/invalid ticket while local expires_at still says
  valid, NOMO immediately arms renewal instead of ignoring that response.

Single-clone policy remains
- One worker clone performs the Delta key flow.
- The other clones are not used for Copy Link.
- The shared license still applies device-wide.
