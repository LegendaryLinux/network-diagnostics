# Network diagnostics

Licensed under the [MIT License](LICENSE).

Collect timestamped connectivity evidence on Arch Linux, retain every individual result in SQLite, and inspect it in a local browser dashboard that refreshes every five seconds. An optional companion on your Ubuntu server measures persistent TCP and UDP exchanges independently of public websites.

The measurements help show when interruptions occur and which paths are affected. They cannot, by themselves, prove which organization or device caused an interruption.

## Run on Arch Linux

Requirements: Python 3.10 or newer, `curl`, and `ping` from `iputils`. No Python packages, frontend build, CDN, or privileged collector process are required. If these dependencies are missing, install them using your normal Arch package-management process (packages `python`, `curl`, and `iputils`). Run the following from this repository:

```bash
cp config.example.json config.json
python -m netdiag collect --config config.json
```

Open **http://127.0.0.1:8765/**. Keep the terminal running until you want to stop collection; press **Ctrl+C** for normal shutdown and a final data flush. Run the same command again to append a new session to the existing dataset. This is a manual process; it does not install a boot service. Closing your terminal or suspending the computer interrupts measurement.

To collect without starting the browser server, add `--no-dashboard`. Only one collector may write a given dataset at a time; a second collector using the same path exits with an error.

Edit `config.json` before collection. The example includes YouTube, Discord, Steam, Crunchyroll, and Netflix endpoints. A missing remote configuration still permits gateway and public-service measurements; the collector prints a warning. Set `remote` to an object containing your server's actual hostname/address and port to enable remote tests:

```json
"remote": {"host": "YOUR_SERVER_ADDRESS", "port": 48123}
```

This is a fragment to insert in the existing JSON configuration, not a complete configuration file. Important settings:

| Setting | Meaning |
| --- | --- |
| `db` | SQLite dataset path; keep this path unchanged to continue the same history. |
| `gateway` | Router IPv4 address; `null` uses the Linux default-route gateway. |
| `remote` | Companion hostname/address and port; `null` disables remote checks. |
| `fast_interval` | Seconds between gateway/remote measurements; default `0.2` (five per second for each probe stream). |
| `public_interval` | Seconds between each service's DNS, fresh TCP, and HTTP(S) checks; default `5`. |
| `timeout` | Probe deadline in seconds; default `2`. |
| `services` | List of objects with `name` and public `url`; no passwords or account tokens. |
| `dns_server` | Optional IPv4 DNS resolver override; otherwise use the first suitable system resolver. |
| `dashboard_port` | Local dashboard port; default `8765`. |

The sampling rate applies to individual probe streams, so the combined number of requests is greater than five per second. Public services are checked at the slower interval to limit load. Public checks are small synthetic reachability requests; they do not download streams or log into accounts. Configure an explicit gateway if your default route points through a VPN or unusual routing setup. The initial implementation uses IPv4 for ICMP, explicit DNS, and UDP companion tests.

## Run the companion as an Ubuntu service

Use Ubuntu Server 22.04 or newer with systemd and Python 3.10+. Copy this repository to the server, then run from the checkout:

```bash
sudo bash deploy/install-remote.sh
```

On first installation, enter a random shared key of at least 32 bytes at the hidden prompt (for example, 64 random hexadecimal characters from a password manager). Save that same key for your client. The installer keeps it in `/etc/network-diagnostics/remote.env`, readable only by root. Rerunning the command updates and restarts the service without prompting for or replacing an existing key.

The companion starts at boot, restarts after failures, and runs as an unprivileged user. Once the installer reports success, you can disconnect SSH. If you previously started the companion manually, stop that process with **Ctrl+C** before installing so it releases the port. No third-party Python packages are needed; if Python is missing, install it with `sudo apt install python3` first.

On the **Arch client**, enter the matching key through a hidden Bash prompt before starting the collector:

```bash
read -rsp 'Diagnostic shared key: ' NETDIAG_KEY
export NETDIAG_KEY
```

The prompt does not echo the secret. Set `remote` in the configuration as described above and run `python -m netdiag collect --config config.json` from the repository. After stopping the collector, `unset NETDIAG_KEY` clears that shell's copy. Ensure the clocks on both computers are synchronized; authenticated requests outside a 30-second clock window are rejected.

Allow inbound **TCP and UDP port 48123** in your server/provider firewall, preferably restricted to your client address. ICMP ping must be allowed separately; it has no port. The installer does not change firewall rules. Public-service HTTPS tests use their normal ports; the companion protocol is not HTTP or TLS.

To check status, follow logs, or stop the companion:

