# DOURMOUSE on other devices — Tailscale (free, private, local)

DOURMOUSE runs on your Mac and binds to `127.0.0.1` by default. To reach it from
your **phone / laptop / any device you own**, use **Tailscale**: a free,
zero-config private mesh (WireGuard). Your data never touches the public
internet, nothing is exposed publicly, and there is no monthly cost.

> **Do NOT use a Cloudflare/public tunnel for this.** It exposes the dashboard
> to the open internet and requires much stricter auth. Tailscale keeps
> everything on your own private network.

---

## 1. Install Tailscale

**Mac (the DOURMOUSE host):**
```bash
brew install --cask tailscale
# open the app, log in with the account you'll also use on your phone
open -a Tailscale
```

**Phone (iOS/Android):** install "Tailscale" from the App Store / Play Store,
log in with the SAME account.

Every device that signs into the same account is on one private network
(tailnet). Each device gets a stable private IP (usually `100.x.y.z`) and a
magic name like `mymac`.

## 2. Configure DOURMOUSE for remote access

Edit the DOURMOUSE `.env` (in the app folder) and add:

```env
# REQUIRED: a shared secret only you know. Use a long random string, e.g.
#   openssl rand -hex 24
DOURMOUSE_ACCESS_TOKEN=CHANGE-ME-to-a-long-random-string

# Bind to all interfaces so Tailscale can reach it (the tunnel is encrypted,
# and the token gates every request).
DOURMOUSE_HOST=0.0.0.0
```

Restart DOURMOUSE (`./stop.command` then `./start.command`). The launcher prints
a confirmation line:

```
DOURMOUSE: binding to 0.0.0.0 with DOURMOUSE_ACCESS_TOKEN set — remote clients must present the token
```

> If you set `DOURMOUSE_HOST=0.0.0.0` WITHOUT a token, DOURMOUSE prints a loud
> warning and serves anyway (backward compatible), but **do not do that on a
> network you don't fully control** — anyone who can reach the port could
> drive the dashboard.

## 3. Find your Mac's tailnet address

```bash
/Applications/Tailscale.app/Contents/MacOS/Tailscale status
# look for your Mac's row → something like 100.101.102.103  mymac
```

## 4. Connect from your phone

1. Open a browser on the phone (Safari/Chrome).
2. Go to `http://100.101.102.103:8765/` (your Mac's tailnet IP, port 8765 —
   or your `DOURMOUSE_UI_PORT`).
3. You'll see the **ACCESS GATE** page. Enter `DOURMOUSE_ACCESS_TOKEN` once; the
   session cookie is stored, so you stay logged in.
4. You now have the full single-page dashboard: agent roster, dispatch,
   live comms, the morning report.

**Pro tip:** "Add to Home Screen" on iOS/Android makes it a full-screen app
icon — a native-feeling Dourmouse on your phone.

## 5. Keeping it safe — checklist

- [ ] `DOURMOUSE_ACCESS_TOKEN` is a long random string (not "password").
- [ ] `DOURMOUSE_HOST=0.0.0.0` is set only together with the token.
- [ ] Only your devices are on the tailnet (Tailscale admin console).
- [ ] The `.env` with the token is never committed (it's gitignored).
- [ ] The desktop app on the Mac itself keeps working with **zero changes** —
      loopback is exempt from the token by design.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Phone can't reach the IP | Both devices must be logged into the SAME Tailscale account; `Tailscale status` on the Mac shows your tailnet IP |
| ACCESS GATE loops after login | Token mismatch — re-enter the exact `DOURMOUSE_ACCESS_TOKEN` value |
| Works on Wi-Fi, not on cellular | Normal — cellular egress still reaches your tailnet via Tailscale's DERP relay; ensure the phone's Tailscale VPN is ON |
| Port already in use | `export DOURMOUSE_UI_PORT=9000` before starting |
