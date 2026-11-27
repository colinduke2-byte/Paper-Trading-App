# End-to-end tests

Playwright tests for the real UI against a real server. `npm test` builds the UI, starts two servers (one
open, one with a passcode for the sign-in tests) on random private ports with temporary data directories,
and runs the suite. Every test fails on a console error or page error.

```bash
npm install
npm test                                   # Chromium projects only: fast, the local default
E2E_ENGINES=webkit npm test                # WebKit projects (needs: npx playwright install --with-deps webkit)
E2E_ENGINES=chromium,firefox,webkit npm test
npx playwright test --project=iphone-14-webkit tests/mobile.spec.ts   # one project, one file (no UI build)
```

## Projects

| Engine | Project | Device | Specs |
|---|---|---|---|
| Chromium | `desktop` | 1440 x 900 | app, keyboard, a11y, visual |
| | `iphone-13`, `pixel-7` | phone emulation | mobile, a11y, visual |
| | `pixel-7-landscape` | Android phone, landscape | mobile |
| | `auth-desktop`, `auth-pixel-7` | | auth (passcode server) |
| Firefox | `desktop-firefox` | 1440 x 900 | app, keyboard, a11y, visual |
| | `auth-desktop-firefox` | | auth |
| WebKit | `desktop-safari` | 1440 x 900 | app, keyboard, a11y, visual |
| | `iphone-14-webkit` | iPhone 14, Safari engine | mobile, a11y, visual |
| | `iphone-13-webkit-landscape` | iPhone 13 landscape | mobile |
| | `ipad-webkit` | iPad portrait (810 px: phone layout) | mobile |
| | `ipad-webkit-landscape` | iPad landscape (1080 px: desktop layout, touch) | app |
| | `auth-iphone-14-webkit` | | auth |

Firefox has no phone emulation in Playwright, so phones run on Chromium (Android) and WebKit (iOS). The WebKit
build Playwright uses is the Safari engine, not Safari itself: it shows layout, viewport, input and event
differences, but not iOS-only behaviour such as the on-screen keyboard resizing the page or Safari's toolbars.

Environment: `E2E_ENGINES`, `E2E_CHANNEL` (`msedge` runs the Chromium projects in Microsoft Edge),
`E2E_BASE_URL` (test a server that is already running, as the Windows CI job does with `run.bat`),
`E2E_PYTHON`, `E2E_PORT`, `E2E_CHROMIUM`, `E2E_SKIP_BUILD=1`.

No retries: a test that fails sometimes is fixed (usually by waiting for the right thing), not retried.

`chart.mobile.spec.ts` sends real touch drags and long-presses through the Chrome DevTools Protocol (Playwright
itself only taps), so it runs on the Chromium phone projects only (`CDP_ONLY` in `playwright.config.ts`).

Known Firefox behaviour (open): about once per CI run, in a different test each time, a Firefox navigation in a
fresh browser context never completes, not even to DOMContentLoaded, until the test times out. So far it followed a
test that reloaded the page or opened a second one. Every `page.goto` and `page.reload` waits for
`domcontentloaded` (`navDefaults` in `tests/fixtures.ts`), not the load event; that did not stop it. In CI the
servers' stdout (one line per HTTP request) is in the job log, which shows whether the hung request reached the
server.

## CI

`.github/workflows/ci.yml` runs one e2e job per engine (WebKit in two shards) and a Windows job that starts
the server with `run.bat` and runs the `desktop` and `auth-desktop` projects in Edge. Traces, screenshots
and the HTML report are uploaded as artifacts only when a job fails.

## Accessibility

`a11y.spec.ts` runs axe-core (WCAG 2.1 A/AA and best practices) on each key screen in the desktop and phone
layouts and on the sign-in screen. Serious and critical violations fail the test; the rest are attached to the
test report. `keyboard.spec.ts` walks the trading path with the keyboard only and checks that dialogs take
focus, keep Tab inside, close on Escape and return focus to the control that opened them.

## Visual baselines

`visual.spec.ts` (in CI, or locally with `E2E_VISUAL=1`) compares a screenshot of each key screen with a stored baseline, per platform and project:
`tests/__screenshots__/<platform>/<project>/visual.spec.ts/<screen>.png`. Prices, sizes, clocks, P&L, dates
and the chart canvases change from run to run, so `tests/visual.css` hides them in place while the screenshot
is taken; the charts are therefore not compared at all. The tolerance is 2 % of pixels
(`playwright.config.ts`). The spec runs on Linux only, because the baselines are made on the CI runner
(fonts and anti-aliasing differ between machines; a baseline made on your own computer will not match CI).

To add or update baselines:

1. Missing baselines are written by every CI run (the test fails once, saying so). To rewrite baselines that
   differ, add the label `update-visual-baselines` to the pull request and push (or re-run the jobs).
2. Each e2e job prints the new or changed files in its step "Print new or changed visual baselines", as a
   base64 tar between marker lines. Fetch the job logs and unpack them from the repository root:

   ```bash
   gh api repos/<owner>/<repo>/actions/jobs/<job id>/logs > job.log
   python tools/baselines_from_log.py job.log
   ```

   (They are also in the job's artifact when the job failed.)
3. Look at the PNGs, commit them, remove the label.
