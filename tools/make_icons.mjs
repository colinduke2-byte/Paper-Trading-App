// Renders the PNG icons and favicon.ico in frontend/public from the SVG marks, using the Playwright
// Chromium that e2e/ installs. Run from the repository root after editing an SVG:
//   node tools/make_icons.mjs
// The output is committed, so building the UI does not need a browser.
import { createRequire } from "node:module";
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const { chromium } = createRequire(join(root, "e2e", "package.json"))("@playwright/test");
const pub = join(root, "frontend", "public");
const svg = (name) => readFileSync(join(pub, "icons", name), "utf8");

const outputs = [
  ["mark.svg", 16, "icons/icon-16.png"],
  ["mark.svg", 32, "icons/icon-32.png"],
  ["mark.svg", 192, "icons/icon-192.png"],
  ["mark.svg", 512, "icons/icon-512.png"],
  ["mark-maskable.svg", 192, "icons/maskable-192.png"],
  ["mark-maskable.svg", 512, "icons/maskable-512.png"],
  // iOS ignores transparency and rounds the corners itself, so the apple-touch-icon is full-bleed.
  ["mark-maskable.svg", 180, "apple-touch-icon.png"],
];

const browser = await chromium.launch();
const page = await browser.newPage({ deviceScaleFactor: 1 });
const png = {};
for (const [src, size, out] of outputs) {
  await page.setViewportSize({ width: size, height: size });
  await page.setContent(`<style>html,body{margin:0;background:transparent}svg{display:block;width:${size}px;height:${size}px}</style>${svg(src)}`);
  const buf = await page.screenshot({ omitBackground: true });
  writeFileSync(join(pub, out), buf);
  png[out] = buf;
  console.log(out, size, buf.length, "bytes");
}
await browser.close();

// favicon.ico holding the 16 and 32 px PNGs (PNG-in-ICO, understood by every current browser).
const imgs = [[16, png["icons/icon-16.png"]], [32, png["icons/icon-32.png"]]];
const header = Buffer.alloc(6 + 16 * imgs.length);
header.writeUInt16LE(0, 0); header.writeUInt16LE(1, 2); header.writeUInt16LE(imgs.length, 4);
let offset = header.length;
imgs.forEach(([size, buf], i) => {
  const e = 6 + 16 * i;
  header.writeUInt8(size, e); header.writeUInt8(size, e + 1); header.writeUInt8(0, e + 2); header.writeUInt8(0, e + 3);
  header.writeUInt16LE(1, e + 4); header.writeUInt16LE(32, e + 6);
  header.writeUInt32LE(buf.length, e + 8); header.writeUInt32LE(offset, e + 12);
  offset += buf.length;
});
writeFileSync(join(pub, "favicon.ico"), Buffer.concat([header, ...imgs.map(([, b]) => b)]));
console.log("favicon.ico", offset, "bytes");
