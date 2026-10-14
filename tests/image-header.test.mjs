import { test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
const source = await readFile(
  new URL("../src/resource_advisor/static/image-header.js", import.meta.url),
  "utf8",
);
const { imageDimensions } = await import(
  "data:text/javascript;base64," + Buffer.from(source).toString("base64")
);
const dimensions = (bytes, type) =>
  imageDimensions(Uint8Array.from(bytes).buffer, type);
test("PNG and baseline/progressive JPEG expose dimensions without decoding pixels", () => {
  const png = [
    137,
    80,
    78,
    71,
    13,
    10,
    26,
    10,
    0,
    0,
    0,
    13,
    73,
    72,
    68,
    82,
    0,
    0,
    2,
    0,
    0,
    0,
    1,
    0,
    ...Array(9).fill(0),
  ];
  assert.deepEqual(dimensions(png, "image/png"), { width: 512, height: 256 });
  for (const frame of [0xc0, 0xc2])
    assert.deepEqual(
      dimensions(
        [255, 216, 255, 224, 0, 4, 1, 2, 255, frame, 0, 8, 8, 1, 0, 2, 0, 3],
        "image/jpeg",
      ),
      { width: 512, height: 256 },
    );
});
test("WebP VP8, lossless and extended headers are accepted; animations are rejected", () => {
  const riff = [...Buffer.from("RIFF"), 0, 0, 0, 0, ...Buffer.from("WEBP")];
  const webp = (type, data) => [
    ...riff,
    ...Buffer.from(type),
    data.length,
    0,
    0,
    0,
    ...data,
  ];
  assert.deepEqual(
    dimensions(webp("VP8 ", [0, 0, 0, 157, 1, 42, 0, 2, 0, 1]), "image/webp"),
    { width: 512, height: 256 },
  );
  const bits = 511 | (255 << 14);
  const lossless = [
    47,
    bits & 255,
    (bits >>> 8) & 255,
    (bits >>> 16) & 255,
    (bits >>> 24) & 255,
  ];
  assert.deepEqual(dimensions(webp("VP8L", lossless), "image/webp"), {
    width: 512,
    height: 256,
  });
  assert.deepEqual(
    dimensions(webp("VP8X", [0, 0, 0, 0, 255, 1, 0, 255, 0, 0]), "image/webp"),
    { width: 512, height: 256 },
  );
  assert.throws(() =>
    dimensions(webp("VP8X", [2, 0, 0, 0, 255, 1, 0, 255, 0, 0]), "image/webp"),
  );
});
test("truncated, mismatched and zero-sized headers are rejected", () => {
  for (const type of ["image/png", "image/jpeg", "image/webp"])
    assert.throws(() => dimensions([255, 216], type));
  assert.throws(() =>
    dimensions([255, 216, 255, 192, 0, 8, 8, 0, 0, 0, 2, 3], "image/jpeg"),
  );
  assert.throws(() => dimensions([255, 216, 255, 224, 0, 1], "image/jpeg"));
});
