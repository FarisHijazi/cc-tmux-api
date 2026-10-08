# Local Markdown dependencies

The renderer in `../app.js` parses with marked, then sanitizes with DOMPurify.
The explicit tag/attribute policy excludes media and active content. Styles for
headings, lists, tables, and code live in `../styles.css`; `../sw.js` caches these
modules as public shell assets. These references are deliberately plain paths,
not `@` includes.

- **marked 18.1.0**: unmodified `package/lib/marked.esm.js` from
  https://registry.npmjs.org/marked/-/marked-18.1.0.tgz, saved as `marked.mjs`.
  License: `MARKED-LICENSE.txt`. Upstream: https://marked.js.org/.
- **DOMPurify 3.4.16**: unmodified `package/dist/purify.es.mjs` from
  https://registry.npmjs.org/dompurify/-/dompurify-3.4.16.tgz, saved as `dompurify.mjs`.
  Licenses: `DOMPURIFY-LICENSE.txt`, `DOMPURIFY-LICENSE-MPL.txt`.
  Upstream: https://github.com/cure53/DOMPurify.

Both package archives were checked against the npm registry SHA-512 integrity
metadata before extraction. No install scripts ran. Existing Preact and htm
vendor files were unchanged. There are no runtime CDN imports.

For updates, replace the upstream modules and licenses together, retain the
sanitizer policy, update the service-worker shell version, and rerun
`../../../../tests/frontend_test.mjs` from the repository root. Its browser
fixtures check hostile HTML/links, absence of remote loads, and mobile layout.
