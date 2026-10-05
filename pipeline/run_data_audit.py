#!/usr/bin/env python
"""
Production 資料 / 帳號稽核（唯讀；不刪除任何東西，不印 email 全文 / 密碼 / hash）
------------------------------------------------------------
python run_data_audit.py

輸出 KEEP / REVIEW / SAFE TO REMOVE 三類。刪除一律由你人工核准後自行執行（docs/production-runbook.md 附清理 SQL，
預設用 transaction + ROLLBACK，確認筆數後才改成 COMMIT）。
"""
from __future__ import annotations

import base64
import hashlib
import sys

# README / seed 公開文件列出的 demo 密碼。只用來「離線比對 hash」判斷帳號是否仍可被任何人用公開密碼登入；不會用來登入。
PUBLIC_DEMO_PASSWORD = b"nba12345678"
SEED_MODEL_MARKERS = ("seed",)
LEGACY_SOURCES = {"bbref", "balldontlie", "nba_api"}     # 目前排程不寫入這些來源的心跳（nba_api 為賽程 fallback，僅在 primary 失敗時寫）


def mask_email(e: str) -> str:
    local, _, dom = e.partition("@")
    return f"{local[:2]}***@{dom}"


def uses_public_demo_password(stored_hash: str) -> bool:
    try:
        scheme, it, salt, h = stored_hash.split("$")
        if scheme != "pbkdf2":
            return False
        got = base64.b64encode(hashlib.pbkdf2_hmac("sha256", PUBLIC_DEMO_PASSWORD, base64.b64decode(salt), int(it))).decode()
        return got == h
    except Exception:  # noqa: BLE001
        return False


def classify_user(*, email: str, demo_password: bool, n_bets: int, n_accounts: int, n_events: int, n_snapshots: int) -> tuple[str, str]:
    refs = n_bets + n_accounts + n_events + n_snapshots
    seedish = email.lower().endswith("@example.com") or email.lower().split("@")[0] in ("demo", "test", "tester")
    if demo_password:
        return ("REVIEW" if refs else "SAFE TO REMOVE",
                "密碼 = README 公開的 demo 密碼 → 網站上線前必須刪除或改密碼"
                + (f"（有 {refs} 筆關聯資料，FK RESTRICT 會擋刪除；請先確認這些是否為假資料）" if refs else "；無任何關聯資料"))
    if seedish:
        return ("REVIEW" if refs else "SAFE TO REMOVE",
                "看起來像測試帳號（@example.com / demo / test）" + (f"，有 {refs} 筆關聯資料" if refs else "，無關聯資料"))
    return "KEEP", "看起來是真實帳號" + (f"（{refs} 筆關聯資料）" if refs else "")


