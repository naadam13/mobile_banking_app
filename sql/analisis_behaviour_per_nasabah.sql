-- =====================================================================
--  ANALISIS BEHAVIOUR PER NASABAH  (mobile banking v2)
--  Database : hasil generate_mobile_banking.py (schema "mbanking", PostgreSQL 13+)
--
--  PRINSIP: SETIAP QUERY MENGEMBALIKAN BARIS PER NASABAH.
--    * Kolom pertama selalu customer_id, lalu full_name dan occupation.
--    * Satu baris = satu nasabah, atau satu nasabah x (bulan | kuartal | produk | fitur | kartu |
--      pinjaman | jadwal) bila yang dibahas memang berdimensi itu. Tidak ada baris ringkasan agregat.
--    * Untuk melihat satu nasabah saja:   SELECT * FROM vp_arus WHERE customer_id = 88;
--      (query tanpa view dibungkus:        SELECT * FROM ( <query> ) q WHERE customer_id = 88;)
--    * Ringkasan lintas nasabah (misalnya rata-rata per pekerjaan) tinggal GROUP BY dari hasil di sini.
--
--  Cara pakai: jalankan seluruh file sekali (membuat view bantu "v_*" dan view hasil "vp_*"),
--              lalu salin query mana saja. Setiap blok berdiri sendiri.
--
--  Isi
--    A. Jenis & waktu transaksi per nasabah                              (A1-A4)
--    B. Pemasukan vs pengeluaran per nasabah                             (B1-B5)
--    C. Fitur: dipakai vs hanya dibuka, adopsi, sesi                     (C1-C5)
--    D. Gajian & perilaku di sekitar gajian                              (D1-D5)
--    E. Cicilan & pembayaran rutin                                       (E1-E6)
--    F. Jadwal Bayar (scheduler)                                         (F1-F4)
--    G. Paylater & KTA                                                   (G1-G4)
--    H. Kartu, Visa & cashless                                           (H1-H3)
--    I. Engagement: aktivitas, siklus hidup, login, notifikasi, gagal,
--       RFM, peringatan churn, perangkat, skor kesehatan                 (I1-I9)
--    Z. Customer 360: satu baris per nasabah, gabungan semua di atas     (Z1)
--
--  Konvensi
--    * Waktu selalu dikonversi ke WIB (AT TIME ZONE 'Asia/Jakarta').
--    * Data berakhir 9 Okt 2026. "12 bulan terakhir" = 10 Okt 2025 - 9 Okt 2026;
--      "12 bulan penuh" = Okt 2025 - Sep 2026 (Okt 2026 tidak penuh, dikecualikan dari analisis bulanan).
--    * "Transaksi" = status SUCCESS, kecuali query yang memang membahas kegagalan.
--    * Pemasukan   = SALARY_CREDIT + TRANSFER_IN + CASH_DEPOSIT + INTEREST
--                    (tidak termasuk saldo awal/migrasi, pencairan pinjaman, pindah dana antar rekening sendiri).
--    * Pengeluaran = semua debit (amount + fee) kecuali pindah dana ke rekening sendiri (tabungan).
-- =====================================================================
SET search_path = mbanking, public;

-- =====================================================================
-- 0. VIEW BANTU
-- =====================================================================
DROP VIEW IF EXISTS v_trx_ok, v_cust_month, v_session, v_user, v_nasabah, v_payday, v_inst, v_sched_cicilan CASCADE;

-- v_nasabah : profil dasar satu baris per nasabah (dipakai di hampir semua query)
CREATE VIEW v_nasabah AS
SELECT c.customer_id, u.user_id, c.full_name, c.occupation, c.city, c.monthly_income,
       u.status AS user_status, u.registered_at, u.push_enabled, u.biometric_enabled,
       date_part('year', age(DATE '2026-10-09', c.birth_date))::int AS age_years,
       CASE WHEN date_part('year', age(DATE '2026-10-09', c.birth_date)) < 25 THEN '1 <25'
            WHEN date_part('year', age(DATE '2026-10-09', c.birth_date)) < 35 THEN '2 25-34'
            WHEN date_part('year', age(DATE '2026-10-09', c.birth_date)) < 45 THEN '3 35-44'
            ELSE '4 45+' END AS age_band
FROM customers c
LEFT JOIN mobile_users u ON u.customer_id = c.customer_id;

-- v_trx_ok : transaksi sukses + kolom waktu WIB + klasifikasi arus kas & kelompok belanja
CREATE VIEW v_trx_ok AS
SELECT t.transaction_id, t.account_id, a.customer_id, t.transaction_type, t.direction,
       t.amount, t.fee, t.channel, t.device_id, t.card_id,
       t.merchant_name, t.merchant_category, t.biller_id,
       t.counterparty_bank_code, t.counterparty_account, t.counterparty_name,
       t.loan_id, t.scheduled_payment_id, t.description, t.created_at,
       (t.created_at AT TIME ZONE 'Asia/Jakarta')::date                               AS trx_date,
       date_trunc('month', t.created_at AT TIME ZONE 'Asia/Jakarta')::date            AS trx_month,
       extract(hour  FROM t.created_at AT TIME ZONE 'Asia/Jakarta')::int              AS trx_hour,
       extract(isodow FROM t.created_at AT TIME ZONE 'Asia/Jakarta')::int             AS trx_isodow,
       CASE WHEN t.description LIKE 'SALDO AWAL%' OR t.description LIKE 'SETORAN AWAL%' THEN 'OPENING'
            WHEN t.transaction_type IN ('SAVINGS_IN','SAVINGS_OUT')                     THEN 'INTERNAL'
            WHEN t.transaction_type = 'LOAN_DISBURSEMENT'                               THEN 'LOAN_IN'
            WHEN t.direction = 'C'                                                      THEN 'INCOME'
            ELSE 'EXPENSE' END                                                          AS flow,
       CASE WHEN t.transaction_type IN ('QRIS_PAYMENT','EWALLET_TOPUP','PULSA_DATA')    THEN 'CASHLESS_APP'
            WHEN t.transaction_type IN ('TRANSFER_BANK','TRANSFER_INTERNAL')            THEN 'TRANSFER'
            WHEN t.transaction_type = 'BILL_PAYMENT'                                    THEN 'TAGIHAN'
            WHEN t.transaction_type LIKE 'CARD\_%'                                      THEN 'KARTU'
            WHEN t.transaction_type = 'ATM_WITHDRAWAL'                                  THEN 'TUNAI'
            WHEN t.transaction_type = 'LOAN_INSTALLMENT'                                THEN 'CICILAN'
            WHEN t.transaction_type = 'ADMIN_FEE'                                       THEN 'BIAYA'
            ELSE 'LAINNYA' END                                                          AS spend_group
FROM transactions t
JOIN accounts a ON a.account_id = t.account_id
WHERE t.status = 'SUCCESS';

-- v_cust_month : arus kas bulanan per nasabah
CREATE VIEW v_cust_month AS
SELECT customer_id, trx_month,
       COALESCE(sum(amount)       FILTER (WHERE flow = 'INCOME'), 0)                              AS income,
       COALESCE(sum(amount + fee) FILTER (WHERE flow = 'EXPENSE'), 0)                             AS expense,
       COALESCE(sum(amount + fee) FILTER (WHERE flow = 'EXPENSE' AND spend_group = 'CASHLESS_APP'), 0) AS exp_cashless,
       COALESCE(sum(amount + fee) FILTER (WHERE flow = 'EXPENSE' AND spend_group = 'KARTU'), 0)   AS exp_kartu,
       COALESCE(sum(amount + fee) FILTER (WHERE flow = 'EXPENSE' AND spend_group = 'TRANSFER'), 0) AS exp_transfer,
       COALESCE(sum(amount + fee) FILTER (WHERE flow = 'EXPENSE' AND spend_group = 'TAGIHAN'), 0) AS exp_tagihan,
       COALESCE(sum(amount + fee) FILTER (WHERE flow = 'EXPENSE' AND spend_group = 'TUNAI'), 0)   AS exp_tunai,
       COALESCE(sum(amount + fee) FILTER (WHERE flow = 'EXPENSE' AND spend_group = 'CICILAN'), 0) AS exp_cicilan,
       COALESCE(sum(amount + fee) FILTER (WHERE flow = 'EXPENSE' AND spend_group = 'BIAYA'), 0)   AS exp_biaya,
       COALESCE(sum(amount)       FILTER (WHERE flow = 'LOAN_IN'), 0)                             AS loan_in,
       count(*) FILTER (WHERE flow = 'EXPENSE')                                                   AS n_expense_trx
FROM v_trx_ok
GROUP BY customer_id, trx_month;

-- v_session : satu baris per sesi login sukses + ringkasan event layar
CREATE VIEW v_session AS
SELECT l.login_id, l.user_id, l.device_id, l.login_at,
       (l.login_at AT TIME ZONE 'Asia/Jakarta')::date                          AS login_date,
       date_trunc('month', l.login_at AT TIME ZONE 'Asia/Jakarta')::date       AS login_month,
       extract(hour FROM l.login_at AT TIME ZONE 'Asia/Jakarta')::int          AS login_hour,
       l.login_method,
       count(e.event_id)                                                       AS n_events,
       count(DISTINCT e.feature_code)                                          AS n_features,
       count(*) FILTER (WHERE e.event_type = 'COMPLETE' AND f.category IN ('TRANSACTION','CREDIT','SETTING')) AS n_complete,
       count(*) FILTER (WHERE e.event_type = 'COMPLETE' AND f.category = 'TRANSACTION')                       AS n_trx_complete,
       max(e.event_at)                                                         AS last_event_at
FROM login_logs l
LEFT JOIN app_events e   ON e.login_id = l.login_id
LEFT JOIN app_features f ON f.feature_code = e.feature_code
WHERE l.status = 'SUCCESS'
GROUP BY l.login_id, l.user_id, l.device_id, l.login_at, l.login_method;

-- =====================================================================
-- A. JENIS & WAKTU TRANSAKSI PER NASABAH
-- =====================================================================
-- A1. Profil jenis transaksi: tiga jenis yang paling sering dipakai tiap nasabah (12 bulan terakhir, transaksi keluar)
CREATE OR REPLACE VIEW vp_jenis AS
WITH t AS (
    SELECT customer_id, transaction_type, count(*) AS cnt
    FROM v_trx_ok
    WHERE flow = 'EXPENSE' AND trx_date >= DATE '2025-10-10'
    GROUP BY 1, 2
), r AS (
    SELECT customer_id, transaction_type, cnt,
           sum(cnt)   OVER (PARTITION BY customer_id) AS tot,
           row_number() OVER (PARTITION BY customer_id ORDER BY cnt DESC, transaction_type) AS rn
    FROM t
), s AS (
    SELECT customer_id, max(tot)::int AS trx_keluar_12bln, count(*) AS jml_jenis_dipakai,
           max(transaction_type) FILTER (WHERE rn = 1) AS jenis_1,
           round(100.0 * max(cnt) FILTER (WHERE rn = 1) / max(tot), 1) AS pct_1,
           max(transaction_type) FILTER (WHERE rn = 2) AS jenis_2,
           round(100.0 * max(cnt) FILTER (WHERE rn = 2) / max(tot), 1) AS pct_2,
           max(transaction_type) FILTER (WHERE rn = 3) AS jenis_3,
           round(100.0 * max(cnt) FILTER (WHERE rn = 3) / max(tot), 1) AS pct_3
    FROM r GROUP BY customer_id
)
SELECT n.customer_id, n.full_name, n.occupation, n.age_band,
       s.trx_keluar_12bln, s.jml_jenis_dipakai, s.jenis_1, s.pct_1, s.jenis_2, s.pct_2, s.jenis_3, s.pct_3
FROM v_nasabah n
LEFT JOIN s ON s.customer_id = n.customer_id;

SELECT * FROM vp_jenis ORDER BY customer_id;

-- A2. Jenis transaksi dominan per kuartal (satu baris per nasabah x kuartal), beserta perubahannya dari kuartal sebelumnya
WITH t AS (
    SELECT customer_id, date_trunc('quarter', trx_date)::date AS kuartal, transaction_type, count(*) AS cnt
    FROM v_trx_ok
    WHERE flow = 'EXPENSE' AND trx_date < DATE '2026-10-01'
    GROUP BY 1, 2, 3
), r AS (
    SELECT customer_id, kuartal, transaction_type, cnt,
           sum(cnt)   OVER (PARTITION BY customer_id, kuartal) AS tot,
           count(*)   OVER (PARTITION BY customer_id, kuartal) AS jml_jenis,
           row_number() OVER (PARTITION BY customer_id, kuartal ORDER BY cnt DESC, transaction_type) AS rn
    FROM t
)
SELECT n.customer_id, n.full_name, n.occupation, r.kuartal,
       r.tot::int AS total_trx_keluar, r.jml_jenis AS jml_jenis_dipakai,
       r.transaction_type AS jenis_dominan, round(100.0 * r.cnt / r.tot, 1) AS pct_dominan,
       lag(r.transaction_type) OVER (PARTITION BY r.customer_id ORDER BY r.kuartal) AS jenis_dominan_kuartal_lalu,
       CASE WHEN lag(r.transaction_type) OVER (PARTITION BY r.customer_id ORDER BY r.kuartal) IS NULL THEN '-'
            WHEN lag(r.transaction_type) OVER (PARTITION BY r.customer_id ORDER BY r.kuartal) = r.transaction_type THEN 'tetap'
            ELSE 'BERGESER' END AS perubahan
FROM r
JOIN v_nasabah n ON n.customer_id = r.customer_id
WHERE r.rn = 1
ORDER BY n.customer_id, r.kuartal;

-- A3. Rincian per kelompok jenis transaksi: 12 bulan terakhir vs 12 bulan sebelumnya (satu baris per nasabah)
SELECT n.customer_id, n.full_name, n.occupation,
       count(*) FILTER (WHERE t.trx_date >= DATE '2025-10-10' AND t.transaction_type = 'QRIS_PAYMENT')   AS qris,
       count(*) FILTER (WHERE t.trx_date >= DATE '2025-10-10' AND t.transaction_type = 'EWALLET_TOPUP')  AS topup_ewallet,
       count(*) FILTER (WHERE t.trx_date >= DATE '2025-10-10' AND t.transaction_type = 'PULSA_DATA')     AS pulsa_data,
       count(*) FILTER (WHERE t.trx_date >= DATE '2025-10-10' AND t.spend_group = 'TRANSFER')            AS transfer,
       count(*) FILTER (WHERE t.trx_date >= DATE '2025-10-10' AND t.spend_group = 'TAGIHAN')             AS tagihan,
       count(*) FILTER (WHERE t.trx_date >= DATE '2025-10-10' AND t.spend_group = 'KARTU')               AS kartu,
       count(*) FILTER (WHERE t.trx_date >= DATE '2025-10-10' AND t.spend_group = 'TUNAI')               AS tarik_tunai,
       count(*) FILTER (WHERE t.trx_date >= DATE '2025-10-10' AND t.spend_group = 'CICILAN')             AS cicilan,
       count(*) FILTER (WHERE t.trx_date >= DATE '2025-10-10')                                           AS total_12bln,
       count(*) FILTER (WHERE t.trx_date <  DATE '2025-10-10')                                           AS total_12bln_sebelumnya,
       -- perubahan hanya dihitung bila 12 bulan sebelumnya punya >= 20 transaksi (agar nasabah baru tidak menghasilkan angka ribuan persen)
       CASE WHEN count(*) FILTER (WHERE t.trx_date < DATE '2025-10-10') >= 20 THEN
            round(100.0 * (count(*) FILTER (WHERE t.trx_date >= DATE '2025-10-10')
                         - count(*) FILTER (WHERE t.trx_date < DATE '2025-10-10'))
                / count(*) FILTER (WHERE t.trx_date < DATE '2025-10-10'), 1) END                         AS perubahan_pct
FROM v_nasabah n
LEFT JOIN v_trx_ok t ON t.customer_id = n.customer_id AND t.flow = 'EXPENSE' AND t.trx_date >= DATE '2024-10-10'
GROUP BY n.customer_id, n.full_name, n.occupation
ORDER BY n.customer_id;

