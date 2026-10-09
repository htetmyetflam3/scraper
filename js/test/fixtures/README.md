# Result page fixtures

Saved snippets of search-result HTML used by `js/test/search_results.test.js`, so
the parser can be tested offline (no network, no engine to be rate limited by).

These are **hand-written, sanitised** snippets. They are not copies of live
pages: no session ids, cookies, tracking tokens or personal data. They reproduce
the *markup shapes* that real responses use, because each shape has broken the
parser at least once:

| Fixture | What it covers |
| --- | --- |
| `mojeek-results.html` | Mojeek: web results are `<li>` inside `ul.results-standard`, URL in `a.ob`, title in `h2 > a`. |
| `searx-results.html` | SearXNG: `<article class="result result-default category-general">` with a `url_header` anchor and the title in `<h3><a>`; both anchors share the result URL, so results are deduped, and the `results` wrapper class must not be mistaken for a result. |
| `brave-results.html` | Brave Search: web results are divs carrying both the `snippet` class and `data-type="web"`, so news/discussion/pagination snippets must be ignored. Title sits in its own `title` div; hrefs are direct. |
| `brave-no-results.html` | Brave's "no results found" page — a legitimate zero. |
| `brave-cloudflare.html` | The Cloudflare "Just a moment..." interstitial Brave serves when it throttles a client — retried, not recorded as done. |
| `google-consent.html` | Google's cookie consent wall: a real page holding no results. Counts as blocked so it is retried instead of being recorded as an empty query. |
| `bing-li-algo.html` | Full-page Bing results: `<li class="b_algo">`, title in `<h2><a>`, mix of `/ck/a?...&u=a1…` redirects and direct hrefs. |
| `bing-div-algo.html` | Bing's simplified page: results are `<div class="b_algo">` with *nested* divs in the caption. This is the shape the old `<li>`-only parser dropped entirely. |
| `bing-algoheader-anchor.html` | A result whose first anchor is the favicon/site link, so the title must be taken from `<h2><a>`. |
| `bing-no-results.html` | Bing's "no results found" page — a legitimate zero. |
| `bing-blocked.html` | A CAPTCHA / "enable JavaScript" interstitial — should be retried, not recorded as done. |
| `bing-empty-unknown.html` | Real HTML with no results and no explanation — the signal that the markup changed. |
| `duckduckgo-results.html` | `html.duckduckgo.com` layout, `class="result__a"` titles, `/l/?uddg=` redirects. |
| `google-results.html` | Google layout, `<h3>` title anchors, `/url?q=` redirects. |

When a crawler run reports pages with "unparseable HTML", save one of those
pages here (sanitised) and add a test — that is what caught the `div.b_algo`
regression.
