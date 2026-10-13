# Network diagnostics

Licensed under the [MIT License](LICENSE).

Collect timestamped connectivity evidence on Arch Linux, retain every individual result in SQLite, and inspect it in a local browser dashboard that refreshes every five seconds. An optional companion on your Ubuntu server measures persistent TCP and UDP exchanges independently of public websites.

The measurements help show when interruptions occur and which paths are affected. They cannot, by themselves, prove which organization or device caused an interruption.

## Run on Arch Linux

Requirements: Python 3.10 or newer and `curl`. No Python packages, frontend build, CDN, or privileged collector process are required. If these dependencies are missing, install them using your normal Arch package-management process (packages `python` and `curl`). Run the following from this repository:

```bash
cp config.example.json config.json
python -m netdiag collect --config config.json
```

Open **http://127.0.0.1:8765/**. Keep the terminal running until you want to stop collection; press **Ctrl+C** for normal shutdown and a final data flush. Run the same command again to append a new session to the existing dataset. This is a manual process; it does not install a boot service. Closing your terminal or suspending the computer interrupts measurement.

To collect without starting the browser server, add `--no-dashboard`. Only one collector may write a given dataset at a time; a second collector using the same path exits with an error.

Edit `config.json` before collection. The example includes YouTube, Discord, Steam, Google, and Yahoo endpoints. A missing remote configuration still permits public-service measurements; the collector prints a warning. Set `remote` to an object containing your server's actual hostname/address and port to enable remote tests:

```json
"remote": {"host": "YOUR_SERVER_ADDRESS", "port": 48123}
```

This is a fragment to insert in the existing JSON configuration, not a complete configuration file. Important settings:

| Setting | Meaning |
| --- | --- |
| `db` | SQLite dataset path; keep this path unchanged to continue the same history. |
| `remote` | Companion hostname/address and port; `null` disables remote checks. |
| `fast_interval` | Seconds between remote persistent TCP/UDP measurements; default `0.2` (five per second for each probe stream). |
| `public_interval` | Seconds between each service's DNS, fresh TCP, and HTTP(S) checks; default `5`. |
| `timeout` | Probe deadline in seconds; default `2`. |
| `services` | List of objects with `name` and public `url`; no passwords or account tokens. |
| `dns_server` | Optional IPv4 DNS resolver override; otherwise use the first suitable system resolver. |
| `dashboard_port` | Local dashboard port; default `8765`. |

The sampling rate applies to individual probe streams, so the combined number of requests is greater than five per second. Public services are checked at the slower interval to limit load. Public checks are small synthetic reachability requests; they do not download streams or log into accounts. Explicit DNS, TCP, and UDP companion tests use IPv4. Legacy `gateway` configuration entries are accepted and ignored.

Completed public-service HTTP responses **401, 403, 405, or 429** trigger increasing backoff for that configured service's **HTTP checks only**. Its DNS and fresh TCP checks continue normally, as do the remote companion and other services. The first delay starts at 60 seconds or twice `public_interval`, whichever is greater; repeated rejections double it. Both the first and subsequent automatic delays are capped at one hour, but never below your configured cadence. Valid `Retry-After` seconds or HTTP-date guidance can extend that delay. HTTP checks resume on the next eligible scheduled tick, without catching up skipped requests. Any other completed HTTP response restores normal cadence and clears the rejection history. Transport errors neither trigger nor reset rejection backoff, even when they include an HTTP status.

A completed HTTP exchange with a valid final status **200–599** is a successful network reachability measurement, including 403, 405, 429 and server-error responses. The HTTP status and timings remain in the individual record; success means the endpoint responded, not that it accepted the request. A transport error remains a failure even if a status was received before the error. During cooldown, skipped HTTP checks are recorded as **`service_backoff` coverage gaps**, with the reason and remaining delay, and are excluded from request percentages. Requests already in flight may finish. Cooldown state survives **Clear history** but resets when collection restarts; your configured interval is unchanged.

## Run the companion as an Ubuntu service

Use Ubuntu Server 22.04 or newer with systemd and Python 3.10+. Copy this repository to the server, then run from the checkout:

```bash
sudo bash deploy/install-remote.sh
```

On first installation, enter a nonempty shared passphrase at the hidden prompt. Use exactly the same passphrase on your client, including spaces and capitalization. The installer keeps it in `/etc/network-diagnostics/remote.env`, readable only by root. Rerunning the command updates and restarts the service without prompting for or replacing an existing key.