-- A4. Kebiasaan waktu bertransaksi per nasabah (12 bulan terakhir, transaksi keluar yang dilakukan nasabah sendiri)
SELECT n.customer_id, n.full_name, n.occupation,
       count(t.*)                                                                          AS total_trx,
       round(100.0 * count(*) FILTER (WHERE t.trx_hour BETWEEN 0 AND 4)   / NULLIF(count(t.*), 0), 1) AS pct_dini_hari,
       round(100.0 * count(*) FILTER (WHERE t.trx_hour BETWEEN 5 AND 10)  / NULLIF(count(t.*), 0), 1) AS pct_pagi,
       round(100.0 * count(*) FILTER (WHERE t.trx_hour BETWEEN 11 AND 14) / NULLIF(count(t.*), 0), 1) AS pct_siang,
       round(100.0 * count(*) FILTER (WHERE t.trx_hour BETWEEN 15 AND 18) / NULLIF(count(t.*), 0), 1) AS pct_sore,
       round(100.0 * count(*) FILTER (WHERE t.trx_hour BETWEEN 19 AND 23) / NULLIF(count(t.*), 0), 1) AS pct_malam,
       mode() WITHIN GROUP (ORDER BY t.trx_hour)                                           AS jam_puncak,
       (ARRAY['Senin','Selasa','Rabu','Kamis','Jumat','Sabtu','Minggu'])[mode() WITHIN GROUP (ORDER BY t.trx_isodow)] AS hari_puncak,
       round(100.0 * count(*) FILTER (WHERE t.trx_isodow IN (6, 7)) / NULLIF(count(t.*), 0), 1)       AS pct_akhir_pekan,
       round(100.0 * count(*) FILTER (WHERE extract(day FROM t.trx_date) <= 5)  / NULLIF(count(t.*), 0), 1) AS pct_tgl_1_5,
       round(100.0 * count(*) FILTER (WHERE extract(day FROM t.trx_date) >= 25) / NULLIF(count(t.*), 0), 1) AS pct_tgl_25_akhir
FROM v_nasabah n
LEFT JOIN v_trx_ok t ON t.customer_id = n.customer_id AND t.flow = 'EXPENSE'
                    AND t.channel <> 'SYSTEM' AND t.trx_date >= DATE '2025-10-10'
GROUP BY n.customer_id, n.full_name, n.occupation
ORDER BY n.customer_id;

-- =====================================================================
-- B. PEMASUKAN vs PENGELUARAN PER NASABAH
-- =====================================================================
-- B1. Arus kas bulanan (satu baris per nasabah x bulan, Jan 2024 - Sep 2026) + berapa bulan berturut-turut defisit
WITH m AS (
    SELECT cm.*, (cm.expense > cm.income) AS defisit
    FROM v_cust_month cm
    WHERE cm.trx_month <= DATE '2026-09-01'
), g AS (
    SELECT m.*,
           row_number() OVER (PARTITION BY customer_id ORDER BY trx_month)
         - row_number() OVER (PARTITION BY customer_id, defisit ORDER BY trx_month) AS pulau
    FROM m
)
SELECT n.customer_id, n.full_name, n.occupation, g.trx_month AS bulan,
       round(g.income)               AS pemasukan,
       round(g.expense)              AS pengeluaran,
       round(g.income - g.expense)   AS selisih,
       round(g.expense / NULLIF(g.income, 0), 2) AS rasio_keluar_masuk,
       CASE WHEN g.income = 0 AND g.expense > 0 THEN 'TANPA PEMASUKAN'
            WHEN g.expense > g.income * 1.05 THEN 'DEFISIT'
            WHEN g.expense > g.income * 0.90 THEN 'IMPAS'
            ELSE 'SURPLUS' END       AS status_bulan,
       CASE WHEN g.defisit THEN row_number() OVER (PARTITION BY g.customer_id, g.defisit, g.pulau ORDER BY g.trx_month) ELSE 0 END AS defisit_berturut,
       round(g.exp_cashless) AS belanja_cashless, round(g.exp_kartu) AS belanja_kartu, round(g.exp_transfer) AS transfer_keluar,
       round(g.exp_tagihan)  AS tagihan,          round(g.exp_tunai) AS tarik_tunai,    round(g.exp_cicilan)  AS cicilan,
       round(g.loan_in)      AS pinjaman_cair
FROM g
JOIN v_nasabah n ON n.customer_id = g.customer_id
ORDER BY n.customer_id, g.trx_month;

-- B2. Ringkasan arus kas 12 bulan penuh (Okt 2025 - Sep 2026): satu baris per nasabah
CREATE OR REPLACE VIEW vp_arus AS
WITH m AS (
    SELECT cm.*, (cm.expense > cm.income) AS defisit
    FROM v_cust_month cm
    WHERE cm.trx_month BETWEEN DATE '2025-10-01' AND DATE '2026-09-01'
), g AS (
    SELECT customer_id, defisit,
           row_number() OVER (PARTITION BY customer_id ORDER BY trx_month)
         - row_number() OVER (PARTITION BY customer_id, defisit ORDER BY trx_month) AS pulau
    FROM m
), run AS (
    SELECT customer_id, max(c) AS maks_defisit_berturut
    FROM (SELECT customer_id, pulau, count(*) AS c FROM g WHERE defisit GROUP BY 1, 2) x
    GROUP BY customer_id
), s AS (
    SELECT customer_id,
           count(*)                                            AS bulan_ada_data,
           round(avg(income))                                  AS rata_pemasukan,
           round(avg(expense))                                 AS rata_pengeluaran,
           round(sum(expense) / NULLIF(sum(income), 0), 2)     AS rasio_keluar_masuk,
           count(*) FILTER (WHERE defisit)                     AS bulan_defisit,
           round(stddev(income) / NULLIF(avg(income), 0), 2)   AS variasi_pemasukan,
           round(100.0 * sum(exp_cashless) / NULLIF(sum(expense), 0), 1) AS pct_cashless,
           round(100.0 * sum(exp_kartu)    / NULLIF(sum(expense), 0), 1) AS pct_kartu,
           round(100.0 * sum(exp_transfer) / NULLIF(sum(expense), 0), 1) AS pct_transfer,
           round(100.0 * sum(exp_tagihan)  / NULLIF(sum(expense), 0), 1) AS pct_tagihan,
           round(100.0 * sum(exp_tunai)    / NULLIF(sum(expense), 0), 1) AS pct_tunai,
           round(100.0 * sum(exp_cicilan)  / NULLIF(sum(expense), 0), 1) AS pct_cicilan
    FROM m GROUP BY customer_id
)
SELECT n.customer_id, n.full_name, n.occupation, n.monthly_income AS penghasilan_terdaftar,
       s.bulan_ada_data, s.rata_pemasukan, s.rata_pengeluaran, s.rasio_keluar_masuk,
       round(s.rata_pemasukan / NULLIF(n.monthly_income, 0), 2) AS pemasukan_vs_terdaftar,
       s.bulan_defisit, COALESCE(r.maks_defisit_berturut, 0) AS maks_defisit_berturut, s.variasi_pemasukan,
       s.pct_cashless, s.pct_kartu, s.pct_transfer, s.pct_tagihan, s.pct_tunai, s.pct_cicilan,
       CASE WHEN s.customer_id IS NULL                  THEN 'Tidak ada data'
            WHEN s.bulan_defisit >= 6                   THEN 'Defisit kronis'
            WHEN s.rasio_keluar_masuk < 0.70            THEN 'Hemat'
            WHEN s.rasio_keluar_masuk < 0.95            THEN 'Seimbang'
            WHEN s.rasio_keluar_masuk < 1.10            THEN 'Tipis'
            ELSE 'Boros' END                           AS kategori_arus
FROM v_nasabah n
LEFT JOIN s   ON s.customer_id = n.customer_id
LEFT JOIN run r ON r.customer_id = n.customer_id;

SELECT * FROM vp_arus ORDER BY customer_id;

-- B3. Dari mana uang masuk (12 bulan terakhir): satu baris per nasabah
SELECT customer_id, full_name, occupation, total_masuk, pct_gaji, pct_transfer_masuk, pct_setoran_tunai, pct_bunga,
       pengirim_unik, bulan_ada_pemasukan,
       CASE GREATEST(pct_gaji, pct_transfer_masuk, pct_setoran_tunai, pct_bunga)
            WHEN pct_gaji            THEN 'Gaji'
            WHEN pct_transfer_masuk  THEN 'Transfer masuk'
            WHEN pct_setoran_tunai   THEN 'Setoran tunai'
            ELSE 'Bunga' END AS sumber_utama
FROM (
    SELECT n.customer_id, n.full_name, n.occupation,
           round(COALESCE(sum(t.amount), 0)) AS total_masuk,
           COALESCE(round(100.0 * sum(t.amount) FILTER (WHERE t.transaction_type = 'SALARY_CREDIT') / NULLIF(sum(t.amount), 0), 1), 0) AS pct_gaji,
           COALESCE(round(100.0 * sum(t.amount) FILTER (WHERE t.transaction_type = 'TRANSFER_IN')   / NULLIF(sum(t.amount), 0), 1), 0) AS pct_transfer_masuk,
           COALESCE(round(100.0 * sum(t.amount) FILTER (WHERE t.transaction_type = 'CASH_DEPOSIT')  / NULLIF(sum(t.amount), 0), 1), 0) AS pct_setoran_tunai,
           COALESCE(round(100.0 * sum(t.amount) FILTER (WHERE t.transaction_type = 'INTEREST')      / NULLIF(sum(t.amount), 0), 1), 0) AS pct_bunga,
           count(DISTINCT t.counterparty_name) FILTER (WHERE t.transaction_type = 'TRANSFER_IN') AS pengirim_unik,
           count(DISTINCT t.trx_month) AS bulan_ada_pemasukan
    FROM v_nasabah n
    LEFT JOIN v_trx_ok t ON t.customer_id = n.customer_id AND t.flow = 'INCOME' AND t.trx_date >= DATE '2025-10-10'
    GROUP BY n.customer_id, n.full_name, n.occupation
) x
ORDER BY customer_id;

-- B4. Tiga kategori merchant tempat nasabah paling banyak membelanjakan uang (12 bulan terakhir)
WITH k AS (
    SELECT customer_id, merchant_category, sum(amount) AS nominal, count(*) AS trx
    FROM v_trx_ok
    WHERE flow = 'EXPENSE' AND merchant_category IS NOT NULL AND trx_date >= DATE '2025-10-10'
    GROUP BY 1, 2
), r AS (
    SELECT *, sum(nominal) OVER (PARTITION BY customer_id) AS tot,
           count(*) OVER (PARTITION BY customer_id) AS jml_kategori,
           row_number() OVER (PARTITION BY customer_id ORDER BY nominal DESC) AS rn
    FROM k
)
SELECT n.customer_id, n.full_name, n.occupation,
       max(r.jml_kategori) AS jml_kategori, round(max(r.tot)) AS total_belanja_merchant,
       max(r.merchant_category) FILTER (WHERE rn = 1) AS kategori_1, round(100.0 * max(r.nominal) FILTER (WHERE rn = 1) / max(r.tot), 1) AS pct_1,
       max(r.merchant_category) FILTER (WHERE rn = 2) AS kategori_2, round(100.0 * max(r.nominal) FILTER (WHERE rn = 2) / max(r.tot), 1) AS pct_2,
       max(r.merchant_category) FILTER (WHERE rn = 3) AS kategori_3, round(100.0 * max(r.nominal) FILTER (WHERE rn = 3) / max(r.tot), 1) AS pct_3
FROM v_nasabah n
LEFT JOIN r ON r.customer_id = n.customer_id
GROUP BY n.customer_id, n.full_name, n.occupation
ORDER BY n.customer_id;

-- B5. Tren 3 bulan terakhir (Jul-Sep 2026) vs 3 bulan sebelumnya (Apr-Jun 2026): peringatan lonjakan pengeluaran / turunnya pemasukan
SELECT customer_id, full_name, occupation, pemasukan_3b, pemasukan_3b_lalu, pengeluaran_3b, pengeluaran_3b_lalu,
       pemasukan_berubah_pct, pengeluaran_berubah_pct,
       CASE WHEN pengeluaran_berubah_pct > 30 AND COALESCE(pemasukan_berubah_pct, 0) < 10 THEN 'LONJAKAN PENGELUARAN'
            WHEN pemasukan_berubah_pct < -30                                              THEN 'PEMASUKAN TURUN'
            WHEN pengeluaran_berubah_pct < -30                                            THEN 'PENGELUARAN TURUN'
            ELSE 'NORMAL' END AS peringatan
FROM (
    SELECT n.customer_id, n.full_name, n.occupation,
           round(COALESCE(sum(c.income)  FILTER (WHERE c.trx_month >= DATE '2026-07-01'), 0)) AS pemasukan_3b,
           round(COALESCE(sum(c.income)  FILTER (WHERE c.trx_month <  DATE '2026-07-01'), 0)) AS pemasukan_3b_lalu,
           round(COALESCE(sum(c.expense) FILTER (WHERE c.trx_month >= DATE '2026-07-01'), 0)) AS pengeluaran_3b,
           round(COALESCE(sum(c.expense) FILTER (WHERE c.trx_month <  DATE '2026-07-01'), 0)) AS pengeluaran_3b_lalu,
           round(100.0 * (sum(c.income)  FILTER (WHERE c.trx_month >= DATE '2026-07-01') - sum(c.income)  FILTER (WHERE c.trx_month < DATE '2026-07-01'))
                 / NULLIF(sum(c.income)  FILTER (WHERE c.trx_month < DATE '2026-07-01'), 0), 1) AS pemasukan_berubah_pct,
           round(100.0 * (sum(c.expense) FILTER (WHERE c.trx_month >= DATE '2026-07-01') - sum(c.expense) FILTER (WHERE c.trx_month < DATE '2026-07-01'))
                 / NULLIF(sum(c.expense) FILTER (WHERE c.trx_month < DATE '2026-07-01'), 0), 1) AS pengeluaran_berubah_pct
    FROM v_nasabah n
    LEFT JOIN v_cust_month c ON c.customer_id = n.customer_id AND c.trx_month BETWEEN DATE '2026-04-01' AND DATE '2026-09-01'
    GROUP BY n.customer_id, n.full_name, n.occupation
) x
ORDER BY customer_id;

-- =====================================================================
-- C. FITUR: DIPAKAI vs HANYA DIBUKA, ADOPSI, SESI  (per nasabah)
--    Status fitur per nasabah:
--      DIPAKAI             : ada event COMPLETE (fitur transaksi/kredit/pengaturan), atau ada INTERACT (fitur informasi)
--      HANYA DIBUKA        : pernah dibuka tetapi tidak pernah diselesaikan / berinteraksi
--      BELUM PERNAH DIBUKA : tidak ada event sama sekali
--    Catatan: CEK_SALDO tidak punya langkah lanjutan, sehingga selalu berstatus HANYA DIBUKA (= dilihat saja).
-- =====================================================================
-- v_nasabah_fitur : grid nasabah x fitur aplikasi (semua fitur muncul, termasuk yang belum pernah dibuka)
CREATE OR REPLACE VIEW v_nasabah_fitur AS
WITH ev AS (
    SELECT u.customer_id, e.feature_code,
           count(*) FILTER (WHERE e.event_type = 'OPEN')     AS n_buka,
           count(*) FILTER (WHERE e.event_type = 'INTERACT') AS n_interaksi,
           count(*) FILTER (WHERE e.event_type = 'COMPLETE') AS n_selesai,
           count(*) FILTER (WHERE e.event_type = 'FAILED')   AS n_gagal,
           count(*) FILTER (WHERE e.event_type = 'OPEN' AND e.event_at >= TIMESTAMPTZ '2026-04-12') AS n_buka_180h,
           min(e.event_at) FILTER (WHERE e.event_type = 'OPEN') AS pertama_buka,
           min(e.event_at) FILTER (WHERE e.event_type = 'COMPLETE'
                                      OR (e.event_type = 'INTERACT' AND f.category IN ('INQUIRY','INFO'))) AS pertama_dipakai,
           max(e.event_at) AS terakhir_aktivitas
    FROM app_events e
    JOIN mobile_users u ON u.user_id = e.user_id
    JOIN app_features f ON f.feature_code = e.feature_code
    GROUP BY u.customer_id, e.feature_code
)
SELECT n.customer_id, n.full_name, n.occupation, f.feature_code, f.category,
       COALESCE(ev.n_buka, 0) AS n_buka, COALESCE(ev.n_interaksi, 0) AS n_interaksi,
       COALESCE(ev.n_selesai, 0) AS n_selesai, COALESCE(ev.n_gagal, 0) AS n_gagal,
       COALESCE(ev.n_buka_180h, 0) AS n_buka_180h,
       ev.pertama_buka, ev.pertama_dipakai, ev.terakhir_aktivitas,
       CASE WHEN ev.pertama_dipakai IS NOT NULL THEN 'DIPAKAI'
            WHEN ev.customer_id IS NOT NULL      THEN 'HANYA DIBUKA'
            ELSE 'BELUM PERNAH DIBUKA' END AS status,
       n.registered_at
FROM v_nasabah n
CROSS JOIN app_features f
LEFT JOIN ev ON ev.customer_id = n.customer_id AND ev.feature_code = f.feature_code;

-- C1. Status tiap fitur untuk tiap nasabah (satu baris per nasabah x fitur)
SELECT customer_id, full_name, occupation, feature_code, category, status,
       n_buka, n_interaksi, n_selesai, n_gagal,
       round(100.0 * n_selesai / NULLIF(n_buka, 0), 1) AS pct_selesai_dari_buka,
       (pertama_buka AT TIME ZONE 'Asia/Jakarta')::date AS pertama_dibuka,
       (terakhir_aktivitas AT TIME ZONE 'Asia/Jakarta')::date AS terakhir_aktivitas