```bash
sudo systemctl status network-diagnostics-remote.service --no-pager
sudo journalctl -u network-diagnostics-remote.service -f
sudo systemctl stop network-diagnostics-remote.service
```

Use each command as needed. **Ctrl+C** exits the log view without stopping the service. Use `sudo systemctl restart network-diagnostics-remote.service` to start it again, or `sudo systemctl disable --now network-diagnostics-remote.service` to stop it and disable boot startup. For updates, rerun the install command. For key changes, edit `/etc/network-diagnostics/remote.env` with `sudoedit`, restart the service, and update the client's key. Keep secrets out of shell commands/history, source files, and the dataset.

<details>
<summary>Protocol details and removal</summary>

The companion sends fixed 97-byte authenticated synthetic responses to fixed 97-byte requests. Responses are bound to the request nonce, sequence, timestamp, and synthetic payload. Invalid, expired, and replayed messages receive no reply. It accepts at most 64 concurrent TCP clients and 200 candidate messages/second globally, with a 10-second idle/write deadline and a bounded replay cache. Rate limits are safety bounds, not target throughput: exceeding them can itself cause probe failures. TCP connect-only checks require no shared secret at the connection handshake; only authenticated application frames receive data. The protocol authenticates synthetic traffic but does not encrypt it; it carries no captured application traffic or user content.

Invalid messages consume the global rate budget, and unauthenticated TCP clients can occupy connection slots until their deadline. Hostile traffic to an exposed port can therefore cause diagnostic failures despite a healthy home connection. Restricting inbound traffic to your client address helps reduce this interference. Compare server availability and load with the recorded failures before drawing conclusions about the ISP.

The service requests graceful shutdown with SIGINT and allows 15 seconds before forced termination. After a server restart, the client should reconnect and retain failures recorded while the server was unavailable. The in-memory replay cache resets at restart; the short freshness window and equal-size replies limit the impact of a captured request during that window.

To uninstall the service and its installed code (the repository and client dataset are unaffected):

```bash
sudo systemctl disable --now network-diagnostics-remote.service
sudo rm /etc/systemd/system/network-diagnostics-remote.service
sudo systemctl daemon-reload
sudo rm /opt/network-diagnostics/netdiag/__init__.py /opt/network-diagnostics/netdiag/protocol.py /opt/network-diagnostics/netdiag/remote.py
sudo rmdir /opt/network-diagnostics/netdiag /opt/network-diagnostics
```

This leaves the protected key available for reinstallation. If you also want to delete it, run `sudo rm /etc/network-diagnostics/remote.env` followed by `sudo rmdir /etc/network-diagnostics`.

</details>

## Inspect results

The dashboard reads the dataset without modifying it. Choose a time range and target/probe filters, compare timing and failure trends, and select a timeframe for paginated individual results. It refreshes every five seconds. A stopped collector leaves the existing history available; start the dashboard separately to inspect it:

```bash
python -m netdiag dashboard --db data/netdiag.sqlite3 --port 8765
```

Use your configured `db` path if it differs. The dashboard binds to loopback and is intended for this computer's browser. Do not expose it through a public reverse proxy: datasets reveal destination names, connectivity patterns, and timestamps. The database can be retained as original evidence; use a SQLite-aware backup while collecting, or stop collection before copying the database and any remaining SQLite sidecars together.

For windows of **15 minutes or less**, graph queries use raw results when the interval contains at most 100,000 matching records. Larger windows or heavier intervals use stored time summaries. Summary boundary buckets include their full underlying interval, so a graph's totals can include measurements just outside a custom boundary; the chart notes this. Raw detail queries retain the exact requested interval. Choose a narrow interval to investigate brief interruptions precisely.

Latency statistics include successful timed measurements. The p50 and p95 values are approximate histogram estimates for each graph bucket. The comparison table shows the minimum-to-maximum range of those bucket estimates; it is not a percentile calculated across the entire selected window. Expand **Observed failure runs in this interval** to inspect consecutive failed probes. A run closed by a measurement gap, normal collector stop, or interrupted session is censored: the observations ended without demonstrating recovery. Only a subsequent successful measurement marks a run as recovered. A single failed result can have zero observed duration, and a failure run does not establish a complete network outage.

Interpret failures by probe type:

