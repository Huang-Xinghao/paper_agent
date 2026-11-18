# LAND MCP bridge

The Windows bridge is installed at C:\Tools\land-mcp and exposed only on
127.0.0.1:8766/mcp. The LAND-MCP-Bridge scheduled task starts at admin user logon,
supervises the MCP process, and reconnects the SSH reverse tunnel:
server 127.0.0.1:18766 -> Windows 127.0.0.1:8766.

## Verified capabilities

- land_health: DLL version, timestamp and channel discovery.
- land_list_channels: directory inventory, including original native identifiers.
- land_get_status: explicitly unknown; live status has not been verified.
- land_get_measurements: explicitly unavailable; all measurements are null.

The SDK's raw box and channel IDs are zero based. Display numbers and channel IDs
use raw + 1, consistent with local SYSINFO filenames. Channel discovery is not
proof of device connectivity, charging or discharging. The first verified snapshot
contained 80 channels. No start/stop, procedure download or calibration functions
are exposed.

The bridge uses the existing x64 LANDCex DLL without modifying vendor files.
DLL calls execute serially and snapshot allocations are always freed.
The HTTP MCP implementation supports initialize, ping, tools/list and tools/call.
It uses stateless JSON responses and a 32-byte random bearer token.
The SSH key is restricted to the reverse listen port, with no PTY, agent or X11 forwarding.

## Paper Agent

Runtime configuration: ~/.config/paper-agent/mcp.json, server name land.
Token file: ~/.config/paper-agent/land-mcp.token (mode 0600).
Phases: experiment_bridge, laboratory, land_smoke.
Frontend: /laboratory?tab=land.
Backend: /api/laboratory/land, using the same snapshot model/cache as BTS.
The existing BTS configuration is preserved.

Source snapshots of the Windows scripts are in scripts/land-mcp/; no token or
private key is stored in the repository.

## Operation

Windows logs: server.log, server-error.log, tunnel.log, tunnel-error.log under
C:\Tools\land-mcp. Check task with Get-ScheduledTask -TaskName LAND-MCP-Bridge.
The task runs in the logged-in user's session, where LANDCore is running.
After a reboot the Windows user must log in, and LAND software must be available.
If Cloudflare Access expires, run:
C:\Tools\cloudflared.exe access login --quiet https://ssh.zhigroup.net
Complete browser authentication; the tunnel supervisor will reconnect.
The SSH password is not stored.

Restart only the API after backend updates:
systemctl --user restart paper-agent-api.service
The frontend dev server reloads changed source; npm run build updates preview output.

## Validation

Verified local DLL enumeration, authenticated MCP from the server through SSH,
official MCP client tool discovery, and Paper Agent snapshot assembly.
Unit tests cover unknown telemetry, disconnection and preserved BTS behavior.
Browser tests cover both vendors, tab switching, discovery-only warnings and mobile layout.

## Public frontend deployment

The public site https://www.xora.top is served by Vercel, not the local Vite
server. Changes require a production Vercel deployment from paper-agent-web.
The linked Vercel project is frontend; vercel.json forwards /api to api.xora.top.
Verified production deployment: frontend-pprc7r3jw-httpsxoratop.vercel.app.
A real browser logged in at https://www.xora.top/laboratory?tab=land and
verified the LAND tab, 80 channels and no JavaScript errors.
A local build or localhost browser check alone does not validate this site.
