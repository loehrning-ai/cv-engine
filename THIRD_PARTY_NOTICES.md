# Third-party notices

The project source code is licensed under the MIT License in [`LICENSE`](LICENSE).
Bundled third-party assets retain their own licenses:

| Asset | Bundled paths | License |
|---|---|---|
| Inter font files | `design/fonts/Inter-*.ttf`, `tools/editor/static/fonts/Inter-*.ttf` | SIL Open Font License 1.1; see [`LICENSES/Inter-OFL.txt`](LICENSES/Inter-OFL.txt) |
| Source Serif 4 font files | `design/fonts/SourceSerif4-*.ttf` | SIL Open Font License 1.1; see [`LICENSES/SourceSerif4-OFL.txt`](LICENSES/SourceSerif4-OFL.txt) |
| CodeMirror 5.65.21 | `tools/editor/static/vendor/codemirror-5.65.21/` | MIT; see [`LICENSES/codemirror-5.65.21-MIT.txt`](LICENSES/codemirror-5.65.21-MIT.txt) |
| js-yaml 4.3.0 | `tools/editor/static/vendor/js-yaml-4.3.0/js-yaml.min.js` | MIT; see [`LICENSES/js-yaml-4.3.0-MIT.txt`](LICENSES/js-yaml-4.3.0-MIT.txt) |
| Paged.js 0.4.3 | `tools/editor/static/vendor/pagedjs-0.4.3/paged.polyfill.js` | MIT; see [`LICENSES/pagedjs-0.4.3-MIT.txt`](LICENSES/pagedjs-0.4.3-MIT.txt) and bundled dependency notices in [`LICENSES/pagedjs-0.4.3-bundled-dependencies.txt`](LICENSES/pagedjs-0.4.3-bundled-dependencies.txt) |
| Supabase JavaScript client 2.110.0 | `tools/editor/static/vendor/supabase-js-2.110.0.js` | MIT; see [`tools/editor/static/vendor/supabase-js-2.110.0.LICENSE.txt`](tools/editor/static/vendor/supabase-js-2.110.0.LICENSE.txt) |
| Lucide 1.26.0 icon geometry | `tools/editor/static/icons.js`, `engine/render/brand_icons.py` | ISC with Feather-derived icons under MIT; see [`LICENSES/lucide-1.26.0-ISC-and-Feather-MIT.txt`](LICENSES/lucide-1.26.0-ISC-and-Feather-MIT.txt) and [`LICENSES/icon-assets.lock.json`](LICENSES/icon-assets.lock.json) |

Exact package versions, registry integrity values, runtime paths, and SHA-256
digests are locked in
[`tools/editor/static/vendor/browser-assets.lock.json`](tools/editor/static/vendor/browser-assets.lock.json).
The Paged.js bundle incorporates event-emitter 0.3.5, clear-cut 2.0.2,
css-tree 1.1.3, and source-map 0.6.1. Their complete notices are redistributed
alongside the bundle.

The notices above apply to the redistributed assets, not to the project's
MIT-licensed source code.

Product and service names in the interface identify user-selected profile
destinations. Their names and trademarks belong to their respective owners.
No affiliation, sponsorship, or endorsement is implied. The public release
does not redistribute third-party brand logos.
