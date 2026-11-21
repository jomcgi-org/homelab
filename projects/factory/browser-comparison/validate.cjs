const { PNG } = require("pngjs");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const png = PNG.sync.read(fs.readFileSync("screen.png"));
assert.deepEqual([png.width, png.height], [640, 480]);
assert.deepEqual([...png.data.subarray(-4)], [36, 86, 120, 255]);