The companion starts at boot, restarts after failures, and runs as an unprivileged user. Once the installer reports success, you can disconnect SSH. If you previously started the companion manually, stop that process with **Ctrl+C** before installing so it releases the port. No third-party Python packages are needed; if Python is missing, install it with `sudo apt install python3` first.

On the **Arch client**, set `remote` in the configuration as described above and start the collector:

```bash
python -m netdiag collect --config config.json
```

Enter the matching passphrase at the hidden prompt; there is no minimum length beyond being nonempty. When a passphrase is supplied, each program derives its authentication key once at startup. The client remembers only the derived authentication key in a private per-remote file under `$XDG_CONFIG_HOME/network-diagnostics` (or `~/.config/network-diagnostics` when unset). The directory is mode `0700` and key files are `0600`; the passphrase itself is not saved on the client, and neither credential is written to your configuration or dataset. Later runs against the same hostname and port use the saved key without prompting. Local-only collection and dashboard inspection do not access the credential cache. For unattended collection, supply `NETDIAG_KEY` through your environment instead; a nonempty environment passphrase overrides the remembered key without replacing it. Without a remembered key, usable terminal, or environment passphrase, remote collection exits before creating a dataset. Cache access errors also stop startup clearly. Ensure the clocks on both computers are synchronized; authenticated requests outside a 30-second clock window are rejected.

To replace a remembered key after changing the server passphrase, run:

```bash
python -m netdiag collect --config config.json --ask-passphrase
```

This explicitly prompts again and replaces the saved key, overriding any `NETDIAG_KEY` environment value for that run. A saved derived key grants access to the companion, so keep these files private even though they do not contain the original passphrase.

**Updating an existing installation:** update the code on both the client and server, and rerun the server installer. Existing `/etc/network-diagnostics/remote.env` values are preserved and now used as passphrases. Older raw-key clients or companions cannot authenticate with the new derived-key version, even when the entered text is identical.

Allow inbound **TCP and UDP port 48123** in your server/provider firewall, preferably restricted to your client address. The installer does not change firewall rules. Public-service HTTPS tests use their normal ports; the companion protocol is not HTTP or TLS.

To check status, follow logs, or stop the companion:

```bash
sudo systemctl status network-diagnostics-remote.service --no-pager
sudo journalctl -u network-diagnostics-remote.service -f
sudo systemctl stop network-diagnostics-remote.service
```

Use each command as needed. **Ctrl+C** exits the log view without stopping the service. Use `sudo systemctl restart network-diagnostics-remote.service` to start it again, or `sudo systemctl disable --now network-diagnostics-remote.service` to stop it and disable boot startup. For updates, rerun the install command. For key changes, edit `/etc/network-diagnostics/remote.env` with `sudoedit`, restart the service, and update the client's key. Keep secrets out of shell commands/history, source files, and the dataset.

<details>
<summary>Protocol details and removal</summary>

Authentication keys are derived using PBKDF2-HMAC-SHA256 with 600,000 iterations, a 32-byte output, and the fixed versioned application salt `network-diagnostics/auth-key/v1`. The exact UTF-8 passphrase is used without trimming or normalization. Derivation produces the required key size; it does not add password entropy. This fixed domain salt is not a unique random salt per installation: captured authenticated traffic permits offline password guessing, and guesses can be reused across installations. Predictable passwords remain guessable; choose a passphrase that is difficult to guess.

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

Dashboard inspection reads the dataset; only the explicitly confirmed **Clear history…** action deletes history. Choose a time range and target/probe filters, compare timing and failure trends, and select a timeframe for paginated individual results. It refreshes every five seconds. A stopped collector leaves the existing history available; start the dashboard separately to inspect it:

```bash
python -m netdiag dashboard --db data/netdiag.sqlite3 --port 8765
```

Use your configured `db` path if it differs. The dashboard binds to loopback and is intended for this computer's browser. Do not expose it through a public reverse proxy: datasets reveal destination names, connectivity patterns, and timestamps. The database can be retained as original evidence; use a SQLite-aware backup while collecting, or stop collection before copying the database and any remaining SQLite sidecars together.

For windows of **15 minutes or less**, latency line-graph queries use raw results when the interval contains at most 100,000 matching records. Larger windows or heavier intervals use stored time summaries. Summary boundary buckets include their full underlying interval, so a graph's totals can include measurements just outside a custom boundary; the chart notes this. Raw detail queries retain the exact requested interval. Choose a narrow interval to investigate brief interruptions precisely.

