// Decode a .bin tile with the browser decoder and print JSON, so the Python
// encoder can be checked against the code the site actually runs.
// Usage: node tests/decode_tile.mjs <tile.bin> [countries.json]

import { readFileSync } from "node:fs";
import { decodeTile } from "../src/decode.js";

const path = process.argv[2];
const countries = process.argv[3]
  ? JSON.parse(readFileSync(process.argv[3], "utf-8"))
  : [];
const buffer = readFileSync(path);
const arrayBuffer = buffer.buffer.slice(
  buffer.byteOffset,
  buffer.byteOffset + buffer.byteLength
);
const tile = decodeTile(arrayBuffer, countries);
console.log(
  JSON.stringify({
    z: tile.z,
    x: tile.x,
    y: tile.y,
    items: tile.items.map((i) => ({
      qid: i.qid,
      lon: Math.round(i.position[0] * 1e4) / 1e4,
      lat: Math.round(i.position[1] * 1e4) / 1e4,
      title: i.title,
      wiki: i.wiki,
      wikiLang: i.wikiLang,
      descr: i.descr,
      hasImage: i.hasImage,
      hasWebsite: i.hasWebsite,
      locSrc: i.locSrc,
      derived: i.derived,
      cat: i.cat,
      sub: i.sub,
      pop: i.pop,
      elev: i.elev,
      year: i.year,
      sitelinks: i.sitelinks,
      country: i.country,
      admin: i.admin,
      score: Math.round(i.score * 1000) / 1000,
      pr: Math.round(i.pr * 1000) / 1000,
      qr: Math.round(i.qr * 1000) / 1000,
    })),
  })
);
