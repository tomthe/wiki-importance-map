"""Stage 4 - join everything into one row per mapped item.

Produces WORK/articles.parquet, the single table the tiles and the search
index are built from, and the thing to query for any offline analysis.

Two ways onto the map
---------------------
Most rows are here because the item has a coordinate of its own (P625). The
rest are here because the item *points at* something that does: a person's
place of birth, a painting's museum, a company's headquarters, a ship's home
port, a novel's setting. No human in Wikidata has a coordinate, but more than
half have a birthplace, and that is by far the most interesting layer this map
was missing.

Derived rows carry `loc_pid` (which property placed them), `loc_qid` and
`loc_label` (the place), so nothing downstream has to guess, and the tooltip
can say "born in Ulm" instead of implying the person is a point on the ground.
Only real P625 items are ever used as a location source - the join is against
`geo`, never against the derived set - so nothing can chain person to person.

Importance
----------
Raw PageRank is dominated by countries, years and languages, so both signals
are log-compressed and normalised *within the mapped set*:

    pr_norm = log10(1 + pagerank) / max(log10(1 + pagerank))
    qr_norm = log10(1 + qrank)    / max(log10(1 + qrank))
    score   = 0.45*pr_norm + 0.45*qr_norm + 0.10*sitelink_norm

Log rather than percentile keeps the heavy tail, which is what makes a label
map readable: Paris really should dwarf a hamlet. Percentile ranks are stored
alongside for filtering ("show me the top 1%").

Adding derived rows moves this scale for everything, which is exactly why the
whole dataset has to be rebuilt and republished rather than patched.

Usage:
    python -m pipeline.build_master
    python -m pipeline.build_master --derived-min-score 0.0   # keep them all
"""

from __future__ import annotations

import argparse
import functools
import time

import duckdb

from pipeline import config
from pipeline.taxonomy import (
    ANCHORS,
    CATEGORY_ID,
    OCCUPATION_ANCHORS,
    load_subclass_graph,
    resolve_anchors,
)

W_PAGERANK = 0.45
W_QRANK = 0.45
W_SITELINKS = 0.10

PEOPLE_CAT = CATEGORY_ID["People"]

# SQL array literal for the precedence order of the derived-location
# properties. list_position() turns it into "which of these came first".
DERIVED_SQL = "[" + ", ".join(str(p) for p in config.DERIVED_PIDS) + "]"