FROM v_nasabah_fitur
ORDER BY customer_id, category, feature_code;

-- C2. Ringkasan fitur per nasabah: berapa yang benar-benar dipakai, berapa yang hanya dibuka, dan daftarnya
CREATE OR REPLACE VIEW vp_fitur AS
SELECT customer_id, full_name, occupation,
       count(*) FILTER (WHERE status = 'DIPAKAI')              AS n_fitur_dipakai,
       count(*) FILTER (WHERE status = 'HANYA DIBUKA')         AS n_fitur_hanya_dibuka,
       count(*) FILTER (WHERE status = 'BELUM PERNAH DIBUKA')  AS n_fitur_belum_pernah,
       count(*) FILTER (WHERE category = 'TRANSACTION' AND status = 'DIPAKAI')       AS n_fitur_transaksi_dipakai,
       count(*) FILTER (WHERE category IN ('CREDIT','SETTING') AND status = 'DIPAKAI') AS n_fitur_kredit_setting_dipakai,
       round(100.0 * count(*) FILTER (WHERE status = 'DIPAKAI') / count(*)) AS kedalaman_pakai_pct,
       string_agg(feature_code, ', ' ORDER BY feature_code) FILTER (WHERE status = 'DIPAKAI')             AS fitur_dipakai,
       string_agg(feature_code, ', ' ORDER BY feature_code) FILTER (WHERE status = 'HANYA DIBUKA')        AS fitur_hanya_dibuka,
       string_agg(feature_code, ', ' ORDER BY feature_code) FILTER (WHERE status = 'BELUM PERNAH DIBUKA') AS fitur_belum_pernah
FROM v_nasabah_fitur
GROUP BY customer_id, full_name, occupation;

SELECT * FROM vp_fitur ORDER BY customer_id;

-- C3. Adopsi fitur strategis per nasabah: berapa hari setelah registrasi pertama kali dibuka dan pertama kali dipakai
SELECT customer_id, full_name, occupation, (registered_at AT TIME ZONE 'Asia/Jakarta')::date AS tgl_registrasi,
       max(status) FILTER (WHERE feature_code = 'JADWAL_BAYAR') AS jadwal_bayar_status,
       max((pertama_buka AT TIME ZONE 'Asia/Jakarta')::date - (registered_at AT TIME ZONE 'Asia/Jakarta')::date) FILTER (WHERE feature_code = 'JADWAL_BAYAR') AS jadwal_bayar_hari_ke_buka,
       max((pertama_dipakai AT TIME ZONE 'Asia/Jakarta')::date - (registered_at AT TIME ZONE 'Asia/Jakarta')::date) FILTER (WHERE feature_code = 'JADWAL_BAYAR') AS jadwal_bayar_hari_ke_pakai,
       max(status) FILTER (WHERE feature_code = 'PAYLATER')     AS paylater_status,
       max((pertama_buka AT TIME ZONE 'Asia/Jakarta')::date - (registered_at AT TIME ZONE 'Asia/Jakarta')::date) FILTER (WHERE feature_code = 'PAYLATER') AS paylater_hari_ke_buka,
       max((pertama_dipakai AT TIME ZONE 'Asia/Jakarta')::date - (registered_at AT TIME ZONE 'Asia/Jakarta')::date) FILTER (WHERE feature_code = 'PAYLATER') AS paylater_hari_ke_pakai,
       max(status) FILTER (WHERE feature_code = 'KTA')          AS kta_status,
       max((pertama_buka AT TIME ZONE 'Asia/Jakarta')::date - (registered_at AT TIME ZONE 'Asia/Jakarta')::date) FILTER (WHERE feature_code = 'KTA') AS kta_hari_ke_buka,
       max((pertama_dipakai AT TIME ZONE 'Asia/Jakarta')::date - (registered_at AT TIME ZONE 'Asia/Jakarta')::date) FILTER (WHERE feature_code = 'KTA') AS kta_hari_ke_pakai,
       max(status) FILTER (WHERE feature_code = 'KARTU')        AS kartu_status,
       max((pertama_buka AT TIME ZONE 'Asia/Jakarta')::date - (registered_at AT TIME ZONE 'Asia/Jakarta')::date) FILTER (WHERE feature_code = 'KARTU') AS kartu_hari_ke_buka,
       max((pertama_dipakai AT TIME ZONE 'Asia/Jakarta')::date - (registered_at AT TIME ZONE 'Asia/Jakarta')::date) FILTER (WHERE feature_code = 'KARTU') AS kartu_hari_ke_pakai
FROM v_nasabah_fitur
GROUP BY customer_id, full_name, occupation, registered_at
ORDER BY customer_id;

-- C4. Kebiasaan sesi per nasabah: seberapa sering membuka aplikasi, untuk apa, dan seberapa lama
WITH s AS (
    SELECT user_id,
           count(*)                                                        AS sesi_total,
           count(*) FILTER (WHERE login_date >= DATE '2026-07-11')         AS sesi_90h,
           count(DISTINCT login_date) FILTER (WHERE login_date >= DATE '2026-07-11') AS hari_aktif_90h,
           round(avg(n_events), 2)                                         AS rata_event_per_sesi,
           round(100.0 * count(*) FILTER (WHERE n_trx_complete > 0) / count(*), 1) AS pct_sesi_bertransaksi,
           round(100.0 * count(*) FILTER (WHERE n_complete = 0)     / count(*), 1) AS pct_sesi_hanya_lihat,
           round((avg(extract(epoch FROM last_event_at - login_at)) FILTER (WHERE last_event_at IS NOT NULL))::numeric) AS rata_detik_per_sesi,
           mode() WITHIN GROUP (ORDER BY login_hour)                       AS jam_login_tersering,
           mode() WITHIN GROUP (ORDER BY login_method)                     AS metode_login_tersering
    FROM v_session GROUP BY user_id
), f AS (
    SELECT user_id, mode() WITHIN GROUP (ORDER BY feature_code) AS fitur_pertama_tersering
    FROM (SELECT DISTINCT ON (login_id) user_id, login_id, feature_code
          FROM app_events ORDER BY login_id, event_at, event_id) x
    GROUP BY user_id
)
SELECT n.customer_id, n.full_name, n.occupation,
       s.sesi_total, s.sesi_90h, s.hari_aktif_90h, round(s.sesi_90h / (90 / 7.0), 1) AS sesi_per_minggu_90h,
       s.rata_event_per_sesi, s.pct_sesi_bertransaksi, s.pct_sesi_hanya_lihat, s.rata_detik_per_sesi,
       s.jam_login_tersering, s.metode_login_tersering, f.fitur_pertama_tersering
FROM v_nasabah n
LEFT JOIN s ON s.user_id = n.user_id
LEFT JOIN f ON f.user_id = n.user_id
ORDER BY n.customer_id;

-- C5. Target edukasi fitur: fitur kredit/pengaturan yang sering dibuka (>= 3x dalam 180 hari) tetapi tidak pernah diselesaikan
--     (satu baris per nasabah x fitur)
SELECT customer_id, full_name, occupation, feature_code, n_buka_180h, n_interaksi, n_selesai, n_gagal,
       (terakhir_aktivitas AT TIME ZONE 'Asia/Jakarta')::date AS terakhir_aktivitas,
       CASE feature_code
            WHEN 'JADWAL_BAYAR' THEN 'Tawarkan panduan membuat jadwal bayar otomatis'
            WHEN 'PAYLATER'     THEN 'Tawarkan aktivasi paylater / jelaskan limit'
            WHEN 'KTA'          THEN 'Kirim simulasi cicilan KTA'
            WHEN 'KARTU'        THEN 'Bantu aktivasi fitur kartu (online / luar negeri)'
            ELSE 'Edukasi fitur' END AS saran
FROM v_nasabah_fitur
WHERE category IN ('CREDIT', 'SETTING') AND n_buka_180h >= 3 AND n_selesai = 0
ORDER BY n_buka_180h DESC, customer_id;

-- =====================================================================
-- D. GAJIAN & PERILAKU DI SEKITAR GAJIAN  (per nasabah)
--    Gaji dikenali dari SALARY_CREDIT berdeskripsi "GAJI ...". (THR "THR ..." dikecualikan.)
--    Tanggal gajian bergeser bila jatuh di akhir pekan (dibayar Jumat sebelumnya).
-- =====================================================================
-- v_payday : deteksi gajian per nasabah bergaji: tanggal modus, kestabilan, dan perusahaan
CREATE VIEW v_payday AS
WITH s AS (
    SELECT customer_id, counterparty_name AS employer, trx_date,
           extract(day FROM trx_date)::int AS dom, amount
    FROM v_trx_ok
    WHERE transaction_type = 'SALARY_CREDIT' AND description LIKE 'GAJI%'
)
SELECT customer_id,
       mode() WITHIN GROUP (ORDER BY employer)  AS employer,
       count(*)                                 AS bulan_terdeteksi,
       mode() WITHIN GROUP (ORDER BY dom)       AS tgl_modus,
       round(stddev(dom), 1)                    AS simpangan_tgl,
       min(dom)                                 AS tgl_terawal,
       max(dom)                                 AS tgl_terakhir,
       round(avg(amount))                       AS gaji_rata,
       CASE WHEN count(*) >= 6 AND (stddev(dom) <= 3.5 OR stddev(dom) IS NULL) THEN 'TETAP' ELSE 'KURANG STABIL' END AS kelas
FROM s
GROUP BY customer_id;

-- D1. Pola tanggal uang masuk utama per nasabah.
--     Nasabah bergaji: dari data gaji. Nasabah lain (wiraswasta, freelancer, mahasiswa, IRT, ...): dari pemasukan terbesar tiap bulan.
CREATE OR REPLACE VIEW vp_gajian AS
WITH big AS (
    SELECT DISTINCT ON (t.customer_id, t.trx_month)
           t.customer_id, t.trx_month, extract(day FROM t.trx_date)::int AS dom, t.amount
    FROM v_trx_ok t
    WHERE t.flow = 'INCOME' AND t.transaction_type IN ('TRANSFER_IN','CASH_DEPOSIT')
      AND t.trx_month BETWEEN DATE '2025-10-01' AND DATE '2026-09-01'
      AND NOT EXISTS (SELECT 1 FROM v_payday p WHERE p.customer_id = t.customer_id)
    ORDER BY t.customer_id, t.trx_month, t.amount DESC
), per AS (
    SELECT customer_id, count(*) AS bulan, round(stddev(dom), 1) AS simpangan_tgl,
           mode() WITHIN GROUP (ORDER BY dom) AS tgl_modus, round(avg(amount)) AS nominal_rata
    FROM big GROUP BY customer_id HAVING count(*) >= 3
)
SELECT x.*,
       CASE WHEN tanggal_masuk_tersering IS NULL THEN '-'
            WHEN tanggal_masuk_tersering <= 5  THEN 'Awal bulan (1-5)'
            WHEN tanggal_masuk_tersering <= 15 THEN 'Pertengahan (6-15)'
            WHEN tanggal_masuk_tersering <= 24 THEN 'Menjelang akhir (16-24)'
            ELSE 'Akhir bulan (25+)' END AS posisi_di_bulan
FROM (
    SELECT n.customer_id, n.full_name, n.occupation,
           CASE WHEN p.customer_id IS NOT NULL THEN 'GAJI'
                WHEN r.customer_id IS NOT NULL THEN 'PEMASUKAN UTAMA'
                ELSE 'TIDAK ADA POLA' END                         AS jenis_pola,
           COALESCE(p.tgl_modus, r.tgl_modus)                     AS tanggal_masuk_tersering,
           COALESCE(p.simpangan_tgl, r.simpangan_tgl)             AS simpangan_tgl,
           COALESCE(p.bulan_terdeteksi, r.bulan)                  AS bulan_terdeteksi,
           p.employer,
           COALESCE(p.gaji_rata, r.nominal_rata)                  AS nominal_rata,
           CASE WHEN p.customer_id IS NOT NULL THEN p.kelas
                WHEN r.bulan >= 6 AND r.simpangan_tgl <= 4 THEN 'TETAP'
                WHEN r.customer_id IS NOT NULL THEN 'TIDAK TERATUR'
                ELSE '-' END                                      AS kelas
    FROM v_nasabah n
    LEFT JOIN v_payday p ON p.customer_id = n.customer_id
    LEFT JOIN per r      ON r.customer_id = n.customer_id
) x;

SELECT * FROM vp_gajian ORDER BY customer_id;

-- D2. Respons terhadap gaji masuk (12 bulan terakhir): seberapa cepat nasabah membuka aplikasi dan apakah notifikasi dibuka
WITH sal AS (
    SELECT t.customer_id, n.user_id, t.created_at AS gaji_at
    FROM v_trx_ok t
    JOIN v_nasabah n ON n.customer_id = t.customer_id
    WHERE t.transaction_type = 'SALARY_CREDIT' AND t.description LIKE 'GAJI%' AND t.trx_date >= DATE '2025-10-10'
), j AS (
    SELECT s.customer_id, s.gaji_at, lg.login_pertama, nf.opened_at IS NOT NULL AS notif_dibuka, nf.notification_id IS NOT NULL AS notif_ada,
           extract(epoch FROM lg.login_pertama - s.gaji_at) / 60 AS menit_ke_login
    FROM sal s
    LEFT JOIN LATERAL (SELECT min(l.login_at) AS login_pertama FROM login_logs l
                       WHERE l.user_id = s.user_id AND l.status = 'SUCCESS'
                         AND l.login_at >= s.gaji_at AND l.login_at < s.gaji_at + interval '24 hours') lg ON true
    LEFT JOIN LATERAL (SELECT n.notification_id, n.opened_at FROM notifications n
                       WHERE n.user_id = s.user_id AND n.category = 'GAJI_MASUK'
                         AND n.sent_at BETWEEN s.gaji_at AND s.gaji_at + interval '10 minutes' LIMIT 1) nf ON true
)
SELECT n.customer_id, n.full_name, n.occupation,
       count(*)                                                                   AS gaji_masuk_12bln,
       round((percentile_cont(0.5) WITHIN GROUP (ORDER BY j.menit_ke_login))::numeric, 0) AS median_menit_ke_login,
       round(100.0 * count(*) FILTER (WHERE j.menit_ke_login <= 60) / count(*), 1)  AS pct_login_dalam_1jam,
       round(100.0 * count(*) FILTER (WHERE j.login_pertama IS NOT NULL) / count(*), 1) AS pct_login_dalam_24jam,
       count(*) FILTER (WHERE j.notif_ada)                                        AS notif_gaji_terkirim,
       round(100.0 * count(*) FILTER (WHERE j.notif_dibuka) / NULLIF(count(*) FILTER (WHERE j.notif_ada), 0), 1) AS pct_notif_dibuka,
       CASE WHEN percentile_cont(0.5) WITHIN GROUP (ORDER BY j.menit_ke_login) <= 60  THEN 'Langsung cek (<= 1 jam)'
            WHEN percentile_cont(0.5) WITHIN GROUP (ORDER BY j.menit_ke_login) <= 360 THEN 'Cek di hari yang sama'
            WHEN percentile_cont(0.5) WITHIN GROUP (ORDER BY j.menit_ke_login) IS NULL THEN 'Tidak pernah cek < 24 jam'
            ELSE 'Cek belakangan' END                                              AS kebiasaan
FROM j
JOIN v_nasabah n ON n.customer_id = j.customer_id
GROUP BY n.customer_id, n.full_name, n.occupation
ORDER BY n.customer_id;

