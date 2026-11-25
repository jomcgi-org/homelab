// Test helper: writes small tar archives for the preview tests.
import { gzipSync } from "node:zlib";

// A minimal ustar writer: [{ path, data?, type? }] -> gzip bytes.
export function tarGz(entries, { gzip = true } = {}) {
  const blocks = [];
  for (const { path, data = "", type = "0", linkname = "" } of entries) {
    const body = Buffer.from(data);
    const header = Buffer.alloc(512);
    header.write(path, 0, 100);
    header.write("0000644\0", 100);
    header.write("0000000\0", 108);
    header.write("0000000\0", 116);
    header.write(`${body.length.toString(8).padStart(11, "0")}\0`, 124);
    header.write("00000000000\0", 136);
    header.write("        ", 148);
    header.write(type, 156);
    header.write(linkname, 157, 100);
    header.write("ustar\0", 257);
    header.write("00", 263);
    let sum = 0;
    for (const byte of header) sum += byte;
    header.write(`${sum.toString(8).padStart(6, "0")}\0 `, 148);
    blocks.push(header, body, Buffer.alloc((512 - (body.length % 512)) % 512));
  }
  blocks.push(Buffer.alloc(1024));
  const tar = Buffer.concat(blocks);
  return gzip ? gzipSync(tar) : tar;
}