# This stage takes about ten minutes; progress should show up in a redirected
# log as it happens rather than all at once at the end.
print = functools.partial(print, flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--derived-min-score",
        type=float,
        default=0.175,
        help=(
            "importance floor for items placed at somebody else's coordinate. "
            "3.78M items clear the sitelink pre-filter, 3.14M of them people; "
            "0.175 is the score of the two-millionth person, which is where "
            "the tile pyramid stays comfortable against the 1 GB GitHub Pages "
            "limit. Items with a coordinate of their own are never dropped."
        ),
    )
    ap.add_argument(
        "--derived-min-sitelinks",
        type=int,
        default=1,
        help=(
            "cheap pre-filter applied before the joins: an item with no "
            "Wikipedia article anywhere is not something the map can label."
        ),
    )
    args = ap.parse_args()

    started = time.perf_counter()
    truthy = str(config.TRUTHY_OUT).replace("\\", "/")
    ranks = str(config.RANKS_OUT).replace("\\", "/")
    sitelinks = str(config.SITELINKS_OUT).replace("\\", "/")

    con = duckdb.connect()
    con.execute("PRAGMA threads=48")
    con.execute("PRAGMA memory_limit='512GB'")

    print("1/10  coordinates")
    # An item can carry several P625 values. Averaging them would invent a
    # location, so keep the real value closest to the median instead.
    con.execute(
        f"""
        CREATE TABLE geo AS
        WITH raw AS (
            SELECT qid, lon, lat FROM read_parquet('{truthy}/coords_*.parquet')
        ),
        med AS (
            SELECT qid, median(lon) AS mlon, median(lat) AS mlat, count(*) AS n_coords
            FROM raw GROUP BY qid
        )
        SELECT raw.qid, raw.lon, raw.lat, med.n_coords
        FROM raw JOIN med USING (qid)
        QUALIFY row_number() OVER (
            PARTITION BY raw.qid
            ORDER BY (raw.lon - med.mlon) * (raw.lon - med.mlon)
                   + (raw.lat - med.mlat) * (raw.lat - med.mlat), raw.lon, raw.lat
        ) = 1
        """
    )
    n_geo = con.execute("SELECT count(*) FROM geo").fetchone()[0]
    print(f"      {n_geo:,} items with a coordinate of their own")

    print("2/10  claims")
    con.execute(
        f"""
        CREATE TABLE claims_item AS
        SELECT * FROM read_parquet('{truthy}/claims_item_*.parquet')
        """
    )

    print("3/10  sitelinks")
    # site ids look like 'enwiki', 'zh_yuewiki'. Sister projects such as
    # 'enwikisource' do not end in 'wiki' so they fall out already; the listed
    # ones do end in 'wiki' but are not language editions, and counting them
    # would inflate the sitelink signal.
    #
    # Unlike before, this is built for *every* item rather than only the
    # geolocated ones, because the derived-location filter below needs a
    # sitelink count for candidates that are not in `geo` at all.
    con.execute(
        f"""
        CREATE TABLE sl_all AS
        SELECT * FROM read_parquet('{sitelinks}/sitelinks.parquet')
        WHERE site LIKE '%wiki'
          AND site NOT IN (
            'commonswiki', 'wikidatawiki', 'specieswiki', 'metawiki',
            'mediawikiwiki', 'incubatorwiki', 'sourceswiki', 'foundationwiki',
            'outreachwiki', 'testwiki', 'wikimaniawiki'
          )
        """
    )
    con.execute(
        """
        CREATE TABLE sl_count AS
        SELECT qid, count(*) AS n FROM sl_all GROUP BY qid
        """
    )

    print("4/10  derived locations")
    # A place that only records its parent area - a Gemeinde with no coordinate
    # of its own - still resolves, one hop up P131. Only when that parent is
    # unambiguous: an item with several P131 values has no single answer, and
    # picking one would put somebody's birthplace in the wrong valley.
    con.execute(
        """
        CREATE TABLE admin_parent AS
        SELECT qid, any_value(value) AS parent
        FROM claims_item WHERE pid = 131
        GROUP BY qid HAVING count(DISTINCT value) = 1
        """
    )
    con.execute(
        f"""
        CREATE TABLE derived AS
        WITH cand AS (
            SELECT c.qid, c.pid, c.value AS place_qid
            FROM claims_item c
            JOIN sl_count s ON s.qid = c.qid
            WHERE c.pid IN ({", ".join(str(p) for p in config.DERIVED_PIDS)})
              AND s.n >= {args.derived_min_sitelinks}
              AND NOT EXISTS (SELECT 1 FROM geo g WHERE g.qid = c.qid)
        ),
        resolved AS (
            SELECT cand.qid, cand.pid,
                   coalesce(g1.qid, g2.qid) AS loc_qid
            FROM cand
            LEFT JOIN geo g1 ON g1.qid = cand.place_qid
            LEFT JOIN admin_parent ap ON ap.qid = cand.place_qid
            LEFT JOIN geo g2 ON g2.qid = ap.parent
        )
        SELECT r.qid, r.pid AS loc_pid, r.loc_qid, g.lon, g.lat
        FROM resolved r
        JOIN geo g ON g.qid = r.loc_qid
        QUALIFY row_number() OVER (
            PARTITION BY r.qid
            ORDER BY list_position({DERIVED_SQL}, r.pid), r.loc_qid
        ) = 1
        """
    )
    n_derived = con.execute("SELECT count(*) FROM derived").fetchone()[0]
    print(f"      {n_derived:,} items placed at somebody else's coordinate")
    for pid, n in con.execute(
        "SELECT loc_pid, count(*) FROM derived GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall():
        print(f"        P{pid:<5} {n:12,d}")

    # Everything downstream keys on this: one row per item that has a place on
    # the map, however it got one. n_coords is 0 for a derived row - it has no
    # coordinate of its own, which is the whole point.
    con.execute(
        """
        CREATE TABLE mapped AS
        SELECT qid, lon, lat, n_coords, 0::UINTEGER AS loc_pid,
               NULL::UINTEGER AS loc_qid
        FROM geo
        UNION ALL
        SELECT qid, lon, lat, 0::BIGINT AS n_coords, loc_pid, loc_qid
        FROM derived
        """
    )
    n_mapped = con.execute("SELECT count(*) FROM mapped").fetchone()[0]
    print(f"      {n_mapped:,} mapped items in total")

    print("5/10  country and admin area")
    # An item can be in several countries - the Danube is in ten, a language in
    # dozens - and picking one of them is not a summary, it is a wrong answer.
    # (P17 of "French" happens to come out as Guernsey.) So count them too, and
    # let the consumer decide: build_tiles drops the name when it is ambiguous
    # rather than putting an arbitrary country in a tooltip.
    #
    # A derived row inherits these from the place it was put at, not from
    # itself: people have citizenship (P27), not P17, and "born in Ulm,
    # Germany" is the line the tooltip wants.
    con.execute(
        """
        CREATE TABLE attr AS
        SELECT qid, coalesce(loc_qid, qid) AS attr_qid FROM mapped
        """
    )
    con.execute(
        """
        CREATE TABLE single_item_claims AS
        SELECT qid,
               max(CASE WHEN pid = 17  THEN value END) AS country_qid,
               max(CASE WHEN pid = 131 THEN value END) AS admin_qid,
               count(DISTINCT CASE WHEN pid = 17  THEN value END) AS n_countries,
               count(DISTINCT CASE WHEN pid = 131 THEN value END) AS n_admin
        FROM claims_item
        WHERE pid IN (17, 131) AND qid IN (SELECT attr_qid FROM attr)
        GROUP BY qid
        """
    )

    print("6/10  numbers, dates, urls, native labels")
    con.execute(
        f"""
        CREATE TABLE extras AS
        WITH nums AS (
            SELECT qid,
                   max(CASE WHEN pid = 1082 THEN value END) AS population,
                   max(CASE WHEN pid = 2044 THEN value END) AS elevation
            FROM read_parquet('{truthy}/claims_num_*.parquet')
            WHERE qid IN (SELECT qid FROM mapped)
            GROUP BY qid
        ),
        times AS (
            SELECT qid,
                   min(CASE WHEN pid = 571 THEN value END) AS inception,
                   min(CASE WHEN pid = 569 THEN value END) AS birth,
                   min(CASE WHEN pid = 570 THEN value END) AS death
            FROM read_parquet('{truthy}/claims_time_*.parquet')
            WHERE qid IN (SELECT qid FROM mapped) GROUP BY qid
        ),
        iris AS (
            SELECT qid,
                   max(CASE WHEN pid = 18  THEN value END) AS image,
                   max(CASE WHEN pid = 856 THEN value END) AS website
            FROM read_parquet('{truthy}/claims_iri_*.parquet')
            WHERE qid IN (SELECT qid FROM mapped) GROUP BY qid
        ),
        mono AS (
            SELECT qid,
                   max(CASE WHEN pid = 1705 THEN value END) AS native_label,
                   max(CASE WHEN pid = 1705 THEN lang  END) AS native_lang
            FROM read_parquet('{truthy}/claims_mono_*.parquet')
            WHERE pid = 1705 AND qid IN (SELECT qid FROM mapped) GROUP BY qid
        )
        SELECT m.qid, nums.population, nums.elevation,
               times.inception, times.birth, times.death,
               iris.image, iris.website, mono.native_label, mono.native_lang
        FROM mapped m
        LEFT JOIN nums  ON nums.qid  = m.qid
        LEFT JOIN times ON times.qid = m.qid
        LEFT JOIN iris  ON iris.qid  = m.qid
        LEFT JOIN mono  ON mono.qid  = m.qid
        """
    )
    # The source place's own population, kept separately: build_tiles uses it
    # to decide how widely to spread the people born there, and a person must
    # never inherit a population of their own.
    con.execute(
        f"""
        CREATE TABLE place_pop AS
        SELECT qid, max(value) AS pop
        FROM read_parquet('{truthy}/claims_num_*.parquet')
        WHERE pid = 1082 AND qid IN (SELECT loc_qid FROM derived)
        GROUP BY qid
        """
    )

    print("7/10  english labels and descriptions")
    con.execute(
        f"""
        CREATE TABLE labels AS
        SELECT qid, any_value(label) AS label_en
        FROM read_parquet('{truthy}/labels_en_*.parquet') GROUP BY qid
        """
    )
    con.execute(
        f"""
        CREATE TABLE descriptions AS
        SELECT qid, any_value(descr) AS descr_en
        FROM read_parquet('{truthy}/descr_en_*.parquet')
        WHERE qid IN (SELECT qid FROM mapped) GROUP BY qid
        """
    )

    print("8/10  titles (english, native, any)")
    # "Original" title = the article in the language edition that matches the
    # language of the item's native label (P1705), which is the closest thing
    # Wikidata has to "what the place calls itself".
    con.execute(
        """
        CREATE TABLE sl AS
        SELECT * FROM sl_all WHERE qid IN (SELECT qid FROM mapped)
        """
    )
    con.execute(
        """
        CREATE TABLE sitelink_agg AS
        SELECT qid,
               count(*) AS n_sitelinks,
               max(CASE WHEN site = 'enwiki' THEN title END) AS title_en
        FROM sl GROUP BY qid
        """
    )
    # A quarter of geolocated items have no English label at all, but 1.3M of
    # them do have an article in some other language. Keeping the best available
    # title means they get a real name on the map instead of "Q12345", and a
    # link that goes to an article that exists.
    con.execute(
        """
        CREATE TABLE any_title AS
        SELECT qid,
               arg_min(title, prio) AS title_any,
               arg_min(site, prio)  AS any_site
        FROM (
            SELECT qid, site, title,
                   CASE
                     WHEN site = 'enwiki' THEN 0
                     WHEN site IN (
                       'dewiki','frwiki','eswiki','itwiki','ruwiki','ptwiki',
                       'nlwiki','plwiki','jawiki','zhwiki','arwiki','fawiki',
                       'trwiki','ukwiki','svwiki','cswiki','huwiki','fiwiki',
                       'nowiki','dawiki','rowiki','elwiki','hewiki','kowiki',
                       'idwiki','viwiki','thwiki','hiwiki'
                     ) THEN 1
                     ELSE 2
                   END * 1000 + length(site) AS prio
            FROM sl
        )
        GROUP BY qid
        """
    )
    con.execute(
        """
        CREATE TABLE native_title AS
        SELECT sl.qid, any_value(sl.title) AS title_native, any_value(sl.site) AS native_site
        FROM sl
        JOIN extras e ON e.qid = sl.qid
        WHERE e.native_lang IS NOT NULL
          AND sl.site = replace(e.native_lang, '-', '_') || 'wiki'
        GROUP BY sl.qid
        """
    )

    print("9/10  importance and categories")
    con.execute(
        f"""
        CREATE TABLE ranks AS
        SELECT m.qid,
               coalesce(p.pagerank, 0.0) AS pagerank,
               coalesce(q.qrank, 0)      AS qrank
        FROM mapped m
        LEFT JOIN read_parquet('{ranks}/pagerank.parquet') p USING (qid)
        LEFT JOIN read_parquet('{ranks}/qrank.parquet')    q USING (qid)
        """
    )

    # One walk of the P279 graph, two anchor sets: P31 says what a thing is,
    # P106 says what a person did.
    children = load_subclass_graph(con, truthy)
    con.register("class_map", resolve_anchors(children, ANCHORS))
    con.register("occupation_map", resolve_anchors(children, OCCUPATION_ANCHORS))
    del children

    con.execute(
        """
        CREATE TABLE instance_of AS
        SELECT c.qid, list(DISTINCT c.value) AS classes
        FROM claims_item c JOIN mapped USING (qid)
        WHERE c.pid = 31 GROUP BY c.qid
        """
    )

    print("10/10  assembling master table")
    con.execute(
        f"""
        CREATE TABLE master AS
        WITH cats AS (
            -- An item can be an instance of several classes; keep the mapped
            -- one with the best (lowest) priority, e.g. "castle" over "building".
            SELECT c.qid,
                   arg_min(m.cat, m.priority)  AS cat,
                   arg_min(m.sub, m.priority)  AS sub
            FROM claims_item c
            JOIN class_map m ON m.class_qid = c.value
            WHERE c.pid = 31 AND c.qid IN (SELECT qid FROM mapped)
            GROUP BY c.qid
        ),
        occs AS (
            -- Same rule, over occupations. Most people have several - the
            -- "politician, lawyer, writer" shape is everywhere - so the
            -- priority in OCCUPATION_ANCHORS decides which one defines them.
            SELECT c.qid, arg_min(m.sub, m.priority) AS occ_sub
            FROM claims_item c
            JOIN occupation_map m ON m.class_qid = c.value
            WHERE c.pid = 106 AND c.qid IN (SELECT qid FROM mapped)
            GROUP BY c.qid
        ),
        joined AS (
            SELECT
                g.qid, g.lon, g.lat, g.n_coords,
                g.loc_pid, g.loc_qid,
                ll.label_en AS loc_label,
                pp.pop      AS loc_pop,
                l.label_en,
                d.descr_en,
                s.title_en,
                nt.title_native, nt.native_site,
                anyt.title_any, anyt.any_site,
                e.native_label, e.native_lang,
                e.population, e.elevation, e.inception, e.birth, e.death,
                e.image, e.website,
                coalesce(s.n_sitelinks, 0) AS n_sitelinks,
                sic.country_qid, sic.admin_qid,
                coalesce(sic.n_countries, 0) AS n_countries,
                coalesce(sic.n_admin, 0) AS n_admin,
                io.classes AS instance_of,
                coalesce(cats.cat, 0) AS cat,
                -- A person's subcategory is their occupation; everything else
                -- keeps the one its class gave it.
                CASE WHEN coalesce(cats.cat, 0) = {PEOPLE_CAT}
                     THEN coalesce(occs.occ_sub, 0)
                     ELSE coalesce(cats.sub, 0) END AS sub,
                r.pagerank, r.qrank
            FROM mapped g
            JOIN attr a                ON a.qid    = g.qid
            LEFT JOIN labels l         ON l.qid    = g.qid
            LEFT JOIN labels ll        ON ll.qid   = g.loc_qid
            LEFT JOIN place_pop pp     ON pp.qid   = g.loc_qid
            LEFT JOIN descriptions d   ON d.qid    = g.qid
            LEFT JOIN sitelink_agg s   ON s.qid    = g.qid
            LEFT JOIN native_title nt  ON nt.qid   = g.qid
            LEFT JOIN any_title anyt   ON anyt.qid = g.qid
            LEFT JOIN extras e         ON e.qid    = g.qid
            LEFT JOIN single_item_claims sic ON sic.qid = a.attr_qid
            LEFT JOIN instance_of io   ON io.qid   = g.qid
            LEFT JOIN cats             ON cats.qid = g.qid
            LEFT JOIN occs             ON occs.qid = g.qid
            JOIN ranks r               ON r.qid    = g.qid
        ),
        logged AS (
            SELECT *,
                   log10(1 + pagerank)             AS pr_log,
                   log10(1 + qrank)                AS qr_log,
                   log10(1 + n_sitelinks)          AS sl_log
            FROM joined
        ),
        scaled AS (
            SELECT *,
                   pr_log / nullif(max(pr_log) OVER (), 0) AS pr_norm,
                   qr_log / nullif(max(qr_log) OVER (), 0) AS qr_norm,
                   sl_log / nullif(max(sl_log) OVER (), 0) AS sl_norm
            FROM logged
        )
        SELECT
            * EXCLUDE (pr_log, qr_log, sl_log),
            {W_PAGERANK} * coalesce(pr_norm, 0)
          + {W_QRANK}    * coalesce(qr_norm, 0)
          + {W_SITELINKS}* coalesce(sl_norm, 0) AS score,
            percent_rank() OVER (ORDER BY pagerank) AS pr_pct,
            percent_rank() OVER (ORDER BY qrank)    AS qr_pct
        FROM scaled
        """
    )

    # The floor lands here rather than earlier because score only exists once
    # the whole set has been normalised. Items with their own coordinate are
    # never dropped - the floor exists to bound how many people the pyramid
    # has to carry, not to thin the map out.
    dropped = con.execute(
        f"""
        SELECT count(*) FROM master
        WHERE loc_pid <> 0 AND score < {args.derived_min_score}
        """
    ).fetchone()[0]
    con.execute(
        f"""
        CREATE TABLE master_kept AS
        SELECT * FROM master
        WHERE loc_pid = 0 OR score >= {args.derived_min_score}
        """
    )
    print(
        f"      derived floor {args.derived_min_score}: dropped {dropped:,}, "
        f"kept {n_derived - dropped:,}"
    )

    # Country and admin labels are just more items in the label table.
    con.execute(
        """
        CREATE TABLE master2 AS
        SELECT m.* EXCLUDE (country_qid, admin_qid),
               m.country_qid, cl.label_en AS country_label,
               m.admin_qid,   al.label_en AS admin_label
        FROM master_kept m
        LEFT JOIN labels cl ON cl.qid = m.country_qid
        LEFT JOIN labels al ON al.qid = m.admin_qid
        """
    )

    out = str(config.MASTER_PARQUET).replace("\\", "/")
    con.execute(
        f"""
        COPY (SELECT * FROM master2 ORDER BY score DESC)
        TO '{out}' (FORMAT parquet, COMPRESSION zstd)
        """
    )

    n, n_named, n_en, n_any, n_cat, n_der = con.execute(
        """
        SELECT count(*),
               count(coalesce(label_en, title_en, native_label, title_any)),
               count(title_en),
               count(any_site),
               count(CASE WHEN cat > 0 THEN 1 END),
               count(CASE WHEN loc_pid <> 0 THEN 1 END)
        FROM master2
        """
    ).fetchone()
    print(f"\nwrote {out}")
    print(f"  {n:,} items")
    print(f"  {n_named:,} with a usable name ({100 * n_named / n:.1f}%)")
    print(f"  {n_en:,} with an English Wikipedia article ({100 * n_en / n:.1f}%)")
    print(f"  {n_any:,} with an article in some language ({100 * n_any / n:.1f}%)")
    print(f"  {n_cat:,} categorised ({100 * n_cat / n:.1f}%)")
    print(f"  {n_der:,} at a derived location ({100 * n_der / n:.1f}%)")
    print("\n  by category:")
    for cat, sub_total in con.execute(
        """
        SELECT cat, count(*) FROM master2 GROUP BY 1 ORDER BY 1
        """
    ).fetchall():
        print(f"    {cat:2d}  {sub_total:12,d}")
    print(f"\n  {(time.perf_counter() - started) / 60:.1f} min")


if __name__ == "__main__":
    main()