-- D3. Pola belanja dalam siklus gaji (nasabah bergaji, >= 6 siklus dalam 12 bulan terakhir):
--     porsi belanja yang habis per fase setelah gajian dan indeks (100 = rata-rata; > 100 = lebih boros dari biasanya)
WITH sal AS (
    SELECT customer_id, trx_date AS gaji_tgl,
           lead(trx_date) OVER (PARTITION BY customer_id ORDER BY trx_date) AS gaji_berikut
    FROM v_trx_ok
    WHERE transaction_type = 'SALARY_CREDIT' AND description LIKE 'GAJI%'
), x AS (
    SELECT t.customer_id, s.gaji_tgl, (t.trx_date - s.gaji_tgl) AS hari_ke, t.amount
    FROM v_trx_ok t
    JOIN sal s ON s.customer_id = t.customer_id AND t.trx_date >= s.gaji_tgl
              AND t.trx_date < COALESCE(s.gaji_berikut, s.gaji_tgl + 31)
    WHERE t.flow = 'EXPENSE' AND t.spend_group IN ('CASHLESS_APP','KARTU','TUNAI','TRANSFER')
      AND t.channel <> 'SYSTEM' AND t.trx_date >= DATE '2025-10-10'
), p AS (
    SELECT customer_id, count(DISTINCT gaji_tgl) AS siklus, sum(amount) AS tot,
           sum(amount) FILTER (WHERE hari_ke <= 2)               AS h0_2,
           sum(amount) FILTER (WHERE hari_ke BETWEEN 3 AND 6)    AS h3_6,
           sum(amount) FILTER (WHERE hari_ke BETWEEN 7 AND 13)   AS h7_13,
           sum(amount) FILTER (WHERE hari_ke BETWEEN 14 AND 20)  AS h14_20,
           sum(amount) FILTER (WHERE hari_ke >= 21)              AS h21p
    FROM x GROUP BY customer_id HAVING count(DISTINCT gaji_tgl) >= 6
)
SELECT n.customer_id, n.full_name, n.occupation, p.siklus,
       round(100.0 * COALESCE(p.h0_2, 0)   / p.tot, 1) AS pct_hari_0_2,
       round(100.0 * COALESCE(p.h3_6, 0)   / p.tot, 1) AS pct_hari_3_6,
       round(100.0 * COALESCE(p.h7_13, 0)  / p.tot, 1) AS pct_hari_7_13,
       round(100.0 * COALESCE(p.h14_20, 0) / p.tot, 1) AS pct_hari_14_20,
       round(100.0 * COALESCE(p.h21p, 0)   / p.tot, 1) AS pct_hari_21_plus,
       round(100.0 * (COALESCE(p.h0_2, 0) / p.tot) / (3 / 31.0))  AS indeks_hari_0_2,
       round(100.0 * (COALESCE(p.h21p, 0) / p.tot) / (10 / 31.0)) AS indeks_hari_21_plus,
       CASE WHEN 100.0 * (COALESCE(p.h0_2, 0) + COALESCE(p.h3_6, 0)) / p.tot >= 40 THEN 'Boros awal bulan (>= 40% di 7 hari pertama)'
            WHEN 100.0 * (COALESCE(p.h0_2, 0) + COALESCE(p.h3_6, 0)) / p.tot >= 30 THEN 'Condong awal bulan'
            ELSE 'Merata' END AS tipe_belanja
FROM p
JOIN v_nasabah n ON n.customer_id = p.customer_id
ORDER BY n.customer_id;

-- v_siklus_gaji : satu baris per gaji masuk (12 bulan terakhir) beserta kesiapan saldo & apa yang dilakukan 3 hari pertama
CREATE OR REPLACE VIEW v_siklus_gaji AS
WITH sal AS (
    SELECT t.customer_id, t.account_id, t.created_at AS gaji_at, t.trx_date AS gaji_tgl, t.amount AS gaji
    FROM v_trx_ok t
    WHERE t.transaction_type = 'SALARY_CREDIT' AND t.description LIKE 'GAJI%' AND t.trx_date >= DATE '2025-10-10'
)
SELECT s.customer_id, s.gaji_tgl, s.gaji,
       COALESCE((SELECT x.balance_after FROM transactions x
                  WHERE x.account_id = s.account_id AND x.status = 'SUCCESS' AND x.created_at < s.gaji_at
                  ORDER BY x.created_at DESC, x.transaction_id DESC LIMIT 1), 0) AS saldo_sebelum,
       (SELECT count(*) FROM transactions x
         WHERE x.account_id = s.account_id AND x.failure_reason = 'SALDO TIDAK CUKUP'
           AND x.created_at BETWEEN s.gaji_at - interval '7 days' AND s.gaji_at) AS gagal_saldo_7h,
       a.menabung, a.transfer_keluar, a.bayar_rutin, a.belanja
FROM sal s
LEFT JOIN LATERAL (
    SELECT COALESCE(sum(t.amount) FILTER (WHERE t.transaction_type = 'SAVINGS_OUT'), 0)                           AS menabung,
           COALESCE(sum(t.amount) FILTER (WHERE t.spend_group = 'TRANSFER'), 0)                                   AS transfer_keluar,
           COALESCE(sum(t.amount + t.fee) FILTER (WHERE t.spend_group IN ('TAGIHAN','CICILAN')), 0)               AS bayar_rutin,
           COALESCE(sum(t.amount + t.fee) FILTER (WHERE t.spend_group IN ('CASHLESS_APP','KARTU','TUNAI')), 0)    AS belanja
    FROM v_trx_ok t
    WHERE t.account_id = s.account_id AND t.flow IN ('EXPENSE','INTERNAL') AND t.direction = 'D'
      AND t.created_at > s.gaji_at AND t.created_at <= s.gaji_at + interval '3 days'
) a ON true;

-- D4. Detail tiap siklus gaji per nasabah (satu baris per nasabah x gaji masuk)
SELECT n.customer_id, n.full_name, n.occupation, g.gaji_tgl, round(g.gaji) AS gaji,
       round(g.saldo_sebelum) AS saldo_sebelum_gaji,
       round(100.0 * g.saldo_sebelum / g.gaji, 1) AS saldo_sebelum_pct_gaji,
       CASE WHEN g.saldo_sebelum < 0.05 * g.gaji THEN 'Hampir habis (< 5% gaji)'
            WHEN g.saldo_sebelum < 0.20 * g.gaji THEN 'Tipis (5-20%)'
            WHEN g.saldo_sebelum < 0.50 * g.gaji THEN 'Cukup (20-50%)'
            ELSE 'Longgar (>= 50%)' END AS kondisi_saldo,
       g.gagal_saldo_7h AS gagal_saldo_7h_sebelum,
       round(100.0 * g.menabung / g.gaji, 1)        AS pct_gaji_ditabung_3h,
       round(100.0 * g.transfer_keluar / g.gaji, 1) AS pct_gaji_transfer_3h,
       round(100.0 * g.bayar_rutin / g.gaji, 1)     AS pct_gaji_bayar_rutin_3h,
       round(100.0 * g.belanja / g.gaji, 1)         AS pct_gaji_belanja_3h
FROM v_siklus_gaji g
JOIN v_nasabah n ON n.customer_id = g.customer_id
ORDER BY n.customer_id, g.gaji_tgl;

-- D5. Kesiapan menjelang gajian dan kebiasaan 3 hari pertama setelah gajian (ringkasan per nasabah dari D4)
SELECT n.customer_id, n.full_name, n.occupation,
       count(*)                                                        AS siklus_gaji,
       round(avg(100.0 * g.saldo_sebelum / g.gaji), 1)                 AS rata_saldo_sebelum_pct_gaji,
       round(100.0 * count(*) FILTER (WHERE g.saldo_sebelum < 0.05 * g.gaji) / count(*), 1) AS pct_siklus_saldo_habis,
       round(avg(g.gagal_saldo_7h), 2)                                 AS rata_gagal_saldo_7h_sebelum,
       round(100.0 * count(*) FILTER (WHERE g.gagal_saldo_7h > 0) / count(*), 1) AS pct_siklus_ada_gagal_saldo,
       round(100.0 * count(*) FILTER (WHERE g.menabung > 0) / count(*), 1)       AS pct_siklus_menabung,
       round(avg(100.0 * g.menabung / g.gaji), 1)        AS rata_pct_gaji_ditabung,
       round(avg(100.0 * g.transfer_keluar / g.gaji), 1) AS rata_pct_gaji_transfer,
       round(avg(100.0 * g.bayar_rutin / g.gaji), 1)     AS rata_pct_gaji_bayar_rutin,
       round(avg(100.0 * g.belanja / g.gaji), 1)         AS rata_pct_gaji_belanja,
       CASE WHEN 100.0 * count(*) FILTER (WHERE g.saldo_sebelum < 0.05 * g.gaji) / count(*) >= 50 THEN 'Sering kehabisan sebelum gajian'
            WHEN 100.0 * count(*) FILTER (WHERE g.saldo_sebelum < 0.20 * g.gaji) / count(*) >= 30 THEN 'Kadang tipis'
            ELSE 'Aman' END AS kesiapan
FROM v_siklus_gaji g
JOIN v_nasabah n ON n.customer_id = g.customer_id
GROUP BY n.customer_id, n.full_name, n.occupation
ORDER BY n.customer_id;

-- =====================================================================
-- E. CICILAN & PEMBAYARAN RUTIN  (per nasabah)
--    Kapan tiap nasabah membayar cicilan (KTA / paylater) dan tagihan rutin, seberapa teratur,
--    dan apa hubungannya dengan tanggal gajian dan fitur Jadwal Bayar.
-- =====================================================================
-- v_sched_cicilan : jadwal bayar bertipe cicilan. Jadwal KTA menempel ke satu pinjaman (loan_id terisi);
--                   jadwal "Tagihan Paylater" berlaku untuk seluruh tagihan paylater nasabah (loan_id kosong).
CREATE VIEW v_sched_cicilan AS
SELECT sp.*, CASE WHEN sp.loan_id IS NULL THEN 'PAYLATER' ELSE 'KTA' END AS product_code
FROM scheduled_payments sp
WHERE sp.payment_type = 'LOAN_INSTALLMENT';

-- v_inst : satu baris per angsuran + info pinjaman, tanggal bayar (WIB), dan apakah Jadwal Bayar aktif pada saat jatuh tempo
CREATE VIEW v_inst AS
SELECT i.installment_id, i.loan_id, i.installment_no, i.due_date, i.amount_due, i.principal_part, i.status,
       i.payment_channel, i.days_late, i.late_fee, i.paid_amount, i.paid_at AS paid_at_ts,
       (i.paid_at AT TIME ZONE 'Asia/Jakarta')::date                          AS paid_date,
       extract(hour FROM i.paid_at AT TIME ZONE 'Asia/Jakarta')::int          AS paid_hour,
       extract(isodow FROM i.paid_at AT TIME ZONE 'Asia/Jakarta')::int        AS paid_isodow,
       (i.paid_at AT TIME ZONE 'Asia/Jakarta')::date - i.due_date             AS offset_hari,
       l.customer_id, l.product_code, l.due_day, l.installment_amount, l.principal, l.tenor_months,
       EXISTS (SELECT 1 FROM v_sched_cicilan sp
               WHERE sp.customer_id = l.customer_id AND sp.product_code = l.product_code
                 AND (sp.loan_id IS NULL OR sp.loan_id = i.loan_id)
                 AND (sp.created_at AT TIME ZONE 'Asia/Jakarta')::date <= i.due_date
                 AND (sp.cancelled_at IS NULL OR (sp.cancelled_at AT TIME ZONE 'Asia/Jakarta')::date > i.due_date)) AS sched_aktif
FROM loan_installments i
JOIN loans l ON l.loan_id = i.loan_id;

-- E1. Ketepatan bayar cicilan per nasabah x produk (seluruh riwayat)
SELECT n.customer_id, n.full_name, n.occupation, i.product_code,
       mode() WITHIN GROUP (ORDER BY i.due_day)                                   AS tgl_jatuh_tempo,
       count(*) FILTER (WHERE i.status = 'PAID')                                  AS angsuran_dibayar,
       count(*) FILTER (WHERE i.status = 'UNPAID')                                AS angsuran_belum_dibayar,
       count(*) FILTER (WHERE i.status = 'UPCOMING')                              AS angsuran_akan_datang,
       count(*) FILTER (WHERE i.status = 'PAID' AND i.days_late > 0)              AS n_telat,
       count(*) FILTER (WHERE i.status = 'PAID' AND i.days_late > 3)              AS n_telat_lebih_3h,
       round(100.0 * count(*) FILTER (WHERE i.status = 'PAID' AND i.days_late > 0)
             / NULLIF(count(*) FILTER (WHERE i.status = 'PAID'), 0), 1)           AS pct_telat,
       round(avg(i.days_late) FILTER (WHERE i.status = 'PAID'), 2)                AS rata_hari_telat,
       max(i.days_late)                                                           AS telat_terlama,
       round(sum(i.late_fee))                                                     AS total_denda,
       count(*) FILTER (WHERE i.payment_channel = 'MOBILE')                       AS via_mobile,
       count(*) FILTER (WHERE i.payment_channel = 'ATM')                          AS via_atm,
       count(*) FILTER (WHERE i.payment_channel = 'SCHEDULER')                    AS via_jadwal_bayar,
       round(100.0 * count(*) FILTER (WHERE i.payment_channel = 'SCHEDULER')
             / NULLIF(count(*) FILTER (WHERE i.status = 'PAID'), 0))              AS pct_via_jadwal
FROM v_inst i
JOIN v_nasabah n ON n.customer_id = i.customer_id
GROUP BY n.customer_id, n.full_name, n.occupation, i.product_code
ORDER BY n.customer_id, i.product_code;

-- E2. Kapan dan seberapa teratur nasabah membayar cicilan (12 bulan terakhir, minimal 4 pembayaran)
--     offset = hari pembayaran dibanding jatuh tempo (negatif = lebih awal). Pola: Rutin tepat / Rutin tapi telat / Tidak rutin.
--     posisi_jatuh_tempo_vs_gajian: berapa hari setelah gajian cicilan itu jatuh tempo (nasabah dengan gaji tetap).
WITH p AS (
    SELECT customer_id, product_code,
           count(*)                                           AS n_bayar,
           mode() WITHIN GROUP (ORDER BY extract(day FROM paid_date)::int) AS tgl_bayar_tersering,
           max(due_day)                                       AS tgl_jatuh_tempo,
           round(avg(offset_hari), 1)                         AS rata_offset,
           round(stddev(offset_hari), 1)                      AS simpangan_offset,
           round(100.0 * count(*) FILTER (WHERE days_late > 0) / count(*)) AS pct_telat,
           round(100.0 * count(*) FILTER (WHERE payment_channel = 'SCHEDULER') / count(*)) AS pct_via_jadwal,
           mode() WITHIN GROUP (ORDER BY paid_hour) FILTER (WHERE payment_channel <> 'SCHEDULER') AS jam_bayar_manual_tersering,
           round(100.0 * count(*) FILTER (WHERE payment_channel <> 'SCHEDULER' AND paid_isodow IN (6, 7))
                 / NULLIF(count(*) FILTER (WHERE payment_channel <> 'SCHEDULER'), 0)) AS pct_manual_akhir_pekan
    FROM v_inst
    WHERE status = 'PAID' AND paid_date >= DATE '2025-10-10'
    GROUP BY customer_id, product_code
    HAVING count(*) >= 4
)
SELECT n.customer_id, n.full_name, n.occupation, p.product_code, p.n_bayar,
       p.tgl_jatuh_tempo, p.tgl_bayar_tersering, p.rata_offset, p.simpangan_offset, p.pct_telat, p.pct_via_jadwal,
       p.jam_bayar_manual_tersering, p.pct_manual_akhir_pekan,
       CASE WHEN p.simpangan_offset <= 2 AND p.rata_offset <= 1   THEN 'Rutin tepat'
            WHEN p.simpangan_offset <= 2.5 AND p.rata_offset > 1  THEN 'Rutin tapi telat'
            ELSE 'Tidak rutin' END AS pola,
       pd.tgl_modus AS tanggal_gajian,
       CASE WHEN pd.tgl_modus IS NULL THEN NULL
            ELSE ((p.tgl_jatuh_tempo - pd.tgl_modus) % 31 + 31) % 31 END AS hari_setelah_gajian_jatuh_tempo
FROM p
JOIN v_nasabah n ON n.customer_id = p.customer_id
LEFT JOIN v_payday pd ON pd.customer_id = p.customer_id AND pd.kelas = 'TETAP'
ORDER BY n.customer_id, p.product_code;

-- E3. Tagihan rutin (BILL_PAYMENT) per nasabah x biller: dibayar tiap bulan, di tanggal berapa, dan lewat Jadwal Bayar atau manual
--     (12 bulan penuh, minimal 6 bulan berbeda)
WITH b AS (
    SELECT customer_id, biller_id, trx_month, extract(day FROM trx_date)::int AS dom,
           (scheduled_payment_id IS NOT NULL) AS via_jadwal, amount, trx_date
    FROM v_trx_ok
    WHERE transaction_type = 'BILL_PAYMENT' AND biller_id IS NOT NULL
      AND trx_month BETWEEN DATE '2025-10-01' AND DATE '2026-09-01'
)
SELECT n.customer_id, n.full_name, n.occupation, bl.category AS kategori_biller, bl.biller_name,
       count(DISTINCT b.trx_month)                                  AS bulan_dibayar_dari_12,
       mode() WITHIN GROUP (ORDER BY b.dom)                         AS tgl_bayar_tersering,
       round(stddev(b.dom), 1)                                      AS simpangan_tgl,
       round(avg(b.amount))                                         AS rata_nominal,
       round(100.0 * count(*) FILTER (WHERE b.via_jadwal) / count(*)) AS pct_via_jadwal,
       max(b.trx_date)                                              AS terakhir_bayar,
       CASE WHEN count(DISTINCT b.trx_month) >= 10 THEN 'RUTIN BULANAN' ELSE 'SERING' END AS keteraturan
FROM b
JOIN v_nasabah n ON n.customer_id = b.customer_id
JOIN billers bl  ON bl.biller_id = b.biller_id
GROUP BY n.customer_id, n.full_name, n.occupation, bl.category, bl.biller_name
HAVING count(DISTINCT b.trx_month) >= 6
ORDER BY n.customer_id, bl.biller_name;

