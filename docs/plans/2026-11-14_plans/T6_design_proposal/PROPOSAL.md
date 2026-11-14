# T6 design proposal: Dourmouse as a real Mac app (2026-11-13)

Status: **proposal, waiting for the owner's yes.** No app code, stylesheet or screen has changed. The mockups are a static HTML page (`mockup.html` in this folder) rendered by headless Chrome into `png/`. They show example content, not live data.

Owner decisions already made: native-Mac direction approved; Apple's cool system neutrals as the base; first full review after Chats, Agents, Research and Security are rebuilt (see `../PL-10_T6_decisions_2026-11-13.md`).

Missing input: the owner's "AI slop" guidance is not yet in `~/Documents/Claude Projects/` (checked 2026-11-13; only `pancakes/` is there). This proposal will be checked against it when it lands.

## 1. What the sources say (checked 2026-11-13)

Apple Human Interface Guidelines (fetched as JSON from developer.apple.com):
- Sidebars: leading side; icons follow the accent colour the user picks in System Settings; at most two levels; Show Sidebar and Hide Sidebar in the View menu; nothing critical at the bottom of a sidebar.
- Split views: one-point dividers; keep the selection highlighted in every pane; give a hidden pane both a toolbar button and a menu command with a shortcut.
- Toolbars: sidebar button and title on the leading edge; inspector button, search and at most one prominent action on the trailing edge; at most three groups; window titles under 15 characters and never the app name; every toolbar item also exists as a menu command.
- Typography: SF Pro; macOS text styles are Large Title 26, Title 1 22, Title 2 17, Title 3 15, Headline 13 bold, Body 13, Callout 12, Subheadline 11, Footnote and Captions 10. Default 13 pt, minimum 10 pt. No light weights.
- Colour: use the dynamic system colours (label, secondary label, separator, window background and so on) instead of hard-coded values; every colour needs light, dark and increased-contrast variants; never rely on colour alone.
- Accessibility: text up to 17 pt needs 4.5:1 contrast; 18 pt or bold text needs 3:1.
- Sheets: a card attached below the toolbar; the parent window dims.
- Writing: pick one capitalisation style per element type and keep it. Apple's own component pages use title case for buttons, menu titles, segment labels and column headings.
- Shortcuts: Command N new, Command F find, Option Command F search field, Option Command I inspector, Control Command F full screen.

