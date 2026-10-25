# ApexMCP

A standalone [MCP](https://modelcontextprotocol.io) server for Neptune Apex aquarium controllers. It talks straight to the controller's local REST API. No MQTT broker, no Home Assistant, no Neptune Fusion cloud login.

**Read-only by default.** Outlet control, feed cycles and program edits each have to be switched on.

> Not affiliated with Neptune Systems. The Apex REST API is undocumented and can change with firmware. Tested only against AOS 5.15L on an AC6L.

## Install

Python 3.11+.

```bash
pip install -e .
```

## Configure

Everything is an environment variable.

| Variable | Default | Notes |
| --- | --- | --- |
| `APEX_HOST` | required | `http://reef.local` or an IP. `.local` names need working mDNS on the machine running the server. |
| `APEX_USERNAME` | `admin` | |
| `APEX_PASSWORD` | empty | Or `APEX_PASSWORD_FILE` to read it from a file: one line is the password, two lines are username then password. Needed for any write. |
| `APEX_ALLOW_WRITE` | `false` | Enables `apex_set_outlet`, `apex_start_feed`, `apex_cancel_feed`. |
| `APEX_ALLOW_PROGRAM_WRITE` | `false` | Also enables `apex_update_program`. Requires `APEX_ALLOW_WRITE`. |
| `APEX_PROTECTED_OUTLETS` | none | Comma-separated device IDs or names (`ReturnPump,2_8`). These accept only `AUTO` and their programs cannot be edited. |
| `APEX_BACKUP_DIR` | `backups` | Where old programs are saved before an edit. |
| `APEX_TIMEOUT_SECONDS` | `10` | |

Disabled tools are not registered at all, so the model never sees them.

### Claude Desktop (Chat tab) and Claude Code

Claude Desktop reads `%APPDATA%\Claude\claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`). Add the block below under `mcpServers` and fully restart the app. Claude Code takes the same definition: save it as JSON and run `claude mcp add-json apex "<json>" --scope user` (on Windows PowerShell this is more reliable than `claude mcp add ... -- python -m apexmcp`, which mangles `-m`).

The server runs on your own machine over stdio, so it works in the desktop app but not in claude.ai in a browser or the mobile app. The settings are read once at startup, so restart the client after changing the password file or any variable.

```json
{
  "mcpServers": {
    "apex": {
      "command": "python",
      "args": ["-m", "apexmcp"],
      "env": {
        "APEX_HOST": "http://reef.local",
        "APEX_PASSWORD_FILE": "C:\\path\\to\\apex-password.txt",
        "APEX_ALLOW_WRITE": "true",
        "APEX_PROTECTED_OUTLETS": "ReturnPump,Heater200"
      }
    }
  }
}
```

Use the Python from the environment where you ran `pip install`. Leave out the two `ALLOW` lines for a read-only server.

## Tools

Always available:

| Tool | What it does |
| --- | --- |
| `apex_system_info` | Identity, firmware, modules, feed state, power-failure times, network quality |
| `apex_get_probes` | Current readings, optionally by type |
| `apex_get_outlets` | Every outlet: mode (AUTO/ON/OFF), energized, power level, pump mode |
| `apex_feed_status` | Running feed cycle and seconds remaining |
| `apex_module_health` | Aquabus module presence and packet error rate |
| `apex_get_config` | One config section, credentials redacted |
| `apex_probe_history` | Controller log for probes (default Temp and pH), downsampled |
| `apex_probe_stats` | min/mean/max, daily means, day-over-day drift |
| `apex_outlet_events` | Exact on/off times from `/rest/olog` |
| `apex_duty_cycle` | Percent on, run count, mean run length |
| `apex_get_program` | Program text for one or all outlets |

With `APEX_ALLOW_WRITE`:

| Tool | What it does |
| --- | --- |
| `apex_set_outlet` | AUTO / ON / OFF. Re-reads the controller and reports before and after. |
| `apex_start_feed` | Feed A to D |
| `apex_cancel_feed` | Cancels only when a real feed is running |

With `APEX_ALLOW_PROGRAM_WRITE`:

| Tool | What it does |
| --- | --- |
| `apex_update_program` | Returns a diff by default. With `confirm=true` it backs up the old program, writes the new one, then re-reads to verify. Unknown directives are rejected. |

## Safety notes

See [SECURITY.md](SECURITY.md) for reporting a problem and the known limits, including prompt injection through controller-supplied names and programs.

- `/rest/config` contains the controller admin password in plaintext. It is stripped from every response, along with the Fusion link key. `nconf` is not available as a config section.
- Manual `ON`/`OFF` overrides an outlet's program until you set it back to `AUTO`. Heaters and return pumps are the obvious candidates for `APEX_PROTECTED_OUTLETS`.
- Cancelling a feed while none is running corrupts the controller's feed block for about 17 hours (`name: 256`, underflowed countdown). `apex_cancel_feed` refuses to do it, and feed data is range-checked on read.
- The controller has no HTTPS on the LAN, so the admin password travels in clear text. Change the default password.
- Status is cached for 3 seconds and 429 responses surface as an error, because the Apex rate-limits.

## Firmware notes

Learned the hard way, and encoded in `apexmcp/model.py`:

- Outlet state is `status[0]` (`AON`, `AOF`, `TBL`, `ON`, `OFF`). The first three mean the outlet is under program control.
- `/rest/ilog` logs inputs only. `/rest/olog` logs outlet changes. For both, `sdate` (`YYMMDD`) is the **start** date and the log runs from there to `sdate + days`, clipped at now. Treating `ilog`'s `sdate` as an end date, as some older write-ups do, returns only today.
- Temperature comes with no unit. Values of 45 or below are guessed as Celsius.
- Day-over-day drift is used instead of a straight-line fit, which reports phantom drift over a window that is not a whole number of daily cycles.

The `/rest/olog` response shape is parsed defensively and has not yet been checked against a live controller. Run `apex_outlet_events` against a logged outlet first.

## Develop

```bash
pip install -e ".[dev]"
pytest
```

The tool logic in `apexmcp/tools.py` is independent of the MCP framework and is tested with a fake controller.

## Credits and license

The Apex client and status parser come from [ReefHub](https://github.com/mad-science-lab/ReefHub). AGPL-3.0, see `LICENSE`.
