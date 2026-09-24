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
- **A determined bypass of lockdown.** A browser's own DNS-over-HTTPS, or an app that
  connects to a fixed IP address, gets past a hosts-file block. The lockdown status says
  so.

## Dourmouse's own attack surface

| Surface | Risk | Mitigation |
|---|---|---|
| The web server (port 8765) | Anyone who reaches it controls the Mac through Dourmouse | Loopback-only by default; a non-loopback bind needs `DOURMOUSE_ACCESS_TOKEN`. The self-audit reports the bind (#112) |
| Auto-approve toggle | Skips every approval gate | Off by default; the self-audit raises it as HIGH when on |
| The lockdown helper (root) | The only root code; a user-writable root script means root for anyone | Installed only under root-owned `/Library/PrivilegedHelperTools`, and the install refuses if any folder above it is writable by others. It writes only validated `0.0.0.0`/`::` lines inside its own marked block of /etc/hosts, and reads its request file only if it is a regular, small file (no symlinks). The self-audit compares the installed copy with the packaged one byte for byte and checks the launchd job (#103, #112) |
| Keys in config | Readable by other local users | The settings writers use 0600, and the self-audit checks both `.env` files and `security.json` |
| `fetch_url` and the research fetcher | Server-side request forgery into the LAN or cloud metadata | `net_guard` refuses non-public addresses at every redirect hop and re-checks the connected address (#085) |
| Quarantine and reports | Other local users reading what was caught | Created 0700 (#112) |
| The AI analyst | Invented findings; security data sent to the cloud | Every point must cite a real finding number, and others are dropped. Privacy mode keeps findings off the cloud (#107, #112) |

## Data that leaves the Mac

- **Chat turns:** everything the chat model is shown goes to the configured cloud
  provider, under the owner's cloud-only model policy.
- **The analyst:** it sends the current findings (titles, evidence, fixes) to Ollama
  Cloud, unless privacy mode is on.
- **The browser-history tool:** in chat, it shows domains and search terms, unless
  privacy mode is on. Then it shows counts only.
- **Nothing else.** Detection, the baseline, the report, lockdown, quarantine and the
  history databases all stay local.

## Assumptions

- The owner is the only person with an administrator account on this Mac.
- macOS's own protections (SIP, Gatekeeper, TCC) work as Apple documents. The report
  flags any that are off.
- The owner reads approval prompts before approving.
