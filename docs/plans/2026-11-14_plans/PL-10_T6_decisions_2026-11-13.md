# PL-10 T6 (UI): owner decisions and research notes, 2026-11-13

## Owner decisions (asked and answered in the T6 session, 2026-11-13)

1. Direction: **approved as written** in PL-10 section T6. Real macOS window chrome (titleBarStyle hiddenInset), sidebar vibrancy, system font at 13 px, the user's macOS accent colour, light and dark following the system, seven sidebar places (Chats, Agents, Research, Library, Browser, Security, Settings), an inspector on the right. Fake dock, menu bar, wallpaper and painted traffic lights are removed.
2. Base neutrals: **Apple's cool system neutrals**, read from macOS at run time (Electron systemPreferences), with fixed fallbacks for the plain-browser preview.
3. First review point: before and after screenshots of Chats, Agents (with the Agent Room), Research and Security, in light and dark. No earlier check-in was requested.

## Where T6 stands

- Worktree: `~/dourmouse-recon/.claude/worktrees/heuristic-curran-114caa`, branch `claude/heuristic-curran-114caa`, based on recon-2026-09-11 at cd777c1. No code changed yet. Nothing committed.
- Done: read HARD_RULES.md, PL-10, memory notes; owner decisions above; downloaded 21 Apple HIG pages as JSON (developer.apple.com/tutorials/data/design/human-interface-guidelines/<page>.json) and read sidebars, split views, toolbars and part of typography.
- Later the same day: research finished, and the design proposal (principles, tokens, seven places, exact change list, mockups in light and dark) is in `T6_design_proposal/PROPOSAL.md` with images in `T6_design_proposal/png/`. Owner rule relayed by the coordinating session: no code, stylesheet or screen changes until the owner has seen the design and said yes in the T6 session. Waiting for that yes.
- Not done: the token file in the repo, the component sheet in the Electron shell, the four screens, the lint check.

## Verified from Apple's HIG so far (primary source, fetched 2026-11-13)

- Sidebars: leading side; sidebar icons use the accent colour by default, and on macOS people expect them to follow the system accent colour they pick (change log June 8, 2026). No more than two levels of hierarchy. Let people hide the sidebar with Show Sidebar and Hide Sidebar in the View menu; do not hide it by default. Avoid critical information or actions at the bottom of a sidebar.
- Split views (inspector): thin divider of one point; keep the current selection highlighted in each pane; give more than one way to reveal a hidden pane (toolbar button and a menu command with a shortcut).
- Toolbars: leading edge holds the sidebar button then the view title; trailing edge holds inspector buttons, search and at most one prominent primary action. Aim for at most three groups. Prefer symbols without borders. Window titles under 15 characters and never the app name. On macOS every toolbar item must also exist as a menu bar command.
- Typography: SF Pro is the macOS system font; macOS default text size 13 pt, minimum 10 pt; avoid Ultralight, Thin and Light weights; macOS does not support Dynamic Type.

## Current code facts that shape the rebuild

- Electron 44.5.1 (castlabs build) is installed only in the main checkout (`~/dourmouse-recon/electron/node_modules`). Main window: `createMainWindow()` in electron/main.js (about line 3791), no titleBarStyle, title "DOURMOUSE // CENTRAL AGENT DISPATCH".
- The OS shell is `ui/shell.html` plus `ui/assets/os/` (boot.js, core, chrome, kit, 20 screens in core/registry.js). Fake chrome lives in chrome/menubar.js, dock.js, wallpaper.js, control-centre.js, notif-centre.js and the `os-lights` span in shell.html.
- 17 test files pin shell markup or stylesheets (for example test_design_tokens.py, test_os_screen_contract.py, test_os_shell_route.py, test_os_ux_fixes.py, test_os_core.py). They need deliberate updates, not deletions.