**Every two minutes** shows a separate stacked percentage graph with fixed, UTC-aligned 120-second bars. Each bar combines the selected target/probe streams and classifies individual requests exactly:

- Dark green: excellent, successful requests taking **50 ms or less**.
- Light green: good, successful requests taking **more than 50 ms and at most 200 ms**.
- Yellow: slow, successful requests taking **more than 200 ms**.
- Red: failed exchanges, regardless of elapsed time. A completed HTTP response is classified by latency rather than marked red because of its status.
- Gray: successful requests without usable timing; these remain in the request denominator rather than being assumed fast.

Measurement gaps and intentional rejection backoff are excluded from request percentages and listed separately. Empty bars mean no measured requests, not zero failures on a working connection. The current partial bar updates every five seconds; selected range boundaries also clip the first/last bar to the exact requested time. The count table shows the latest displayed bar until you select one, with separate excellent/good counts and percentages.

Click a bar or use **Inspect two-minute timeframe** to select its exact raw-result interval. The graph shows at most **120 bars (four hours)** at a time, even for long history windows; **Older bars**, **Newer bars**, and **Latest bars** navigate within the selected overall range without merging bars. Choosing a bar or paging freezes the overall time window; enable **Follow latest** to resume live scrolling. **Clear timeframe** restores raw results for the entire selected range.

Older datasets receive a one-time HTTP reachability correction on the next collector startup. Previously status-based HTTP failures with completed responses are reclassified as successful; transport failures remain failures. Original sample IDs, HTTP statuses, timings and detail are preserved, and affected streams’ timing summaries, outcome bars and failure runs are rebuilt together atomically. The terminal reports progress by stream. Large histories can delay collection startup and require additional temporary SQLite journal space. Until this correction completes, dashboard history reads ask you to start collection once instead of showing outdated HTTP failure counts.

Existing datasets gain exact outcome summaries on the next collector startup. This one-time scan preserves raw records, reports progress in the terminal, and may take time for large histories before collection starts. History inspection remains read-only and can read older raw history in bounded chunks. If an outcome page or partial boundary exceeds the 250,000-record exact-read limit, it shows an explicit error; start collection once to build summaries, or narrow the range/select fewer targets. A very dense partial boundary may still need a narrower range or fewer targets after summaries exist. No samples are silently omitted from a displayed bar.

Latency statistics include successful timed measurements. The p50 and p95 values are approximate histogram estimates for each graph bucket. The comparison table shows the minimum-to-maximum range of those bucket estimates; it is not a percentile calculated across the entire selected window. Expand **Observed failure runs in this interval** to inspect consecutive failed probes. A run closed by a measurement gap, normal collector stop, or interrupted session is censored: the observations ended without demonstrating recovery. Only a subsequent successful measurement marks a run as recovered. A single failed result can have zero observed duration, and a failure run does not establish a complete network outage.

Interpret failures by probe type:

- UDP reports missing authenticated round-trip replies. It cannot distinguish forward-path loss, return-path loss, server failure, or packet filtering by itself.
- Persistent TCP detects delayed replies, broken connections, and reconnects. TCP retransmission can hide individual lost IP packets and turn them into higher latency.
- Fresh TCP checks measure connection establishment. Completed HTTP responses count as network success, including throttling, bot rejection and server-error statuses. The raw table labels them **Reachable · HTTP 403**, for example, and retains the status and individual timings. HTTP transport failures remain failures even when a status is available.
- DNS, connect, TLS, and request timings apply where the protocol exposes those stages. Website reachability does not establish that video playback, Discord voice, or game traffic works correctly.
- Measurement gaps from sleep, process stoppage, or collector scheduling are distinct from observed probe failures. A remote server outage also produces failures even when your home connection works.

When presenting evidence, include the time zone, affected time ranges, comparison across protocols/targets, and whether the collector and companion were running. An Ethernet connection removes Wi-Fi as one variable, but does not isolate the modem, router, host, or ISP by itself.

### Clear history

In **Collection context**, choose **Clear history…**. The confirmation names the loaded dataset and requires typing `CLEAR`. Confirming permanently deletes its individual results, timing/outcome summaries, failure runs, sessions, and old target context. Configuration and remembered credentials are preserved; this action cannot be undone.

