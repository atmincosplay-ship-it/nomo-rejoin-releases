NOMO REJOIN V4.58.47 — OPTION 13 SECRET REPROMPT

Problem fixed
Option 13 previously reused any non-empty saved NOMO secret automatically.
The secret could be invalid, but the script would not discover that until a
later D1 Market registry request returned HTTP 401.

New behavior
1. Option 13 tests the saved Worker URL and secret first.
2. A successful health check reuses them automatically.
3. These explicit authentication failures trigger an immediate new-secret prompt:
   - HTTP 401
   - unauthorized
   - Missing or invalid NOMO secret
   - invalid NOMO secret / invalid secret
4. Only cloudflare_secret is cleared in nomo.json and hatcher_reporter_config.json.
5. CAPTCHA solver configuration and all solver_* keys are preserved.
6. Timeouts, DNS problems, and server errors do not erase the saved secret.

Install over
/storage/emulated/0/Download/nomo_rejoin
