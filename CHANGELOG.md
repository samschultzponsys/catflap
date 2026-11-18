# Changelog

Versions are `MAJOR.MINOR`; the minor number runs 0–9 and then rolls over
(1.9 → 2.0). Every release is tagged `vMAJOR.MINOR`.

## 2.2

- **Connecting no longer fails with "the router didn't answer in time".** That message was
  wrong: nothing was waiting on a router. CatFlap talks to NetworkManager over one D-Bus
  connection, and when the tray's status check ran while a site was connecting (or two sites
  at once), one could take the other's reply, so the connect waited 30 seconds and gave up.
  Restarting CatFlap fixed it until the next time. Calls are now taken one at a time, a stuck
  connection is reopened instead of breaking later calls, and if NetworkManager really doesn't
  reply, CatFlap says it's a problem on this computer and what to do: try again, restart
  CatFlap, or restart NetworkManager.
- **Only one CatFlap at a time.** Starting CatFlap while it's already running (from the app
  menu, at login, or an update restart that raced the old copy) gave a second tray icon that
  then had to be quit separately. Now the running CatFlap just opens its window, and an
  update restart waits for the old copy to finish quitting.

## 2.1

- **Next can't be clicked twice.** While a wizard step is working (creating the site,
  signing in, looking up consoles), its buttons are greyed out and clicks and keys are
  ignored. A quick second click on Next used to start the same step again: it created the
  site, then showed an error from the second attempt.
- **Routers behind another router (double NAT).** For example a UDM Pro plugged in behind a
  UCG Fiber. When the site's address is a private one (10.x, 192.168.x, 172.16–31.x),
  CatFlap now says so and what to do, everywhere it matters: the wizard asks for the front
  router's public IP or DDNS name (when UniFi reports a private WAN address, and when a
  WireGuard file points at one), the pre-connect check warns, and a failed connection
  says "forward UDP port 51821 to 10.0.1.30" with the site's real port and address
  instead of a generic list of causes (which also always said 51820).
- **UniFi server instructions match today's UniFi.** The "create a VPN server" steps follow
  the current *New VPN Server* window: keep the detected Server Address and port, what the
  *behind NAT* warning means, *Alternate Address for Clients*, and the VPN range under
  **Advanced → Manual**. CatFlap suggests a VPN range (10.250.N.1/24) that none of your
  other sites, their networks or this site's networks use, and lists the ones in use. It
  also warns when a server shares its VPN range (often UniFi's automatic 192.168.3.x) with
  another site.

## 2.0

- **Update checks you can't miss.** Every time CatFlap starts it asks GitHub for the newest
  release (it used to reuse the previous day's answer, so a new release could go unseen
  for up to a day), and it checks again every 6 hours while it runs. The first time
  it sees a new version, it opens the update window; after that, a notification and the
  tray menu's **Update to X…** item remind you. Same on Windows and Linux.

## 1.9

- **Flatpak: CatFlap asks for AdGuard permission as soon as it sees AdGuard.** The sandbox
  can't see `adguard-cli` until it's allowed, so CatFlap now spots AdGuard from inside
  the sandbox (its proxy answers on this computer, or it re-signs HTTPS) and immediately
  shows the one command to run, with **Copy** and **I've run it: Restart CatFlap**. It
  asks again at every start until it's allowed (or you tick *Don't ask again*), the tray
  icon shows a red **!** shield, and the tray menu has **⚠ Let CatFlap control AdGuard…**.
- **CatFlap restarts itself after an update.** It checks every minute whether a newer
  CatFlap was installed (from the update window, a terminal, Discover or dnf) and
  restarts into it. Restarting always ends the old copy, and the new one waits a moment
  so its tray icon appears reliably (inside the Flatpak both copies otherwise claim the
  same tray icon).
- **Update AdGuard** works while AdGuard is running: AdGuard refuses to update while its
  proxy runs, so CatFlap stops it, updates, and starts it again in the same terminal.
- **HTTPS check:** recognizes AdGuard's default of filtering HTTPS only in browsers, and
  says so instead of "CatFlap's own test connection wasn't filtered". Non-browser apps
  with their own certificate store (Steam, Discord, Slack, Spotify, Thunderbird) are
  shown as information, not problems, since AdGuard doesn't filter them by default.
  "Add to this browser" on items that are already fine is now **Add anyway**.
- The tray menu and Settings say **AdGuard Ad Blocking**.

## 1.8