When using the collecting process’s dashboard, collection pauses briefly, clears the history consistently, and resumes with a fresh session. Existing target/probe selections, graph intervals, raw-result pages, and selected records reset to the default live view. New measurements can appear immediately after the clear. A standalone dashboard can clear a stopped dataset, but refuses if a separate collector still holds its writer lock: use that collector’s dashboard or stop collection first.

Cancel or Escape closes the confirmation before submission. While clearing, the dialog and refresh controls remain locked until the request returns. The dashboard never retries a clear automatically. If a response is lost, it reports that completion is unknown; close the dialog and inspect the refreshed history before deciding whether another clear is needed. Clearing history is not automatic retention or disk compaction; SQLite can reuse the released space without immediately reducing the database file’s size.

## Retention and disk growth

Every individual result is retained indefinitely, including failures, unless you explicitly clear history through the dashboard. There is no configured size limit, automatic deletion, or raw-data expiration. SQLite uses batched writes and indexed raw records; time summaries let long-range charts avoid loading all raw records. Historical detail, including any previously collected ICMP results, remains available through bounded, paginated queries. The dashboard exposes storage usage.

The dashboard receives the latest 1,000 target definitions, where each distinct name/probe/endpoint/interval combination is a separate definition. Extensive configuration changes can eventually hide older streams from graph selection; their individual results remain in SQLite.

The collector creates a new dataset directory with mode `0700`, and the database, lock, and SQLite sidecars use private `0600` permissions. An existing dataset directory must be owned by your user and must not be writable by the group or other users; otherwise collection refuses to start. Existing dataset files must also be owned regular files with a single link. Use a directory you control rather than a shared writable location. Backups need the same care because they contain your measurement history.

At five samples/second, **one probe stream produces 432,000 records/day**. The two remote streams produce 864,000 records/day before public-service checks. Allow substantial disk space: row and index sizes, targets, error detail, and SQLite journal activity affect actual growth. Summary data is additional to the retained raw data. Check actual dataset growth early in your run rather than assuming a fixed bytes/sample ratio. Live SQLite `-wal` and `-shm` sidecars can add temporary disk use. Collection cannot continue safely after the filesystem fills; a storage error must be treated as a stopped measurement run, not as evidence of network loss.

## Verification

```bash
python -m unittest discover -s tests -v
```

The tests use local synthetic fixtures rather than sustained traffic against public services. Socket tests need permission to create loopback TCP/UDP sockets. Deployment to an actual Ubuntu host and real external connectivity require your server configuration; they are not implied by local unit-test success.

The development environment used for this implementation blocks network socket creation, and an actual browser session and Ubuntu deployment have not been verified there. After configuring your server and shared key, perform these practical checks on your machines:

1. Install and enable the Ubuntu service as described above. Confirm `sudo systemctl is-enabled network-diagnostics-remote.service` reports `enabled` and `sudo systemctl is-active network-diagnostics-remote.service` reports `active`. On Arch, run `python -m netdiag collect --config config.json` with the matching `remote` configuration and key. Confirm the dashboard shows successful remote exchanges, then disconnect SSH and confirm those exchanges continue. At a suitable time, reboot Ubuntu and confirm the service starts automatically and exchanges resume; the reboot interval should remain recorded as failures.
2. Open `http://127.0.0.1:8765/` in your browser. Confirm results update after five seconds. Choose **Last 15 minutes**, change **Target**, **Probe**, and **Graph metric**, then click a graph timeframe or select **Inspect timeframe**. Select a result timestamp to inspect its full record, and use **Older results** and **Newer results** when enough records exist. Try a **Custom interval**, **Apply interval**, **Clear timeframe**, **Follow latest**, and **Refresh now**.
3. Run `sudo systemctl stop network-diagnostics-remote.service` on Ubuntu while leaving collection running. Confirm failed remote exchanges appear. Run `sudo systemctl start network-diagnostics-remote.service` and confirm successful persistent TCP reconnection and resumed UDP replies. To check restart-on-failure during this test session, run `sudo systemctl kill --signal=SIGKILL --kill-who=main network-diagnostics-remote.service`; confirm it returns to `active` after about five seconds, and inspect the journal for the restart. This deliberately interrupts the companion, so mark that interval as a server test when interpreting the dataset.
4. Stop the collector with Ctrl+C, restart it against the same dataset, and confirm older records remain alongside the new session. Stop it again and use the standalone dashboard command above to inspect the retained history. A stopped or interrupted collection session must not be interpreted as network recovery.
