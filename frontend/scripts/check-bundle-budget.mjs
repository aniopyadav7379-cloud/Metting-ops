import { gzipSync } from 'node:zlib';
import { readFile, readdir } from 'node:fs/promises';
import path from 'node:path';

const distDir = path.resolve('dist');
const html = await readFile(path.join(distDir, 'index.html'), 'utf8');
const initialAssets = [...html.matchAll(/(?:src|href)="\/?assets\/([^"?]+\.js)"/g)]
  .map((match) => match[1]);

if (initialAssets.length === 0) {
  throw new Error('No initial JavaScript assets found in dist/index.html. Refusing to skip the bundle budget.');
}

const sizes = await Promise.all(initialAssets.map(async (asset) => {
  const bytes = await readFile(path.join(distDir, 'assets', asset));
  return { asset, gzipBytes: gzipSync(bytes).byteLength };
}));
const initialGzipBytes = sizes.reduce((total, item) => total + item.gzipBytes, 0);

const allAssets = await readdir(path.join(distDir, 'assets'));
const jsAssets = allAssets.filter((asset) => asset.endsWith('.js'));
const allSizes = await Promise.all(jsAssets.map(async (asset) => {
  const bytes = await readFile(path.join(distDir, 'assets', asset));
  return { asset, gzipBytes: gzipSync(bytes).byteLength };
}));
const largest = allSizes.sort((a, b) => b.gzipBytes - a.gzipBytes)[0];

const INITIAL_GZIP_BUDGET = 650 * 1024;
const override = process.env.MEETING_OPS_BUNDLE_BUDGET_OVERRIDE;
console.log(`Initial JavaScript: ${(initialGzipBytes / 1024).toFixed(1)} KiB gzip across ${sizes.length} asset(s).`);
console.log(`Largest JavaScript: ${largest.asset} (${(largest.gzipBytes / 1024).toFixed(1)} KiB gzip).`);

if (initialGzipBytes > INITIAL_GZIP_BUDGET) {
  if (!override) {
    throw new Error(
      `Initial JavaScript exceeds the ${INITIAL_GZIP_BUDGET / 1024} KiB gzip budget. ` +
      'Use MEETING_OPS_BUNDLE_BUDGET_OVERRIDE with a linked issue/approval only for a reviewed exception.',
    );
  }
  console.warn(`Bundle budget override recorded: ${override}`);
}