- **AdGuard for Linux in the tray.** If AdGuard's command-line app (`adguard-cli`) is
  installed, the tray menu gets an AdGuard section: an **AdGuard ad blocking** checkbox
  (on/off), **Pause AdGuard** for 5 minutes, 30 minutes, 1 hour or until you resume
  (CatFlap turns it back on when the time is up), **Resume AdGuard now**, and an
  **AdGuard** submenu (filter lists, updates, browser extension, setup).
- **AdGuard status on the tray icon:** a small shield in the top-left corner: green with
  a tick while protecting, amber with a pause mark while paused, white with a slash when
  off. It never covers the VPN dot (bottom-right). **Settings → AdGuard ad blocker → Show
  AdGuard's status on the tray icon** turns it off. The tooltip says the same in words.
- **Check HTTPS filtering…** (tray → AdGuard, or Settings → AdGuard ad blocker): finds
  everything that has to trust AdGuard's certificate for HTTPS filtering to work: the
  system certificates, each Firefox-family profile (including Flatpak browsers) and the
  Chrome-family store. Anything that isn't right gets a **Fix** button: trust the
  certificate system-wide (asks for your password), add it to a browser (needs
  `certutil`), or exclude an app from HTTPS filtering.
- **Exclude an app from HTTPS filtering…:** adds the app to AdGuard's `proxy.yaml`
  (`bypass_https`, or `bypass` to skip filtering it entirely), keeps a backup and
  restarts AdGuard. For apps that pin their certificates (banking, some games and chat
  apps, developer tools).
- **Gateways and AdGuard's HTTPS filtering:** when AdGuard (Linux or Windows) re-signs
  the connection to your gateway, CatFlap now says exactly that and how to exclude it,
  instead of *gateway TLS certificate changed*. Adding a gateway never pins AdGuard's
  stand-in certificate by mistake.
- **AdGuard updates:** when `adguard-cli` says a newer version is available, the tray menu
  shows **Update AdGuard to X…** (runs `adguard-cli update` in a terminal) and CatFlap
  notifies you once per version. The AdGuard submenu also has *License information*,
  *Set up the HTTPS filtering certificate…* (`cert`) and *Start AdGuard when the computer
  starts…* (`install-service`). Status is read from AdGuard CLI 1.5's actual output.
- **New command:** `catflap adguard [status|on|off|pause [MIN]|resume|https-check|exclude APP]`.
- **Other software list:** AdGuard Ad Blocker (Windows) and AdGuard for Linux are listed
  as *works alongside CatFlap*, with no uninstall box and no popup. AdGuard VPN is listed
  as a full-tunnel VPN like the others.
- Flatpak: controlling AdGuard needs one extra permission, which the Flatpak doesn't
  have by default; CatFlap shows the one-line command (Settings → AdGuard ad blocker).
