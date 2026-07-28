# Wikipedia PageRank map

Every geolocated Wikipedia article on a map, with label size driven by how
important the subject is. Importance combines two independent signals:
[danker](https://github.com/athalhammer/danker) PageRank over the link graph of
*all* Wikipedia language editions, and
[QRank](https://qrank.wmcloud.org/) pageviews.

Plus two and a half million things that have no coordinate of their own but
point at something that does: **people at their birthplace**, paintings at their
museum, battles where they were fought, companies at their headquarters, ships
at their home port, novels where they are set. No human in Wikidata has a
P625 — and between them, those are most of what anyone actually looks up. They
are drawn as hollow dots and the map never claims they are really there; see
[Things that are not at a coordinate](#things-that-are-not-at-a-coordinate).

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

1. **`extract_truthy`** (66 min) — streams the 43 GB dump, which is **990 GiB**
   of N-Triples. A single bzip2 stream cannot be split by byte offset, so one
   reader process decompresses with `indexed_bzip2` (which decodes bz2 blocks
   across all cores — 270 MB/s sustained, against 26 MB/s for stock `bz2`) and
   feeds line-aligned chunks to 16 parser processes over one queue each. Each
   parser scans with regexes anchored on distinctive predicate literals, so the
   ~97% of lines nobody wants are rejected inside the C matcher and never become
   Python objects. Output: 12,350,782 coordinates, 192,414,001 item claims,
   15,996,828 dates, 92,416,762 English labels, 102,203,884 descriptions.

   Adding the ten derived-location properties and the two person dates cost
   nothing measurable in time — they are extra alternatives in regexes that
   were already running — and 16.8% more item claims (164,767,921 → 192,414,001),
   most of it P106, which averages two occupations per person.

2. **`extract_ranks`** (20 s) — danker (25,816,446 items) and QRank
   (28,880,307 items) to parquet keyed by Q-number.

3. **`extract_sitelinks`** (5 min) — 99,742,340 sitelinks from the MySQL dump.

4. **`build_master`** (25 min) — one DuckDB job. 12,078,243 items have a
   coordinate of their own; 3,777,688 more clear the sitelink filter and borrow
   one; the importance floor keeps 2,467,766 of those, for **14,546,009 rows**.
   88.6% have a usable name, 61.5% an article in some language, 21.2% an English
   one, 97.2% categorised, 17.0% at a borrowed coordinate.

   | property | items placed |
   |---|---|
   | P19 place of birth | 3,018,600 |
   | P159 headquarters | 291,331 |
   | P276 location | 258,091 |
   | P20 place of death | 88,604 |
   | P840 narrative location | 71,948 |
   | P937 work location | 22,983 |
   | P551 residence | 19,421 |
   | P504 / P532 home port | 6,710 |

5. **`build_tiles`** (2 min) — 111,827 tiles, 532 MB, mean 4.7 KiB per tile, in
   six pack files.

6. **`build_search`** (1 min) — 51,175 prefix shards, 76 MB, in one pack file.
   Reads the country table out of `manifest.json`, so it has to run *after*
   `build_tiles`.

That is 608 MB in **nine files** — seven packs plus `manifest.json` and
`search.json`. It was 459 MB before people, artworks, events, ships and
companies were added, and 816 MB in 183,437 files before that — 1.1 GB of
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
| borrowed location | `loc_pid`, `loc_qid`, `loc_label`, `loc_pop` |
| category | `cat`, `sub`, `instance_of` (list of class Q-ids) |
| importance | `pagerank`, `qrank`, `n_sitelinks`, `pr_norm`, `qr_norm`, `sl_norm`, `score`, `pr_pct`, `qr_pct` |
| extras | `population`, `elevation`, `inception`, `birth`, `death`, `image`, `website` |

`loc_pid` is 0 for an item standing on its own coordinate and the placing
property otherwise; `n_coords` is 0 for those rows, since they have none of
their own. `loc_pop` is the *place's* population, carried only so `build_tiles`
knows how far to spread the people born there — a person never has one.

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

### Things that are not at a coordinate

**No human in Wikidata has a P625.** Nor does a painting, a battle, a novel or
a company — and between them that is most of what people actually look up. But
they all *point at* something that does have one: a place of birth, the museum
holding the picture, where the battle was fought, the city a story is set in,
a headquarters, a home port.

So an item joins the map one of two ways. Either it has its own coordinate, or
it borrows one:

| property | what it places | code |
|---|---|---|
| P19 place of birth | people | 1 |
| P20 place of death | people with no birthplace | 2 |
| P276 location | battles, events, artworks in museums | 3 |
| P159 headquarters | companies and organisations | 4 |
| P840 narrative location | novels, films, plays — where fiction happens | 5 |
| P504 / P532 home port | ships | 6 |
| P551 / P937 residence, work location | anyone the first six missed | 7 |

Place of birth is first because it is both the most often present and the most
informative; place of death only stands in when there is no birthplace. The
code goes into three spare bits of the tile's `flags` byte, so a derived item
costs no more to store than a place does.

Four rules keep this from turning into fiction:

* **Only real P625 items are location sources.** The join is against `geo`,
  never against the derived set, so nothing can chain person → person → place.
  One hop up `P131` is allowed when the birthplace itself has no coordinate —
  a Gemeinde that only records its parent — and only when that parent is
  unambiguous, on the same principle as everything else here.
* **Country and admin come from the place, not the item.** People have
  citizenship (P27), not P17; inheriting Ulm's country is what makes the
  tooltip read correctly with no new columns.
* **The client always says whose coordinate it is.** The hover reads *born in
  Ulm, Germany*, never *Ulm, Germany*. Without the preposition the line is
  indistinguishable from a place's own address and quietly asserts that
  Einstein is a point in southern Germany. The detail panel goes further: it
  labels the position approximate, and it drops the OpenStreetMap and Google
  Maps links, because those would open a pin on a spot this map invented.
* **Derived dots are drawn hollow.** A non-colour channel, so it stacks with
  the category hue instead of competing with it, and it says the one thing
  colour cannot: filled means the item really is there.

**Spreading the pile.** Everyone born in Paris resolves to one point. Left
alone that is a single dot with tens of thousands of things behind it — the
collision pass can only ever show one, and the rest are invisible and
unhoverable. So the k-th item at a place, in score order, is moved to radius
`R·√(k/K)` at `k` golden angles round the circle: phyllotaxis, the way a
sunflower packs seeds evenly into a disc. The most important one stays exactly
on the place. `R` comes from the place's population, clamped to 200 m – 8 km,
so a village does not scatter its people across a county. No random number
generator is involved, so a rebuild puts everyone back where they were.

**A floor, not a truncation** — the same argument as the search index. Roughly
12.6M humans have a birthplace, and carrying all of them would roughly double
the pyramid and push at the 1 GB Pages limit. `--derived-min-score` (with a
`--derived-min-sitelinks` pre-filter) trims the tail. Items with a coordinate
of their own are never dropped by it: the floor exists to bound how many people
the pyramid carries, not to thin the map out.

The one real cost is that **every score moved**. Normalisation happens within
the mapped set, so adding two million people shifts `pr_pct`, `qr_pct` and
every tile boundary. That is unavoidable, and it is exactly why the data had to
be rebuilt and republished wholesale rather than patched.

### Importance

Raw PageRank is dominated by countries, years and languages, so both signals
are log-compressed and normalised *within the mapped set*:

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

**An anchor is never overridden by the graph.** A class named by hand keeps
what it was named as; the BFS only fills in classes nobody listed. Without that
rule an inherited priority could out-rank an explicit one, and it did: "monarch"
is pinned to People/Monarch & noble, but monarch is a subclass of politician, so
the walk reached it one step out and Elizabeth II came out as a Politician. The
same edge turned every philosopher into a Scientist and Mozart into an Artist.

### What a person did

People are the one category whose subcategory does not come from P31 — every
one of them is just "human". It comes from P106 (occupation) instead, resolved
against the same P279 graph with its own anchor list.

The hard part is that Wikidata lists *everything anybody ever did*. Descartes is
a military officer, Lincoln a farmer, Leonardo a diplomat, Osama bin Laden a
civil engineer. Ordering the anchors by how *specific* an occupation is puts
exactly those first and produces nonsense, so `OCCUPATION_ANCHORS` is ordered by
**how rarely the occupation is incidental**: roles that are usually somebody's
whole identity at the top, footnotes at the bottom. Nobody is a painter in
passing, so the visual arts sit above the sciences — that is what stops Leonardo
being a Scientist and Michelangelo a Writer.

Of the forty best-known people on the map, thirty-eight now land where you would
expect. The two that do not are not fixable from here: Wikidata lists Galileo's
occupations as including "politician", and Kant's as including "physicist".
Believing the data is the right default, and the alternative orderings that
rescue those two break Churchill and Einstein instead.

There are exactly **nine coloured categories**, and nine is a ceiling that was
measured rather than chosen. A map shows every category at once, so the palette
has to clear the *all-pairs* colour-vision gates, not the easier adjacent-pairs
ones. Sweeping OKLCH space against the shipped eight:

| hues | worst all-pairs CVD ΔE | verdict |
|---|---|---|
| 8 (as shipped) | 11.1 light / 8.7 dark | pass |
| 9 | 9.9 light / 8.7 dark | pass |
| 10 | 7.6 | only the 6–8 "floor" band |

So People — which needed its own category, because nothing else describes a
person — took the ninth slot, and the other new layers became subcategories of
what already existed. A tenth hue would have degraded every existing category's
separation to buy a colour for one layer, which is a bad trade.

The eight original hues are untouched. People's was picked by brute force
against them: in dark mode it costs the palette nothing at all (the binding
pairs are the same two as before), and in light mode it takes the worst pair
from 11.1 to 9.9. Re-run `dataviz`'s `validate_palette.js` under `--pairs all`
against `#f8f8f6` and `#0e0e0e` before touching a hex.

Detail lives in the subcategories, which are read as text and are therefore
free — People has 28 of them. Colour is never the only channel: the filter
list, the tooltip and the detail panel all name the category.

**Events live under "Other".** Battles, sieges, treaties, earthquakes and
festivals are not places, and they were the layer with the weakest claim on the
last hue. They get the neutral swatch and a real subcategory list instead, so
ticking "Battle" on its own still turns the map into a war map. If a tenth hue
ever becomes worth its cost, they are the ones to promote.

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
case (Wikipedia titles cannot contain `|`, so it is a safe separator).

`flags` bit 0 means an article exists, bit 1 an image, bit 2 a website, and
**bits 3–5 hold the location source** — 0 for an item's own P625, 1–7 for the
property that lent it one (see the table above). Three bits, seven meanings,
and bits 6–7 are still spare. `manifest.json` ships `locSources`, the phrase
each code turns into, so the wording has one home rather than being duplicated
in the client.

Two columns do double duty for derived items, which is why they cost nothing:
`admin` carries the borrowed place's name ("Ulm", not "Baden-Württemberg"),
which is the word the tooltip needs and which repeats hard enough inside a tile
for the per-tile string table to squash it; and `year` carries a person's birth
year rather than a founding date, with the client keying off the category to
label it correctly.

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

Adding 2.47M borrowed-coordinate items took the pyramid from 408 MB to 532 MB —
+30% for +20% more items, so a person costs slightly *less* than a place, which
is what you would expect from a row whose population, elevation and admin area
are all sentinels.

**The deepest zoom needs a budget now.** Zoom 12 used to take everything left
over, which was fine when every item had its own coordinate. It is not fine when
two million people are stacked on a few thousand cities: the fullest z12 tile
over central Paris wants 22,559 items and would encode to 850 KiB for one
viewport. So the last level is capped — but the cap falls on borrowed
coordinates only. An item with a real P625 is never dropped: it is genuinely
there, it was on the map before People existed, and evicting a quarter of a
million real places to make room for synthetic points would be a straight
regression. At `--deep-capacity 8000` the worst tile is 358 KiB against a
326 KiB floor set by the real places alone, and 94,418 people (3.8% of the
derived items) are dropped and reported.

One consequence worth knowing: search indexes `articles.parquet`, not the
tiles, so it can find a person the deepest zoom does not draw. They are the
lowest-scoring people in the densest city centres, and the label budget would
never have drawn them anyway, but the two sets are no longer identical.

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
default 0.20 keeps 3.14M of 12.9M named items. `--cap` (500 entries per
3-character shard) survives as a bound on the worst keystroke, but with the
floor in place it rarely binds: of 51,273 shards, 1,912 reach it.

The index went from 520 MB in 84,682 files to **76 MB in one**, and the root
file every visitor loads from 1.26 MB to 5 KiB.

Two and a quarter times as many items clear the floor as before, and all of the
growth is the new layers: 1,821,024 of the 3,144,411 are at borrowed
coordinates. Places went slightly *down*, 1.39M to 1,323,387, because
normalisation happens within the mapped set and a few of the two million people
have a higher PageRank than anything with a coordinate — which raises the
divisor and pushes a thin band of places back under the floor.

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

The water is also faded — 45 % opacity in `positron`, 65 % in `dark`. A label's
halo is worth only the contrast between it and what is behind it, and water is
the one large surface that sits well away from the land tone: `positron` paints
it rgb(194,200,202) against rgb(242,243,240) of land, so a white halo over the
sea gives up most of its separation. Water is drawn straight onto the
background, so raising its transparency blends it toward the land colour
without having to parse and mix the style's own colours, which arrive as
`rgb()`, `hsl()` and hex. `dark` fades less: its water is *lighter* than its
land and starts from 15 levels of contrast rather than 48, so fading it as hard
would erase the coastline to buy back very little.

## Tests

```sh
python -m tests.test_tile_format    # Python encoder -> JS decoder round trip
python -m tests.test_packfile       # pack offsets, indexes, range coalescing
python -m tests.test_build_master   # the join's SQL, on synthetic shards
python -m tests.test_spread         # the phyllotaxis spread's invariants
```

All four run in seconds and need no dumps. The first two exist because the
same arithmetic is written twice, in two languages: an off-by-one in an offset
does not fail loudly, it hands the decoder somebody else's bytes.

`test_build_master` covers the derived-location join specifically: that a
person lands on their birthplace's real coordinate, that P20 stands in when
there is no P19, that a coordinate-less birthplace resolves one hop up P131 but
an *ambiguous* one does not, that country is inherited from the place rather
than the person, that occupation priority picks the subcategory, and that the
floor can drop derived rows but never an item with a coordinate of its own.

`test_spread` guards the one place the pipeline invents data. It checks that
real coordinates are untouched, that the most important item at a place stays
exactly on it, that the radius clamp holds at 78°N and with no population at
all, that the points fill the disc rather than a ring, that the result does not
depend on input row order — and that `build_tiles` and `build_search` land on
identical coordinates, since they filter different rows and a divergence there
would fly you to a person's birthplace with the person off-screen.

For the site itself, start a headless Chromium with
`--remote-debugging-port=9222` and run:

```sh
node tests/hash_check.mjs                          # no browser needed
node tests/browser_check.mjs http://localhost:8000/ shot.png
node tests/interaction_check.mjs http://localhost:8000/
```

`hash_check` round-trips the category selection through its URL form and is
mostly about links this build did not write: a token from an older manifest, one
somebody edited by hand, one from a future taxonomy. It needs no browser and no
data, because `encodeSelection` and `parseSelection` are plain functions.

`browser_check` reports console errors, failed requests, how many tile and
search requests were made and how many labels were drawn, and saves a
screenshot. `interaction_check` drives the real UI: the default category
selection, that People is present and starts on, filtering a category off and
back on, subcategory expansion, search, flying to a result, the detail panel
with its live Wikipedia summary and thumbnail, that the new tile columns reach
that panel, the density slider, all three crowding controls, population sizing,
and all / none. It also drives the shareable URL in both directions — that a
change to the selection reaches the hash, that a pasted hash reaches the panel
and the map, and that a link with no selection restores the defaults.

It also pins down the honesty rules for a borrowed coordinate, which are the
easiest thing here to regress silently: that the place line reads "born in Ulm,
Germany", that the panel names the place, flags the position as approximate,
claims no exact coordinates, labels the year as a birth rather than a founding,
and withholds the OpenStreetMap and Google Maps links.

Order matters in that file. The default-selection check has to come first,
because later steps switch categories on deliberately — and the crowding steps
need them all on, or too few labels fit for any of the caps to bind and they
would assert nothing.

There is no Node on this machine's PATH; the scripts run under the Node bundled
inside VS Code (`ELECTRON_RUN_AS_NODE=1 "…/Code.exe" script.mjs`), and the
Python tests find it automatically.

To exercise the whole path without an hour of extraction:

```sh
export WIKIMAP_WORK=/tmp/fake WIKIMAP_DATA=/tmp/fakedata
python -m tests.make_fake_master
python -m pipeline.build_tiles --max-zoom 9 --deep-capacity 300
python -m pipeline.build_search --min-score 0.2
```

`WIKIMAP_DATA` exists because those two stages publish to the *site's* data
directory whatever `WIKIMAP_WORK` says, so this used to overwrite a real build
with test data. Set it and `data/` is safe. The fixture hangs 900 synthetic
people off each city at the city's exact coordinate, so the phyllotaxis spread
and the deepest-zoom budget both have something to bite on.

## The site

| Module | Responsibility |
|---|---|
| `src/main.js` | wiring, view state, URL hash, controls |
| `src/pack.js` | range reads over the packed files, request coalescing |
| `src/tiles.js` | `TileManager` — what to load, caching, aborting, eviction |
| `src/decode.js` | the binary tile decoder |
| `src/declutter.js` | screen-space label selection and displacement |
| `src/layers.js` | deck.gl dot, leader-line and (unused by default) text layers |
| `src/labelcanvas.js` | the labels, drawn with `fillText` onto a canvas over deck's |
| `src/categories.js` | category/subcategory filter, panel, and its URL form |
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

### Label contrast

Labels are drawn from a signed-distance field, which is what allows a halo. The
field is generated as `1 - (distance/radius + cutoff)` and the shader reads 0.75
as the glyph edge, so everything below is arithmetic on those two numbers.

deck exposes two knobs over it — `fontSettings.smoothing`, the half-width of the
alpha ramp at the glyph edge, and `outlineWidth`, which sets the threshold the
halo runs out to — and **neither is scaled by the size a label is drawn at**.
In pixels they come out as

```
ramp = 2 * smoothing * (radius / fontSize) * drawnPx * dpr
halo = (0.75 - outlineBuffer - smoothing) * (radius / fontSize) * drawnPx * dpr
```

so exactly one label size gets a one-pixel edge. At deck's defaults that size is
~16 device pixels: below it there is less than a pixel of ramp left, the glyph
edge stops being antialiased at all and one-pixel stems snap on and off between
pixels; above it the edge goes soft. Most labels here are 10–14 CSS pixels, so
on a 1× display the whole map sat on the aliased side — a 12 px label had 0.44
pixels of ramp, which is what "mangled at the pixel level" looks like.

So `src/layers.js` inverts it: the pixel widths are the input (`labelTuning`,
live on `window` so they can be tried from the console) and the two uniforms are
solved for, per size bucket and for the display the page is on. There are two
buckets because one master cannot serve 9 and 46 pixels — the small labels get
minified past their stems, the large ones magnified — and because a bucket is a
size range the solve can be right about. `buffer` has to hold the whole field,
which reaches `0.75 * radius`, so the two move together.

The ramp is set to **2.4 px, not the textbook 1.0**, which is the interesting
result. A one-pixel ramp is correct antialiasing and it looked worse. At 10–14
px the sampling noise from minifying the master is larger than the letterform
detail, so a wide ramp wins by low-pass filtering the noise away: the glyphs go
soft but their weight stops varying letter to letter, and even weight is what
reads. Sharp-but-eroded loses to soft-but-even. Note the `smoothing` clamp at
0.25 binds in the small bucket on a 1× display, so ramp values above ~2.33 are
all the same picture there.

Note also that `outlineWidth` saturates: `max(smoothing, 0.75 * (1 - w))` means
every `w` above `1 - smoothing/0.75` is the same value, and `outlineWidth: 2.5`
draws exactly what `1` draws.

A black halo in light was tried, on the theory that a white one at 10–14 px is a
fringe pushed into the counters of e, a and o and between the stems of m —
thinning the letterforms it is there to protect — where a dark one would thicken
them. It does thicken them, and it looks worse: the letters go muddy and the
category colours go muddier. The halo is white over the light basemap and black
over the dark one, opaque in both, because its outer edge is a gradient already
and a colour below full alpha is faded twice.

### Labels on a canvas

None of the above fixes the floor: deck positions each glyph quad at a
fractional device pixel with no hinting, so the stems land wherever they land
and letters differ in weight within a word. Native rasterisers snap stems to the
pixel grid; an SDF atlas cannot. `src/labelcanvas.js` is the way out — the
labels are drawn with `fillText` onto a 2D canvas over deck's, which hints the
stems, antialiases per size, takes a halo width in real pixels, and lets each
label be rounded onto the pixel grid. It is the default; `window.labelRenderer`
switches back to the distance field for comparison.

The cost is CPU: rasterising text is orders of magnitude dearer than drawing a
textured quad, and 1,500 labels × 2 (stroke and fill) per frame would not hold
60fps. So it is split the way the declutter pass already splits — the labels are
rasterised **once into a single atlas canvas** when the selection or a size
slider changes, and every frame after that is one `drawImage` per label out of
that atlas. Panning rasterises nothing and re-uploads no texture. One atlas
rather than a canvas per label, because a few hundred small canvases are a few
hundred textures for the compositor, and past a browser-specific limit they stop
being GPU-backed at all, which turns every blit into an upload.

What it gives up:

* **A hitch when the atlas rebuilds.** ~300 labels is a few milliseconds, 1,500
  is tens. It lands on the frame after the view settles, which is already the
  frame that re-declutters.
* **Labels creep against the basemap by up to half a pixel** while panning,
  because they snap to whole device pixels and the map under them does not. That
  snapping is most of what makes them crisp; `canvasLabelTuning.snap` turns it
  off to show the trade.
* **Two renderers to keep in step.** Hover and click are handled at the deck
  level in `main.js` and ask the canvas first, since its labels are on top of
  everything deck draws. The overlay is `pointer-events: none`, so deck keeps
  dragging, zooming and picking the dots.
* **Atlas memory**, roughly 4 bytes per device pixel of drawn text — a few MB at
  the default density, ~15 MB at 1,500 labels on a 2× display.

### Interaction

Settlements and Administrative start unchecked. They are the two categories the
importance score favours hardest, so with them on the first view is city and
country names — which is what the basemap underneath already says. Both are one
click away in the filter list, and `DEFAULT_OFF` in `src/main.js` is the whole
of it. People starts *on*: it is the layer this map did not have, and unlike
Settlements it duplicates nothing on the basemap underneath.

Hover shows the name, category, description, where it is, population,
elevation, founding year and both importance signals — all of it out of the
tile, so nothing is fetched while panning. Clicking opens a panel that adds the
Wikipedia summary and thumbnail for that one item from the REST API, with links
to Wikipedia, Wikidata, OpenStreetMap and Google Maps.

### Sharing a view

The URL hash holds the position **and the category selection**, because those
are the two halves of what someone is looking at and a screenshot can only show
one of them. A link to nothing but battles is a different map from a link to the
same coordinates.

```
#5.00/48.8566/2.3522                 where the map is looking
#5.00/48.8566/2.3522/cat=0~1         …showing only Other → Battle
```

Everything after the three numbers is a `key=value` segment, so a link made
before `cat=` existed still works and an unknown key is ignored rather than
throwing the view away. The selection grammar (`src/categories.js`) uses only
characters a fragment leaves alone, so nothing is ever percent-escaped:

| token | means |
|---|---|
| `all`, `none` | every category, no category |
| `0.2.9` | those three, all of their subcategories |
| `0~1,3-5` | category 0, only subcategories 1 and 3–5 |
| `9!0` | category 9, all of its subcategories except 0 |
| *(absent)* | however the map opens — see `DEFAULT_OFF` |

`~` and `!` both exist because a partial selection is usually "the one I ticked"
or "the one I unticked", and whichever spells it shorter is the one written:
unticking one of People's 28 subcategories costs three characters, not sixty.

Three rules keep old and hand-edited links from doing something stupid:

* **The default is never spelled out.** A selection appears in the hash only
  when it differs from what the site opens with, so the common link stays short
  and keeps meaning "the default" if the defaults later change.
* **Unknown ids and out-of-range subcategories are dropped, not fatal.** A
  category the manifest no longer has is skipped and the rest of the token still
  applies, because a rebuild can renumber the taxonomy.
* **A token that means nothing leaves the defaults alone.** `cat=banana`
  resolves to no selection at all, and resolving that to an empty map would turn
  one stale link into a blank page. `cat=none` is a choice and is honoured.

Pasting a link into a tab that already has the map open works too: `hashchange`
applies the whole link, position and selection together. `history.replaceState`
does not fire that event, so the handler only ever sees a link from outside.

For an item at a borrowed coordinate all of that shifts one step back from the
map. The dot is hollow, the hover reads *born in Ulm, Germany*, the panel calls
the position approximate and names the place, the year is labelled "Born"
rather than "Founded", and the two map links are gone — an OpenStreetMap pin on
a phyllotaxis offset would be the most confident lie on the page.

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

**The one thing Pages cannot do is forget.** Half a gigabyte of data is
comfortable against the 1 GB published-site limit, but git keeps every version:
rebuild twice and the repository is past its recommended size, and only a
history rewrite gets it back. Six files instead of 183,437 does not change
that, it just makes each copy cheaper to move around.

So `data/` is gitignored on `master`, which carries only source and its
history, and the site is published to a **`gh-pages` branch that is one root
commit**:

```sh
sh tools/publish.sh              # push
sh tools/publish.sh --dry-run    # build the commit, print it, push nothing
```

The script works through a temporary `GIT_INDEX_FILE` and `git commit-tree`
with no `-p`, so it never checks a branch out, never touches the working tree,
and produces a commit with no parent. The branch is replaced wholesale every
time, which means the remote holds exactly one copy of the data no matter how
often the pipeline is rerun. It also refuses to push if any file has crept over
GitHub's 100 MiB hard limit, because that rejection otherwise arrives only
after uploading everything. Point Pages at `gh-pages` / root once.

Two things to know about the rewrite that got here: force-pushing rewrote
public history, so any existing clone needs `git fetch && git reset --hard
origin/master`; and GitHub does not immediately reclaim unreachable blobs, so
the repository's reported size stays high until their gc runs. Neither blocks
anything at this size.

The alternative, if the data keeps growing, is to keep Pages for the site and
put `data/` in an object store with free egress — Cloudflare R2 with a public
bucket, CORS enabled, and `DATA_URL` in `src/main.js` pointed at it. That
retires the "git cannot forget" problem rather than managing it.

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
* **A derived position is not a position.** The dot for a person is their
  birthplace nudged aside by up to eight kilometres so the others born there
  stay reachable. Everything the UI can say about it, it says — hollow dot,
  "born in", "approximate", no map links — but a screenshot cannot, so treat
  the People layer as a density map of *where people came from*, not of where
  anything is.
* Place of birth is a blunt instrument. It puts everyone born in a hospital
  town there rather than where they lived or worked, and for people who moved
  as infants it is close to meaningless. P551 residence and P937 work location
  are recorded far too rarely (2.6% and 7.9% of humans) to lead with.
* The people floor is an importance floor, so who is on the map depends on
  PageRank and pageviews, which are not evenly distributed across languages or
  centuries. The map under-represents everyone the encyclopaedias do.
* Deepest-zoom tiles have a hard per-tile budget now, and overflow is dropped
  rather than pushed deeper — there is nowhere deeper. `build_tiles` prints how
  many and how full the fullest tile wanted to be; if that number is ever
  large, raise `--deep-capacity` or the floor rather than ignoring it.
* Individual animals — Laika, Hachikō — are not included. They would need
  anchoring on species classes, which also catch the species article itself,
  and the mis-categorisation risk was not worth the handful of rows.