-- E4. Beban dan kepatuhan cicilan per nasabah (hanya nasabah yang pernah punya pinjaman)
CREATE OR REPLACE VIEW vp_cicilan AS
WITH l AS (
    SELECT customer_id,
           count(*)                                             AS pinjaman_total,
           count(*) FILTER (WHERE status = 'ACTIVE')            AS pinjaman_aktif,
           count(*) FILTER (WHERE status = 'ACTIVE' AND product_code = 'PAYLATER') AS paylater_aktif,
           count(*) FILTER (WHERE status = 'ACTIVE' AND product_code = 'KTA')      AS kta_aktif,
           COALESCE(sum(installment_amount) FILTER (WHERE status = 'ACTIVE'), 0)   AS cicilan_bulanan
    FROM loans GROUP BY customer_id
), o AS (
    SELECT l2.customer_id, round(sum(i.principal_part)) AS sisa_pokok, round(sum(i.amount_due)) AS sisa_tagihan
    FROM loan_installments i JOIN loans l2 ON l2.loan_id = i.loan_id
    WHERE i.status IN ('UNPAID', 'UPCOMING')
    GROUP BY l2.customer_id
), t AS (
    SELECT customer_id, count(*) AS n_bayar_6bln,
           count(*) FILTER (WHERE days_late > 0) AS n_telat_6bln,
           count(*) FILTER (WHERE days_late > 3) AS n_telat_gt3h_6bln,
           round(sum(late_fee)) AS denda_6bln,
           bool_or(sched_aktif) AS pakai_jadwal_6bln
    FROM v_inst WHERE status = 'PAID' AND paid_date >= DATE '2026-04-10'
    GROUP BY customer_id
), g AS (
    SELECT a.customer_id,
           count(*) FILTER (WHERE tr.failure_reason = 'SALDO TIDAK CUKUP') AS gagal_bayar_saldo_12bln,
           count(*) FILTER (WHERE tr.failure_reason <> 'SALDO TIDAK CUKUP') AS gagal_bayar_teknis_12bln
    FROM transactions tr JOIN accounts a ON a.account_id = tr.account_id
    WHERE tr.transaction_type = 'LOAN_INSTALLMENT' AND tr.status = 'FAILED' AND tr.created_at >= TIMESTAMPTZ '2025-10-10'
    GROUP BY a.customer_id
)
SELECT n.customer_id, n.full_name, n.occupation, n.monthly_income AS penghasilan_terdaftar,
       l.pinjaman_total, l.pinjaman_aktif, l.paylater_aktif, l.kta_aktif,
       round(l.cicilan_bulanan) AS cicilan_bulanan,
       round(100.0 * l.cicilan_bulanan / NULLIF(n.monthly_income, 0), 1) AS beban_pct_penghasilan,
       CASE WHEN l.pinjaman_aktif = 0 THEN '-'
            WHEN l.cicilan_bulanan / NULLIF(n.monthly_income, 0) < 0.10 THEN 'Ringan (< 10%)'
            WHEN l.cicilan_bulanan / NULLIF(n.monthly_income, 0) < 0.25 THEN 'Sedang (10-25%)'
            WHEN l.cicilan_bulanan / NULLIF(n.monthly_income, 0) < 0.40 THEN 'Berat (25-40%)'
            ELSE 'Sangat berat (>= 40%)' END AS kategori_beban,
       o.sisa_pokok, o.sisa_tagihan,
       COALESCE(t.n_bayar_6bln, 0) AS n_bayar_6bln, COALESCE(t.n_telat_6bln, 0) AS n_telat_6bln,
       COALESCE(t.n_telat_gt3h_6bln, 0) AS n_telat_gt3h_6bln,
       round(100.0 * t.n_telat_6bln / NULLIF(t.n_bayar_6bln, 0), 1) AS pct_telat_6bln,
       COALESCE(t.denda_6bln, 0) AS denda_6bln,
       COALESCE(g.gagal_bayar_saldo_12bln, 0) AS gagal_bayar_saldo_12bln,
       COALESCE(g.gagal_bayar_teknis_12bln, 0) AS gagal_bayar_teknis_12bln
FROM l
JOIN v_nasabah n ON n.customer_id = l.customer_id
LEFT JOIN o ON o.customer_id = l.customer_id
LEFT JOIN t ON t.customer_id = l.customer_id
LEFT JOIN g ON g.customer_id = l.customer_id;

SELECT * FROM vp_cicilan ORDER BY customer_id;

-- E5. Pembayaran cicilan & tagihan yang GAGAL per nasabah (12 bulan terakhir): penyebab dan apakah akhirnya berhasil dalam 24 jam
WITH f AS (
    SELECT a.customer_id, tr.transaction_type, tr.failure_reason, tr.channel,
           EXISTS (SELECT 1 FROM transactions s
                   WHERE s.account_id = tr.account_id AND s.transaction_type = tr.transaction_type
                     AND s.loan_id IS NOT DISTINCT FROM tr.loan_id AND s.biller_id IS NOT DISTINCT FROM tr.biller_id
                     AND s.status = 'SUCCESS' AND s.created_at > tr.created_at
                     AND s.created_at <= tr.created_at + interval '24 hours') AS berhasil_24jam
    FROM transactions tr
    JOIN accounts a ON a.account_id = tr.account_id
    WHERE tr.transaction_type IN ('LOAN_INSTALLMENT', 'BILL_PAYMENT') AND tr.status = 'FAILED'
      AND tr.created_at >= TIMESTAMPTZ '2025-10-10'
), ok AS (
    SELECT customer_id,
           count(*) FILTER (WHERE transaction_type = 'LOAN_INSTALLMENT') AS cicilan_sukses,
           count(*) FILTER (WHERE transaction_type = 'BILL_PAYMENT')     AS tagihan_sukses
    FROM v_trx_ok
    WHERE transaction_type IN ('LOAN_INSTALLMENT', 'BILL_PAYMENT') AND trx_date >= DATE '2025-10-10'
    GROUP BY customer_id
)
SELECT n.customer_id, n.full_name, n.occupation,
       count(*) FILTER (WHERE f.transaction_type = 'LOAN_INSTALLMENT')                          AS cicilan_gagal,
       COALESCE(ok.cicilan_sukses, 0)                                                           AS cicilan_sukses,
       count(*) FILTER (WHERE f.transaction_type = 'BILL_PAYMENT')                              AS tagihan_gagal,
       COALESCE(ok.tagihan_sukses, 0)                                                           AS tagihan_sukses,
       count(*) FILTER (WHERE f.failure_reason = 'SALDO TIDAK CUKUP')                           AS gagal_saldo_tidak_cukup,
       count(*) FILTER (WHERE f.failure_reason IN ('TIMEOUT', 'GANGGUAN JARINGAN'))             AS gagal_teknis,
       count(*) FILTER (WHERE f.channel = 'SYSTEM')                                             AS gagal_saat_jadwal_bayar,
       round(100.0 * count(*) FILTER (WHERE f.berhasil_24jam) / count(*), 1)                    AS pct_berhasil_24jam_kemudian
FROM f
JOIN v_nasabah n ON n.customer_id = f.customer_id
LEFT JOIN ok ON ok.customer_id = f.customer_id
GROUP BY n.customer_id, n.full_name, n.occupation, ok.cicilan_sukses, ok.tagihan_sukses
ORDER BY cicilan_gagal DESC, tagihan_gagal DESC, n.customer_id;

-- E6. Skor risiko keterlambatan per nasabah (hanya yang punya pembayaran cicilan dalam 6 bulan terakhir), makin tinggi makin berisiko
--     skor = telat > 3 hari (maks 40) + persentase telat (maks 30) + beban cicilan (maks 20) + gagal saldo (maks 10)
SELECT customer_id, full_name, occupation, n_bayar_6bln, n_telat_6bln, n_telat_gt3h_6bln, pct_telat_6bln,
       kategori_beban, gagal_bayar_saldo_12bln, pakai_jadwal_6bln,
       skor_risiko,
       CASE WHEN skor_risiko >= 60 THEN 'TINGGI' WHEN skor_risiko >= 30 THEN 'SEDANG' ELSE 'RENDAH' END AS risiko
FROM (
    SELECT c.customer_id, c.full_name, c.occupation, c.n_bayar_6bln, c.n_telat_6bln, c.n_telat_gt3h_6bln, c.pct_telat_6bln,
           c.kategori_beban, c.gagal_bayar_saldo_12bln,
           COALESCE((SELECT bool_or(i.sched_aktif) FROM v_inst i
                     WHERE i.customer_id = c.customer_id AND i.status = 'PAID' AND i.paid_date >= DATE '2026-04-10'), false) AS pakai_jadwal_6bln,
           round(LEAST(c.n_telat_gt3h_6bln * 8, 40)
               + LEAST(COALESCE(c.pct_telat_6bln, 0) * 0.3, 30)
               + CASE c.kategori_beban WHEN 'Sangat berat (>= 40%)' THEN 20 WHEN 'Berat (25-40%)' THEN 14
                                       WHEN 'Sedang (10-25%)' THEN 7 ELSE 0 END
               + LEAST(c.gagal_bayar_saldo_12bln * 2, 10)) AS skor_risiko
    FROM vp_cicilan c
    WHERE c.n_bayar_6bln > 0
) x
ORDER BY skor_risiko DESC, customer_id;

-- =====================================================================
-- F. JADWAL BAYAR (SCHEDULER)  (per nasabah)
--    Apakah nasabah mengaktifkan penjadwalan pembayaran cicilan (dan tagihan/transfer), dan apa dampaknya.
-- =====================================================================
-- F1. Status Jadwal Bayar per nasabah (semua nasabah)
CREATE OR REPLACE VIEW vp_jadwal AS
WITH sp AS (
    SELECT customer_id,
           count(*) FILTER (WHERE payment_type = 'LOAN_INSTALLMENT' AND status = 'ACTIVE')    AS jadwal_cicilan_aktif,
           count(*) FILTER (WHERE payment_type = 'LOAN_INSTALLMENT' AND status = 'CANCELLED') AS jadwal_cicilan_batal,
           count(*) FILTER (WHERE payment_type = 'BILL'     AND status = 'ACTIVE')            AS jadwal_tagihan_aktif,
           count(*) FILTER (WHERE payment_type = 'BILL'     AND status = 'CANCELLED')         AS jadwal_tagihan_batal,
           count(*) FILTER (WHERE payment_type = 'TRANSFER' AND status = 'ACTIVE')            AS jadwal_transfer_aktif,
           count(*) FILTER (WHERE payment_type = 'TRANSFER' AND status = 'CANCELLED')         AS jadwal_transfer_batal,
           sum(run_count) AS eksekusi, sum(success_count) AS berhasil, sum(fail_count) AS gagal,
           (min(created_at) AT TIME ZONE 'Asia/Jakarta')::date AS pertama_dibuat
    FROM scheduled_payments GROUP BY customer_id
), ln AS (
    SELECT customer_id, count(*) AS pinjaman_total, count(*) FILTER (WHERE status = 'ACTIVE') AS pinjaman_aktif
    FROM loans GROUP BY customer_id
), m AS (
    SELECT customer_id, n_buka AS buka_menu, n_selesai AS selesai_menu
    FROM v_nasabah_fitur WHERE feature_code = 'JADWAL_BAYAR'
)
SELECT n.customer_id, n.full_name, n.occupation,
       COALESCE(ln.pinjaman_total, 0) AS pinjaman_total, COALESCE(ln.pinjaman_aktif, 0) AS pinjaman_aktif,
       COALESCE(sp.jadwal_cicilan_aktif, 0)  AS jadwal_cicilan_aktif,  COALESCE(sp.jadwal_cicilan_batal, 0)  AS jadwal_cicilan_batal,
       COALESCE(sp.jadwal_tagihan_aktif, 0)  AS jadwal_tagihan_aktif,  COALESCE(sp.jadwal_tagihan_batal, 0)  AS jadwal_tagihan_batal,
       COALESCE(sp.jadwal_transfer_aktif, 0) AS jadwal_transfer_aktif, COALESCE(sp.jadwal_transfer_batal, 0) AS jadwal_transfer_batal,
       COALESCE(sp.eksekusi, 0) AS eksekusi, COALESCE(sp.berhasil, 0) AS berhasil, COALESCE(sp.gagal, 0) AS gagal,
       round(100.0 * sp.gagal / NULLIF(sp.eksekusi, 0), 1) AS pct_gagal,
       sp.pertama_dibuat, COALESCE(m.buka_menu, 0) AS buka_menu_jadwal, COALESCE(m.selesai_menu, 0) AS selesai_menu_jadwal,
       CASE WHEN ln.pinjaman_total IS NULL            THEN 'TIDAK PUNYA CICILAN'
            WHEN COALESCE(sp.jadwal_cicilan_aktif, 0) > 0 THEN 'AKTIF'
            WHEN COALESCE(sp.jadwal_cicilan_batal, 0) > 0 THEN 'PERNAH TAPI BATAL'
            ELSE 'BELUM PERNAH' END                   AS status_jadwal_cicilan,
       CASE WHEN COALESCE(sp.jadwal_tagihan_aktif, 0) + COALESCE(sp.jadwal_transfer_aktif, 0) > 0 THEN 'AKTIF'
            WHEN COALESCE(sp.jadwal_tagihan_batal, 0) + COALESCE(sp.jadwal_transfer_batal, 0) > 0 THEN 'PERNAH TAPI BATAL'
            ELSE 'BELUM PERNAH' END                   AS status_jadwal_tagihan_transfer
FROM v_nasabah n
LEFT JOIN sp ON sp.customer_id = n.customer_id
LEFT JOIN ln ON ln.customer_id = n.customer_id
LEFT JOIN m  ON m.customer_id = n.customer_id;

SELECT * FROM vp_jadwal ORDER BY customer_id;

-- F2. Sebelum vs sesudah mengaktifkan Jadwal Bayar cicilan (satu baris per nasabah x produk yang pernah dijadwalkan)
WITH j AS (
    SELECT customer_id, product_code, min(created_at AT TIME ZONE 'Asia/Jakarta')::date AS tgl_aktif,
           bool_or(status = 'ACTIVE') AS masih_aktif
    FROM v_sched_cicilan GROUP BY customer_id, product_code
), x AS (
    SELECT j.customer_id, j.product_code, j.tgl_aktif, j.masih_aktif,
           count(*) FILTER (WHERE i.due_date <  j.tgl_aktif) AS n_sebelum,
           count(*) FILTER (WHERE i.due_date <  j.tgl_aktif AND i.days_late > 0) AS telat_sebelum,
           round(avg(i.days_late) FILTER (WHERE i.due_date < j.tgl_aktif), 2) AS rata_telat_sebelum,
           count(*) FILTER (WHERE i.due_date >= j.tgl_aktif) AS n_sesudah,
           count(*) FILTER (WHERE i.due_date >= j.tgl_aktif AND i.days_late > 0) AS telat_sesudah,
           round(avg(i.days_late) FILTER (WHERE i.due_date >= j.tgl_aktif), 2) AS rata_telat_sesudah
    FROM j
    LEFT JOIN v_inst i ON i.customer_id = j.customer_id AND i.product_code = j.product_code AND i.status = 'PAID'
    GROUP BY j.customer_id, j.product_code, j.tgl_aktif, j.masih_aktif
)
SELECT n.customer_id, n.full_name, n.occupation, x.product_code, x.tgl_aktif, x.masih_aktif,
       x.n_sebelum, x.telat_sebelum, round(100.0 * x.telat_sebelum / NULLIF(x.n_sebelum, 0), 1) AS pct_telat_sebelum, x.rata_telat_sebelum,
       x.n_sesudah, x.telat_sesudah, round(100.0 * x.telat_sesudah / NULLIF(x.n_sesudah, 0), 1) AS pct_telat_sesudah, x.rata_telat_sesudah,
       CASE WHEN x.n_sebelum < 3 OR x.n_sesudah < 3 THEN 'Data belum cukup (< 3 angsuran di salah satu sisi)'
            WHEN 1.0 * x.telat_sesudah / x.n_sesudah < 1.0 * x.telat_sebelum / x.n_sebelum THEN 'MEMBAIK'
            WHEN 1.0 * x.telat_sesudah / x.n_sesudah > 1.0 * x.telat_sebelum / x.n_sebelum THEN 'MEMBURUK'
            ELSE 'SAMA' END AS dampak
FROM x
JOIN v_nasabah n ON n.customer_id = x.customer_id
ORDER BY n.customer_id, x.product_code;

