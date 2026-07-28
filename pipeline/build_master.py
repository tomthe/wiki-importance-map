"""Stage 4 - join everything into one row per geolocated item.

Produces WORK/articles.parquet, the single table the tiles and the search
index are built from, and the thing to query for any offline analysis.

Importance
----------
Raw PageRank is dominated by countries, years and languages, so both signals
are log-compressed and normalised *within the geolocated subset*:

    pr_norm = log10(1 + pagerank) / max(log10(1 + pagerank))
    qr_norm = log10(1 + qrank)    / max(log10(1 + qrank))
    score   = 0.45*pr_norm + 0.45*qr_norm + 0.10*sitelink_norm

Log rather than percentile keeps the heavy tail, which is what makes a label
map readable: Paris really should dwarf a hamlet. Percentile ranks are stored
alongside for filtering ("show me the top 1%").

Usage:
    python -m pipeline.build_master
"""

from __future__ import annotations

import functools
import time

import duckdb

from pipeline import config
from pipeline.taxonomy import build_class_map

W_PAGERANK = 0.45
W_QRANK = 0.45
W_SITELINKS = 0.10

# This stage takes about ten minutes; progress should show up in a redirected
# log as it happens rather than all at once at the end.
print = functools.partial(print, flush=True)


def main() -> None:
    started = time.perf_counter()
    truthy = str(config.TRUTHY_OUT).replace("\\", "/")
    ranks = str(config.RANKS_OUT).replace("\\", "/")
    sitelinks = str(config.SITELINKS_OUT).replace("\\", "/")

    con = duckdb.connect()
    con.execute("PRAGMA threads=48")
    con.execute("PRAGMA memory_limit='512GB'")

    print("1/8  coordinates")
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
    print(f"     {n_geo:,} geolocated items")

    print("2/8  claims")
    con.execute(
        f"""
        CREATE TABLE claims_item AS
        SELECT * FROM read_parquet('{truthy}/claims_item_*.parquet')
        """
    )
    con.execute(
        f"""
        CREATE TABLE instance_of AS
        SELECT c.qid, list(DISTINCT c.value) AS classes
        FROM claims_item c JOIN geo USING (qid)
        WHERE c.pid = 31 GROUP BY c.qid
        """
    )
    # An item can be in several countries - the Danube is in ten, a language in
    # dozens - and picking one of them is not a summary, it is a wrong answer.
    # (P17 of "French" happens to come out as Guernsey.) So count them too, and
    # let the consumer decide: build_tiles drops the name when it is ambiguous
    # rather than putting a arbitrary country in a tooltip.
    con.execute(
        """
        CREATE TABLE single_item_claims AS
        SELECT qid,
               max(CASE WHEN pid = 17  THEN value END) AS country_qid,
               max(CASE WHEN pid = 131 THEN value END) AS admin_qid,
               count(DISTINCT CASE WHEN pid = 17  THEN value END) AS n_countries,
               count(DISTINCT CASE WHEN pid = 131 THEN value END) AS n_admin
        FROM claims_item WHERE pid IN (17, 131) AND qid IN (SELECT qid FROM geo)
        GROUP BY qid
        """
    )

    print("3/8  numbers, dates, urls, native labels")
    con.execute(
        f"""
        CREATE TABLE extras AS
        WITH nums AS (
            SELECT qid,
                   max(CASE WHEN pid = 1082 THEN value END) AS population,
                   max(CASE WHEN pid = 2044 THEN value END) AS elevation
            FROM read_parquet('{truthy}/claims_num_*.parquet')
            WHERE qid IN (SELECT qid FROM geo) GROUP BY qid
        ),
        times AS (
            SELECT qid, min(value) AS inception
            FROM read_parquet('{truthy}/claims_time_*.parquet')
            WHERE pid = 571 AND qid IN (SELECT qid FROM geo) GROUP BY qid
        ),
        iris AS (
            SELECT qid,
                   max(CASE WHEN pid = 18  THEN value END) AS image,
                   max(CASE WHEN pid = 856 THEN value END) AS website
            FROM read_parquet('{truthy}/claims_iri_*.parquet')
            WHERE qid IN (SELECT qid FROM geo) GROUP BY qid
        ),
        mono AS (
            SELECT qid,
                   max(CASE WHEN pid = 1705 THEN value END) AS native_label,
                   max(CASE WHEN pid = 1705 THEN lang  END) AS native_lang
            FROM read_parquet('{truthy}/claims_mono_*.parquet')
            WHERE pid = 1705 AND qid IN (SELECT qid FROM geo) GROUP BY qid
        )
        SELECT geo.qid, nums.population, nums.elevation, times.inception,
               iris.image, iris.website, mono.native_label, mono.native_lang
        FROM geo
        LEFT JOIN nums  USING (qid)
        LEFT JOIN times USING (qid)
        LEFT JOIN iris  USING (qid)
        LEFT JOIN mono  USING (qid)
        """
    )

    print("4/8  english labels and descriptions")
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
        WHERE qid IN (SELECT qid FROM geo) GROUP BY qid
        """
    )

    print("5/8  sitelinks (english title, native title, count)")
    # "Original" title = the article in the language edition that matches the
    # language of the item's native label (P1705), which is the closest thing
    # Wikidata has to "what the place calls itself".
    # site ids look like 'enwiki', 'zh_yuewiki'. Sister projects such as
    # 'enwikisource' do not end in 'wiki' so they fall out already; the listed
    # ones do end in 'wiki' but are not language editions, and counting them
    # would inflate the sitelink signal.
    con.execute(
        f"""
        CREATE TABLE sl AS
        SELECT * FROM read_parquet('{sitelinks}/sitelinks.parquet')
        WHERE qid IN (SELECT qid FROM geo)
          AND site LIKE '%wiki'
          AND site NOT IN (
            'commonswiki', 'wikidatawiki', 'specieswiki', 'metawiki',
            'mediawikiwiki', 'incubatorwiki', 'sourceswiki', 'foundationwiki',
            'outreachwiki', 'testwiki', 'wikimaniawiki'
          )
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

    print("6/8  importance")
    con.execute(
        f"""
        CREATE TABLE ranks AS
        SELECT geo.qid,
               coalesce(p.pagerank, 0.0) AS pagerank,
               coalesce(q.qrank, 0)      AS qrank
        FROM geo
        LEFT JOIN read_parquet('{ranks}/pagerank.parquet') p USING (qid)
        LEFT JOIN read_parquet('{ranks}/qrank.parquet')    q USING (qid)
        """
    )

    print("7/8  categories")
    class_map = build_class_map(con, truthy)
    con.register("class_map", class_map)

    print("8/8  assembling master table")
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
            WHERE c.pid = 31 AND c.qid IN (SELECT qid FROM geo)
            GROUP BY c.qid
        ),
        joined AS (
            SELECT
                g.qid, g.lon, g.lat, g.n_coords,
                l.label_en,
                d.descr_en,
                s.title_en,
                nt.title_native, nt.native_site,
                anyt.title_any, anyt.any_site,
                e.native_label, e.native_lang,
                e.population, e.elevation, e.inception, e.image, e.website,
                coalesce(s.n_sitelinks, 0) AS n_sitelinks,
                sic.country_qid, sic.admin_qid,
                coalesce(sic.n_countries, 0) AS n_countries,
                coalesce(sic.n_admin, 0) AS n_admin,
                io.classes AS instance_of,
                coalesce(cats.cat, 0) AS cat,
                coalesce(cats.sub, 0) AS sub,
                r.pagerank, r.qrank
            FROM geo g
            LEFT JOIN labels l         USING (qid)
            LEFT JOIN descriptions d   USING (qid)
            LEFT JOIN sitelink_agg s   USING (qid)
            LEFT JOIN native_title nt  USING (qid)
            LEFT JOIN any_title anyt   USING (qid)
            LEFT JOIN extras e         USING (qid)
            LEFT JOIN single_item_claims sic USING (qid)
            LEFT JOIN instance_of io   USING (qid)
            LEFT JOIN cats             USING (qid)
            JOIN ranks r               USING (qid)
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

    # Country and admin labels are just more items in the label table.
    con.execute(
        """
        CREATE TABLE master2 AS
        SELECT m.* EXCLUDE (country_qid, admin_qid),
               m.country_qid, cl.label_en AS country_label,
               m.admin_qid,   al.label_en AS admin_label
        FROM master m
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

    n, n_named, n_en, n_any, n_cat = con.execute(
        """
        SELECT count(*),
               count(coalesce(label_en, title_en, native_label, title_any)),
               count(title_en),
               count(any_site),
               count(CASE WHEN cat > 0 THEN 1 END)
        FROM master2
        """
    ).fetchone()
    print(f"\nwrote {out}")
    print(f"  {n:,} items")
    print(f"  {n_named:,} with a usable name ({100 * n_named / n:.1f}%)")
    print(f"  {n_en:,} with an English Wikipedia article ({100 * n_en / n:.1f}%)")
    print(f"  {n_any:,} with an article in some language ({100 * n_any / n:.1f}%)")
    print(f"  {n_cat:,} categorised ({100 * n_cat / n:.1f}%)")
    print(f"  {(time.perf_counter() - started) / 60:.1f} min")


if __name__ == "__main__":
    main()
