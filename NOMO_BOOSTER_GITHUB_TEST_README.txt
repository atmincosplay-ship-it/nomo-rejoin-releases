NOMO BOOSTER — GITHUB TEST CHANNEL V0.2.1
================================================

FIX
---
The old updater downloaded into Termux internal storage:

/data/data/com.termux/files/usr/tmp/...

and then tried to rename directly into:

/storage/emulated/0/Download/...

Android reports:

OSError: [Errno 18] Invalid cross-device link

V0.2.1 now:
1. downloads and validates in Termux temp
2. copies to a temporary sibling beside the destination
3. renames that sibling on the same filesystem
4. keeps a backup of the previous destination

UPLOAD
------
Upload both files to the GitHub repository root:

nomo_booster_test.py
nomo_booster_probe.lua

ONE-TIME REPAIR
---------------
The installed updater is already broken, so replace it once after uploading:

curl -fL "https://raw.githubusercontent.com/atmincosplay-ship-it/nomo-rejoin-releases/main/nomo_booster_test.py" -o /storage/emulated/0/Download/nomo_booster_test/nomo_booster_test.py

Then:

nomoboost update && nomoboost install

FUTURE COMMANDS
---------------
nomoboost update
nomoboost install
nomoboost status
nomoboost status USERNAME --details
nomoboost remove

Production remains separate:

nomo update && nomo