Electron 44.5.1 (the version in `~/dourmouse-recon/electron`), from its type definitions:
- `titleBarStyle: 'hiddenInset'`, `trafficLightPosition`, `vibrancy: 'sidebar'`, `visualEffectState`.
- `systemPreferences.getAccentColor()`, `getColor(...)` for every dynamic macOS colour above, `getSystemColor(...)` for red, orange, green and the others, `nativeTheme` for dark mode, increased contrast and reduced transparency.
- `nativeImage.createFromNamedImage` accepts SF Symbol names, so the sidebar can use real SF Symbols.
- Liquid Glass (macOS 26's floating sidebar) is not exposed to Electron; the sidebar uses the classic system sidebar material.

OpenAI (Apps SDK design guidelines, developers.openai.com): inherit the system font stack and never use custom fonts; system colours for text, icons and dividers; brand colour only on logos, icons, badges and primary buttons; no coloured backgrounds behind text; no custom gradients or patterns; system spacing and corner radii; monochrome outlined icons; WCAG AA contrast. The openai.com/brand page refused the fetch (HTTP 403) and was not read.

Anthropic: the official brand skill lists Anthropic's own colours (#141413, #faf9f5, accent #d97757) and fonts (Poppins, Lora); these are Anthropic's identity and are not used here. Anthropic's published frontend-design skill lists what generated design looks like, and Dourmouse today matches most of it: a near-black background with one bright accent, a tracked-out uppercase label above every heading, "WORD // fragment" labels, a tinted near-black instead of black, a monospace face for small data labels, and identical cards with gradient washes. It also names the warm cream background with a terracotta accent as a tell, which supports choosing Apple's neutrals.

## 2. Principles

1. **The Mac draws the window.** Real traffic lights, real menu bar, real sidebar material. Dourmouse never paints operating-system chrome.
2. **Content first, chrome quiet.** Three panes: sidebar, content, inspector. One-point dividers. No cards around everything.
3. **System colour only.** Neutrals and the accent come from macOS. Three status colours (good, warning, critical) always come with a word.
4. **System type at macOS sizes.** The system font; the mono face only for code and command output. Numbers use tabular figures, not a mono face.
5. **Words do one job.** Sentence case inside the window. No subtitle under a title, no taglines, no uppercase labels, no arrows added to buttons, no em dashes, no "A, B, C" strings joined by middle dots.
6. **True states only.** A count, badge or status shows a real, current value or nothing. An empty view says in one line what will appear there.
7. **Every action has a menu command;** the frequent ones have shortcuts.
8. **Approvals are sheets; receipts and details live in the inspector.**
9. **No decoration.** No gradients, glows, neon, painted glass or shadows, except the one shadow under sheets, menus and popovers.
10. **Motion only to show what changed,** and none when Reduce Motion is on.

## 3. Tokens (the token file to be written as `ui/assets/os/tokens.css`)

In the Electron shell every neutral and the accent are read from macOS at start and whenever the appearance changes. The values below are fallbacks for a plain browser.

| Token | macOS source | Light fallback | Dark fallback |
|---|---|---|---|
| bg-content | control-background | #ffffff | #1e1e1e |
| bg-sidebar | sidebar material (transparent over vibrancy) | #e9e9eb | #2b2b2d |
| bg-inspector | window-background, lightened | #f5f5f7 | #262628 |
| text | label | black 85% | white 85% |
| text-2 | secondary-label, darkened to 4.5:1 | black 60% | white 60% |
| text-3 (placeholders) | placeholder-text, darkened to 4.5:1 | black 55% | white 55% |
| separator | separator | black 10% | white 10% |
| fill, fill-2 | quaternary fills | black 5%, 9% | white 6%, 11% |
| accent | getAccentColor() | #007aff | #0a84ff |
| accent-fill | accent darkened until white text is 4.5:1 | #0066d6 | #0066d6 |
| link | link | #0068da | #409cff |
| good, warning, critical | getSystemColor green, orange, red | #28cd41, #ff9500, #ff3b30 | #32d74b, #ff9f0a, #ff453a |
| critical-text (destructive buttons) | red, darkened to 4.5:1 | #d70015 | #ff453a |

Deliberate departure from Apple's defaults: Apple's secondary label (about 4:1 on white) and white text on its blue accent (about 4:1) fall short of the 4.5:1 the plan requires, so text-2, placeholders, accent fills and red text are darkened at run time until they pass.

Type scale: 22 (window-level titles, rare), 17 (page title), 15 (toolbar title and section titles), 13 (body, rows, controls; 20 px line height for chat and long text), 12 (table headers, small buttons), 11 (sidebar section headers, captions). Weights 400, 500, 600 only.

Spacing on 4 and 8: 4, 8, 12, 16, 20, 24, 32. Sizes: toolbar 52, sidebar 220 (resizable), inspector 280 (resizable, hideable), sidebar and table rows 28 to 30, controls 28 (small 24). Radii follow size: 5 to 6 for controls and rows, 8 for content blocks, 12 for sheets and popovers.

## 4. Seven places instead of twenty screens

| Place | Holds today's screens | Shortcut |
|---|---|---|
| Chats | Home, Code, Voice (as a composer mode), Projects (as chat folders) | Command 1, Command N new chat |
| Agents | Office (Team), Agent Room (new, from T3), Command Center and Orchestration (Activity), Goals and Timetable (Jobs), Agent Smith, Apps | Command 2 |
| Research | Research; News and Atlas as a News view | Command 3 |
| Library | Wiki, Media, Comms (mail), the T4 knowledge base | Command 4 |
| Browser | Browser | Command 5 |
| Security | Security, plus the camera and microphone kill switches | Command 6 |
| Settings | Settings, the T4 connections page | Command 7 and Command comma |

Retired chrome: fake menu bar, dock, wallpaper and its picker, control centre, notification centre (macOS notifications plus the inspector replace it), painted traffic lights, the SPEC overlay button (moves to a Developer menu item), the shortcuts panel (the real menus list the shortcuts). The launcher stays as Command K search.

Only the four first-milestone places are rebuilt now. The other screens keep working inside their place and pick up the tokens; they are rebuilt in milestone 2.

## 5. Exactly what changes in code for milestone 1 (after the owner's yes)

Electron:
- `electron/main.js` `createMainWindow()` (about line 3791): add `titleBarStyle: 'hiddenInset'`, `trafficLightPosition`, `vibrancy: 'sidebar'`, `visualEffectState: 'followWindow'`; the window title comes from the page (the place name) instead of "DOURMOUSE // CENTRAL AGENT DISPATCH".
- New application menu (there is none today; Electron's default menu shows): Dourmouse, File, Edit, View, Window, Help, with the shortcuts above, Show and Hide Sidebar (Control Command S) and Show and Hide Inspector (Option Command I).
- New appearance bridge: reads the accent and system colours, sends them to the page, and resends on every appearance or accent change. `electron/preload.js` exposes it read-only. Sidebar icons as SF Symbols through `nativeImage.createFromNamedImage`.

Page:
- New `ui/assets/os/tokens.css` and `ui/assets/os/core/appearance.js` (applies the colours, runs the contrast guard).
- `ui/shell.html`: new skeleton (sidebar, toolbar, content, inspector, sheet layer); wallpaper, menu bar, panels, dock, spec bar and `#lights` removed.
- `ui/assets/os/chrome/`: new sidebar, toolbar, inspector and sheet modules; menubar, dock, wallpaper, control-centre and notif-centre retired after their real functions have moved.
- `ui/assets/os/core/registry.js`: seven places, each with views.
- Rebuilt screens: Chats (from Home and the thread kit), Agents (Team, Room, Activity, Jobs), Research, Security.
- `dourmouse-ui.css`, `dourmouse-os.css`, `os/shell.css`, `os/kit/kit.css`, `os/palette.css`: retoned to the tokens; Space Grotesk and the mono faces stop being used (font files removed in milestone 2).
- Not touched in milestone 1: the classic `/console` and `/workspace` pages, and the screens listed under "keep working" above.

Honest dependencies: the Agent Room needs T3's room API and the capability tests; research stages need T1's job; the security areas need T2's checks. Until each exists, its view shows what the backend really returns today, or one line saying it is not built yet. Never sample data.

Tests: 17 files pin the current markup or styles (`test_design_tokens.py`, `test_os_screen_contract.py`, `test_os_shell_route.py`, `test_os_ux_fixes.py`, `test_os_core.py`, `test_ui_focus_visible.py`, `test_ux_round_i1.py`, `test_webui.py` and nine more). Each is updated to pin the new structure, not deleted.

Lint: `scripts/ui_style_lint.py` plus a test. It fails on any gradient, `text-shadow`, coloured or blurred glow, `text-transform: uppercase`, letter-spaced labels, a `font-family` outside the tokens, and colour literals outside the token file in new stylesheets.

Records per change: numbered finding in `docs/ENGINEERING_AUDIT.md` and `docs/GODSPEED_ROADMAP.md`, ledger row, screenshots in `EVIDENCE/` from an isolated instance in the real Electron shell, full suite green with counts in the commit.

## 6. Small decisions for the owner (defaults in bold)

1. Capitalisation: **title case only in the macOS menu bar (as every Mac app), sentence case everywhere inside the window**; or sentence case everywhere.
2. Approvals: **a quiet "waiting for you" line in the chat and a queue in the inspector; the sheet opens when you press Review**; or the sheet opens by itself the moment an agent asks.
3. Chat text: **13 px, Apple's body size**; or 15 px for long reading.
4. Mail (today's Comms screen): **inside Library**; or inside Agents with the Mail and Calendar agent.

## 7. Mockup files

`png/<view>_<light|dark>.png` for chats, approval, agents (Team), room, research, security and components. The window frame and traffic lights in the mockups are drawn to show placement; in the app macOS draws them. Icons are stand-ins for SF Symbols.
