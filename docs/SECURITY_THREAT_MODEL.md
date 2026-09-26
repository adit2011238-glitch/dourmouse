# Dourmouse security: threat model

Scope: Dourmouse running on the owner's own Mac, for that Mac only (owner decision,
2026-09-24). Findings #084-#112 in `ENGINEERING_AUDIT.md` hold the evidence for each claim
here.

## What is being protected

1. The Mac itself: its files, accounts, and what runs on it.
2. The owner's data that Dourmouse can reach: mail, documents, browser history, keys in
   the Dourmouse config.
3. The owner's attention: an alarm must mean something, so every finding carries its
   evidence, and Dourmouse never claims to see more than it sees.

## Who it defends against

| Adversary | Example | What Dourmouse does |
|---|---|---|
| Someone on the same network | Café Wi-Fi, a compromised router, ARP spoofing | Watches gateway identity, ARP duplicates, DNS changes, weak Wi-Fi; scans at once when the network changes (#108) |
| Malware arriving by download | A trojan disguised as a PDF, an app in a zip | Assesses every file in Downloads by content, origin, signature and Gatekeeper (#101); quarantine is reversible (#106) |
| Malware already running | A process in /tmp or ~/Downloads talking to the network, a new LaunchAgent | Flags processes running from odd places, unsigned processes new to the network, and new or changed startup items (#099) |
| Someone watching the owner | MDM, a configuration profile, an extra root certificate, a proxy, remote-control software | The "am I being monitored?" check shows each indicator as present, absent or unknown (#102) |
| A prompt injection | A web page or email telling the chat model to act | Every action that changes the machine is approval-gated (#103, #106, #112); the analyst only explains, never acts (#107) |
| The owner's own distraction | A site or app they chose to keep closed | Lockdown (#103) |

## What it does not defend against

- **A root-level or kernel compromise.** Such malware can lie to every tool Dourmouse
  runs (ps, lsof, netstat, launchctl). Dourmouse runs as the user, so it cannot see past
  that.
- **Malicious file contents.** Contents are only checked for malware when a scanner
  (ClamAV) is installed, and none is installed. Every download verdict says so.
- **Anything off this Mac.** The router's firmware, other devices' intentions, and the
  cloud model provider's handling of what it is sent are outside its reach.
- **Encrypted traffic.** It is never intercepted. Browser history comes from the
  browsers' own databases (#111), not the network.
- **A process running as the owner.** It can read the owner's files and call the local
  server, exactly as the owner can. Approval gates guard the model, not the owner's own
  programs.
- **A determined bypass of lockdown.** A browser's own DNS-over-HTTPS, or an app that
  connects to a fixed IP address, gets past a hosts-file block. The lockdown status says
  so.

## Dourmouse's own attack surface

| Surface | Risk | Mitigation |
|---|---|---|
| The web server (port 8765) | Anyone who reaches it controls the Mac through Dourmouse. A web page in the owner's own browser reaches `127.0.0.1` too, and used to be indistinguishable from the app | Loopback-only by default; a non-loopback bind needs `DOURMOUSE_ACCESS_TOKEN`. The self-audit reports the bind (#112). A request from a loopback client must name the server itself in `Host` (stops DNS rebinding), and a state-changing one must carry no foreign `Origin` or cross-site `Sec-Fetch-Site` (stops cross-site forgery); the file-preview routes, which answer with wildcard CORS, need a per-launch token; the Electron pane bridge refuses browser-originated requests (#135). Reaching the app through a local proxy under another name means listing that name in `DOURMOUSE_ALLOWED_HOSTS`. Any process running as the owner can still call the server |
| Auto-approve toggle | Skips every approval gate | Off by default; the self-audit raises it as HIGH when on |
| The lockdown helper (root) | The only root code; a user-writable root script means root for anyone | Installed only under root-owned `/Library/PrivilegedHelperTools`, and the install refuses if any folder above it is writable by others. It writes only validated `0.0.0.0`/`::` lines inside its own marked block of /etc/hosts. It opens its request file without following links and judges the open file (a regular, small file owned by the owner and not writable by others), refuses names macOS depends on (Apple, iCloud, certificate and revocation hosts, localhost, `.local`), and treats a corrupt request as empty so the block can always be removed. The owner's account can still ask it to block any other website system-wide, which is what it is for. The self-audit compares the installed copy with the packaged one byte for byte and checks the launchd job (#103, #112, #136). An existing install must re-run the install command once to get the hardened helper |
| Keys in config | Readable by other local users | The settings writers use 0600, and the self-audit checks both `.env` files and `security.json` |
| `fetch_url` and the research fetcher | Server-side request forgery into the LAN or cloud metadata | `net_guard` refuses non-public addresses at every redirect hop and re-checks the connected address (#085) |
| Quarantine and reports | Other local users reading what was caught | Created 0700 (#112) |
| The AI analyst | Invented findings; security data sent to the cloud; attacker-chosen names (files, URLs, SSIDs) steering it | Every point must cite a real finding number, and others are dropped. Findings reach the model as fenced data with control characters stripped and lengths capped, and the alert severity comes from the deterministic finding, never from the model. Privacy mode keeps findings off the cloud, in the analyst and in the chat tools that return security evidence (#107, #112, #136) |
| Model-written code | A page or email steers the model into running code as the owner, with the server's keys in its environment | `run_python` and every compute job run in a kernel-enforced sandbox: reads only the system, the interpreter and their own scratch folder; writes only that folder; no network; an allowlist environment with no keys; CPU and file-size limits; the whole process group killed on a timeout. `run_python_host` and the Claude Code and Codex CLIs need the owner's approval of the exact code or task. The file tools refuse Dourmouse's own state and secret locations (#137) |
| Standing instructions | A schedule or an autonomous goal does something no one approved | `schedule_recurring` needs approval showing the tool, its arguments and the cadence; a scheduled run goes through the same policy, hook and ledger path as any tool call and its output is scrubbed of credentials; a parked goal task's approval covers the exact actions the owner was shown, once each. The Autonomous mode toggle remains the owner's own switch to skip approvals (#137) |
| Self-extension | A description or a later edit makes approved code do more than the reviewer saw | The module is generated from data with no string interpolation, the reviewer sees the exact source, and its hash is checked at every load. Approved tools still run inside the server process (#138) |
| The browser pane and its proxy | The proxy fetches any URL for the model and serves it from the app's own origin | The check and the proxy refuse non-public addresses at every hop, and proxied pages carry a CSP sandbox so they can never act as the app (#139). Electron denies every web permission except the app's own microphone, camera, clipboard write and fullscreen, and keeps its windows on the app's own origin |
| Secrets in text | A key reaches the cloud model in a tool result or a transcript | Shape patterns (provider keys, bearer tokens, assignments to secret-named variables) and exact matches of every secret in the owner's own `.env` (raw, URL-encoded, base64) are redacted from tool results, errors included. A key pasted in a paraphrase or split across lines is not recognised (#138) |
| Lockdown in the browser | A page path (not a whole site) stays reachable | An optional Manifest V3 extension blocks path-level entries from the local blocklist. It covers only browsers where it is installed (#141) |
| History databases | A damaged office log stops the app from starting | A damaged file is set aside under a dated name and a fresh one starts (#140) |

## Data that leaves the Mac

- **Chat turns:** everything the chat model is shown goes to the configured cloud
  provider, under the owner's cloud-only model policy.
- **The analyst:** it sends the current findings (titles, evidence, fixes) to Ollama
  Cloud, unless privacy mode is on.
- **The browser-history tool:** in chat, it shows domains and search terms, unless
  privacy mode is on. Then it shows counts only.
- **Security evidence in chat:** with privacy mode on, the chat tools that return
  security evidence (status, exposed services, sentry scans, downloads, reports,
  quarantine) return a plain "withheld" note instead (#136).
- **Nothing else.** Detection, the baseline, the report, lockdown, quarantine and the
  history databases all stay local.

## Assumptions

- The owner is the only person with an administrator account on this Mac.
- macOS's own protections (SIP, Gatekeeper, TCC) work as Apple documents. The report
  flags any that are off.
- The owner reads approval prompts before approving.
