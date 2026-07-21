NOMO REJOIN V4.58.48

Fixes:
- Option 13 validates NOMO secret through authenticated /accounts.
- Explicit HTTP 401 clears only cloudflare_secret and asks for a replacement.
- D1 account read failure no longer crashes private-server setup.
- '# NOMO REJOIN' is line 2, so GitHub updater validation succeeds.
- Updater scans the first 12000 characters for the marker.
- solver_* CAPTCHA settings are preserved.
