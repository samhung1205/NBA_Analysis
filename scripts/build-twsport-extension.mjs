/** Package a local unpacked extension for ONE exact site. No account, subscription or publishing. */
import { readFile, writeFile, mkdir, copyFile } from 'node:fs/promises';
import { createHash, generateKeyPairSync } from 'node:crypto';
import { resolve } from 'node:path';
const args = process.argv.slice(2);
const value = name => args[args.indexOf(name) + 1];
if (!args.includes('--site')) throw new Error('用法：node scripts/build-twsport-extension.mjs --site https://你的網站 [--out /path]');
const url = new URL(value('--site'));
if ((url.protocol !== 'https:' && !(url.protocol === 'http:' && ['localhost', '127.0.0.1'].includes(url.hostname)))
  || url.username || url.password || url.pathname !== '/' || url.search || url.hash) throw new Error('請提供確切網站 origin（HTTPS，或本機 HTTP）');
const out = resolve(args.includes('--out') ? value('--out') : '.browser-extension');
await mkdir(out, { recursive: true });
let key;
try { key = JSON.parse(await readFile(resolve(out, 'manifest.json'), 'utf8')).key; } catch { /* first package */ }
// A fresh clone must rebuild the published extension with the same public ID.
if (!key) {
  try { key = (await readFile('extensions/twsport-sync/manifest-key.txt', 'utf8')).trim(); } catch { /* unpublished extension */ }
}
if (!key) key = generateKeyPairSync('rsa', { modulusLength: 2048 }).publicKey.export({ type: 'spki', format: 'der' }).toString('base64');
const id = [...createHash('sha256').update(Buffer.from(key, 'base64')).digest().subarray(0, 16)]
  .map(b => String.fromCharCode(97 + (b >> 4), 97 + (b & 15))).join('');
const manifest = {
  manifest_version: 3, name: 'NBA 台彩盤口同步助手', version: '1.0.0', key,
  description: '在你正常開啟台彩並要求同步時，取得 NBA 盤口；不自動下注、不繞過防護。',
  permissions: ['scripting', 'webNavigation'],
  host_permissions: ['https://www.sportslottery.com.tw/*', 'https://www-talo-ssb-pr.sportslottery.com.tw/*'],
  externally_connectable: { matches: [`${url.origin}/decision*`] },
  background: { service_worker: 'background.js', type: 'module' },
};
const schema = JSON.parse(await readFile('pipeline/core/odds/browser_schema.json', 'utf8'));
await writeFile(resolve(out, 'manifest.json'), JSON.stringify(manifest, null, 2));
await writeFile(resolve(out, 'config.js'), `export const SITE_ORIGIN = ${JSON.stringify(url.origin)};\nexport const SCHEMA = ${JSON.stringify(schema)};\n`);
for (const file of ['background.js', 'collector.js']) await copyFile(`extensions/twsport-sync/${file}`, resolve(out, file));
console.log(`套件：${out}\n網站：${url.origin}\nTWSPORT_EXTENSION_ID=${id}\n僅完成打包，尚未安裝或啟用。`);
