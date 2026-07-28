# Wikipedia PageRank map

Every geolocated Wikipedia article on a map, with label size driven by how
important the subject is. Importance combines two independent signals:
[danker](https://github.com/athalhammer/danker) PageRank over the link graph of
*all* Wikipedia language editions, and
[QRank](https://qrank.wmcloud.org/) pageviews.

The repository holds two things: a data pipeline that turns raw Wikimedia dumps
into map tiles, and a static site that reads them. There is no server and no
build step.

```
pipeline/   dumps -> articles.parquet -> tiles + search index
data/       pipeline output (gitignored)
src/        the site, as ES modules
tools/      serve.py, a dev server that understands HTTP Range
index.html  the page
tests/      Python-encoder / JS-decoder round trips, browser checks
```

## Running the site

```sh
python tools/serve.py     # http://localhost:8000/
```

**Not `python -m http.server`.** The data lives in packed files that the
browser reads a few kilobytes at a time with an HTTP `Range` header, and the
standard library's handler ignores that header: it answers `200` with the whole
file, so one 4 KB tile becomes a 90 MB download. `tools/serve.py` is the same
thing with ranges implemented. Every real host already does this correctly —
GitHub Pages, Cloudflare, nginx, Apache — and the page checks on startup and
says so rather than hanging.

`data/` must contain `manifest.json`, `tiles.NNN.bin`, `search.json` and
`search.NNN.bin` — see below.

## The data pipeline

### Inputs

Download these into one directory (default `G:\tt\wikidata`, override with
`WIKIMAP_DUMPS`):

| File | Size | Source |
|---|---|---|
| `latest-truthy.nt.bz2` | 43 GB | `dumps.wikimedia.org/wikidatawiki/entities/` |
| `<date>.allwiki.links.rank.bz2` | 226 MB | `danker.s3.amazonaws.com` |
| `qrank*.gz` | 109 MB | `qrank.wmcloud.org/download/qrank.csv.gz` |
| `wikidatawiki-latest-wb_items_per_site.sql.gz` | 1.9 GB | `dumps.wikimedia.org/wikidatawiki/latest/` |

The last one is needed because **the truthy dump contains no sitelinks** — it
has labels, descriptions and statements, but not the Wikipedia article titles.
Those come from Wikidata's `wb_items_per_site` table instead, which has the
title in every language edition, so it also supplies the "original" title and a
sitelink count.

### Stages

```sh
python -m pipeline.run_all            # everything, about an hour
python -m pipeline.run_all --list     # what the stages are
python -m pipeline.run_all --from master
```

Measured on a 256-core machine, writing to a network share:

1. **`extract_truthy`** (67 min) — streams the 43 GB dump, which is **990 GiB**
   of N-Triples. A single bzip2 stream cannot be split by byte offset, so one
   reader process decompresses with `indexed_bzip2` (which decodes bz2 blocks
   across all cores — 270 MB/s sustained, against 26 MB/s for stock `bz2`) and
   feeds line-aligned chunks to 16 parser processes over one queue each. Each
   parser scans with regexes anchored on distinctive predicate literals, so the
   ~97% of lines nobody wants are rejected inside the C matcher and never become
   Python objects. Output: 12,350,782 coordinates, 164,767,921 item claims,
   92,416,762 English labels, 102,203,884 descriptions.

2. **`extract_ranks`** (20 s) — danker (25,816,446 items) and QRank
   (28,880,307 items) to parquet keyed by Q-number.

3. **`extract_sitelinks`** (5 min) — 99,742,340 sitelinks from the MySQL dump.

4. **`build_master`** (11 min) — one DuckDB job producing 12,078,243 rows:
   86.3% with a usable name, 53.6% with an article in some language, 11.7% with
   an English one, 97.2% categorised.

5. **`build_tiles`** (3 min) — 98,786 tiles, 408 MB, mean 4.0 KiB per tile, in
   five pack files.

6. **`build_search`** (1 min) — 36,983 prefix shards, 50 MB, in one pack file.
   Reads the country table out of `manifest.json`, so it has to run *after*
   `build_tiles`.

That is 459 MB in **eight files** — six packs plus `manifest.json` and
`search.json` — where the previous build was 816 MB in 183,437, and 1.1 GB of
actual disk once cluster slack was counted.

A caveat on the regexes, since it is the one thing that will bite anyone
editing them: the quoted-literal pattern is written as the unrolled
`[^"\\]*(?:\\.[^"\\]*)*`, not the obvious `(?:[^"\\]+|\\.)*`. The obvious form
is ambiguous, so every non-English label — most of a 990 GiB dump — backtracks
exponentially when the trailing `@en` test fails. That one change took the
parser from 1.8 MB/s to 74 MB/s per core.

### `articles.parquet`

One row per geolocated item, sorted by score. Columns:

| Group | Columns |
|---|---|
| identity | `qid`, `label_en`, `descr_en`, `title_en`, `title_native`, `native_site`, `native_label`, `native_lang`, `title_any`, `any_site` |
| location | `lon`, `lat`, `n_coords`, `country_qid`, `country_label`, `n_countries`, `admin_qid`, `admin_label`, `n_admin` |
| category | `cat`, `sub`, `instance_of` (list of class Q-ids) |
| importance | `pagerank`, `qrank`, `n_sitelinks`, `pr_norm`, `qr_norm`, `sl_norm`, `score`, `pr_pct`, `qr_pct` |
| extras | `population`, `elevation`, `inception`, `image`, `website` |

A quarter of geolocated items have no English label at all — many are bot
imports from national registries. 1.3M of those do have an article in some
other language (Cebuano, Chechen, Tatar, Serbian, Russian…), so `title_any`
keeps the best available title and the map shows a real name instead of
`Q12345`. Names are resolved as `label_en → title_en → native_label →
title_any`. The 1.65M items with no name anywhere stay in `articles.parquet`
but are left out of the tiles, since a tile is a list of labels.

Two notes on correctness, both of which are easy to get wrong:

* P625 serialises as `Point(lon lat)` — **longitude first**. Values on another
  globe carry a globe prefix (`"<.../Q308> Point(...)"`), so requiring the
  literal to start with `Point(` keeps the data on Earth.
* An item can have several P625 values. Averaging them would invent a location
  that is not in Wikidata, so `build_master` keeps the real value closest to the
  median and records how many there were in `n_coords`.
* The same applies to P17 and P131, and it is worse there because the value
  looks plausible. `n_countries` and `n_admin` record how many there were, and
  `build_tiles` blanks the name when it is more than one — a tooltip that says
  nothing beats one that says Guernsey. This costs almost nothing and buys a
  lot, because the ambiguous items are exactly the ones you see first:

  | item | P17 values | the arbitrary pick |
  |---|---|---|
  | English | 94 | Saint Helena, Ascension and Tristan da Cunha |
  | French | 44 | Guernsey |
  | Mediterranean Sea | 22 | Palestine |
  | German | 12 | Liechtenstein |

  Only 0.35% of the items that had a country lose it, and 6.2% of those with an
  admin area — mostly rivers, ranges and roads, which genuinely do not have one.

### Importance

Raw PageRank is dominated by countries, years and languages, so both signals
are log-compressed and normalised *within the geolocated subset*:

```
pr_norm = log10(1 + pagerank) / max(log10(1 + pagerank))
qr_norm = log10(1 + qrank)    / max(log10(1 + qrank))
score   = 0.45*pr_norm + 0.45*qr_norm + 0.10*sitelink_norm
```

Log rather than percentile, because a label map wants the heavy tail: Paris
really should dwarf a hamlet. Percentile ranks (`pr_pct`, `qr_pct`) are stored
too, for "show me the top 1%" style filtering.

### Categories

Wikidata has no "type of thing" field, just P31 pointing at tens of thousands
of classes. `pipeline/taxonomy.py` pins ~135 well-known classes to a
(category, subcategory) pair, then walks the P279 subclass graph — 5,212,393
edges — so every other class inherits from the nearest anchor. That covers
267,156 classes and categorises 97.2% of items. Anchors carry a priority, so an
item that is both a castle and a building comes out as a castle.

Run `--verify` after editing the anchors. Q-ids are easy to mistype and the
result is silently wrong rather than broken: the first draft of this list had
Q16560789 as "police station", when it is actually a person, and Q39594 as
"cliff", when it is "bay".

```sh
python -m pipeline.survey_classes --top 400   # which classes actually matter
python -m pipeline.taxonomy --verify          # check anchor ids against the dump
```

There are exactly **eight coloured categories** because that is where
categorical colour stops working. A map shows every category at once, so the
palette had to clear the all-pairs colour-vision gates, not the easier
adjacent-pairs ones. Detail lives in the subcategories, which are read as text
and are therefore free. Colour is never the only channel: the filter list, the
tooltip and the detail panel all name the category.

## One file, not a hundred thousand

The tiles and the search shards used to be one small file each — 98,755 of the
first and 84,682 of the second, 183,437 in all. That is the worst possible
shape for this data.

* Shared hosting counts inodes, and 183k breaches many plans outright.
* A 4 KiB filesystem cluster is larger than most of the blobs, so 816 MB of
  bytes occupied 1.1 GB of disk — `du` said 471M + 616M where the byte counts
  were 296 MB + 520 MB.
* `git status`, clone and push all crawl, especially over a network share.
* Every pipeline rerun rewrites all of them, and git keeps every old copy
  forever.

So blobs go into a handful of large files and the browser asks for one with an
HTTP `Range` header. `pipeline/packfile.py` writes them; `src/pack.js` reads
them.

**Why a handful and not literally one.** GitHub blocks any single file over
100 MiB on push — a hard limit, not a warning — and GitHub Pages does not
resolve Git LFS pointers, so LFS is not a way out. One 400 MB pack would rule
out the host this map most wants to be on. Parts are capped at 90 MiB
(`--part-bytes`), which keeps every one of them comfortably under the limit.
Callers still see one flat address space; a blob never straddles a part, so a
read is always one request.

Two things fall out of the layout that are worth more than the file count:

* Tiles are written in `(zoom, x, y)` order, so the tiles a viewport needs are
  mostly *adjacent in the pack*. `coalesce()` in `src/pack.js` merges reads that
  are within 24 KB of each other. The opening view fetches 13 tiles in 12
  requests; a deep-zoom view of 30 tiles takes 23.
* Because the order is fixed, the per-zoom index only stores lengths — an
  offset is the running sum onto the zoom's base. And the index is itself a
  range in the pack, so it is fetched when a zoom is first visited instead of
  all thirteen at startup: 431 KiB of indexes exist, and the first view of the
  world needs 0.4 KiB of them.

## Tile format

`data/tiles.NNN.bin`, addressed by the per-zoom index; `data/manifest.json`
says where each zoom's index lives.

Every item is written into **exactly one** tile: the shallowest zoom whose tile
still has room, taking items in importance order. Zoom 0 holds the few hundred
most important places on Earth, zoom 1 the next band, and so on. Drawing zoom Z
means unioning the tiles for z = 0..Z that cover the viewport.

Each tile has two budgets: a global one (`--capacity`, 200 items by score) and
a per-category one (`--cat-quota`, 8 items). Without the second, filtering to
"Museums" at world zoom would show nothing, because no museum outranks the top
few hundred cities.

The payload is columnar, little-endian, gzipped, with every numeric array on a
4-byte boundary so the decoder can make typed-array views without copying:

```
0   "WMT2", uint16 version, uint16 z
8   uint32 x, uint32 y, uint32 count
20  uint32 titleBytes, wikiBytes, descrBytes
32  uint32 adminCount, adminBytes, reserved, reserved
48  qid u32[n], titleOff u32[n+1], wikiOff u32[n+1], descrOff u32[n+1],
    lon f32[n], lat f32[n], population u32[n],
    score u16[n], pr u16[n], qr u16[n], country u16[n], admin u16[n],
    elevation i16[n], year i16[n],                              (pad to 4)
    cat u8[n], sub u8[n], flags u8[n], sitelinks u8[n],
    adminOff u32[adminCount+1], adminBlob, titles, wikis, descrs
```

`wiki` holds `lang|title` — the article to link to, preferring English. The
title part is left empty when it equals the drawn label, which is the common
case (Wikipedia titles cannot contain `|`, so it is a safe separator). `flags`
bit 0 means an article exists, bit 1 an image, bit 2 a website.

Optional values use sentinels rather than a null mask, because a sentinel
compresses to nothing: population `0xFFFFFFFF`, country and admin `0xFFFF`,
elevation and year `-32768`.

### What the extra columns cost

The tile used to carry only what the map draws. Everything else was a network
round trip on click, which meant the hover could say nothing useful. Measured
by appending each candidate column to 271 real tiles sampled across all
thirteen zooms and re-gzipping:

| Field | encoding | bytes/item |
|---|---|---|
| `country` | global u16 id | 0.10 |
| `n_sitelinks` | u8 clamped | 0.23 |
| `population` | u32 exact | 0.4 |
| `inception` | i16 year | 0.33 |
| `elevation` | i16 metres | 0.39 |
| `admin` | per-tile dict + u16 | 1.3 |
| `descr_en` | u32 offsets + utf8 | 5.6 |

Country costs almost nothing because a tile is nearly always one country and
gzip erases the column. Admin has 293,833 distinct values — far too many for a
global table, but only a handful in any one tile, which is the whole reason it
gets a private string table instead of a name per row. Everything except
`descr_en` together is under 3 bytes an item.

`descr_en` is the expensive one and it is also the single line that most often
makes a label make sense — "capital of France", "highest mountain in Germany".
It is in. All told the pyramid went from 310 MB to 408 MB, +32%, for a hover
that answers the question instead of restating the label.

85% of tiled items have a description, 6.8% have a population, 12% an
elevation, 11% a founding date.

## Search

`data/search.json` plus `data/search.NNN.bin`.

Names are normalised (lowercase, accents stripped) and indexed under the first
1, 2 and 3 characters of the whole name and of each word, so "gate" finds
"Golden Gate Bridge". An entry is
`[name, lon, lat, qid, score, cat, country]` — enough to render a result, fly
to it and build a link with no second lookup.

**An importance floor, not a truncation.** The old index took the top three
million items by score and kept every one under every prefix it matched: 424 MB
over 59,518 shards, with a worst case where typing three common letters
downloaded 7.65 MB to render twelve rows. Capping each shard fixes the size but
answers the wrong question — it makes what you can find depend on how many
other things happen to share your prefix. `--min-score` does it properly: an
item is findable if it clears the floor, wherever it sits alphabetically. The
default 0.20 keeps 1.39M of 10.4M named items. `--cap` (500 entries per
3-character shard) survives as a bound on the worst keystroke, but with the
floor in place it rarely binds: of 36,983 shards, 1,043 reach it.

The index went from 520 MB in 84,682 files to **50 MB in one**, and the root
file every visitor loads from 1.26 MB to 3 KiB.

Lookup is at most three range requests and usually one:

```
data/search.json          root: first character -> where its directory lives
  -> directory chunk      that letter's prefixes -> shard offsets
     -> shard             gzipped JSON, best score first
```

Characters with fewer than 64 prefixes share one "rare" chunk, which keeps the
root — the only part every visitor downloads — to a few kilobytes. The old
build shipped a 1.26 MB prefix manifest before you could type anything.

Packing also retires the `con.json` problem: Windows refuses to create a file
called `con`, `prn`, `aux` or `nul`, and `con` (Concord, Constantinople) turns
up immediately in real data. Byte offsets have no such opinions.

## The basemap

OpenFreeMap, not CARTO. CARTO's tiles work without a key, but their terms have
no anonymous tier — free use is for "CARTO grantees" and commercial use needs
an Enterprise licence — so the map was running on something it was not entitled
to and could have been rate-limited without warning. OpenFreeMap states no
limit on views or requests, allows commercial use, needs no key or
registration, and can be self-hosted if the donated hosting goes away. There is
no SLA, which is the trade.

OpenFreeMap has no no-labels variant, and this map is nothing but labels — two
sets of place names on top of each other is unreadable. So `src/basemap.js`
fetches the style and drops every layer with a `layout.text-field` (19 of 55 in
`positron`: place names, road names, shields, water names) before MapLibre sees
it. That is why the style is an object rather than a URL, and why it has to
resolve before the map is created. Attribution is required and their style JSON
does not carry it, so it is attached to the sources where MapLibre's
attribution control finds it. If the style server cannot be reached, the labels
are drawn on plain ground rather than not at all.

## Tests

```sh
python -m tests.test_tile_format    # Python encoder -> JS decoder round trip
python -m tests.test_packfile       # pack offsets, indexes, range coalescing
python -m tests.test_build_master   # the join's SQL, on synthetic shards
```

All three run in seconds and need no dumps. The first two exist because the
same arithmetic is written twice, in two languages: an off-by-one in an offset
does not fail loudly, it hands the decoder somebody else's bytes.

For the site itself, start a headless Chromium with
`--remote-debugging-port=9222` and run:

```sh
node tests/browser_check.mjs http://localhost:8000/ shot.png
node tests/interaction_check.mjs http://localhost:8000/
```

`browser_check` reports console errors, failed requests, how many tile and
search requests were made and how many labels were drawn, and saves a
screenshot. `interaction_check` drives the real UI: the default category
selection, filtering a category off and back on, subcategory expansion, search,
flying to a result, the detail panel with its live Wikipedia summary and
thumbnail, that the new tile columns reach that panel, the density slider, all
three crowding controls, population sizing, and all / none.

Order matters in that file. The default-selection check has to come first,
because later steps switch categories on deliberately — and the crowding steps
need them all on, or too few labels fit for any of the caps to bind and they
would assert nothing.

There is no Node on this machine's PATH; the scripts run under the Node bundled
inside VS Code (`ELECTRON_RUN_AS_NODE=1 "…/Code.exe" script.mjs`), and the
Python tests find it automatically.

To exercise the whole path without an hour of extraction:

```sh
WIKIMAP_WORK=/tmp/fake python -m tests.make_fake_master
WIKIMAP_WORK=/tmp/fake python -m pipeline.build_tiles --max-zoom 9
WIKIMAP_WORK=/tmp/fake python -m pipeline.build_search --min-score 0.2
```

Note that this overwrites `data/`, wherever `WIKIMAP_WORK` points.

## The site

| Module | Responsibility |
|---|---|
| `src/main.js` | wiring, view state, URL hash, controls |
| `src/pack.js` | range reads over the packed files, request coalescing |
| `src/tiles.js` | `TileManager` — what to load, caching, aborting, eviction |
| `src/decode.js` | the binary tile decoder |
| `src/declutter.js` | screen-space label selection and displacement |
| `src/layers.js` | deck.gl label, leader-line and dot layers |
| `src/categories.js` | category/subcategory filter and panel |
| `src/search.js` | prefix search against the packed shards |
| `src/basemap.js` | OpenFreeMap style, with the text layers removed |
| `src/ui.js` | tooltip and detail panel |

### Label decluttering

A viewport can hold 25,000 places and perhaps 300 readable names, so choosing
which names to draw is most of what makes the map legible.

deck.gl ships a `CollisionFilterExtension` for this, and the old version used
it. It is gone, because its behaviour depends on the renderer: on a software
rasteriser it culled 297 of 300 labels over central Paris, and a page cannot
detect that it happened. `declutter.js` does the same job in JS — walk
candidates in importance order, keep a label if its box does not touch a box
already kept, stop at the budget — which costs a few milliseconds and always
gives the same answer. It also reserves the panel rectangles, so no label is
drawn where a panel would cover it.

The selection is recomputed when the view settles (130 ms), not per frame:
labels are anchored to the map, so the set stays correct while panning, and
recomputing mid-drag would make them flicker.

### Crowding

The hard case is several important things at one point. A cathedral, the square
it stands on and the city named after it share a coordinate, and plain
collision testing shows one and silently drops the rest. There are three
controls because they answer different questions:

* **Move crowded labels aside** (on by default). Before giving up on a label,
  try it directly above, directly below and then beside its anchor, out to
  three label-heights. A leader line is drawn when it ends up more than 12 px
  away. Costs nothing and recovers most of them.
* **Show all labels, overlapping.** Skip collision testing entirely and draw
  whatever the density budget allows. Illegible in a city centre, and the only
  way to see that eleven things are stacked on one dot.
* **Maximum importance.** Hide the loudest items so the next tier is not
  competing with them at all — this is what replaced the old minimum-importance
  slider, which only ever removed things that were already too small to read.
  Squared, so the top of the slider gets most of the travel.

Displaced labels are unprojected back to a coordinate rather than kept as a
pixel offset, so the leader line has a real end point and the text layer needs
no special handling. Both drift together during a zoom and are recomputed when
the view settles, which is already how the selection itself works.

### Label size

Size comes from importance by default. **Size by population** blends
`log10(population)` into it for the two thirds of a million items that have
one; items without a population keep their importance and stay comparable, so
the slider does not make the mountains vanish. The blended value drives the
collision order too, so whatever is drawn bigger also wins.

### Interaction

Settlements and Administrative start unchecked. They are the two categories the
importance score favours hardest, so with them on the first view is city and
country names — which is what the basemap underneath already says. Both are one
click away in the filter list, and `DEFAULT_OFF` in `src/main.js` is the whole
of it.

Hover shows the name, category, description, where it is, population,
elevation, founding year and both importance signals — all of it out of the
tile, so nothing is fetched while panning. Clicking opens a panel that adds the
Wikipedia summary and thumbnail for that one item from the REST API, with links
to Wikipedia, Wikidata, OpenStreetMap and Google Maps. The map position lives
in the URL hash, so a view can be shared.

## Hosting

The site is a few hundred kilobytes; the data is the question.

|  | GitHub Pages | shared hosting (+ Cloudflare) |
|---|---|---|
| CDN | yes, Fastly | only what Cloudflare adds |
| HTTP Range | yes (`206`, `Accept-Ranges: bytes`) | yes, Apache and nginx both |
| single-file limit | **100 MiB, hard** | your plan |
| published site limit | 1 GB | your plan |
| bandwidth | 100 GB/month, soft | your plan |
| republishing data | a new commit each time, kept forever | rsync overwrites in place |
| headers | fixed | yours |

The 100 MiB limit is why the packs are split. With that done both hosts work,
and the choice is really between *audience* (Pages: free CDN, no account
needed, an obvious URL) and *iteration* (shared hosting: `rsync` the changed
parts, no history to grow).

Three things to know if you put Cloudflare in front of netcup:

* Cloudflare caches by file *extension*, not MIME type, and only a fixed list
  of them by default. This is why the pack parts are named `tiles.000.bin` and
  not `tiles.pack.000`: `bin` is on that list, `000` is not, so they are cached
  in front of your origin with no configuration at all. The per-file cache limit
  is 512 MB on Free, Pro and Business, and the parts are 90 MiB.
* `manifest.json` and `search.json` are *not* cached by default (JSON never is),
  which is the right default — they change whenever you republish.
* A range request for a part Cloudflare has not cached yet can make it pull more
  from your origin than the browser asked for, while it fills the cache. So
  expect the first visitors after a deploy to cost the origin more than their
  share; it is a warm-up cost, not a per-request one. Worth watching your netcup
  traffic on the first day rather than assuming the CDN absorbs everything.

**The one thing Pages still cannot do is forget.** 459 MB of data is comfortable
against the 1 GB published-site limit, but git keeps every version: rebuild
twice and the repository is past its recommended size, and only a history
rewrite gets it back. Six files instead of 183,437 does not change that, it just
makes each copy cheaper to move around.

Two ways out, both fine:

* Publish the data from an **orphan branch** and replace it wholesale each time,
  so there is exactly one copy in history:

  ```sh
  git checkout --orphan data-only && git rm -rf --cached . 
  git add -f data && git commit -m "data $(date +%F)"
  git push -f origin data-only        # then point Pages at this branch
  ```

* Or keep Pages for the site and put the data in an object store with free
  egress — Cloudflare R2 with a public bucket, CORS enabled, and `DATA_URL` in
  `src/main.js` pointed at it.

Note Cloudflare *Pages* is out regardless: it caps at 20,000 files, and it
capped at 20,000 files back when there were 183,437 of them too.

`index.html` still pulls MapLibre and deck.gl from unpkg. Vendor both if you
care about a third party's outage being your white screen.

## Known limits

* The pyramid is built from the fixed 45/45/10 score. The sliders re-weight
  *label size* live, but not which items are in which tile.
* Category filtering only hides what the tiles already contain. The per-category
  quota keeps every category represented at every zoom, but a filtered view is
  not as dense as a dedicated per-category pyramid would be.
* Population is exact where Wikidata has it, which is 6.8% of items, and
  Wikidata contains nine items claiming over a billion people and one claiming
  five billion. The encoder clips rather than corrects.
