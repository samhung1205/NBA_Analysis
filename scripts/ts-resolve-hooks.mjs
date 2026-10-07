// Node ESM resolve hook：讓 `node --experimental-strip-types` 能載入 src/ 內用 bundler 風格寫的匯入
// （'../db' → ../db/index.ts、'./auth' → ./auth.ts）。只給測試腳本用，不影響 vite / wrangler build。
export async function resolve(specifier, context, next) {
  try {
    return await next(specifier, context)
  } catch (e) {
    if (!/^\.\.?\//.test(specifier)) throw e
    for (const suffix of ['.ts', '/index.ts']) {
      try {
        return await next(specifier + suffix, context)
      } catch { /* try next */ }
    }
    throw e
  }
}
