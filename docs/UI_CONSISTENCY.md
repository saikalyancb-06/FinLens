# One look for every signed-in page

Every signed-in page uses the same type scale (`html.app-inner`); only the
public pages (`/`, `/home`, `/pricing`, `/contact`, `/login`) keep their own.
Reports, Ingestion, Reconciliation and the Dashboard used to run on smaller
scales, so the same table or button was a different size from tab to tab.

The same kind of thing is built the same way everywhere:

| Thing | Build it with | Size |
|---|---|---|
| Page title + subtitle + actions | `<PageHeader title subtitle actions>` | title 0.9rem, subtitle 0.54rem |
| Tabs under a title | `.page-tabs` / `.page-tab` (`.active`) | 0.57rem |
| Card heading | `.card-header h3` (+ `.card-subtitle`) | 0.66rem, upper-case |
| Stat tiles | `<StatTiles tiles=[{label, value, sub, warn}]>` | label 0.45, value 0.8, note 0.42rem |
| Messages | `<Banner kind="ok/error/warn/info">` | 0.54rem |
| Form field label | `.field-label` | 0.45rem, upper-case |
| Form fields in a card | plain `input` / `select` | 0.5rem |
| Transaction rows in Review Queue | `<ReviewTxnCells>` (Manual Review, Duplicates, BRS) | — |

The rules are the last block of `app/static/styles.css` ("ONE LOOK FOR EVERY
SIGNED-IN PAGE") and win over older per-page tweaks. When adding a page, use
these components rather than inline font sizes.