def main() -> int:
    import core.config  # noqa: F401  （載入 pipeline/.env）
    import psycopg
    from psycopg.rows import dict_row

    conn = psycopg.connect(core.config.settings.database_url, row_factory=dict_row, prepare_threshold=None,
                           connect_timeout=15)
    conn.read_only = True
    cur = conn.cursor()

    def rows(sql, params=()):
        cur.execute(sql, params)
        return cur.fetchall()

    def n(sql, params=()):
        return int(next(iter(rows(sql, params)[0].values())))

    out: list[tuple[str, str, str]] = []

    for u in rows("SELECT id, email, password_hash, created_at FROM users ORDER BY id"):
        uid = u["id"]
        cat, why = classify_user(
            email=u["email"], demo_password=uses_public_demo_password(u["password_hash"]),
            n_bets=n("SELECT COUNT(*) FROM bets WHERE user_id = %s", (uid,)),
            n_accounts=n("SELECT COUNT(*) FROM bankroll_accounts WHERE user_id = %s", (uid,)),
            n_events=n("SELECT COUNT(*) FROM bet_events WHERE user_id = %s", (uid,)),
            n_snapshots=n("SELECT COUNT(*) FROM decision_snapshots WHERE user_id = %s", (uid,)))
        out.append((cat, f"user #{uid} {mask_email(u['email'])} (created {u['created_at']:%Y-%m-%d})", why))

    for t, label, empty_msg in (
            ("bets", "實際注單 bets", "0 筆：沒有假注單"),
            ("bankroll_accounts", "bankroll 帳戶", "0 筆：尚未設定資金"),
            ("bankroll_ledger", "bankroll ledger", "0 筆"),
            ("decision_snapshots", "decision snapshots", "0 筆（排程器上線後才會產生）"),
            ("paper_strategy_decisions", "paper decisions", "0 筆（2026-27 prospective 尚未開始累積）"),
            ("odds_snapshots", "盤口快照", "0 筆（尚無任何真實盤口；沒有 seed 殘留）")):
        c = n(f"SELECT COUNT(*) FROM {t}")
        out.append(("KEEP", label, empty_msg if c == 0 else f"{c} 筆（若非預期，請人工檢視來源）"))

    seed_models = rows("SELECT model_version, COUNT(*) AS c FROM model_metrics WHERE " +
                       " OR ".join(["model_version ILIKE %s"] * len(SEED_MODEL_MARKERS)) + " GROUP BY model_version",
                       tuple(f"%{m}%" for m in SEED_MODEL_MARKERS))
    for r in seed_models:
        out.append(("SAFE TO REMOVE", f"model_metrics {r['model_version']} × {r['c']}",
                    "seed 假績效，會顯示在網站「模型績效」頁；無 FK 參照"))
    n_seed_pred = n("SELECT COUNT(*) FROM predictions WHERE model_version ILIKE '%%seed%%'")
    out.append(("SAFE TO REMOVE" if n_seed_pred else "KEEP", f"predictions model_version 含 seed × {n_seed_pred}",
                "seed 假預測" if n_seed_pred else "沒有 seed 預測"))
    for r in rows("SELECT model_version, COUNT(*) AS c FROM predictions WHERE model_version NOT ILIKE '%%seed%%' GROUP BY 1 ORDER BY 1"):
        out.append(("KEEP", f"predictions {r['model_version']} × {r['c']}",
                    "歷史（回測 / 舊版）預測；不是 ml-v2 prospective，不得當作 ROI 或 prospective 證據"))

    n_seed_games = n("SELECT COUNT(*) FROM games WHERE nba_game_id IS NULL OR nba_game_id ILIKE '%%seed%%'")
    out.append(("SAFE TO REMOVE" if n_seed_games else "KEEP", f"games 缺 nba_game_id / seed × {n_seed_games}",
                "疑似 seed 賽事" if n_seed_games else "賽事皆有 NBA 官方 id"))

    for r in rows("SELECT source_key, last_status, last_attempt_at FROM data_sources ORDER BY source_key"):
        if r["source_key"] in LEGACY_SOURCES:
            out.append(("REVIEW", f"data_sources.{r['source_key']}",
                        f"最後 {r['last_attempt_at']:%Y-%m-%d}；目前排程不會更新，會在系統狀態頁顯示為過期（可保留或刪除該列）"))
        elif r["source_key"] in ("twsport", "oddsapi"):
            out.append(("KEEP", f"data_sources.{r['source_key']}", "seed 心跳；排程器上線後第一次執行就會覆寫"))

    order = {"SAFE TO REMOVE": 0, "REVIEW": 1, "KEEP": 2}
    for cat in ("SAFE TO REMOVE", "REVIEW", "KEEP"):
        print(f"\n== {cat} ==")
        for c, what, why in sorted(out, key=lambda x: order[x[0]]):
            if c == cat:
                print(f"  - {what}: {why}")
    blockers = [o for o in out if o[0] != "KEEP" and o[1].startswith("user #") and "公開的 demo 密碼" in o[2]]
    if blockers:
        print("\n⚠ 網站上線前阻擋項：有帳號仍使用 README 公開的 demo 密碼。")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
