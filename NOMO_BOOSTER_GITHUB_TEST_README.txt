NOMO BOOSTER — SEPARATE GITHUB TEST CHANNEL
===========================================

UPLOAD THESE TWO FILES TO THE REPOSITORY ROOT
---------------------------------------------
nomo_booster_test.py
nomo_booster_probe.lua

Repository:
atmincosplay-ship-it/nomo-rejoin-releases

ONE-TIME INSTALL IN TERMUX
--------------------------
mkdir -p /storage/emulated/0/Download/nomo_booster_test && \
curl -fL "https://raw.githubusercontent.com/atmincosplay-ship-it/nomo-rejoin-releases/main/nomo_booster_test.py" \
-o /storage/emulated/0/Download/nomo_booster_test/nomo_booster_test.py && \
curl -fL "https://raw.githubusercontent.com/atmincosplay-ship-it/nomo-rejoin-releases/main/nomo_booster_probe.lua" \
-o /storage/emulated/0/Download/nomo_booster_test/nomo_booster_probe.lua && \
printf '%s\n' \
'#!/data/data/com.termux/files/usr/bin/bash' \
'cd /storage/emulated/0/Download/nomo_booster_test' \
'exec python nomo_booster_test.py "$@"' \
> "$PREFIX/bin/nomoboost" && \
chmod +x "$PREFIX/bin/nomoboost"

COMMANDS
--------
nomoboost update
nomoboost install
nomoboost status
nomoboost status USERNAME --details
nomoboost remove
nomoboost version

NORMAL TEST FLOW
----------------
nomoboost update && nomoboost install

Restart one Booster clone.

Then:
nomoboost status USERNAME --details

After testing:
nomoboost remove

PRODUCTION IS SEPARATE
----------------------
nomo update && nomo

The test channel never replaces:
- nomo_rejoin.py
- nomo_pet_counter.lua
- the nomo launcher
- normal <username>_state.json
