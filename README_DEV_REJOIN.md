NOMO Rejoin Dev
================

This dev channel now uses the full source UI/menu/table from the working
rejoin source, with dev paths so stable `nomo` is not overwritten.
V4.59.1 adds a shared core rejoin wrapper used by the queued mode opens.

Install / Update
----------------

Run once in Termux:

```sh
curl -L -H "Accept: application/vnd.github.raw" -H "User-Agent: nomo-dev-installer" "https://api.github.com/repos/atmincosplay-ship-it/nomo-rejoin-releases/contents/install_dev.sh?ref=main" | sh
```

Update:

```sh
nomo-dev update
```

Open:

```sh
nomo-dev
```

Test Flow
---------

1. Run `nomo-dev version`.
2. Run `nomo-dev`.
3. Choose option `1` to start the source-style rejoin loop.
4. Use the source stop behavior: `Q + Enter` / Ctrl+C.

Dev Isolation
-------------

The dev source stores its own files under:

```sh
/storage/emulated/0/Download/nomo_rejoin_dev_source
```

On first run it copies existing stable source config from:

```sh
/storage/emulated/0/Download/nomo_rejoin
```

Only missing files are copied. Stable files are not deleted or overwritten.