-- F3. Nasabah yang membayar cicilan rutin secara manual tetapi tidak punya Jadwal Bayar aktif (peluang aktivasi)
--     rutin = >= 3 pembayaran dalam 12 bulan terakhir dengan simpangan tanggal bayar <= 2 hari
--     prioritas TINGGI bila >= 30% pembayarannya telat, SEDANG bila sering membuka menu Jadwal Bayar (>= 3x), selain itu RENDAH
WITH p AS (
    SELECT customer_id, product_code, count(*) AS n_bayar,
           count(*) FILTER (WHERE payment_channel <> 'SCHEDULER') AS n_manual,
           round(avg(offset_hari), 1) AS rata_offset, round(stddev(offset_hari), 1) AS simpangan_offset,
           round(100.0 * count(*) FILTER (WHERE days_late > 0) / count(*)) AS pct_telat,
           round(sum(late_fee)) AS denda
    FROM v_inst
    WHERE status = 'PAID' AND paid_date >= DATE '2025-10-10'
    GROUP BY customer_id, product_code
    HAVING count(*) >= 3
), s AS (
    SELECT customer_id, product_code, count(*) AS pernah_jadwal, count(*) FILTER (WHERE status = 'ACTIVE') AS aktif
    FROM v_sched_cicilan GROUP BY customer_id, product_code
)
SELECT n.customer_id, n.full_name, n.occupation, p.product_code, p.n_bayar, p.n_manual,
       p.rata_offset, p.simpangan_offset, p.pct_telat, p.denda,
       CASE WHEN COALESCE(s.pernah_jadwal, 0) = 0 THEN 'Belum pernah' ELSE 'Pernah, sudah batal' END AS riwayat_jadwal,
       f.n_buka AS buka_menu_jadwal,
       CASE WHEN p.pct_telat >= 30 THEN 'TINGGI' WHEN f.n_buka >= 3 THEN 'SEDANG' ELSE 'RENDAH' END AS prioritas
FROM p
JOIN v_nasabah n ON n.customer_id = p.customer_id
LEFT JOIN s ON s.customer_id = p.customer_id AND s.product_code = p.product_code
LEFT JOIN v_nasabah_fitur f ON f.customer_id = p.customer_id AND f.feature_code = 'JADWAL_BAYAR'
WHERE COALESCE(s.aktif, 0) = 0 AND (p.simpangan_offset <= 2 OR p.simpangan_offset IS NULL)
ORDER BY CASE WHEN p.pct_telat >= 30 THEN 1 WHEN f.n_buka >= 3 THEN 2 ELSE 3 END, p.n_bayar DESC, n.customer_id;

-- F4. Rincian tiap jadwal (satu baris per jadwal bayar, lengkap dengan customer_id): kesehatan eksekusi, umur, alasan batal, posisi terhadap gajian
SELECT n.customer_id, n.full_name, n.occupation, sp.schedule_id, sp.payment_type, sp.label, sp.amount_mode,
       sp.day_of_month, sp.notify_before_days, sp.status, sp.cancel_reason,
       (sp.created_at AT TIME ZONE 'Asia/Jakarta')::date AS dibuat,
       (sp.cancelled_at AT TIME ZONE 'Asia/Jakarta')::date AS dibatalkan,
       round(extract(epoch FROM (COALESCE(sp.cancelled_at, TIMESTAMPTZ '2026-10-09') - sp.created_at)) / 86400) AS umur_hari,
       sp.run_count, sp.success_count, sp.fail_count,
       round(100.0 * sp.fail_count / NULLIF(sp.run_count, 0), 1) AS pct_gagal,
       (SELECT count(*) FROM transactions t WHERE t.scheduled_payment_id = sp.schedule_id AND t.failure_reason = 'SALDO TIDAK CUKUP') AS gagal_saldo,
       pd.tgl_modus AS tanggal_gajian,
       CASE WHEN pd.tgl_modus IS NULL THEN NULL
            ELSE ((sp.day_of_month - pd.tgl_modus) % 31 + 31) % 31 END AS hari_setelah_gajian
FROM scheduled_payments sp
JOIN v_nasabah n ON n.customer_id = sp.customer_id
LEFT JOIN v_payday pd ON pd.customer_id = sp.customer_id AND pd.kelas = 'TETAP'
ORDER BY n.customer_id, sp.schedule_id;

-- =====================================================================
-- G. PAYLATER & KTA  (per nasabah)
-- =====================================================================
-- G1. Posisi tiap nasabah di funnel paylater dan KTA (satu baris per nasabah x produk, semua nasabah)
--     BELUM PERNAH MEMBUKA -> HANYA MEMBUKA MENU -> DITOLAK / DISETUJUI BELUM DIPAKAI -> MEMAKAI
WITH prod(product_code) AS (VALUES ('PAYLATER'), ('KTA')),
buka AS (
    SELECT customer_id, feature_code AS product_code, n_buka, n_interaksi
    FROM v_nasabah_fitur WHERE feature_code IN ('PAYLATER', 'KTA')
), ap AS (
    SELECT customer_id, product_code,
           count(*) AS n_pengajuan,
           count(*) FILTER (WHERE status = 'APPROVED') AS n_disetujui,
           count(*) FILTER (WHERE status = 'REJECTED') AS n_ditolak,
           (array_agg(reject_reason ORDER BY applied_at DESC) FILTER (WHERE status = 'REJECTED'))[1] AS alasan_tolak_terakhir,
           max(approved_amount) AS limit_disetujui,
           (max(applied_at) AT TIME ZONE 'Asia/Jakarta')::date AS terakhir_mengajukan
    FROM loan_applications GROUP BY customer_id, product_code
), ln AS (
    SELECT customer_id, product_code, count(*) AS n_pinjaman,
           count(*) FILTER (WHERE status = 'ACTIVE') AS n_aktif,
           round(sum(principal)) AS total_cair,
           (min(disbursed_at) AT TIME ZONE 'Asia/Jakarta')::date AS pertama_cair
    FROM loans GROUP BY customer_id, product_code
)
SELECT n.customer_id, n.full_name, n.occupation, p.product_code,
       COALESCE(b.n_buka, 0) AS n_buka_menu, COALESCE(b.n_interaksi, 0) AS n_interaksi,
       COALESCE(a.n_pengajuan, 0) AS n_pengajuan, COALESCE(a.n_disetujui, 0) AS n_disetujui, COALESCE(a.n_ditolak, 0) AS n_ditolak,
       a.alasan_tolak_terakhir, round(a.limit_disetujui) AS limit_disetujui,
       COALESCE(l.n_pinjaman, 0) AS n_pinjaman, COALESCE(l.n_aktif, 0) AS n_aktif, l.total_cair, l.pertama_cair,
       CASE WHEN COALESCE(l.n_pinjaman, 0) > 0       THEN 'MEMAKAI'
            WHEN COALESCE(a.n_disetujui, 0) > 0      THEN 'DISETUJUI BELUM DIPAKAI'
            WHEN COALESCE(a.n_ditolak, 0) > 0        THEN 'DITOLAK'
            WHEN COALESCE(b.n_buka, 0) > 0           THEN 'HANYA MEMBUKA MENU'
            ELSE 'BELUM PERNAH MEMBUKA' END AS posisi_funnel
FROM v_nasabah n
CROSS JOIN prod p
LEFT JOIN buka b ON b.customer_id = n.customer_id AND b.product_code = p.product_code
LEFT JOIN ap a   ON a.customer_id = n.customer_id AND a.product_code = p.product_code
LEFT JOIN ln l   ON l.customer_id = n.customer_id AND l.product_code = p.product_code
ORDER BY n.customer_id, p.product_code;

-- G2. Pola pemakaian paylater per nasabah (hanya nasabah yang pernah memakai): frekuensi, nominal, kategori, waktu dalam siklus gaji, utilisasi limit
WITH pl AS (
    SELECT l.customer_id, l.loan_id, l.principal, l.tenor_months, l.merchant_category, l.status,
           (l.disbursed_at AT TIME ZONE 'Asia/Jakarta')::date AS tgl,
           extract(day FROM l.disbursed_at AT TIME ZONE 'Asia/Jakarta')::int AS dom
    FROM loans l WHERE l.product_code = 'PAYLATER'
), s AS (
    SELECT pl.customer_id,
           count(*)                                                AS pembelian_total,
           count(*) FILTER (WHERE tgl >= DATE '2025-10-10')        AS pembelian_12bln,
           round(sum(principal) FILTER (WHERE tgl >= DATE '2025-10-10')) AS nominal_12bln,
           round(avg(principal))                                   AS rata_nominal,
           round(avg(tenor_months), 1)                             AS rata_tenor,
           mode() WITHIN GROUP (ORDER BY merchant_category)        AS kategori_terbanyak,
           max(tgl)                                                AS pembelian_terakhir,
           round(100.0 * count(*) FILTER (WHERE pd.tgl_modus IS NOT NULL AND ((dom - pd.tgl_modus) % 31 + 31) % 31 >= 21)
                 / NULLIF(count(*) FILTER (WHERE pd.tgl_modus IS NOT NULL), 0)) AS pct_beli_menjelang_gajian
    FROM pl
    LEFT JOIN v_payday pd ON pd.customer_id = pl.customer_id AND pd.kelas = 'TETAP'
    GROUP BY pl.customer_id
), lim AS (
    SELECT DISTINCT ON (customer_id) customer_id, approved_amount AS limit_paylater
    FROM loan_applications WHERE product_code = 'PAYLATER' AND status = 'APPROVED'
    ORDER BY customer_id, applied_at DESC
), o AS (
    SELECT i.customer_id, round(sum(i.principal_part)) AS sisa_pokok
    FROM v_inst i WHERE i.product_code = 'PAYLATER' AND i.status IN ('UNPAID', 'UPCOMING')
    GROUP BY i.customer_id
)
SELECT n.customer_id, n.full_name, n.occupation,
       s.pembelian_total, s.pembelian_12bln, s.nominal_12bln, s.rata_nominal, s.rata_tenor,
       s.kategori_terbanyak, s.pembelian_terakhir, s.pct_beli_menjelang_gajian,
       lim.limit_paylater, o.sisa_pokok,
       round(100.0 * o.sisa_pokok / NULLIF(lim.limit_paylater, 0), 1) AS utilisasi_limit_pct,
       CASE WHEN round(100.0 * o.sisa_pokok / NULLIF(lim.limit_paylater, 0)) >= 80 THEN 'Hampir penuh'
            WHEN round(100.0 * o.sisa_pokok / NULLIF(lim.limit_paylater, 0)) >= 40 THEN 'Sedang'
            ELSE 'Longgar' END AS kategori_utilisasi
FROM s
JOIN v_nasabah n ON n.customer_id = s.customer_id
LEFT JOIN lim ON lim.customer_id = s.customer_id
LEFT JOIN o   ON o.customer_id = s.customer_id
ORDER BY s.pembelian_12bln DESC, n.customer_id;

-- G3. KTA: satu baris per pinjaman KTA (dengan customer_id) - kemajuan pembayaran dan ke mana dana dipakai dalam 7 hari pertama
SELECT n.customer_id, n.full_name, n.occupation, l.loan_id, l.status,
       (l.disbursed_at AT TIME ZONE 'Asia/Jakarta')::date AS tgl_cair,
       round(l.principal) AS pokok, l.tenor_months, l.interest_rate_pa, round(l.installment_amount) AS cicilan_bulanan,
       i.n_dibayar, i.n_total, i.n_telat, i.n_belum_bayar,
       round(use7.transfer_keluar) AS transfer_keluar_7h, round(use7.tarik_tunai) AS tarik_tunai_7h,
       round(use7.belanja) AS belanja_7h, round(use7.tabungan) AS ditabung_7h, round(use7.tagihan_cicilan) AS bayar_tagihan_7h,
       round(100.0 * (use7.transfer_keluar + use7.tarik_tunai + use7.belanja + use7.tabungan + use7.tagihan_cicilan) / l.principal) AS pct_pokok_terpakai_7h,
       CASE GREATEST(use7.transfer_keluar, use7.tarik_tunai, use7.belanja, use7.tabungan, use7.tagihan_cicilan)
            WHEN 0 THEN '-'
            WHEN use7.transfer_keluar THEN 'Transfer keluar'
            WHEN use7.tarik_tunai     THEN 'Tarik tunai'
            WHEN use7.belanja         THEN 'Belanja'
            WHEN use7.tabungan        THEN 'Ditabung'
            ELSE 'Bayar tagihan/cicilan' END AS penggunaan_utama_7h
FROM loans l
JOIN v_nasabah n ON n.customer_id = l.customer_id
LEFT JOIN LATERAL (
    SELECT count(*) FILTER (WHERE status = 'PAID') AS n_dibayar, count(*) AS n_total,
           count(*) FILTER (WHERE status = 'PAID' AND days_late > 0) AS n_telat,
           count(*) FILTER (WHERE status = 'UNPAID') AS n_belum_bayar
    FROM loan_installments WHERE loan_id = l.loan_id AND status <> 'CANCELLED'
) i ON true
LEFT JOIN LATERAL (
    SELECT COALESCE(sum(t.amount) FILTER (WHERE t.spend_group = 'TRANSFER' AND t.flow = 'EXPENSE'), 0) AS transfer_keluar,
           COALESCE(sum(t.amount) FILTER (WHERE t.spend_group = 'TUNAI'), 0) AS tarik_tunai,
           COALESCE(sum(t.amount) FILTER (WHERE t.spend_group IN ('CASHLESS_APP', 'KARTU')), 0) AS belanja,
           COALESCE(sum(t.amount) FILTER (WHERE t.transaction_type = 'SAVINGS_IN'), 0) AS tabungan,
           COALESCE(sum(t.amount) FILTER (WHERE t.spend_group IN ('TAGIHAN', 'CICILAN')), 0) AS tagihan_cicilan
    FROM v_trx_ok t
    WHERE t.customer_id = l.customer_id AND t.created_at > l.disbursed_at
      AND t.created_at <= l.disbursed_at + interval '7 days' AND t.transaction_id <> COALESCE(l.disbursement_transaction_id, -1)
) use7 ON true
WHERE l.product_code = 'KTA'
ORDER BY n.customer_id, l.disbursed_at;

-- G4. Profil risiko kredit per nasabah (hanya nasabah yang punya pinjaman aktif):
--     skor = beban cicilan (maks 35) + bulan defisit (maks 30) + keterlambatan (maks 20) + tumpukan pinjaman (maks 15)
CREATE OR REPLACE VIEW vp_risiko_kredit AS
SELECT c.customer_id, c.full_name, c.occupation, c.pinjaman_aktif, c.paylater_aktif, c.kta_aktif,
       c.cicilan_bulanan, c.beban_pct_penghasilan, c.kategori_beban, c.sisa_pokok,
       a.kategori_arus, a.bulan_defisit, a.rasio_keluar_masuk,
       c.n_bayar_6bln, c.pct_telat_6bln, c.n_telat_gt3h_6bln,
       round(CASE c.kategori_beban WHEN 'Sangat berat (>= 40%)' THEN 35 WHEN 'Berat (25-40%)' THEN 25
                                   WHEN 'Sedang (10-25%)' THEN 12 ELSE 0 END
           + LEAST(COALESCE(a.bulan_defisit, 0) * 3, 30)
           + LEAST(COALESCE(c.pct_telat_6bln, 0) * 0.2, 20)
           + CASE WHEN c.pinjaman_aktif >= 7 THEN 15 WHEN c.pinjaman_aktif >= 4 THEN 8 WHEN c.pinjaman_aktif >= 2 THEN 3 ELSE 0 END) AS skor_risiko,
       CASE WHEN round(CASE c.kategori_beban WHEN 'Sangat berat (>= 40%)' THEN 35 WHEN 'Berat (25-40%)' THEN 25 WHEN 'Sedang (10-25%)' THEN 12 ELSE 0 END
                     + LEAST(COALESCE(a.bulan_defisit, 0) * 3, 30) + LEAST(COALESCE(c.pct_telat_6bln, 0) * 0.2, 20)
                     + CASE WHEN c.pinjaman_aktif >= 7 THEN 15 WHEN c.pinjaman_aktif >= 4 THEN 8 WHEN c.pinjaman_aktif >= 2 THEN 3 ELSE 0 END) >= 60 THEN 'KRITIS'
            WHEN round(CASE c.kategori_beban WHEN 'Sangat berat (>= 40%)' THEN 35 WHEN 'Berat (25-40%)' THEN 25 WHEN 'Sedang (10-25%)' THEN 12 ELSE 0 END
                     + LEAST(COALESCE(a.bulan_defisit, 0) * 3, 30) + LEAST(COALESCE(c.pct_telat_6bln, 0) * 0.2, 20)
                     + CASE WHEN c.pinjaman_aktif >= 7 THEN 15 WHEN c.pinjaman_aktif >= 4 THEN 8 WHEN c.pinjaman_aktif >= 2 THEN 3 ELSE 0 END) >= 40 THEN 'WASPADA'
            WHEN round(CASE c.kategori_beban WHEN 'Sangat berat (>= 40%)' THEN 35 WHEN 'Berat (25-40%)' THEN 25 WHEN 'Sedang (10-25%)' THEN 12 ELSE 0 END
                     + LEAST(COALESCE(a.bulan_defisit, 0) * 3, 30) + LEAST(COALESCE(c.pct_telat_6bln, 0) * 0.2, 20)
                     + CASE WHEN c.pinjaman_aktif >= 7 THEN 15 WHEN c.pinjaman_aktif >= 4 THEN 8 WHEN c.pinjaman_aktif >= 2 THEN 3 ELSE 0 END) >= 20 THEN 'PANTAU'
            ELSE 'SEHAT' END AS tingkat_risiko
