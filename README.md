# Mr. Coffee

A coffee cup in the macOS menu bar that keeps your Mac awake while Claude Code works, then lets it
sleep when the work is done.

## Quick start

You need macOS and [uv](https://docs.astral.sh/uv/). From a checkout of this repository:

```sh
uv tool install .
mrcoffee install
```

That's it. The cup fills while Claude is working and empties about two seconds after it finishes.
There is nothing else to set up.

`mrcoffee install` starts the app now and at every login, and adds hooks, one permission and a short
guidance block to your Claude Code config (`settings.json` is backed up first). Use `--no-claude` to
get only the menu bar app. If your shell cannot find `mrcoffee`, run `uv tool update-shell`.

## The menu

Click the cup. It is filled while the Mac is kept awake and an outline when it is not.

| Item | What it does |
|---|---|
| Status line | `Off`, or why the Mac is awake, for example `Awake · manual + 2 agents` |
| Keep Awake | Keeps the Mac awake until you untick it |
| Keep Display On | Also keeps the display on while the Mac is awake |
| Release Agent Holds | Drops every hold Claude Code created, if one is stuck |
| Quit | Stops the app until your next login or `mrcoffee install` |

## Useful commands

```sh
mrcoffee radar                                    # status, active holds, is the app running
uv tool install --reinstall . && mrcoffee install # upgrade
mrcoffee uninstall && uv tool uninstall mrcoffee  # uninstall (add --purge to delete data and logs)
```

## How it works

```
 Claude Code hooks ──┐
 mrcoffee acquire ───┼── write holds ──▶  holds.db  ◀── reads every 2 s ── menu bar app ──▶ caffeinate
 menu clicks ────────┘                    (SQLite)
```

Each reason to stay awake is a "hold" row in a SQLite file. Claude Code's hooks add a hold when you
send a prompt and remove it when the turn ends. For longer background work, Claude runs
`mrcoffee acquire` and `mrcoffee release` itself. The menu bar app runs `caffeinate` while any
unexpired hold exists, and stops it when none do. caffeinate is tied to the app's process, so it
can never be left running on its own.

## Known limits

- Closing the lid on battery with no external display still sleeps the Mac.
- Pressing Esc in Claude Code skips the `Stop` hook, so the hold lasts until the next turn ends or
  4 hours pass. Use Release Agent Holds to drop it sooner.
- After Quit, nothing keeps the Mac awake. `mrcoffee radar` warns about this.

## Development

Run `uv run pytest`. On macOS this includes tests against the real caffeinate and app, which briefly
show a cup in the menu bar.

## License

MIT. See [LICENSE](LICENSE).
