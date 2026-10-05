/**
 * 資料庫抽象層 (DB Adapter)
 * ------------------------------------------------------------
 * 規格書 §0 銜接原則：正式環境必須使用「階段一 (Cloudflare Pages) 與
 * 階段二 (Python 排程/爬蟲) 都連得到」的獨立 Postgres。
 *
 * 因此本層提供統一介面，依環境變數自動選擇驅動：
 *   1. 若設定 DATABASE_URL  → 使用 Postgres（正式環境，階段二共用）
 *   2. 否則若有 D1 binding  → 使用 D1/SQLite（沙盒開發期，方便本機驗證 UI/API）
 *
 * 所有 SQL 一律以 `?` 佔位符撰寫；Postgres adapter 會自動轉為 $1..$n。
 * 業務程式碼（routes/pages）只依賴此介面，切換資料庫時無需改動任何一行。
 */

export interface Db {
  /** 查詢多列 */
  all<T = any>(sql: string, params?: unknown[]): Promise<T[]>
  /** 查詢單列（無資料回傳 null） */
  one<T = any>(sql: string, params?: unknown[]): Promise<T | null>
  /** 執行寫入，回傳 lastInsertId（若可取得） */
  run(sql: string, params?: unknown[]): Promise<{ lastInsertId?: number | string }>
  /**
   * 多個寫入語句在同一個交易內執行（全部成功或全部失敗），回傳每個語句影響的列數。
   * D1：batch()（SQLite 交易）；Postgres：BEGIN … COMMIT。
   * D.5 的樂觀鎖（UPDATE … WHERE risk_state_version = ?）與後續 INSERT 一起送出，避免兩個請求同時用掉同一份額度。
   */
  atomic(steps: { sql: string; params?: unknown[] }[]): Promise<number[]>
  /** 目前使用的驅動名稱，供 /api/system/status 顯示 */
  readonly driver: 'postgres' | 'd1'
}

/* ------------------------------------------------------------------ */
/* D1 / SQLite adapter                                                 */
/* ------------------------------------------------------------------ */

class D1Db implements Db {
  readonly driver = 'd1' as const
  constructor(private d1: D1Database) {}

  async all<T>(sql: string, params: unknown[] = []): Promise<T[]> {
    const stmt = this.d1.prepare(sql).bind(...(params as any[]))
    const { results } = await stmt.all<T>()
    return (results ?? []) as T[]
  }

  async one<T>(sql: string, params: unknown[] = []): Promise<T | null> {
    const rows = await this.all<T>(sql, params)
    return rows.length ? rows[0] : null
  }

  async run(sql: string, params: unknown[] = []) {
    const res = await this.d1.prepare(sql).bind(...(params as any[])).run()
    return { lastInsertId: res.meta?.last_row_id }
  }

  async atomic(steps: { sql: string; params?: unknown[] }[]) {
    const stmts = steps.map((s) => this.d1.prepare(s.sql).bind(...((s.params ?? []) as any[])))
    const res = await this.d1.batch(stmts)
    return res.map((r: any) => Number(r?.meta?.changes ?? 0))
  }
}

/* ------------------------------------------------------------------ */
/* Postgres adapter                                                    */
/* ------------------------------------------------------------------ */

/** 將 `?` 佔位符轉為 Postgres 的 $1, $2 ... */
export function toPgPlaceholders(sql: string): string {
  let i = 0
  return sql.replace(/\?/g, () => `$${++i}`)
}

class PostgresDb implements Db {
  readonly driver = 'postgres' as const
  constructor(private sql: any) {}

  async all<T>(rawSql: string, params: unknown[] = []): Promise<T[]> {
    const text = toPgPlaceholders(rawSql)
    const rows = await this.sql.unsafe(text, params as any[])
    return rows as unknown as T[]
  }

  async one<T>(rawSql: string, params: unknown[] = []): Promise<T | null> {
    const rows = await this.all<T>(rawSql, params)
    return rows.length ? rows[0] : null
  }

  async run(rawSql: string, params: unknown[] = []) {
    // Postgres 取得新 id 需靠 RETURNING id；呼叫端若需要 id 請自行加上
    const rows = await this.all<any>(rawSql, params)
    const first = rows?.[0]
    return { lastInsertId: first?.id }
  }

  async atomic(steps: { sql: string; params?: unknown[] }[]) {
    return this.sql.begin(async (tx: any) => {
      const counts: number[] = []
      for (const s of steps) {
        const r = await tx.unsafe(toPgPlaceholders(s.sql), (s.params ?? []) as any[])
        counts.push(Number(r?.count ?? 0))
      }
      return counts
    })
  }
}

/* ------------------------------------------------------------------ */
/* Factory                                                             */
/* ------------------------------------------------------------------ */

export type AppBindings = {
  DB?: D1Database
  DATABASE_URL?: string
  SESSION_SECRET?: string
  ALLOW_REGISTRATION?: string
}

export async function getDb(env: AppBindings): Promise<Db> {
  if (env.DATABASE_URL) {
    // 每個請求建立獨立連線，不可跨請求快取：Cloudflare Workers 禁止
    // 一個請求沿用另一個請求開啟的 socket I/O，跨請求共用連線物件
    // 會被 runtime 判定為掛起（實測會直接 500 "Worker's code had hung"）。
    const { default: postgres } = await import('postgres')
    const pgClient = postgres(env.DATABASE_URL, {
      max: 1,
      idle_timeout: 5,
      connect_timeout: 10,
      prepare: false, // 相容 Supabase pgbouncer
    })
    return new PostgresDb(pgClient)
  }
  if (env.DB) return new D1Db(env.DB)
  throw new Error(
    '未設定資料庫：請提供 DATABASE_URL (Postgres) 或 D1 binding「DB」。詳見 .env.example'
  )
}

/** 供 SQL 依方言微調（少數必要處，例如 boolean 與 JSON 欄位） */
export function isPg(db: Db) {
  return db.driver === 'postgres'
}
