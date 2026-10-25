# Security

## Reporting a problem

Use GitHub's private vulnerability reporting on this repository (Security tab, "Report a vulnerability"). Please do not open a public issue for anything that exposes credentials or lets an assistant act on a controller without consent.

## What this server protects

- **Credentials.** The controller's `/rest/config` contains the admin password in plaintext. ApexMCP strips it, the Fusion link key and anything else under credential-like keys from every response. The password is read from an environment variable or a file and is hidden from `repr()` of the settings object.
- **Equipment.** Outlet control, feed control and program edits are off by default. Disabled tools are not registered, so a model cannot call them. Protected outlets accept only `AUTO`, and program edits show a diff and write a backup before they apply.

## Known limits

- **No TLS.** The Apex local interface is plain HTTP, so the admin password and every response cross your LAN unencrypted. Keep the controller off the internet, change the default password, and run this server only on a trusted network.
- **Prompt injection.** Outlet names, program text and other fields come from the controller and are returned to the model as data. Anyone who can edit those fields can put instructions in them. Keep write access off unless you are watching, and keep heaters and return pumps in `APEX_PROTECTED_OUTLETS`.
- **Local server only.** This is a stdio server meant to run on your own machine. Do not expose it, or the Apex, to the internet.
- **Undocumented API.** Neptune does not publish the REST API. Firmware changes can alter behaviour, including what the config contains. Check the redaction against your own firmware before trusting it.
