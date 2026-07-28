// Run the browser's pack-reading helpers from the command line, so the Python
// writers can be checked against the code the site actually runs.
//
//   node tests/pack_check.mjs zoomindex <blob.gz> <base>
//   node tests/pack_check.mjs directory <blob.gz> <base>
//   node tests/pack_check.mjs coalesce  <spec.json>

import { readFileSync } from "node:fs";
import { gunzipSync } from "node:zlib";

import { decodeZoomIndex } from "../src/decode.js";
import { decodeDirectory } from "../src/search.js";
import { coalesce } from "../src/pack.js";

const [mode, path, baseArg] = process.argv.slice(2);

function bufferOf(file) {
  const raw = gunzipSync(readFileSync(file));
  return raw.buffer.slice(raw.byteOffset, raw.byteOffset + raw.byteLength);
}

if (mode === "zoomindex") {
  const index = decodeZoomIndex(bufferOf(path), Number(baseArg));
  console.log(
    JSON.stringify({
      count: index.count,
      keys: [...index.keys],
      lengths: [...index.lengths],
      offsets: [...index.offsets],
    })
  );
} else if (mode === "directory") {
  const map = decodeDirectory(bufferOf(path), Number(baseArg));
  console.log(JSON.stringify(Object.fromEntries(map)));
} else if (mode === "coalesce") {
  const spec = JSON.parse(readFileSync(path, "utf-8"));
  const runs = coalesce(spec.items, {
    gap: spec.gap,
    maxRun: spec.maxRun,
    partAt: spec.partStarts
      ? (offset) => {
          let part = 0;
          for (let i = 0; i < spec.partStarts.length; i++) {
            if (offset >= spec.partStarts[i]) part = i;
          }
          return part;
        }
      : undefined,
  });
  console.log(
    JSON.stringify(
      runs.map((r) => ({ start: r.start, length: r.length, keys: r.items.map((i) => i.key) }))
    )
  );
} else {
  console.error("unknown mode");
  process.exit(2);
}
