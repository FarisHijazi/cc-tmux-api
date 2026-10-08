# Primary browser references

Research checked 2026-10-08. These are source-based findings and derived HQ test
recommendations, not observations of the live native UI (owned by another agent).
Deliberately plain links, not `@` includes.

1. Remote Control has named sessions, direct links/QR codes, and online markers.
   Retain host/project identity alongside titles.
   [Anthropic](https://code.claude.com/docs/en/remote-control#connect-from-another-device)
2. Connection failure does not necessarily stop the local session. Show explicit
   connectivity errors and do not equate disconnected with finished.
   [Anthropic](https://code.claude.com/docs/en/remote-control#check-connection-status)
3. Visual Viewport accounts for keyboard and zoom. Check composer bounds on real
   iPhone through keyboard open/dismiss and rotation.
   [WebKit](https://webkit.org/blog/9674/new-webkit-features-in-safari-13/)
4. Dynamic viewport units and safe-area padding solve different layout concerns.
   Reduced-height Chrome screenshots do not certify the iOS keyboard.
   [Viewport units](https://webkit.org/blog/12445/new-webkit-features-in-safari-15-4/),
   [safe areas](https://webkit.org/blog/7929/designing-websites-for-iphone-x/)
5. iOS 26 defaults Home Screen installs to web apps, with a user opt-out. Test
   browser and standalone modes separately.
   [WebKit](https://webkit.org/blog/17333/webkit-features-in-safari-26-0/#every-site-can-be-a-web-app-on-ios-and-ipados)
6. iPhone Web Push requires a Home Screen web app and permission requested from a
   user gesture; Focus affects delivery. Test denial, lock screen, and deep link.
   [WebKit](https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/)
7. iOS can suspend background pages. Refresh on visible `visibilitychange`,
   reconnect transport, preserve drafts, and avoid uncertain action replay.
   [WebKit](https://webkit.org/blog/8970/how-web-content-can-affect-power-usage/)
8. `pageshow` covers page-cache restoration, distinct from visibility changes.
   Test Back/Forward restoration and avoid unload-only draft persistence.
   [WebKit](https://webkit.org/blog/516/webkit-page-cache-ii-the-unload-event/)