FROM vp_cicilan c
LEFT JOIN vp_arus a ON a.customer_id = c.customer_id
WHERE c.pinjaman_aktif > 0;

SELECT * FROM vp_risiko_kredit ORDER BY skor_risiko DESC, customer_id;

-- =====================================================================
-- H. KARTU, VISA & CASHLESS  (per nasabah)
-- =====================================================================
-- H1. Satu baris per kartu (dengan customer_id): jaringan, pemakaian, kartu tidur, jeda aktivasi online/luar negeri ke pemakaian pertama
SELECT n.customer_id, n.full_name, n.occupation, c.card_id, c.network, c.card_type, c.status, c.is_contactless,
       (c.online_enabled_at AT TIME ZONE 'Asia/Jakarta')::date AS online_aktif_sejak,
       (c.intl_enabled_at   AT TIME ZONE 'Asia/Jakarta')::date AS luar_negeri_aktif_sejak,
       count(t.*)                                              AS trx_total,
       count(t.*) FILTER (WHERE t.trx_date >= DATE '2025-10-10') AS trx_12bln,
       round(sum(t.amount))                                    AS nominal_total,
       round(avg(t.amount))                                    AS rata_tiket,
       round(100.0 * count(*) FILTER (WHERE t.transaction_type = 'CARD_CONTACTLESS') / NULLIF(count(t.*), 0)) AS pct_contactless,
       round(100.0 * count(*) FILTER (WHERE t.transaction_type = 'CARD_POS')         / NULLIF(count(t.*), 0)) AS pct_pos,
       round(100.0 * count(*) FILTER (WHERE t.transaction_type = 'CARD_ONLINE')      / NULLIF(count(t.*), 0)) AS pct_online,
       round(100.0 * count(*) FILTER (WHERE t.transaction_type = 'CARD_INTL')        / NULLIF(count(t.*), 0)) AS pct_luar_negeri,
       max(t.trx_date)                                         AS terakhir_dipakai,
       DATE '2026-10-09' - max(t.trx_date)                     AS hari_sejak_terakhir_pakai,
       CASE WHEN count(t.*) = 0 THEN 'TIDAK PERNAH DIPAKAI'
            WHEN DATE '2026-10-09' - max(t.trx_date) > 90 THEN 'TIDUR (> 90 hari)'
            ELSE 'AKTIF' END                                   AS status_pemakaian,
       (min(t.created_at) FILTER (WHERE t.transaction_type = 'CARD_ONLINE')::date - c.online_enabled_at::date) AS hari_aktivasi_online_ke_pakai,
       (min(t.created_at) FILTER (WHERE t.transaction_type = 'CARD_INTL')::date   - c.intl_enabled_at::date)   AS hari_aktivasi_luar_negeri_ke_pakai
FROM cards c
JOIN accounts a ON a.account_id = c.account_id
JOIN v_nasabah n ON n.customer_id = a.customer_id
LEFT JOIN v_trx_ok t ON t.card_id = c.card_id
GROUP BY n.customer_id, n.full_name, n.occupation, c.card_id, c.network, c.card_type, c.status, c.is_contactless,
         c.online_enabled_at, c.intl_enabled_at
ORDER BY n.customer_id, c.card_id;

-- H2. Kebiasaan cashless per nasabah: porsi belanja non-tunai (QRIS + e-wallet + pulsa + kartu) per kuartal terakhir,
--     kontribusi Visa, dan transaksi luar negeri (12 bulan terakhir)
WITH q AS (
    SELECT t.customer_id,
           sum(t.amount) FILTER (WHERE t.trx_date BETWEEN DATE '2025-10-01' AND DATE '2025-12-31' AND t.spend_group IN ('CASHLESS_APP','KARTU')) AS c1,
           sum(t.amount) FILTER (WHERE t.trx_date BETWEEN DATE '2025-10-01' AND DATE '2025-12-31' AND t.spend_group IN ('CASHLESS_APP','KARTU','TUNAI')) AS b1,
           sum(t.amount) FILTER (WHERE t.trx_date BETWEEN DATE '2026-01-01' AND DATE '2026-03-31' AND t.spend_group IN ('CASHLESS_APP','KARTU')) AS c2,
           sum(t.amount) FILTER (WHERE t.trx_date BETWEEN DATE '2026-01-01' AND DATE '2026-03-31' AND t.spend_group IN ('CASHLESS_APP','KARTU','TUNAI')) AS b2,
           sum(t.amount) FILTER (WHERE t.trx_date BETWEEN DATE '2026-04-01' AND DATE '2026-06-30' AND t.spend_group IN ('CASHLESS_APP','KARTU')) AS c3,
           sum(t.amount) FILTER (WHERE t.trx_date BETWEEN DATE '2026-04-01' AND DATE '2026-06-30' AND t.spend_group IN ('CASHLESS_APP','KARTU','TUNAI')) AS b3,
           sum(t.amount) FILTER (WHERE t.trx_date BETWEEN DATE '2026-07-01' AND DATE '2026-09-30' AND t.spend_group IN ('CASHLESS_APP','KARTU')) AS c4,
           sum(t.amount) FILTER (WHERE t.trx_date BETWEEN DATE '2026-07-01' AND DATE '2026-09-30' AND t.spend_group IN ('CASHLESS_APP','KARTU','TUNAI')) AS b4,
           sum(t.amount) FILTER (WHERE t.spend_group IN ('CASHLESS_APP','KARTU'))  AS cashless_12bln,
           sum(t.amount) FILTER (WHERE t.spend_group = 'TUNAI')                    AS tunai_12bln,
           sum(t.amount) FILTER (WHERE t.spend_group = 'KARTU' AND c.network = 'VISA') AS visa_12bln,
           sum(t.amount) FILTER (WHERE t.spend_group = 'KARTU')                    AS kartu_12bln,
           count(*) FILTER (WHERE t.transaction_type = 'CARD_INTL')                AS trx_luar_negeri,
           round(sum(t.amount) FILTER (WHERE t.transaction_type = 'CARD_INTL'))    AS nominal_luar_negeri,
           mode() WITHIN GROUP (ORDER BY tr.currency_original) FILTER (WHERE tr.currency_original IS NOT NULL) AS mata_uang_asing_utama
    FROM v_trx_ok t
    JOIN transactions tr ON tr.transaction_id = t.transaction_id
    LEFT JOIN cards c ON c.card_id = t.card_id
    WHERE t.flow = 'EXPENSE' AND t.trx_date >= DATE '2025-10-01' AND t.trx_date <= DATE '2026-09-30'
    GROUP BY t.customer_id
)
SELECT n.customer_id, n.full_name, n.occupation, n.age_band,
       round(100.0 * q.c1 / NULLIF(q.b1, 0), 1) AS pct_cashless_q4_2025,
       round(100.0 * q.c2 / NULLIF(q.b2, 0), 1) AS pct_cashless_q1_2026,
       round(100.0 * q.c3 / NULLIF(q.b3, 0), 1) AS pct_cashless_q2_2026,
       round(100.0 * q.c4 / NULLIF(q.b4, 0), 1) AS pct_cashless_q3_2026,
       round(100.0 * q.cashless_12bln / NULLIF(q.cashless_12bln + q.tunai_12bln, 0), 1) AS pct_cashless_12bln,
       round(q.cashless_12bln) AS cashless_12bln, round(q.tunai_12bln) AS tunai_12bln,
       round(100.0 * q.c4 / NULLIF(q.b4, 0) - 100.0 * q.c1 / NULLIF(q.b1, 0), 1) AS perubahan_poin,
       CASE WHEN 100.0 * q.cashless_12bln / NULLIF(q.cashless_12bln + q.tunai_12bln, 0) >= 95 THEN 'Hampir sepenuhnya cashless'
            WHEN 100.0 * q.cashless_12bln / NULLIF(q.cashless_12bln + q.tunai_12bln, 0) >= 80 THEN 'Dominan cashless'
            WHEN 100.0 * q.cashless_12bln / NULLIF(q.cashless_12bln + q.tunai_12bln, 0) >= 50 THEN 'Campuran'
            ELSE 'Masih dominan tunai' END AS kategori_cashless,
       round(100.0 * q.visa_12bln / NULLIF(q.kartu_12bln, 0), 1) AS pct_kartu_via_visa,
       q.trx_luar_negeri, q.nominal_luar_negeri, q.mata_uang_asing_utama
FROM v_nasabah n
LEFT JOIN q ON q.customer_id = n.customer_id
ORDER BY n.customer_id;

-- H3. Transaksi kartu yang ditolak per nasabah (12 bulan terakhir)
SELECT n.customer_id, n.full_name, n.occupation,
       count(*) FILTER (WHERE tr.status = 'SUCCESS') AS kartu_sukses,
       count(*) FILTER (WHERE tr.status = 'FAILED')  AS kartu_ditolak,
       round(100.0 * count(*) FILTER (WHERE tr.status = 'FAILED') / count(*), 2) AS pct_ditolak,
       count(*) FILTER (WHERE tr.failure_reason = 'SALDO TIDAK CUKUP')                 AS ditolak_saldo,
       count(*) FILTER (WHERE tr.failure_reason IN ('TIMEOUT', 'GANGGUAN JARINGAN'))   AS ditolak_teknis,
       count(*) FILTER (WHERE tr.status = 'FAILED' AND tr.transaction_type = 'CARD_ONLINE')      AS ditolak_online,
       count(*) FILTER (WHERE tr.status = 'FAILED' AND tr.transaction_type = 'CARD_CONTACTLESS') AS ditolak_contactless,
       count(*) FILTER (WHERE tr.status = 'FAILED' AND tr.transaction_type = 'CARD_POS')         AS ditolak_pos,
       count(*) FILTER (WHERE tr.status = 'FAILED' AND tr.transaction_type = 'CARD_INTL')        AS ditolak_luar_negeri
FROM transactions tr
JOIN accounts a ON a.account_id = tr.account_id
JOIN v_nasabah n ON n.customer_id = a.customer_id
WHERE tr.transaction_type LIKE 'CARD\_%' AND tr.created_at >= TIMESTAMPTZ '2025-10-10'
GROUP BY n.customer_id, n.full_name, n.occupation
ORDER BY ditolak_saldo DESC, n.customer_id;

-- =====================================================================
-- I. ENGAGEMENT  (per nasabah)
-- =====================================================================
-- I1. Aktivitas bulanan (satu baris per nasabah x bulan): hari aktif, sesi, transaksi, dan stickiness (hari aktif / hari dalam bulan)
WITH s AS (
    SELECT user_id, login_month, count(*) AS sesi, count(DISTINCT login_date) AS hari_aktif
    FROM v_session GROUP BY user_id, login_month
)
SELECT n.customer_id, n.full_name, n.occupation, s.login_month AS bulan, s.sesi, s.hari_aktif,
       COALESCE(c.n_expense_trx, 0) AS trx_keluar,
       CASE WHEN s.login_month = DATE '2026-10-01' THEN 9
            ELSE extract(day FROM (s.login_month + interval '1 month' - interval '1 day'))::int END AS hari_dalam_periode,
       round(100.0 * s.hari_aktif / CASE WHEN s.login_month = DATE '2026-10-01' THEN 9
            ELSE extract(day FROM (s.login_month + interval '1 month' - interval '1 day')) END, 1) AS stickiness_pct,
       CASE WHEN s.hari_aktif >= 0.5 * CASE WHEN s.login_month = DATE '2026-10-01' THEN 9
                 ELSE extract(day FROM (s.login_month + interval '1 month' - interval '1 day')) END THEN 'Sangat sering'
            WHEN s.hari_aktif >= 0.2 * CASE WHEN s.login_month = DATE '2026-10-01' THEN 9
                 ELSE extract(day FROM (s.login_month + interval '1 month' - interval '1 day')) END THEN 'Rutin'
            ELSE 'Jarang' END AS tingkat_aktivitas,
       s.sesi - lag(s.sesi) OVER (PARTITION BY n.customer_id ORDER BY s.login_month) AS selisih_sesi_dari_bulan_lalu
FROM s
JOIN v_nasabah n ON n.user_id = s.user_id
LEFT JOIN v_cust_month c ON c.customer_id = n.customer_id AND c.trx_month = s.login_month
ORDER BY n.customer_id, s.login_month;

-- I2. Siklus hidup nasabah di aplikasi: registrasi, bulan aktif, login terakhir, dan tahap (BARU / AKTIF / MENURUN / TIDUR / CHURN)
CREATE OR REPLACE VIEW vp_siklus AS
WITH s AS (
    SELECT user_id, min(login_date) AS login_pertama, max(login_date) AS login_terakhir,
           count(*) AS sesi_total, count(DISTINCT login_month) AS bulan_aktif
    FROM v_session GROUP BY user_id
)
SELECT n.customer_id, n.full_name, n.occupation, n.user_status,
       (n.registered_at AT TIME ZONE 'Asia/Jakarta')::date AS tgl_registrasi,
       ((DATE '2026-10-09' - (n.registered_at AT TIME ZONE 'Asia/Jakarta')::date) / 30)::int AS usia_bulan,
       s.login_pertama, s.login_terakhir, s.sesi_total, s.bulan_aktif,
       DATE '2026-10-09' - s.login_terakhir AS hari_sejak_login_terakhir,
       round(100.0 * s.bulan_aktif / GREATEST(((DATE '2026-10-09' - (n.registered_at AT TIME ZONE 'Asia/Jakarta')::date) / 30) + 1, 1)) AS pct_bulan_aktif,
       CASE WHEN n.user_status = 'INACTIVE'                         THEN 'CHURN (dinonaktifkan)'
            WHEN s.login_terakhir IS NULL                           THEN 'BELUM PERNAH LOGIN'
            WHEN DATE '2026-10-09' - s.login_terakhir > 60          THEN 'TIDUR (> 60 hari)'
            WHEN DATE '2026-10-09' - s.login_terakhir > 30          THEN 'MENURUN (> 30 hari)'
            WHEN (DATE '2026-10-09' - (n.registered_at AT TIME ZONE 'Asia/Jakarta')::date) <= 90 THEN 'BARU (<= 3 bulan)'
            ELSE 'AKTIF' END AS tahap
FROM v_nasabah n
LEFT JOIN s ON s.user_id = n.user_id;

SELECT * FROM vp_siklus ORDER BY customer_id;

-- I3. Cara login dan kegagalan login per nasabah (12 bulan terakhir)
SELECT n.customer_id, n.full_name, n.occupation, n.biometric_enabled,
       count(*) AS percobaan_login,
       round(100.0 * count(*) FILTER (WHERE l.login_method = 'BIOMETRIC') / count(*), 1) AS pct_biometrik,
       round(100.0 * count(*) FILTER (WHERE l.login_method = 'PIN')       / count(*), 1) AS pct_pin,
       round(100.0 * count(*) FILTER (WHERE l.login_method = 'PASSWORD')  / count(*), 1) AS pct_password,
       count(*) FILTER (WHERE l.status = 'FAILED')                                     AS login_gagal,
       round(100.0 * count(*) FILTER (WHERE l.status = 'FAILED') / count(*), 1)        AS pct_gagal,
       count(*) FILTER (WHERE l.failure_reason = 'BIOMETRIK_GAGAL') AS gagal_biometrik,
       count(*) FILTER (WHERE l.failure_reason = 'PIN_SALAH')       AS gagal_pin,
       count(*) FILTER (WHERE l.failure_reason = 'PASSWORD_SALAH')  AS gagal_password,
       count(DISTINCT l.device_id)                                  AS jml_perangkat_dipakai
FROM v_nasabah n
JOIN login_logs l ON l.user_id = n.user_id AND l.login_at >= TIMESTAMPTZ '2025-10-10'
GROUP BY n.customer_id, n.full_name, n.occupation, n.biometric_enabled
ORDER BY n.customer_id;

-- I4. Respons terhadap notifikasi per nasabah (seluruh riwayat)
SELECT n.customer_id, n.full_name, n.occupation, n.push_enabled,
       count(x.*) FILTER (WHERE x.category = 'PROMO')             AS promo_kirim,
       count(x.opened_at) FILTER (WHERE x.category = 'PROMO')     AS promo_buka,
       count(x.*) FILTER (WHERE x.category = 'GAJI_MASUK')        AS gaji_kirim,
       count(x.opened_at) FILTER (WHERE x.category = 'GAJI_MASUK') AS gaji_buka,
       count(x.*) FILTER (WHERE x.category = 'DUE_REMINDER')      AS pengingat_tagihan_kirim,
       count(x.opened_at) FILTER (WHERE x.category = 'DUE_REMINDER') AS pengingat_tagihan_buka,
       count(x.*) FILTER (WHERE x.category = 'SCHEDULE_REMINDER') AS pengingat_jadwal_kirim,
       count(x.opened_at) FILTER (WHERE x.category = 'SCHEDULE_REMINDER') AS pengingat_jadwal_buka,
       count(x.*)                                                  AS total_kirim,
       round(100.0 * count(x.opened_at) / NULLIF(count(x.*), 0), 1) AS pct_dibuka,
       round((percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM x.opened_at - x.sent_at) / 60))::numeric, 1) AS median_menit_ke_buka,
       round(100.0 * count(*) FILTER (WHERE x.login_3jam) / NULLIF(count(x.*), 0), 1) AS pct_diikuti_login_3jam