- RPM: recommends `nss-tools` (for adding AdGuard's certificate to browsers).

## 1.7

- Windows: the WireGuard app is always listed under other VPN software, even before
  CatFlap's own engine is set up (for example after updating from 1.4 inside the app,
  which skips the installer's engine download). Ticking **Uninstall** then downloads
  CatFlap's engine from wireguard.com first, so your sites keep working.

## 1.6

- **Other VPN software, one list with checkboxes.** CatFlap scans the first time it starts
  and after every update, and lists everything it finds (WiFiman, the WireGuard app,
  Private Internet Access, Tailscale, NordVPN, work VPNs…) in one window. Each item has
  **Uninstall** (Windows: runs its own uninstaller), **Don't warn again**, and for the
  WireGuard app **Hide tray icon**.
- "Don't warn again" now remembers the version: when that program is updated to a newer
  version, CatFlap mentions it again. Choices from 1.5 are kept.
- **Settings → Check for other VPN software…** scans on demand and always lists
  everything found, including programs you silenced.

## 1.5

- **Windows: no second WireGuard app.** CatFlap now uses only WireGuard's engine
  (`wireguard.exe`/`wg.exe` in its documented tunnel-service mode). The official signed
  MSI is downloaded from wireguard.com and the engine is extracted from it, not
  installed, so the WireGuard window and tray icon never appear. The first site setup
  puts the engine in `Program Files\CatFlap\WireGuard` in the same permission prompt,
  after checking WireGuard LLC's signature.
- Windows installer: a **Select Components** page shows the WireGuard engine download
  (about 3 MB) and lets you untick it; the Ready page lists exactly what will be
  downloaded. Opted out, CatFlap uses WireGuard for Windows if you install it, or offers
  to download the engine when you add a site.
- Windows: if WireGuard for Windows (the app) is installed, CatFlap warns about running
  both and offers to uninstall it, hide just its tray icon, or keep it. Sites set up with it switch to CatFlap's engine on the
  next connect; any site whose tunnel or program has gone is repaired on connect.
- **Warnings about other VPN software** (Windows and Linux): at start CatFlap lists any
  VPN apps that can interfere (Tailscale, ZeroTier, NordVPN, Proton VPN, Mullvad,
  Cloudflare WARP, Cisco/GlobalProtect/FortiClient and more), what each can do, and how
  to use it alongside CatFlap. "Don't warn about these again" silences them.
- Windows: removing the last site removes the engine; uninstalling CatFlap removes its
  tunnels and engine (`catflap-cli remove-tunnels`).
- Windows fixes: the permission-prompt step no longer gives up after 30 seconds, and
  paths with spaces (like `C:\Program Files`) are passed to it correctly.
  If that step fails, the message now says why, and it works when PowerShell 7 is
  also installed.
- Releases are now checked on a real Windows machine (GitHub's Windows runner): download,
  set up, connect, disconnect and remove a test tunnel.

## 1.4

- Windows: the installer downloads and runs the official WireGuard for Windows
  installer when it's missing, and CatFlap offers the same ("Install WireGuard?")
  if it's ever missing later. Everything else was already bundled.
- Windows: if WiFiman Desktop (Teleport) is installed, CatFlap offers to uninstall it
  at start (Uninstall / Keep it / Don't ask again), using WiFiman's own uninstaller.

## 1.3

- **Windows (preview):** a `catflap-X.Y-setup.exe` installer on every release (per-user
  install, Start menu entry, optional start at sign-in). Uses WireGuard for Windows;
  each site needs one administrator prompt when it's set up, then connecting and
  disconnecting need no prompts. Updates install silently and reopen CatFlap.
- **Import merges** with what's already on the computer: new items are added,
  identical ones left alone, and for conflicts you choose keep mine / replace / keep
  both (`--on-conflict` on the command line).

## 1.2

- Tray icon with several sites connected: the tile blends their colors (default), or
  cycles through them, or stays blue: Settings → Tray icon when connected. With
  one site it uses that site's color.
- Several sites at once, handled automatically: two sites using the same networks
  offer to **switch** (`catflap up --switch`); sites that only share a VPN server
  address both connect, with that address left to the first tunnel.
- Fixed: the red failure dot also showed for informational notes such as "You're
  here".
- Fixed: a site reachable only through another connected CatFlap tunnel was
  mistaken for "you're on site".

## 1.1

- Tray icon: turns into the solid blue app icon with a green dot when connected (the
  dot shows the count when several sites are up); red dot after a failure.
- Tray menu: short statuses ("Connected (workaround)", "You're here"…); the window
  keeps the full text.
- Icon name now changes with every release, so the app menu can't end up with a
  missing (?) or old icon after an update.

## 1.0

- The app is called **CatFlap** everywhere you see it: menu, taskbar, window titles,
  notifications and the setup guides. The command stays `catflap`.
- README header: icon centered above the title, so the heading line no longer runs
  through it.

## 0.9

- 24 site colors in two rows plus **Custom…** for any other color, in the Add a site
  guide and the Advanced view's Color… button.
- American spelling everywhere ("color").
- Shorter UniFi server steps, with what matters on the client screen: click
  **download** before **Save** (only needed if catflap asks for the file), and
  don't regenerate the public key afterwards.
- Smarter about address clashes: when the network you're on uses the same
  addresses as the site, catflap connects anyway, routing everything at the site
  through the VPN except your own address and your local router, and tells you.
- Better "you're at this site already" detection (public IP vs the site's
  endpoint or WAN IP, or your router being the site's router), shown as a note
  instead of an error.
- Full messages on the site cards instead of cut-off text.

## 0.8

- Icon: the launcher's icon name now changes whenever the artwork changes (Flatpak
  and RPM), so KDE can't keep showing an old cached icon in the taskbar and app
  menu, even across reinstalls.
- UniFi: exact steps for creating the WireGuard server, including the client UniFi
  requires at the same time (make it this computer and download its file), and
  what to put in each field (port, a unique VPN subnet per site, DNS).

## 0.7

- UniFi through the cloud: when the cloud connector refuses UniFi's internal API
  (it does for keys made with the new "Create New API Key" screen), the guide
  explains why and continues with a downloaded WireGuard file. It still checks
  the WireGuard server exists and lists your networks from the API.
- The guide lists the minimum key scope (Site Manager + UniFi Applications →
  Network, the sites you need) and recommends an expiry; an expired key gets a
  clear message.

- Updater: once the new version is installed, catflap restarts by itself after a
  short countdown, which you can cancel. In the Flatpak it notices when you've run
  the install command and then restarts into the new version.
- README: how to clear KDE's icon cache when the taskbar keeps an old icon.
- UniFi cloud keys: the guide, error messages and README now explain that a
  unifi.ui.com key only covers the site group (Fabric) chosen in the top-left
  dropdown when it was created; other groups need their own key.

## 0.6

- New app icon (Z1a): a white cat sitting in the blue tunnel, reaching up.
- Tray icon redrawn from the same cat outline: grey outline when disconnected, the
  site's color when connected (split for several sites), red dot after a failure.

## 0.5

- **UniFi through the cloud:** the guide now uses an account API key from
  unifi.ui.com and lets you pick your console. Traffic goes through Ubiquiti's cloud
  connector, so no local address is needed. Direct access is still there for when
  you're on site. CLI: `gateway add --cloud --console`, `gateway consoles`.
- **Exact steps per brand** in the Add a site guide (UniFi, Omada, Alta Labs).
- **Omada / other routers without a config file:** a new typed-in route where
  catflap creates this computer's keys and you add it as a WireGuard peer.
- No more "router address" question for file imports: pick the networks to reach
  (pre-ticked from the file), with brand-specific help on where to find them.
- The cat smiles now.
- The title bar shows the current icon: windows load it from the icon shipped
  inside catflap instead of the desktop's icon cache.

## 0.4

- New app icon (T1): the first icon's cat sitting in a blue tunnel.
- Tray icon redrawn to match: grey outline when disconnected, cat and tunnel in
  the site's color when connected, red dot after a failure.
- Windows, wizards and the update window use the installed app icon, so changing
  `catflap/resources/catflap.svg` changes it everywhere.

## 0.3

- New update window: what's new, the exact commands to update by hand (with
  Copy commands and Open download page), and an Update / Download button.
- catflap restarts itself into the new version after an update (Flatpak uses the
  portal's latest-version launch), then confirms the version it's running, or
  says if the update wasn't installed yet.
- Clicking the "update available" notification opens the update window.
- Four more icon options combining A's design with C's blue palette (O–R).

## 0.2

- First-start setup guide: choose a **Basic** or **Advanced** look (with a live example
  of each), then add the first site.
- **Basic view:** one big card per site with a Connect button; a clear "Add your
  first site" screen when empty.
- **Advanced view:** sites and gateways tables, a details pane, checks, edit, color,
  re-apply, remove/revoke, and API-key management.
- **Add a site** guide: UniFi automatic setup (API key help, connection test, site
  choice, VPN server check with fix-it steps, network picker that flags clashes),
  or a downloaded WireGuard file for Omada, Alta Labs and others, with per-vendor
  download instructions. **Add a gateway** guide for UniFi.
- Sites have friendly names ("Mum's house"); the tray menu has "Add a site…".
- Plain-language error messages.
- Fixed: settings could be briefly lost when the window saved while the
  background refresh reloaded them.
- Six icon options in `docs/icon-options/`.

## 0.1

- First milestone: vendor driver interface; UniFi driver (official integration API for
  sites/networks, classic API for the WireGuard server, peers, WAN IP and DDNS).
- Omada, Alta Labs and generic drivers: safe import of a downloaded `.conf`.
- NetworkManager backend (Linux) with enforced safe defaults and read-back verification.
- Windows backend (WireGuard for Windows tunnel service): under development.
- Pre-flight checks (overlaps, on-site/linked site, CGNAT, DDNS) and post-connect health check.
- Tray app: per-connection colors, minimize to tray, close-to-tray prompt, notifications,
  autostart.
- Multiple gateways/instances, each with its own credentials; credentials stored only in
  the local keyring.
- Encrypted export/import to move a setup to another PC.
- Update checks with install-method-aware update instructions and automatic update.
- Flatpak (KDE 6.11 + PySide base app, 12 MB bundle) with a NetworkManager D-Bus
  backend that needs no nmcli/ip/ping inside the sandbox.