- ICMP loss can reflect routers deprioritizing or blocking ping; compare it with TCP/UDP/HTTPS at the same time.
- UDP reports missing authenticated round-trip replies. It cannot distinguish forward-path loss, return-path loss, server failure, or packet filtering by itself.
- Persistent TCP detects delayed replies, broken connections, and reconnects. TCP retransmission can hide individual lost IP packets and turn them into higher latency.
- Fresh TCP checks measure connection establishment. Public HTTP errors, including throttling or bot rejection, are application responses rather than proof of packet loss.
- DNS, connect, TLS, and request timings apply where the protocol exposes those stages. Website reachability does not establish that video playback, Discord voice, or game traffic works correctly.
- Measurement gaps from sleep, process stoppage, or collector scheduling are distinct from observed probe failures. Gateway failures plus external failures suggest a different problem from external failures with a healthy gateway. A remote server outage also produces failures even when your home connection works.

When presenting evidence, include the time zone, affected time ranges, comparison across protocols/targets, and whether the collector and companion were running. An Ethernet connection removes Wi-Fi as one variable, but does not isolate the modem, router, host, or ISP by itself.

## Retention and disk growth

Every individual result is retained indefinitely, including failures. There is no configured size limit, automatic deletion, or raw-data expiration. SQLite uses batched writes and indexed raw records; time summaries let long-range charts avoid loading all raw records. Historical detail remains available through bounded, paginated queries. The dashboard exposes storage usage.

The dashboard receives the latest 1,000 target definitions, where each distinct name/probe/endpoint/interval combination is a separate definition. Extensive configuration changes can eventually hide older streams from graph selection; their individual results remain in SQLite.

The collector creates a new dataset directory with mode `0700`, and the database, lock, and SQLite sidecars use private `0600` permissions. An existing dataset directory must be owned by your user and must not be writable by the group or other users; otherwise collection refuses to start. Existing dataset files must also be owned regular files with a single link. Use a directory you control rather than a shared writable location. Backups need the same care because they contain your measurement history.

At five samples/second, **one probe stream produces 432,000 records/day**. Five such streams produce 2.16 million records/day before public-service checks. Allow substantial disk space: row and index sizes, targets, error detail, and SQLite journal activity affect actual growth. Summary data is additional to the retained raw data. Check actual dataset growth early in your run rather than assuming a fixed bytes/sample ratio. Live SQLite `-wal` and `-shm` sidecars can add temporary disk use. Collection cannot continue safely after the filesystem fills; a storage error must be treated as a stopped measurement run, not as evidence of network loss.

## Verification

```bash
python -m unittest discover -s tests -v
```

The tests use local synthetic fixtures rather than sustained traffic against public services. Socket tests need permission to create loopback TCP/UDP sockets. Deployment to an actual Ubuntu host and real external connectivity require your server configuration; they are not implied by local unit-test success.

The development environment used for this implementation blocks network socket creation, and an actual browser session and Ubuntu deployment have not been verified there. After configuring your server and shared key, perform these practical checks on your machines:

1. Install and enable the Ubuntu service as described above. Confirm `sudo systemctl is-enabled network-diagnostics-remote.service` reports `enabled` and `sudo systemctl is-active network-diagnostics-remote.service` reports `active`. On Arch, run `python -m netdiag collect --config config.json` with the matching `remote` configuration and key. Confirm the dashboard shows successful remote exchanges, then disconnect SSH and confirm those exchanges continue. At a suitable time, reboot Ubuntu and confirm the service starts automatically and exchanges resume; the reboot interval should remain recorded as failures.
2. Open `http://127.0.0.1:8765/` in your browser. Confirm results update after five seconds. Choose **Last 15 minutes**, change **Target**, **Probe**, and **Graph metric**, then click a graph timeframe or select **Inspect timeframe**. Select a result timestamp to inspect its full record, and use **Older results** and **Newer results** when enough records exist. Try a **Custom interval**, **Apply interval**, **Clear timeframe**, **Follow latest**, and **Refresh now**.
3. Run `sudo systemctl stop network-diagnostics-remote.service` on Ubuntu while leaving collection running. Confirm failed remote exchanges appear. Run `sudo systemctl start network-diagnostics-remote.service` and confirm successful persistent TCP reconnection and resumed UDP replies. ICMP may remain successful during this test because the server host itself is still reachable. To check restart-on-failure during this test session, run `sudo systemctl kill --signal=SIGKILL --kill-who=main network-diagnostics-remote.service`; confirm it returns to `active` after about five seconds, and inspect the journal for the restart. This deliberately interrupts the companion, so mark that interval as a server test when interpreting the dataset.
4. Stop the collector with Ctrl+C, restart it against the same dataset, and confirm older records remain alongside the new session. Stop it again and use the standalone dashboard command above to inspect the retained history. A stopped or interrupted collection session must not be interpreted as network recovery.
