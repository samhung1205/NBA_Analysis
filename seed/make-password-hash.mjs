#!/usr/bin/env node
/**
 * 產生與 src/lib/auth.ts 相同格式的密碼雜湊
 * 用法：node seed/make-password-hash.mjs "你的密碼"
 * 輸出：pbkdf2$100000$<salt_b64>$<hash_b64>
 */
import { webcrypto as crypto } from 'node:crypto'

const password = process.argv[2]
if (!password) {
  console.error('用法：node seed/make-password-hash.mjs "<password>"')
  process.exit(1)
}

const ITER = 100_000   // Cloudflare WebCrypto 上限
const salt = crypto.getRandomValues(new Uint8Array(16))
const key = await crypto.subtle.importKey(
  'raw',
  new TextEncoder().encode(password),
  'PBKDF2',
  false,
  ['deriveBits']
)
const bits = await crypto.subtle.deriveBits(
  { name: 'PBKDF2', salt, iterations: ITER, hash: 'SHA-256' },
  key,
  256
)
const b64 = (b) => Buffer.from(b).toString('base64')
console.log(`pbkdf2$${ITER}$${b64(salt)}$${b64(bits)}`)