FROM v_nasabah n
LEFT JOIN LATERAL (
    SELECT nt.category, nt.sent_at, nt.opened_at,
           EXISTS (SELECT 1 FROM login_logs l WHERE l.user_id = nt.user_id AND l.status = 'SUCCESS'
                     AND l.login_at BETWEEN nt.sent_at AND nt.sent_at + interval '3 hours') AS login_3jam
    FROM notifications nt WHERE nt.user_id = n.user_id
) x ON true
GROUP BY n.customer_id, n.full_name, n.occupation, n.push_enabled
ORDER BY n.customer_id;

-- I5. Transaksi gagal per nasabah (12 bulan terakhir, di luar eksekusi sistem): seberapa sering gagal, mengapa, dan apakah dicoba lagi
WITH f AS (
    SELECT a.customer_id, tr.status, tr.failure_reason, tr.channel,
           CASE WHEN tr.status = 'FAILED' AND tr.channel = 'MOBILE' THEN
                EXISTS (SELECT 1 FROM transactions s
                        WHERE s.account_id = tr.account_id AND s.transaction_type = tr.transaction_type
                          AND s.status = 'SUCCESS' AND s.created_at > tr.created_at
                          AND s.created_at <= tr.created_at + interval '30 minutes') END AS coba_lagi_berhasil
    FROM transactions tr JOIN accounts a ON a.account_id = tr.account_id
    WHERE tr.created_at >= TIMESTAMPTZ '2025-10-10' AND tr.channel <> 'SYSTEM'
)
SELECT n.customer_id, n.full_name, n.occupation,
       count(*)                                                                      AS percobaan,
       count(*) FILTER (WHERE f.status = 'FAILED')                                   AS gagal,
       round(100.0 * count(*) FILTER (WHERE f.status = 'FAILED') / count(*), 2)      AS pct_gagal,
       count(*) FILTER (WHERE f.failure_reason = 'SALDO TIDAK CUKUP')                AS gagal_saldo,
       count(*) FILTER (WHERE f.failure_reason IN ('TIMEOUT', 'GANGGUAN JARINGAN'))  AS gagal_teknis,
       count(*) FILTER (WHERE f.failure_reason = 'TRANSAKSI DITOLAK BANK PENERIMA')  AS ditolak_bank_penerima,
       count(*) FILTER (WHERE f.failure_reason = 'LIMIT HARIAN TERLAMPAUI')          AS limit_harian,
       round(100.0 * count(*) FILTER (WHERE f.coba_lagi_berhasil)
             / NULLIF(count(*) FILTER (WHERE f.coba_lagi_berhasil IS NOT NULL), 0), 1) AS pct_gagal_mobile_berhasil_30mnt,
       CASE WHEN 100.0 * count(*) FILTER (WHERE f.status = 'FAILED') / count(*) >= 15 THEN 'TINGGI'
            WHEN 100.0 * count(*) FILTER (WHERE f.status = 'FAILED') / count(*) >= 5  THEN 'SEDANG'
            ELSE 'RENDAH' END AS tingkat_kegagalan
FROM f
JOIN v_nasabah n ON n.customer_id = f.customer_id
GROUP BY n.customer_id, n.full_name, n.occupation
ORDER BY pct_gagal DESC, n.customer_id;

-- I6. Segmentasi RFM per nasabah (90 hari terakhir): Recency login, Frequency transaksi keluar, Monetary nominal keluar
CREATE OR REPLACE VIEW vp_rfm AS
WITH r AS (
    SELECT n.customer_id, n.full_name, n.occupation,
           DATE '2026-10-09' - (SELECT max(s.login_date) FROM v_session s WHERE s.user_id = n.user_id) AS recency_hari,
           COALESCE(t.frekuensi, 0) AS frekuensi, COALESCE(t.moneter, 0) AS moneter
    FROM v_nasabah n
    LEFT JOIN (SELECT customer_id, count(*) AS frekuensi, sum(amount + fee) AS moneter
               FROM v_trx_ok WHERE flow = 'EXPENSE' AND trx_date >= DATE '2026-07-11' GROUP BY customer_id) t
           ON t.customer_id = n.customer_id
), s AS (
    SELECT *, ntile(4) OVER (ORDER BY recency_hari DESC NULLS FIRST) AS skor_r,
              ntile(4) OVER (ORDER BY frekuensi) AS skor_f,
              ntile(4) OVER (ORDER BY moneter)   AS skor_m
    FROM r
)
SELECT customer_id, full_name, occupation, recency_hari, frekuensi AS frekuensi_90h, round(moneter) AS moneter_90h,
       skor_r, skor_f, skor_m, skor_r + skor_f + skor_m AS skor_total,
       CASE WHEN recency_hari > 60 OR recency_hari IS NULL THEN 'Hilang (tidak login > 60 hari)'
            WHEN skor_r + skor_f + skor_m >= 10 THEN 'Juara (aktif, sering, nominal besar)'
            WHEN skor_r + skor_f + skor_m >= 8  THEN 'Setia'
            WHEN skor_r + skor_f + skor_m >= 6  THEN 'Potensial'
            ELSE 'Berisiko' END AS segmen
FROM s;

SELECT * FROM vp_rfm ORDER BY skor_total DESC, customer_id;

-- I7. Peringatan dini churn per nasabah: login 90 hari terakhir dibanding 90 hari sebelumnya
CREATE OR REPLACE VIEW vp_churn AS
WITH w AS (
    SELECT user_id,
           count(*) FILTER (WHERE login_date >= DATE '2026-07-10')                                  AS login_baru,
           count(*) FILTER (WHERE login_date >= DATE '2026-04-10' AND login_date < DATE '2026-07-10') AS login_lama,
           max(login_date) AS login_terakhir
    FROM v_session GROUP BY user_id
), t AS (
    SELECT customer_id,
           count(*) FILTER (WHERE trx_date >= DATE '2026-07-10') AS trx_baru,
           count(*) FILTER (WHERE trx_date >= DATE '2026-04-10' AND trx_date < DATE '2026-07-10') AS trx_lama
    FROM v_trx_ok WHERE flow = 'EXPENSE' GROUP BY customer_id
)
SELECT n.customer_id, n.full_name, n.occupation, n.user_status,
       COALESCE(w.login_lama, 0) AS login_90h_sebelumnya, COALESCE(w.login_baru, 0) AS login_90h_terakhir,
       round(100.0 * (COALESCE(w.login_baru, 0) - w.login_lama) / NULLIF(w.login_lama, 0)) AS perubahan_login_pct,
       COALESCE(t.trx_lama, 0) AS trx_90h_sebelumnya, COALESCE(t.trx_baru, 0) AS trx_90h_terakhir,
       round(100.0 * (COALESCE(t.trx_baru, 0) - t.trx_lama) / NULLIF(t.trx_lama, 0)) AS perubahan_trx_pct,
       DATE '2026-10-09' - w.login_terakhir AS hari_sejak_login_terakhir,
       CASE WHEN n.user_status = 'INACTIVE' THEN 'SUDAH NONAKTIF'
            WHEN COALESCE(w.login_lama, 0) >= 20 AND COALESCE(w.login_baru, 0) < 0.2 * w.login_lama THEN 'KRITIS'
            WHEN COALESCE(w.login_lama, 0) >= 20 AND COALESCE(w.login_baru, 0) < 0.5 * w.login_lama THEN 'WASPADA'
            WHEN DATE '2026-10-09' - w.login_terakhir > 30 THEN 'WASPADA'
            WHEN COALESCE(w.login_lama, 0) < 20 AND COALESCE(w.login_baru, 0) < 20 THEN 'AKTIVITAS RENDAH'
            ELSE 'NORMAL' END AS peringatan
FROM v_nasabah n
LEFT JOIN w ON w.user_id = n.user_id
LEFT JOIN t ON t.customer_id = n.customer_id;

SELECT * FROM vp_churn ORDER BY CASE peringatan WHEN 'KRITIS' THEN 1 WHEN 'WASPADA' THEN 2 WHEN 'SUDAH NONAKTIF' THEN 3 WHEN 'AKTIVITAS RENDAH' THEN 4 ELSE 5 END,
                                perubahan_login_pct, customer_id;

-- I8. Perangkat per nasabah: perangkat utama, versi aplikasi, dan tingkat gagal teknis transaksi mobile (12 bulan terakhir)
WITH d AS (
    SELECT user_id, count(*) AS jml_perangkat, count(*) FILTER (WHERE is_active) AS perangkat_aktif,
           (array_agg(device_name ORDER BY last_used_at DESC NULLS LAST))[1] AS perangkat_utama,
           (array_agg(os ORDER BY last_used_at DESC NULLS LAST))[1] AS os_utama,
           (array_agg(app_version ORDER BY last_used_at DESC NULLS LAST))[1] AS versi_aplikasi,
           (max(last_used_at) AT TIME ZONE 'Asia/Jakarta')::date AS terakhir_dipakai
    FROM devices GROUP BY user_id
), t AS (
    SELECT a.customer_id, count(*) AS percobaan_mobile,
           count(*) FILTER (WHERE tr.failure_reason IN ('TIMEOUT', 'GANGGUAN JARINGAN')) AS gagal_teknis
    FROM transactions tr JOIN accounts a ON a.account_id = tr.account_id
    WHERE tr.channel = 'MOBILE' AND tr.created_at >= TIMESTAMPTZ '2025-10-10'
    GROUP BY a.customer_id
)
SELECT n.customer_id, n.full_name, n.occupation, d.jml_perangkat, d.perangkat_aktif, d.perangkat_utama, d.os_utama,
       d.versi_aplikasi, d.terakhir_dipakai, t.percobaan_mobile, t.gagal_teknis,
       round(100.0 * t.gagal_teknis / NULLIF(t.percobaan_mobile, 0), 2) AS pct_gagal_teknis
FROM v_nasabah n
LEFT JOIN d ON d.user_id = n.user_id
LEFT JOIN t ON t.customer_id = n.customer_id
ORDER BY n.customer_id;

-- I9. Skor kesehatan nasabah (0-100): keaktifan 30 + keragaman fitur 20 + ketepatan cicilan 25 + tekanan arus kas 25
CREATE OR REPLACE VIEW vp_skor AS
WITH akt AS (
    SELECT user_id, count(*) FILTER (WHERE login_date >= DATE '2026-07-10') AS sesi_90h
    FROM v_session GROUP BY user_id
), fitur AS (
    SELECT e.user_id, count(DISTINCT e.feature_code) AS n_fitur
    FROM app_events e WHERE e.event_type = 'COMPLETE' AND e.event_at >= TIMESTAMPTZ '2026-07-10' GROUP BY e.user_id
), tepat AS (
    SELECT customer_id, 1 - avg((days_late > 0)::int) AS pct_tepat
    FROM v_inst WHERE status = 'PAID' AND paid_date >= DATE '2026-04-10' GROUP BY customer_id
), arus AS (
    SELECT customer_id, sum(expense) / NULLIF(sum(income), 0) AS rasio
    FROM v_cust_month WHERE trx_month BETWEEN DATE '2026-04-01' AND DATE '2026-09-01' GROUP BY customer_id
), k AS (
    SELECT n.customer_id, n.full_name, n.occupation, n.user_status,
           round((LEAST(COALESCE(a.sesi_90h, 0) / 60.0, 1) * 30)::numeric, 1)                   AS skor_aktif,
           round((LEAST(COALESCE(f.n_fitur, 0) / 6.0, 1) * 20)::numeric, 1)                     AS skor_fitur,
           round((COALESCE(t.pct_tepat, 1) * 25)::numeric, 1)                                   AS skor_cicilan,
           round(((CASE WHEN r.rasio IS NULL THEN 0.5 WHEN r.rasio <= 0.8 THEN 1 WHEN r.rasio >= 1.2 THEN 0
                       ELSE (1.2 - r.rasio) / 0.4 END) * 25)::numeric, 1)                       AS skor_arus_kas,
           a.sesi_90h, f.n_fitur AS fitur_selesai_90h, round(t.pct_tepat * 100) AS pct_tepat_cicilan, round(r.rasio, 2) AS rasio_keluar_masuk_6bln
    FROM v_nasabah n
    LEFT JOIN akt a   ON a.user_id = n.user_id
    LEFT JOIN fitur f ON f.user_id = n.user_id
    LEFT JOIN tepat t ON t.customer_id = n.customer_id
    LEFT JOIN arus r  ON r.customer_id = n.customer_id
)
SELECT k.*, k.skor_aktif + k.skor_fitur + k.skor_cicilan + k.skor_arus_kas AS skor_total,
       CASE WHEN k.skor_aktif + k.skor_fitur + k.skor_cicilan + k.skor_arus_kas >= 80 THEN 'Sehat (>= 80)'
            WHEN k.skor_aktif + k.skor_fitur + k.skor_cicilan + k.skor_arus_kas >= 60 THEN 'Cukup (60-79)'
            WHEN k.skor_aktif + k.skor_fitur + k.skor_cicilan + k.skor_arus_kas >= 40 THEN 'Perlu perhatian (40-59)'
            ELSE 'Berisiko (< 40)' END AS kategori
FROM k;

SELECT * FROM vp_skor ORDER BY skor_total, customer_id;

-- =====================================================================
-- Z. CUSTOMER 360 : satu baris per nasabah, gabungan semua analisis di atas
-- =====================================================================
-- Z1. Siap dipakai untuk segmentasi, dashboard, atau diekspor ke CSV:
--       \copy (SELECT * FROM ( <query Z1> ) q) TO 'customer_360.csv' CSV HEADER
SELECT n.customer_id, n.full_name, n.occupation, n.city, n.age_band, n.monthly_income AS penghasilan_terdaftar,
       -- siklus hidup & engagement
       sk.tahap AS tahap_siklus, sk.usia_bulan, sk.hari_sejak_login_terakhir, ch.peringatan AS peringatan_churn,
       rf.segmen AS segmen_rfm, sc.skor_total AS skor_kesehatan, sc.kategori AS kategori_kesehatan,
       -- jenis transaksi
       j.jenis_1 AS jenis_transaksi_utama, j.pct_1 AS pct_jenis_utama, j.trx_keluar_12bln,
       -- pemasukan vs pengeluaran
       a.kategori_arus, a.rata_pemasukan, a.rata_pengeluaran, a.rasio_keluar_masuk, a.bulan_defisit, a.maks_defisit_berturut,
       -- gajian
       g.jenis_pola AS pola_pemasukan, g.tanggal_masuk_tersering AS tanggal_gajian, g.kelas AS kestabilan_gajian, g.posisi_di_bulan,
       -- fitur
       f.n_fitur_dipakai, f.n_fitur_hanya_dibuka, f.fitur_hanya_dibuka,
       -- cicilan & kredit
       c.pinjaman_aktif, c.paylater_aktif, c.kta_aktif, c.cicilan_bulanan, c.kategori_beban, c.pct_telat_6bln, c.denda_6bln,
       rk.skor_risiko AS skor_risiko_kredit, rk.tingkat_risiko AS tingkat_risiko_kredit,
       -- jadwal bayar
       jd.status_jadwal_cicilan, jd.status_jadwal_tagihan_transfer, jd.buka_menu_jadwal,
       CASE WHEN c.pinjaman_aktif > 0 AND jd.status_jadwal_cicilan <> 'AKTIF' AND COALESCE(c.pct_telat_6bln, 0) >= 30
            THEN 'TAWARKAN JADWAL BAYAR' ELSE '-' END AS saran_jadwal_bayar
FROM v_nasabah n
LEFT JOIN vp_siklus sk        ON sk.customer_id = n.customer_id
LEFT JOIN vp_churn ch         ON ch.customer_id = n.customer_id
LEFT JOIN vp_rfm rf           ON rf.customer_id = n.customer_id
LEFT JOIN vp_skor sc          ON sc.customer_id = n.customer_id
LEFT JOIN vp_jenis j          ON j.customer_id  = n.customer_id
LEFT JOIN vp_arus a           ON a.customer_id  = n.customer_id
LEFT JOIN vp_gajian g         ON g.customer_id  = n.customer_id
LEFT JOIN vp_fitur f          ON f.customer_id  = n.customer_id
LEFT JOIN vp_cicilan c        ON c.customer_id  = n.customer_id
LEFT JOIN vp_risiko_kredit rk ON rk.customer_id = n.customer_id
LEFT JOIN vp_jadwal jd        ON jd.customer_id = n.customer_id
ORDER BY n.customer_id;
