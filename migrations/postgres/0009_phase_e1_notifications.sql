-- ============================================================
-- Phase E.1 — Telegram 推播紀錄（notify-v1）
-- ------------------------------------------------------------
-- 純新增，不修改任何既有表。只有 Python 排程器（pipeline/core/notify/）讀寫；網站不使用（D1 不需要對應 migration）。
--
-- notification_events：每一則（嘗試）推播一列。
--   dedup_key UNIQUE   先 INSERT … ON CONFLICT DO NOTHING 再送出 = 「領取」：同一則訊息不會被送兩次
--                      （即使誤開兩個 replica）；送出失敗保留 failed 列，之後最多重試 MAX_ATTEMPTS 次。
--   kind               actionable / daily_summary / system_alert
--   payload            結構化內容（例如系統告警當下的 FAIL 集合），供下一輪比對；不存任何 secret。
-- 未套用時 notify job 只回報 warn 心跳，不送任何訊息。
-- ============================================================

CREATE TABLE IF NOT EXISTS notification_events (
  id            BIGSERIAL PRIMARY KEY,
  channel       TEXT NOT NULL DEFAULT 'telegram',
  kind          TEXT NOT NULL CHECK (kind IN ('actionable', 'daily_summary', 'system_alert')),
  dedup_key     TEXT NOT NULL UNIQUE,
  user_id       INTEGER REFERENCES users(id) ON DELETE SET NULL,
  betting_day   DATE,
  message       TEXT NOT NULL,
  payload       JSONB NOT NULL DEFAULT '{}'::jsonb,
  status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'failed')),
  attempts      INTEGER NOT NULL DEFAULT 0,
  last_error    TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  sent_at       TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_notification_kind_created ON notification_events(kind, created_at DESC);
