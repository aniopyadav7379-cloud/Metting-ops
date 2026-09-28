import sharp from 'sharp';
import { writeFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const outDir = resolve(__dirname, '..', 'public', 'icons');

const PURPLE = '#7c3aed';
const WHITE = '#ffffff';

function buildSvg(size) {
  const radius = Math.round(size * 0.22);
  const stroke = Math.max(6, Math.round(size * 0.04));
  const cx = size / 2;
  const cy = size / 2 + size * 0.02;
  const micWidth = size * 0.30;
  const micHeight = size * 0.42;
  const micRadius = micWidth / 2;
  const micX = cx - micWidth / 2;
  const micY = cy - micHeight / 2 - size * 0.04;
  const standLength = size * 0.18;
  const baseWidth = size * 0.22;
  const baseY = micY + micHeight + size * 0.08;
  const baseStroke = Math.max(8, Math.round(size * 0.045));
  return `<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#a855f7"/>
      <stop offset="55%" stop-color="${PURPLE}"/>
      <stop offset="100%" stop-color="#4f46e5"/>
    </linearGradient>
  </defs>
  <rect x="0" y="0" width="${size}" height="${size}" rx="${radius}" ry="${radius}" fill="url(#bg)"/>
  <rect x="${micX}" y="${micY}" width="${micWidth}" height="${micHeight}" rx="${micRadius}" ry="${micRadius}" fill="${WHITE}"/>
  <path d="M ${cx - baseWidth} ${baseY - standLength} a ${baseWidth} ${baseWidth} 0 0 0 ${baseWidth * 2} 0" fill="none" stroke="${WHITE}" stroke-width="${baseStroke}" stroke-linecap="round"/>
  <line x1="${cx}" y1="${baseY}" x2="${cx}" y2="${baseY + size * 0.07}" stroke="${WHITE}" stroke-width="${baseStroke}" stroke-linecap="round"/>
  <line x1="${cx - baseWidth * 0.85}" y1="${baseY + size * 0.09}" x2="${cx + baseWidth * 0.85}" y2="${baseY + size * 0.09}" stroke="${WHITE}" stroke-width="${baseStroke}" stroke-linecap="round"/>
</svg>`;
}

async function emit(size, filename) {
  const svg = buildSvg(size);
  const png = await sharp(Buffer.from(svg)).png().toBuffer();
  writeFileSync(resolve(outDir, filename), png);
  console.log(`wrote ${filename} (${png.length} bytes)`);
}

await emit(192, 'icon-192.png');
await emit(512, 'icon-512.png');
await emit(180, 'apple-touch-icon.png');
const maskSvg = buildSvg(512);
writeFileSync(resolve(outDir, 'icon-512-maskable.png'), await sharp(Buffer.from(maskSvg)).png().toBuffer());
console.log('done');
