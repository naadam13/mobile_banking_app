#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate_mobile_banking.py  -  Generator data dummy MOBILE BANKING (PostgreSQL 13+)

Menghasilkan SATU file .sql (skema + data via COPY ... FROM STDIN + indeks + view + COMMENT ON)
yang siap dijalankan dengan psql.  Hanya memakai pustaka standar Python 3.8+ (tanpa pip install).

Contoh:
    python3 generate_mobile_banking.py                           # 500 nasabah -> mobile_banking_v2.sql
    python3 generate_mobile_banking.py --customers 2000 --seed 7
    python3 generate_mobile_banking.py --customers 800 --intensity 1.5   # nasabah lebih aktif
    python3 generate_mobile_banking.py --load mbanking_db        # langsung muat ke database (butuh psql di PATH)
    createdb mbanking && psql -d mbanking -f mobile_banking_v2.sql

Yang disimulasikan (semua fiktif):
  * Nasabah, rekening (utama + tabungan kedua), kartu debit (VISA/Mastercard/GPN), pengguna aplikasi, perangkat.
  * Gaji (tanggal gajian per perusahaan, mundur ke hari kerja sebelumnya), THR, uang saku, penghasilan tidak tetap.
  * Pengeluaran harian dengan pola jam/hari/gajian/musim, QRIS, e-wallet, transfer, tagihan, pulsa, tarik tunai,
    kartu (gesek, contactless, online, luar negeri/Visa) dan langganan bulanan (Netflix, Spotify, dst).
  * Pembayaran rutin: kos/kontrakan, arisan, internet, BPJS, asuransi, multifinance.
  * Produk kredit: PAYLATER dan KTA (pengajuan, persetujuan, pencairan, cicilan, telat, gagal bayar).
  * Fitur JADWAL BAYAR (scheduler): dibuat/dibatalkan, dijalankan otomatis, gagal karena saldo, jatuh ke manual.
  * Log login, event layar aplikasi (dibuka vs benar-benar dipakai), notifikasi push, gangguan layanan.
  * Perilaku churn bertahap (aktivitas menurun lalu berhenti memakai aplikasi).
"""
import argparse, bisect, hashlib, datetime as dt, heapq, math, os, random, subprocess, sys, tempfile, uuid
from collections import defaultdict
from types import SimpleNamespace as NS

DATE, DT, TD = dt.date, dt.datetime, dt.timedelta

# --------------------------------------------------------------------------------------
# 0. ARGUMEN
# --------------------------------------------------------------------------------------
ap = argparse.ArgumentParser(description="Generator data dummy mobile banking (PostgreSQL)")
ap.add_argument("--customers", type=int, default=500, help="jumlah nasabah (default 500)")
ap.add_argument("--seed", type=int, default=2026, help="seed acak agar hasil bisa direproduksi")
ap.add_argument("--intensity", type=float, default=1.0, help="pengali jumlah transaksi/aktivitas (default 1.0)")
ap.add_argument("--start", default="2024-01-01", help="tanggal awal data")
ap.add_argument("--end", default="2026-10-09 09:30", help="waktu akhir data (WIB)")
ap.add_argument("--out", default="mobile_banking_v2.sql", help="nama file keluaran")
ap.add_argument("--load", metavar="DBNAME", help="langsung muat ke database ini lewat psql (file tetap ditulis)")
ap.add_argument("--quiet", action="store_true")
ARGS = ap.parse_args()

rng = random.Random(ARGS.seed)
N_CUST = ARGS.customers
START = DT.fromisoformat(ARGS.start + " 00:00") if len(ARGS.start) == 10 else DT.fromisoformat(ARGS.start)
END = DT.fromisoformat(ARGS.end.replace("T", " "))
START_D, END_D = START.date(), END.date()
INTENS = ARGS.intensity
def log(*a):
    if not ARGS.quiet: print(*a, file=sys.stderr, flush=True)

# --------------------------------------------------------------------------------------
# 1. DDL
# --------------------------------------------------------------------------------------
DDL = r"""
SET client_min_messages = warning;
DROP SCHEMA IF EXISTS mbanking CASCADE;
CREATE SCHEMA mbanking;
SET search_path = mbanking, public;

CREATE TABLE branches (
    branch_id    serial       PRIMARY KEY,
    branch_code  varchar(10)  NOT NULL UNIQUE,
    branch_name  varchar(100) NOT NULL,
    city         varchar(60)  NOT NULL,
    province     varchar(60)  NOT NULL
);
CREATE TABLE banks (
    bank_code char(3)     PRIMARY KEY,
    bank_name varchar(80) NOT NULL
);
CREATE TABLE customers (
    customer_id    bigint        PRIMARY KEY,
    cif_number     varchar(12)   NOT NULL UNIQUE,
    nik            char(16)      NOT NULL UNIQUE,
    full_name      varchar(100)  NOT NULL,
    gender         char(1)       NOT NULL CHECK (gender IN ('L','P')),
    birth_date     date          NOT NULL,
    phone_number   varchar(16)   NOT NULL UNIQUE,
    email          varchar(120)  NOT NULL UNIQUE,
    address        text          NOT NULL,
    city           varchar(60)   NOT NULL,
    province       varchar(60)   NOT NULL,
    occupation     varchar(60)   NOT NULL,
    monthly_income numeric(15,2) NOT NULL CHECK (monthly_income >= 0),
    kyc_level      varchar(10)   NOT NULL CHECK (kyc_level IN ('BASIC','FULL')),
    branch_id      int           NOT NULL REFERENCES branches(branch_id),
    created_at     timestamptz   NOT NULL
);
CREATE TABLE accounts (
    account_id     bigint        PRIMARY KEY,
    account_number varchar(12)   NOT NULL UNIQUE,
    customer_id    bigint        NOT NULL REFERENCES customers(customer_id),
    branch_id      int           NOT NULL REFERENCES branches(branch_id),
    product_type   varchar(20)   NOT NULL CHECK (product_type IN ('TABUNGAN','TABUNGAN_PLUS','GIRO')),
    currency       char(3)       NOT NULL DEFAULT 'IDR',
    balance        numeric(18,2) NOT NULL DEFAULT 0 CHECK (balance >= 0),
    status         varchar(10)   NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','DORMANT','BLOCKED','CLOSED')),
    opened_at      timestamptz   NOT NULL,
    closed_at      timestamptz
);
CREATE TABLE cards (
    card_id           bigint       PRIMARY KEY,
    account_id        bigint       NOT NULL REFERENCES accounts(account_id),
    card_type         varchar(10)  NOT NULL CHECK (card_type IN ('DEBIT','CREDIT')),
    network           varchar(12)  NOT NULL CHECK (network IN ('VISA','MASTERCARD','GPN')),
    masked_number     varchar(19)  NOT NULL,
    card_holder_name  varchar(100) NOT NULL,
    issued_date       date         NOT NULL,
    expiry_date       date         NOT NULL,
    credit_limit      numeric(15,2),
    status            varchar(10)  NOT NULL CHECK (status IN ('ACTIVE','BLOCKED','EXPIRED')),
    is_contactless    boolean      NOT NULL DEFAULT false,
    online_enabled_at timestamptz,
    intl_enabled_at   timestamptz
);
CREATE TABLE mobile_users (
    user_id           bigint       PRIMARY KEY,
    customer_id       bigint       NOT NULL UNIQUE REFERENCES customers(customer_id),
    username          varchar(40)  NOT NULL UNIQUE,
    password_hash     varchar(60)  NOT NULL,
    pin_hash          varchar(60)  NOT NULL,
    status            varchar(10)  NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','INACTIVE','LOCKED')),
    registered_at     timestamptz  NOT NULL,
    last_login_at     timestamptz,
    push_enabled      boolean      NOT NULL DEFAULT true,
    biometric_enabled boolean      NOT NULL DEFAULT false
);
CREATE TABLE devices (
    device_id     uuid         PRIMARY KEY,
    user_id       bigint       NOT NULL REFERENCES mobile_users(user_id),
    device_name   varchar(60)  NOT NULL,
    os            varchar(10)  NOT NULL CHECK (os IN ('ANDROID','IOS')),
    os_version    varchar(10)  NOT NULL,
    app_version   varchar(10)  NOT NULL,
    is_active     boolean      NOT NULL DEFAULT true,
    registered_at timestamptz  NOT NULL,
    last_used_at  timestamptz
);
CREATE TABLE login_logs (
    login_id       bigint      PRIMARY KEY,
    user_id        bigint      NOT NULL REFERENCES mobile_users(user_id),
    device_id      uuid        NOT NULL REFERENCES devices(device_id),
    login_at       timestamptz NOT NULL,
    ip_address     inet        NOT NULL,
    login_method   varchar(10) NOT NULL CHECK (login_method IN ('BIOMETRIC','PASSWORD','PIN')),
    status         varchar(10) NOT NULL CHECK (status IN ('SUCCESS','FAILED')),
    failure_reason varchar(40)
);
CREATE TABLE beneficiaries (
    beneficiary_id bigint       PRIMARY KEY,
    customer_id    bigint       NOT NULL REFERENCES customers(customer_id),
    bank_code      char(3)      NOT NULL REFERENCES banks(bank_code),
    account_number varchar(20)  NOT NULL,
    account_name   varchar(100) NOT NULL,
    nickname       varchar(40),
    created_at     timestamptz  NOT NULL
);
CREATE TABLE billers (
    biller_id   serial       PRIMARY KEY,
    biller_code varchar(20)  NOT NULL UNIQUE,
    biller_name varchar(80)  NOT NULL,
    category    varchar(20)  NOT NULL
);
CREATE TABLE transaction_types (
    type_code   varchar(20)   PRIMARY KEY,
    type_name   varchar(60)   NOT NULL,
    direction   char(1)       NOT NULL CHECK (direction IN ('D','C')),
    default_fee numeric(12,2) NOT NULL DEFAULT 0
);
CREATE TABLE app_features (
    feature_code varchar(20)  PRIMARY KEY,
    feature_name varchar(60)  NOT NULL,
    category     varchar(12)  NOT NULL CHECK (category IN ('INQUIRY','TRANSACTION','CREDIT','SETTING','INFO')),
    launched_at  timestamptz  NOT NULL,
    description  varchar(200)
);
CREATE TABLE loan_products (
    product_code   varchar(10)   PRIMARY KEY,
    product_name   varchar(60)   NOT NULL,
    min_amount     numeric(15,2) NOT NULL,
    max_amount     numeric(15,2) NOT NULL,
    min_tenor      int           NOT NULL,
    max_tenor      int           NOT NULL,
    rate_pa_min    numeric(5,2)  NOT NULL,
    rate_pa_max    numeric(5,2)  NOT NULL,
    late_fee       numeric(12,2) NOT NULL,
    launched_at    timestamptz   NOT NULL
);
CREATE TABLE loan_applications (
    application_id   bigint        PRIMARY KEY,
    customer_id      bigint        NOT NULL REFERENCES customers(customer_id),
    product_code     varchar(10)   NOT NULL REFERENCES loan_products(product_code),
    applied_at       timestamptz   NOT NULL,
    requested_amount numeric(15,2) NOT NULL,
    requested_tenor  int           NOT NULL,
    status           varchar(10)   NOT NULL CHECK (status IN ('APPROVED','REJECTED')),
    decided_at       timestamptz   NOT NULL,
    approved_amount  numeric(15,2),
    reject_reason    varchar(60),
    device_id        uuid          REFERENCES devices(device_id)
);
CREATE TABLE loans (
    loan_id                    bigint        PRIMARY KEY,
    customer_id                bigint        NOT NULL REFERENCES customers(customer_id),
    account_id                 bigint        NOT NULL REFERENCES accounts(account_id),
    product_code               varchar(10)   NOT NULL REFERENCES loan_products(product_code),
    application_id             bigint        NOT NULL REFERENCES loan_applications(application_id),
    principal                  numeric(15,2) NOT NULL CHECK (principal > 0),
    tenor_months               int           NOT NULL CHECK (tenor_months > 0),
    interest_rate_pa           numeric(5,2)  NOT NULL,
    installment_amount         numeric(15,2) NOT NULL,
    due_day                    int           NOT NULL CHECK (due_day BETWEEN 1 AND 28),
    disbursed_at               timestamptz   NOT NULL,
    first_due_date             date          NOT NULL,
    maturity_date              date          NOT NULL,
    status                     varchar(10)   NOT NULL CHECK (status IN ('ACTIVE','PAID_OFF','DEFAULT')),
    closed_at                  timestamptz,
    merchant_name              varchar(80),
    merchant_category          varchar(20),
    disbursement_transaction_id bigint
);
CREATE TABLE scheduled_payments (
    schedule_id       bigint        PRIMARY KEY,
    customer_id       bigint        NOT NULL REFERENCES customers(customer_id),
    account_id        bigint        NOT NULL REFERENCES accounts(account_id),
    payment_type      varchar(20)   NOT NULL CHECK (payment_type IN ('LOAN_INSTALLMENT','BILL','TRANSFER')),
    loan_id           bigint        REFERENCES loans(loan_id),
    biller_id         int           REFERENCES billers(biller_id),
    beneficiary_id    bigint        REFERENCES beneficiaries(beneficiary_id),
    label             varchar(60)   NOT NULL,
    amount_mode       varchar(10)   NOT NULL CHECK (amount_mode IN ('FIXED','FULL_DUE')),
    fixed_amount      numeric(15,2),
    frequency         varchar(10)   NOT NULL DEFAULT 'MONTHLY',
    day_of_month      int           NOT NULL CHECK (day_of_month BETWEEN 1 AND 28),
    notify_before_days int          NOT NULL DEFAULT 0,
    created_at        timestamptz   NOT NULL,
    created_device_id uuid          REFERENCES devices(device_id),
    status            varchar(10)   NOT NULL CHECK (status IN ('ACTIVE','CANCELLED')),
    cancelled_at      timestamptz,
    cancel_reason     varchar(60),
    run_count         int           NOT NULL DEFAULT 0,
    success_count     int           NOT NULL DEFAULT 0,
    fail_count        int           NOT NULL DEFAULT 0,
    last_run_at       timestamptz
);
CREATE TABLE transactions (
    transaction_id         bigint        PRIMARY KEY,
    reference_no           varchar(20)   NOT NULL UNIQUE,
    account_id             bigint        NOT NULL REFERENCES accounts(account_id),
    transaction_type       varchar(20)   NOT NULL REFERENCES transaction_types(type_code),
    direction              char(1)       NOT NULL CHECK (direction IN ('D','C')),
    amount                 numeric(18,2) NOT NULL CHECK (amount > 0),
    fee                    numeric(18,2) NOT NULL DEFAULT 0 CHECK (fee >= 0),
    balance_after          numeric(18,2) NOT NULL CHECK (balance_after >= 0),
    currency               char(3)       NOT NULL DEFAULT 'IDR',
    counterparty_bank_code char(3)       REFERENCES banks(bank_code),
    counterparty_account   varchar(30),
    counterparty_name      varchar(100),
    biller_id              int           REFERENCES billers(biller_id),
    channel                varchar(10)   NOT NULL CHECK (channel IN ('MOBILE','ATM','BRANCH','SYSTEM','CARD')),
    device_id              uuid          REFERENCES devices(device_id),
    description            varchar(200),
    status                 varchar(10)   NOT NULL CHECK (status IN ('SUCCESS','FAILED','PENDING')),
    failure_reason         varchar(60),
    created_at             timestamptz   NOT NULL,
    card_id                bigint        REFERENCES cards(card_id),
    merchant_name          varchar(80),
    merchant_category      varchar(20),
    loan_id                bigint        REFERENCES loans(loan_id),
    scheduled_payment_id   bigint        REFERENCES scheduled_payments(schedule_id),
    currency_original      char(3),
    amount_original        numeric(18,2),
    fx_rate                numeric(12,4)
);
CREATE TABLE loan_installments (
    installment_id         bigint        PRIMARY KEY,
    loan_id                bigint        NOT NULL REFERENCES loans(loan_id),
    installment_no         int           NOT NULL,
    due_date               date          NOT NULL,
    amount_due             numeric(15,2) NOT NULL,
    principal_part         numeric(15,2) NOT NULL,
    interest_part          numeric(15,2) NOT NULL,
    paid_at                timestamptz,
    paid_amount            numeric(15,2),
    days_late              int,
    late_fee               numeric(12,2) NOT NULL DEFAULT 0,
    payment_transaction_id bigint        REFERENCES transactions(transaction_id),
    payment_channel        varchar(10)   CHECK (payment_channel IN ('MOBILE','SCHEDULER','ATM')),
    status                 varchar(10)   NOT NULL CHECK (status IN ('PAID','UNPAID','UPCOMING','CANCELLED'))
);
CREATE TABLE app_events (
    event_id       bigint       PRIMARY KEY,
    user_id        bigint       NOT NULL REFERENCES mobile_users(user_id),
    device_id      uuid         NOT NULL REFERENCES devices(device_id),
    login_id       bigint       NOT NULL REFERENCES login_logs(login_id),
    feature_code   varchar(20)  NOT NULL REFERENCES app_features(feature_code),
    event_type     varchar(10)  NOT NULL CHECK (event_type IN ('OPEN','INTERACT','COMPLETE','FAILED')),
    detail         varchar(30),
    event_at       timestamptz  NOT NULL,
    transaction_id bigint       REFERENCES transactions(transaction_id),
    loan_id        bigint       REFERENCES loans(loan_id)
);
CREATE TABLE notifications (
    notification_id bigint       PRIMARY KEY,
    user_id         bigint       NOT NULL REFERENCES mobile_users(user_id),
    category        varchar(20)  NOT NULL CHECK (category IN ('PROMO','GAJI_MASUK','DUE_REMINDER','SCHEDULE_REMINDER')),
    sent_at         timestamptz  NOT NULL,
    opened_at       timestamptz,
    related_loan_id bigint       REFERENCES loans(loan_id)
);
"""

# Urutan muat tabel + kolom COPY (urutan kolom harus sama dengan tuple yang ditulis)
TABLES = {
 "branches": "branch_id,branch_code,branch_name,city,province",
 "banks": "bank_code,bank_name",
 "billers": "biller_id,biller_code,biller_name,category",
 "transaction_types": "type_code,type_name,direction,default_fee",
 "app_features": "feature_code,feature_name,category,launched_at,description",
 "loan_products": "product_code,product_name,min_amount,max_amount,min_tenor,max_tenor,rate_pa_min,rate_pa_max,late_fee,launched_at",
 "customers": "customer_id,cif_number,nik,full_name,gender,birth_date,phone_number,email,address,city,province,occupation,monthly_income,kyc_level,branch_id,created_at",
 "accounts": "account_id,account_number,customer_id,branch_id,product_type,currency,balance,status,opened_at,closed_at",
 "cards": "card_id,account_id,card_type,network,masked_number,card_holder_name,issued_date,expiry_date,credit_limit,status,is_contactless,online_enabled_at,intl_enabled_at",
 "mobile_users": "user_id,customer_id,username,password_hash,pin_hash,status,registered_at,last_login_at,push_enabled,biometric_enabled",
 "devices": "device_id,user_id,device_name,os,os_version,app_version,is_active,registered_at,last_used_at",
 "beneficiaries": "beneficiary_id,customer_id,bank_code,account_number,account_name,nickname,created_at",
 "login_logs": "login_id,user_id,device_id,login_at,ip_address,login_method,status,failure_reason",
 "loan_applications": "application_id,customer_id,product_code,applied_at,requested_amount,requested_tenor,status,decided_at,approved_amount,reject_reason,device_id",
 "loans": "loan_id,customer_id,account_id,product_code,application_id,principal,tenor_months,interest_rate_pa,installment_amount,due_day,disbursed_at,first_due_date,maturity_date,status,closed_at,merchant_name,merchant_category,disbursement_transaction_id",
 "scheduled_payments": "schedule_id,customer_id,account_id,payment_type,loan_id,biller_id,beneficiary_id,label,amount_mode,fixed_amount,frequency,day_of_month,notify_before_days,created_at,created_device_id,status,cancelled_at,cancel_reason,run_count,success_count,fail_count,last_run_at",
 "transactions": "transaction_id,reference_no,account_id,transaction_type,direction,amount,fee,balance_after,currency,counterparty_bank_code,counterparty_account,counterparty_name,biller_id,channel,device_id,description,status,failure_reason,created_at,card_id,merchant_name,merchant_category,loan_id,scheduled_payment_id,currency_original,amount_original,fx_rate",
 "loan_installments": "installment_id,loan_id,installment_no,due_date,amount_due,principal_part,interest_part,paid_at,paid_amount,days_late,late_fee,payment_transaction_id,payment_channel,status",
 "app_events": "event_id,user_id,device_id,login_id,feature_code,event_type,detail,event_at,transaction_id,loan_id",
 "notifications": "notification_id,user_id,category,sent_at,opened_at,related_loan_id",
}

POST_DDL = r"""
ALTER TABLE loans ADD CONSTRAINT fk_loans_disb_tx FOREIGN KEY (disbursement_transaction_id) REFERENCES transactions(transaction_id);

CREATE INDEX idx_accounts_customer   ON accounts (customer_id);
CREATE INDEX idx_cards_account       ON cards (account_id);
CREATE INDEX idx_devices_user        ON devices (user_id, registered_at);
CREATE INDEX idx_login_user_time     ON login_logs (user_id, login_at);
CREATE INDEX idx_login_time          ON login_logs (login_at);
CREATE INDEX idx_benef_customer      ON beneficiaries (customer_id);
CREATE INDEX idx_tx_account_time     ON transactions (account_id, created_at);
CREATE INDEX idx_tx_time             ON transactions (created_at);
CREATE INDEX idx_tx_type_time        ON transactions (transaction_type, created_at);
CREATE INDEX idx_tx_device           ON transactions (device_id) WHERE device_id IS NOT NULL;
CREATE INDEX idx_tx_loan             ON transactions (loan_id) WHERE loan_id IS NOT NULL;
CREATE INDEX idx_tx_sched            ON transactions (scheduled_payment_id) WHERE scheduled_payment_id IS NOT NULL;
CREATE INDEX idx_loans_customer      ON loans (customer_id);
CREATE INDEX idx_inst_loan           ON loan_installments (loan_id, installment_no);
CREATE INDEX idx_inst_due            ON loan_installments (due_date);
CREATE INDEX idx_sched_customer      ON scheduled_payments (customer_id);
CREATE INDEX idx_events_user_time    ON app_events (user_id, event_at);
CREATE INDEX idx_events_feature      ON app_events (feature_code, event_type);
CREATE INDEX idx_events_login        ON app_events (login_id);
CREATE INDEX idx_notif_user_time     ON notifications (user_id, sent_at);

CREATE VIEW v_monthly_transaction_summary AS
SELECT date_trunc('month', created_at AT TIME ZONE 'Asia/Jakarta')::date AS month,
       transaction_type,
       count(*)    AS trx_count,
       sum(amount) AS total_amount,
       sum(fee)    AS total_fee
FROM transactions
WHERE status = 'SUCCESS'
GROUP BY 1, 2;

CREATE VIEW v_customer_overview AS
SELECT c.customer_id, c.cif_number, c.full_name, c.city, c.occupation, c.monthly_income,
       count(DISTINCT a.account_id) AS account_count,
       sum(a.balance)               AS total_balance,
       u.username, u.status AS mobile_status, u.last_login_at
FROM customers c
JOIN accounts a ON a.customer_id = c.customer_id
LEFT JOIN mobile_users u ON u.customer_id = c.customer_id
GROUP BY c.customer_id, c.cif_number, c.full_name, c.city, c.occupation, c.monthly_income,
         u.username, u.status, u.last_login_at;
"""

# --------------------------------------------------------------------------------------
# 2. PENULIS COPY (tiap tabel ke berkas sementara, digabung di akhir)
# --------------------------------------------------------------------------------------
TMPDIR = tempfile.mkdtemp(prefix="mbgen_")
_files = {t: open(os.path.join(TMPDIR, t + ".tsv"), "w", encoding="utf-8", newline="\n") for t in TABLES}
ROWCOUNT = defaultdict(int)

def _f(v):
    if v is None: return "\\N"
    t = type(v)
    if t is str: return v.replace("\\", "\\\\").replace("\t", " ").replace("\n", " ")
    if t is int: return str(v)
    if t is bool: return "t" if v else "f"
    if t is DT: return v.strftime("%Y-%m-%d %H:%M:%S+07")
    if t is DATE: return v.isoformat()
    if t is float: return "%.2f" % v
    if t is uuid.UUID: return str(v)
    return str(v)

def W(table, *vals):
    _files[table].write("\t".join(map(_f, vals)) + "\n")
    ROWCOUNT[table] += 1

# --------------------------------------------------------------------------------------
# 3. DATA REFERENSI
# --------------------------------------------------------------------------------------
MALE = "Budi Agus Andi Bambang Dedi Eko Fajar Hendra Irfan Joko Rizky Dimas Yoga Bayu Wahyu Rudi Teguh Arif Fauzi Ilham Reza Gilang Aditya Naufal Hadi Slamet Yusuf Rahmat Ahmad Muhammad Taufik Doni Ferry Galih Hanif Ivan Krisna Lukman".split()
FEMALE = "Siti Sri Dewi Ratna Putri Ayu Rina Wulan Indah Nurul Fitri Lestari Maya Anisa Citra Dian Eka Fani Gita Hana Intan Jihan Kartika Lia Mega Nadia Okta Rani Sari Tika Umi Vina Winda Yuni Zahra Annisa Bella Cindy".split()
LAST = "Santoso Wijaya Saputra Pratama Kusuma Hidayat Nugroho Setiawan Gunawan Susanto Purnomo Utomo Wibowo Hartono Firmansyah Ramadhan Permana Siregar Nasution Lubis Harahap Simanjuntak Sitompul Tanuwijaya Halim Hakim Maulana Prasetyo Suryanto Rahman Yulianto Kurniawan Handoko Wahyudi Cahyono Budiman Lestari Anggoro Mahendra Fadillah".split()
STREETS = ["Merdeka","Sudirman","Thamrin","Diponegoro","Ahmad Yani","Gatot Subroto","Mawar","Melati","Kenanga","Cendana","Pahlawan","Kebon Jeruk","Raya Bogor","Veteran","Pemuda","Imam Bonjol","Cempaka","Anggrek","Kemang Raya","Cipaganti"]
DOMAINS = ["gmail.com"]*3 + ["yahoo.co.id","outlook.com","icloud.com","proton.me"]
PHONE_PFX = "0811 0812 0813 0821 0822 0852 0853 0857 0877 0878 0895 0896 0881 0838".split()
IP_PFX = ["114.125","36.79","180.244","112.215","182.253","103.47","125.160","202.67"]
BANK_CODES = "002 008 009 011 013 014 022 200 213 451 535 542".split()
ANDROID = ["Samsung Galaxy A54","Samsung Galaxy S23","Xiaomi Redmi Note 12","Xiaomi 13T","OPPO A78","OPPO Reno10","vivo Y27","vivo V29","realme 11","Infinix Hot 30","POCO X5 Pro"]
IOS = ["iPhone 11","iPhone 12","iPhone 13","iPhone 14","iPhone 15"]
AND_V, IOS_V, APP_V = ["11","12","13","14"], ["16.7","17.0","17.4","18.0"], ["3.4.0","3.5.2","3.6.1","3.7.0","4.0.0"]
CITIES = [("Jakarta Selatan","DKI Jakarta","3171",10),("Jakarta Timur","DKI Jakarta","3172",9),("Jakarta Pusat","DKI Jakarta","3173",6),
 ("Jakarta Barat","DKI Jakarta","3174",8),("Jakarta Utara","DKI Jakarta","3175",5),("Bandung","Jawa Barat","3273",8),
 ("Bekasi","Jawa Barat","3275",8),("Depok","Jawa Barat","3276",6),("Bogor","Jawa Barat","3271",4),("Tangerang","Banten","3671",8),
 ("Surabaya","Jawa Timur","3578",8),("Malang","Jawa Timur","3573",3),("Semarang","Jawa Tengah","3374",4),
 ("Yogyakarta","DI Yogyakarta","3471",4),("Medan","Sumatera Utara","1275",5),("Makassar","Sulawesi Selatan","7371",3),
 ("Denpasar","Bali","5171",2),("Palembang","Sumatera Selatan","1671",2),("Balikpapan","Kalimantan Timur","6471",1)]
# nama, penghasilan min, max, bergaji tetap, bobot
OCCS = [("Karyawan Swasta",4500000,18000000,True,35),("PNS/ASN",4500000,12000000,True,10),("Guru/Dosen",3500000,9000000,True,6),
 ("Pegawai BUMN",8000000,25000000,True,6),("Profesional",15000000,60000000,True,5),("Wiraswasta",5000000,45000000,False,18),
 ("Freelancer",3000000,20000000,False,8),("Mahasiswa",1500000,4000000,False,7),("Ibu Rumah Tangga",2000000,8000000,False,5)]
BANKS = [("002","Bank BRI"),("008","Bank Mandiri"),("009","Bank BNI"),("011","Bank Danamon"),("013","Bank Permata"),("014","Bank BCA"),
 ("022","Bank CIMB Niaga"),("200","Bank BTN"),("213","Bank BTPN (Jenius)"),("451","Bank Syariah Indonesia"),("535","SeaBank"),
 ("542","Bank Jago"),("999","Bank Nusantara Digital (bank sendiri / fiktif)")]
BILLERS = [("PLNPRE","PLN Token Listrik","LISTRIK"),("PLNPOST","PLN Tagihan Listrik","LISTRIK"),("PDAM","PDAM Air Minum","AIR"),
 ("BPJSKES","BPJS Kesehatan","BPJS"),("BPJSTK","BPJS Ketenagakerjaan","BPJS"),("INDIHOME","IndiHome","INTERNET"),
 ("FIRSTMEDIA","First Media","INTERNET"),("BIZNET","Biznet Home","INTERNET"),("MNCVISION","MNC Vision","TV"),
 ("ASURANSI1","Asuransi Jiwa Contoh","ASURANSI"),("ADIRA","Adira Finance","MULTIFINANCE"),("FIF","FIFGROUP","MULTIFINANCE"),
 ("SAMSAT","Pajak Kendaraan (Samsat)","PAJAK"),("PBB","PBB Daerah","PAJAK"),("SPPKAMPUS","Pembayaran SPP Kampus","PENDIDIKAN"),
 ("TSEL","Telkomsel","PULSA"),("ISAT","Indosat Ooredoo","PULSA"),("XL","XL Axiata","PULSA"),("TRI","Tri","PULSA"),("SMART","Smartfren","PULSA"),
 ("GOPAY","GoPay","EWALLET"),("OVO","OVO","EWALLET"),("DANA","DANA","EWALLET"),("SHOPEEPAY","ShopeePay","EWALLET"),("LINKAJA","LinkAja","EWALLET")]
BID = {b[0]: i + 1 for i, b in enumerate(BILLERS)}
TTYPES = [("SALARY_CREDIT","Gaji Masuk","C",0),("TRANSFER_IN","Transfer Masuk","C",0),("CASH_DEPOSIT","Setor Tunai","C",0),
 ("INTEREST","Bunga Tabungan","C",0),("LOAN_DISBURSEMENT","Pencairan Pinjaman (KTA)","C",0),("SAVINGS_IN","Pindah Dana dari Rekening Sendiri","C",0),
 ("TRANSFER_INTERNAL","Transfer Sesama Bank","D",0),("TRANSFER_BANK","Transfer Antar Bank (BI-FAST)","D",2500),
 ("QRIS_PAYMENT","Pembayaran QRIS","D",0),("EWALLET_TOPUP","Top Up E-Wallet","D",1000),("BILL_PAYMENT","Pembayaran Tagihan","D",2500),
 ("PULSA_DATA","Pulsa & Paket Data","D",0),("ATM_WITHDRAWAL","Tarik Tunai ATM","D",0),("ADMIN_FEE","Biaya Administrasi Bulanan","D",0),
 ("CARD_POS","Belanja Kartu Debit (Gesek/Chip)","D",0),("CARD_CONTACTLESS","Belanja Kartu Debit Contactless","D",0),
 ("CARD_ONLINE","Belanja Online Kartu Debit","D",0),("CARD_INTL","Belanja Kartu Debit Luar Negeri (Visa/Mastercard)","D",0),
 ("LOAN_INSTALLMENT","Bayar Cicilan (KTA/Paylater)","D",0),("SAVINGS_OUT","Pindah Dana ke Rekening Sendiri","D",0)]
FEATURES = [  # kode, nama, kategori, rilis, deskripsi
 ("CEK_SALDO","Cek Saldo","INQUIRY",START,"Melihat saldo rekening"),
 ("MUTASI","Mutasi Rekening","INQUIRY",START,"Melihat riwayat transaksi"),
 ("TRANSFER","Transfer","TRANSACTION",START,"Transfer sesama bank, antar bank, dan antar rekening sendiri"),
 ("QRIS","Bayar QRIS","TRANSACTION",START,"Pembayaran ke merchant QRIS"),
 ("TOPUP_EWALLET","Top Up E-Wallet","TRANSACTION",START,"Isi saldo e-wallet"),
 ("TAGIHAN","Bayar Tagihan","TRANSACTION",START,"Listrik, air, BPJS, internet, asuransi, multifinance, dst"),
 ("PULSA_DATA","Pulsa & Paket Data","TRANSACTION",START,"Beli pulsa dan paket data"),
 ("KARTU","Kartu Saya","SETTING",START,"Info kartu, limit, aktivasi belanja online/luar negeri"),
 ("PROMO","Promo","INFO",START,"Daftar promo dan penawaran"),
 ("PROFIL","Profil & Pengaturan","SETTING",START,"Ganti PIN, biometrik, notifikasi"),
 ("BANTUAN","Pusat Bantuan","INFO",START,"FAQ dan hubungi layanan"),
 ("JADWAL_BAYAR","Jadwal Bayar (Scheduler)","SETTING",DT(2024,4,1),"Pembayaran otomatis bulanan: cicilan, tagihan, transfer rutin"),
 ("CICILAN","Cicilan Saya","CREDIT",DT(2024,7,1),"Lihat dan bayar cicilan KTA/Paylater"),
 ("PAYLATER","Paylater","CREDIT",DT(2024,7,1),"Beli sekarang bayar nanti dengan cicilan"),
 ("KTA","Kredit Tanpa Agunan","CREDIT",DT(2025,1,15),"Simulasi dan pengajuan KTA digital")]
FLAUNCH = {f[0]: f[3] for f in FEATURES}
SCHED_LAUNCH, PL_LAUNCH, KTA_LAUNCH = FLAUNCH["JADWAL_BAYAR"], FLAUNCH["PAYLATER"], FLAUNCH["KTA"]
LOANPRODS = [("PAYLATER","Paylater (bayar nanti, cicil 1-12 bulan)",500000,25000000,1,12,0,30,15000,PL_LAUNCH),
             ("KTA","Kredit Tanpa Agunan Digital",5000000,50000000,12,48,11.5,18,25000,KTA_LAUNCH)]

# Merchant: (nama, kategori, pengali nominal, bobot popularitas)
OFFLINE = [("Warung Makan Bu Sri","FNB",.5,6),("Kopi Kenangan","FNB",.45,7),("Starbucks","FNB",1.1,4),("Janji Jiwa","FNB",.4,5),("Chatime","FNB",.5,3),
 ("McDonald's","FNB",.9,5),("KFC","FNB",.9,4),("Pizza Hut","FNB",1.8,2),("Burger King","FNB",1.0,2),("Bakso Mas Joko","FNB",.4,3),
 ("Nasi Padang Sederhana","FNB",.7,4),("Ayam Geprek Bensu","FNB",.6,3),("Warteg Bahari","FNB",.35,4),("Solaria","FNB",1.3,2),("Sushi Tei","FNB",2.2,1.5),
 ("Indomaret","GROCERY",.6,12),("Alfamart","GROCERY",.6,11),("Superindo","GROCERY",2.0,4),("Hypermart","GROCERY",2.5,3),("Transmart","GROCERY",2.8,2),
 ("Toko Kelontong Berkah","GROCERY",.4,3),("Pasar Segar","GROCERY",.9,2),("SPBU Pertamina","FUEL",1.4,6),("Shell","FUEL",1.8,1.5),("BP-AKR","FUEL",1.5,1),
 ("Apotek K-24","HEALTH",.8,3),("Guardian","HEALTH",1.0,2),("Kimia Farma","HEALTH",.9,2),("Klinik Pratama Sehat","HEALTH",2.0,1),("RS Siloam","HEALTH",6.0,.4),
 ("Uniqlo","SHOPPING",4.0,1.2),("Miniso","SHOPPING",1.5,1.5),("Gramedia","SHOPPING",2.0,1),("Ace Hardware","SHOPPING",3.0,.8),("Matahari","SHOPPING",3.0,.8),
 ("Erafone","SHOPPING",8.0,.4),("XXI Cinema","ENTERTAINMENT",1.4,2),("CGV","ENTERTAINMENT",1.4,1.5),("Timezone","ENTERTAINMENT",1.2,.8),
 ("Parkir Mall","TRANSPORT",.3,3),("Laundry Kilat","OTHER",.8,1.5),("Barbershop","OTHER",.9,1.2),("Salon Cantik","OTHER",1.6,.8)]
ONLINE = [("Tokopedia","SHOPPING",1.0,10),("Shopee","SHOPPING",.9,12),("Lazada","SHOPPING",1.0,5),("Bukalapak","SHOPPING",.8,2),("Blibli","SHOPPING",1.4,2),
 ("Zalora","SHOPPING",1.6,2),("Traveloka","TRAVEL",3.5,3),("Tiket.com","TRAVEL",3.2,2.5),("Agoda","TRAVEL",4.0,1),("Grab","TRANSPORT",.4,7),
 ("Gojek","TRANSPORT",.4,8),("Google Play","ENTERTAINMENT",.3,4),("Apple App Store","ENTERTAINMENT",.4,3),("Steam","ENTERTAINMENT",.6,1),
 ("Sociolla","SHOPPING",.9,1),("Canva","OTHER",.4,.8),("Halodoc","HEALTH",.6,.8),("Ruangguru","EDUCATION",1.2,.7)]
INTL = [("Amazon US","SHOPPING","USD",25,2),("Apple Services","ENTERTAINMENT","USD",5,3),("Airbnb","TRAVEL","USD",90,1.5),("Booking.com","TRAVEL","EUR",110,1.5),
 ("AliExpress","SHOPPING","USD",18,2),("Grab Singapore","TRANSPORT","SGD",12,1.5),("Don Don Donki","GROCERY","SGD",30,1),("Changi Airport","SHOPPING","SGD",45,1),
 ("Uniqlo Tokyo","SHOPPING","JPY",6000,1),("Starbucks Kuala Lumpur","FNB","MYR",25,1),("Klook","TRAVEL","USD",35,1.5),("Steam Global","ENTERTAINMENT","USD",12,1)]
SUBS = [("Netflix",54000,186000),("Spotify",54990,54990),("YouTube Premium",59000,59000),("Disney+ Hotstar",39000,159000),("Google One",29000,29000),("iCloud+",15000,45000)]
PL_MERCH = [("Tokopedia","SHOPPING"),("Shopee","SHOPPING"),("Lazada","SHOPPING"),("Blibli","SHOPPING"),("Traveloka","TRAVEL"),("Tiket.com","TRAVEL"),
 ("Erafone","ELEKTRONIK"),("iBox","ELEKTRONIK"),("Electronic City","ELEKTRONIK"),("Zalora","SHOPPING"),("Informa","FURNITUR")]
FX_BASE = {"USD": (15700, 16400), "EUR": (17000, 19000), "SGD": (11600, 12700), "MYR": (3300, 3800), "JPY": (105, 112)}
EMPLOYERS = {  # (nama perusahaan) per kelompok pekerjaan -> hari gajian tetap per perusahaan (0 = hari kerja terakhir bulan)
 "Karyawan Swasta": [("PT Nusantara Teknologi",25),("PT Sinar Mulia Abadi",25),("PT Global Retailindo",28),("PT Maju Bersama Logistik",27),("PT Kreasi Digital",25),
   ("PT Bumi Properti",1),("PT Cahaya Manufaktur",25),("PT Andalan Finansial",0),("PT Samudra Pangan",26),("PT Harapan Media",25),
   ("PT Karya Konstruksi",28),("PT Mitra Sehat Farmasi",20),("PT Pelita Energi",25),("PT Tirta Konsultan",1),("PT Garuda Otomotif",27)],
 "PNS/ASN": [("Pemprov DKI Jakarta",1),("Pemkot Bandung",1),("Kementerian Contoh",1),("Pemkab Bogor",2),("Pemprov Jawa Timur",1)],
 "Guru/Dosen": [("Dinas Pendidikan",1),("Universitas Nusantara",28),("Yayasan Pendidikan Harapan",25),("SMA Negeri Contoh",1)],
 "Pegawai BUMN": [("PT Energi Negeri (Persero)",25),("PT Kereta Nusantara (Persero)",25),("PT Bank Daerah (Persero)",27),("PT Telekomunikasi Contoh (Persero)",25)],
 "Profesional": [("PT Konsultan Global",28),("RS Medika Utama",25),("Kantor Hukum Bersama",0),("PT Arsitek Kreatif",25)]}

# profil jam transaksi (24 bobot) per kelompok
def _prof(d):
    cum, s = [], 0.0
    for h in range(24):
        s += d.get(h, 0.15); cum.append(s)
    return cum
PROF = {
 "office":  _prof({6:1,7:4,8:6,9:3,10:2.5,11:3,12:6,13:5,14:2.5,15:2,16:2.5,17:4,18:5,19:5,20:4.5,21:3.5,22:2,23:.8}),
 "student": _prof({7:1.5,8:2,9:2,10:2.5,11:3,12:5,13:4,14:3,15:3,16:3.5,17:4,18:4.5,19:5,20:6,21:6,22:5,23:3.5,0:1.5}),
 "flex":    _prof({7:1,8:2,9:3.5,10:4,11:4,12:4.5,13:3.5,14:3.5,15:3.5,16:3.5,17:3.5,18:3.5,19:4,20:4,21:3,22:1.5}),
 "online":  _prof({7:.8,8:1,9:1.2,10:1.5,11:1.5,12:2,13:2,14:2,15:2,16:2.5,17:3,18:3.5,19:5,20:7,21:8,22:7,23:4,0:2.5,1:1}),
}
PROF_FOR_OCC = {"Karyawan Swasta":"office","PNS/ASN":"office","Guru/Dosen":"office","Pegawai BUMN":"office","Profesional":"office",
                "Wiraswasta":"flex","Freelancer":"flex","Ibu Rumah Tangga":"flex","Mahasiswa":"student"}

# gangguan layanan (mulai, selesai, peluang gagal untuk transaksi MOBILE)
INCIDENTS = [(DT(2025,3,18,14,0), DT(2025,3,18,16,0), .5), (DT(2026,1,5,8,0), DT(2026,1,5,10,30), .4)]
LEBARAN = [DATE(2024,4,10), DATE(2025,3,31), DATE(2026,3,20)]
ISLAMIC_BONUS_DAYS = 10
JAKARTA_HOLIDAYS = set()

# --------------------------------------------------------------------------------------
# 4. FUNGSI BANTU
# --------------------------------------------------------------------------------------
def poisson(lam):
    if lam <= 0: return 0
    if lam > 25: return max(0, int(rng.gauss(lam, math.sqrt(lam)) + .5))
    L, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= L: return k
        k += 1

def lognorm(mean, sigma): return mean * math.exp(rng.gauss(0, sigma) - sigma * sigma / 2)
def clamp(x, lo, hi): return lo if x < lo else hi if x > hi else x
def rnd(x, unit): return int(round(x / unit)) * unit
def add_months(d, n):
    m = d.month - 1 + n; y = d.year + m // 12; m = m % 12 + 1
    return DATE(y, m, min(d.day, 28))
def last_day(y, m): return (DATE(y + (m == 12), m % 12 + 1, 1) - TD(days=1))
def biz_back(d):
    while d.weekday() >= 5: d -= TD(days=1)
    return d
def payday_date(y, m, pd):
    if pd == 0: return biz_back(last_day(y, m))
    return biz_back(DATE(y, m, min(pd, 28)))
def sample_time(d, cum):
    h = bisect.bisect_left(cum, rng.random() * cum[-1])
    return DT(d.year, d.month, d.day, h, rng.randrange(60), rng.randrange(60))
def wpick(items, weights): return rng.choices(items, weights=weights, k=1)[0]
def fx_rate(cur, d):
    lo, hi = FX_BASE[cur]
    frac = (d - START_D).days / max(1, (END_D - START_D).days)
    return round(lo + (hi - lo) * frac + rng.gauss(0, (hi - lo) * .03), 4)
def iso_dow(d): return d.weekday()

def pmt(P, n, rate_pa):
    r = rate_pa / 1200.0
    return P / n if r == 0 else P * r / (1 - (1 + r) ** -n)

def fmt_ref(t, tid): return "TRX%s%09d" % (t.strftime("%Y%m%d"), tid)

# --------------------------------------------------------------------------------------
# 5. MESIN SIMULASI: ID, transaksi, sesi aplikasi, event
# --------------------------------------------------------------------------------------
_ids = defaultdict(int)
def nid(k):
    _ids[k] += 1
    return _ids[k]

DEFAULT_FEE = {t[0]: t[3] for t in TTYPES}
FEAT_OF = {"TRANSFER_BANK": "TRANSFER", "TRANSFER_INTERNAL": "TRANSFER", "SAVINGS_OUT": "TRANSFER", "QRIS_PAYMENT": "QRIS",
           "EWALLET_TOPUP": "TOPUP_EWALLET", "BILL_PAYMENT": "TAGIHAN", "PULSA_DATA": "PULSA_DATA", "LOAN_INSTALLMENT": "CICILAN"}
FAIL_TECH = ["TIMEOUT", "GANGGUAN JARINGAN"]

def td(**k): return TD(**k)

def post(c, acct, ttype, direction, amount, t, channel, fee=0, desc=None, device=None, cp_bank=None, cp_acct=None,
         cp_name=None, biller=None, card=None, merch=None, mcat=None, loan=None, sched=None, orig=None,
         attempt_p=1.0, user_initiated=True):
    """Catat satu transaksi + perbarui saldo. Return (status, tx_id): SUCCESS / FAILED / SKIP (tidak dicoba)."""
    if t > END:
        return "SKIP", None, None
    amount = int(amount); fee = int(fee)
    reason = None
    if direction == "D":
        if acct.bal < amount + fee:
            if rng.random() > attempt_p:
                return "SKIP", None, None
            reason = "SALDO TIDAK CUKUP"
        else:
            if ttype in ("TRANSFER_BANK", "TRANSFER_INTERNAL") and acct.xfer.get(t.date(), 0) + amount > 25_000_000:
                reason = "LIMIT HARIAN TERLAMPAUI"
            else:
                p = 0.012 if channel == "MOBILE" else 0.003
                if channel == "MOBILE":
                    for a, b, pp in INCIDENTS:
                        if a <= t <= b: p = pp
                if rng.random() < p: reason = rng.choice(FAIL_TECH)
                elif ttype == "TRANSFER_BANK" and rng.random() < 0.004: reason = "TRANSAKSI DITOLAK BANK PENERIMA"
    tid = nid("tx")
    cur = orig[0] if orig else None
    ao = orig[1] if orig else None
    fx = orig[2] if orig else None
    if reason:
        W("transactions", tid, fmt_ref(t, tid), acct.id, ttype, direction, amount, 0, acct.bal, "IDR", cp_bank, cp_acct, cp_name,
          biller, channel, device, desc, "FAILED", reason, t, card, merch, mcat, loan, sched, cur, ao, fx)
        return "FAILED", tid, reason
    if direction == "D":
        acct.bal -= amount + fee
        if ttype in ("TRANSFER_BANK", "TRANSFER_INTERNAL"):
            acct.xfer[t.date()] = acct.xfer.get(t.date(), 0) + amount
        if user_initiated: acct.last_debit = t
    else:
        acct.bal += amount
    W("transactions", tid, fmt_ref(t, tid), acct.id, ttype, direction, amount, fee, acct.bal, "IDR", cp_bank, cp_acct, cp_name,
      biller, channel, device, desc, "SUCCESS", None, t, card, merch, mcat, loan, sched, cur, ao, fx)
    if direction == "C" and getattr(acct, "fee_pending", None):
        collect_fees(c, acct, t)
    return "SUCCESS", tid, None

def collect_fees(c, acct, t):
    """Biaya admin bulanan yang tertunda (saldo kurang saat jatuh tempo) dipotong begitu saldo sudah cukup.
    Tidak memakai rng, jadi tidak menggeser urutan acak data lain."""
    if acct.status == "DORMANT_FULL" or (c.full_end and t.date() > c.full_end + td(days=60)):
        return
    while acct.fee_pending and acct.bal >= acct.fee_pending[0]:
        fee = acct.fee_pending.pop(0)
        acct.bal -= fee
        tid = nid("tx")
        W("transactions", tid, fmt_ref(t, tid), acct.id, "ADMIN_FEE", "D", fee, 0, acct.bal, "IDR", None, None, None,
          None, "SYSTEM", None, "BIAYA ADMIN BULANAN (TERTUNDA)", "SUCCESS", None, t, None, None, None, None, None, None, None, None)

def _ret(r):  # normalisasi tuple (status, tid, reason)
    return r if len(r) == 3 else (r[0], r[1], None)

def push(c, t, kind, payload=None):
    if t > END: return
    if t < c.now: t = c.now + td(seconds=1)
    c.seq += 1
    item = (t, c.seq, kind, payload)
    if t.date() == c.cur_day: heapq.heappush(c.heap, item)
    else: c.plan[t.date()].append(item)

# ---- sesi aplikasi & event ----
def device_at(c, t):
    dv = c.devices[0]
    for d in c.devices:
        if d.reg <= t: dv = d
    return dv

def app_ok(c, t):
    return c.act_t <= t <= c.app_end

def mobile_session(c, t, browse=False):
    s = c.sess
    if s is not None and s.last_t <= t <= s.last_t + td(minutes=20):
        s.last_t = t
        return s
    dv = device_at(c, t)
    login_t = t - td(seconds=rng.randint(2, 5) if browse else rng.randint(25, 90))
    if login_t < dv.reg: login_t = dv.reg + td(seconds=1)
    if login_t < c.act_t: login_t = c.act_t
    bio = c.bio_from is not None and login_t >= c.bio_from
    r = rng.random()
    method = ("BIOMETRIC" if rng.random() < 0.88 else "PIN") if bio else ("PASSWORD" if r < 0.55 else "PIN")
    pf = c.login_fail_p * {"BIOMETRIC": 0.5, "PASSWORD": 1.6, "PIN": 1.0}[method]
    for a, b, pp in INCIDENTS:
        if a <= login_t <= b: pf = max(pf, 0.2)
    ip = "%s.%d.%d" % (c.ip_pfx, rng.randrange(255), 1 + rng.randrange(253))
    nfail = 0
    if rng.random() < pf:
        nfail = 1 + (rng.random() < 0.25)
        for k in range(nfail):
            ft = login_t - td(seconds=12 * (nfail - k) + rng.randint(0, 8))
            if ft < dv.reg: ft = dv.reg
            W("login_logs", nid("login"), c.id, dv.id, ft, ip, method, "FAILED",
              {"PASSWORD": "PASSWORD_SALAH", "PIN": "PIN_SALAH", "BIOMETRIC": "BIOMETRIK_GAGAL"}[method])
        if method == "BIOMETRIC": method = "PIN"
    lid = nid("login")
    W("login_logs", lid, c.id, dv.id, login_t, ip, method, "SUCCESS", None)
    c.last_login = login_t
    if method == "BIOMETRIC": c.bio_used = True
    dv.last = login_t
    s = NS(login_id=lid, device_id=dv.id, login_t=login_t, last_t=t, fresh=True)
    c.sess = s
    return s

def ev(c, s, feat, etype, t, detail=None, tx=None, loan=None):
    if t < FLAUNCH[feat]: return
    W("app_events", nid("event"), c.id, s.device_id, s.login_id, feat, etype, detail, t, tx, loan)

def mobile_post(c, acct, ttype, direction, amount, t, feat=None, attempt_p=1.0, **kw):
    """Transaksi yang diinisiasi nasabah di aplikasi: buat sesi login + event layar."""
    if not app_ok(c, t):
        return "SKIP", None, None
    s = mobile_session(c, t)
    feat = feat or FEAT_OF.get(ttype)
    if s.fresh:
        s.fresh = False
        if rng.random() < 0.45: ev(c, s, "CEK_SALDO", "OPEN", s.login_t + td(seconds=3))
        if rng.random() < 0.05:
            other = rng.choice(["TRANSFER", "QRIS", "TOPUP_EWALLET", "TAGIHAN", "PULSA_DATA"])
            if other != feat: ev(c, s, other, "OPEN", s.login_t + td(seconds=8))
    open_t = max(t - td(seconds=rng.randint(5, 20)), s.last_t if s.last_t < t else s.login_t + td(seconds=1))
    if open_t >= t: open_t = t - td(seconds=1)
    ev(c, s, feat, "OPEN", open_t)
    r = _ret(post(c, acct, ttype, direction, amount, t, "MOBILE", device=s.device_id, attempt_p=attempt_p, **kw))
    if r[0] == "SUCCESS":
        ev(c, s, feat, "COMPLETE", t, tx=r[1], loan=kw.get("loan"))
    elif r[0] == "FAILED":
        ev(c, s, feat, "FAILED", t, detail=(r[2] or "")[:30], loan=kw.get("loan"))
    s.last_t = max(s.last_t, t)
    return r

BROWSE_BASE = [("CEK_SALDO", .50), ("MUTASI", .20), ("PROMO", .07), ("KARTU", .035), ("PROFIL", .02), ("BANTUAN", .01),
               ("TRANSFER", .05), ("QRIS", .03), ("TOPUP_EWALLET", .03), ("TAGIHAN", .03), ("PULSA_DATA", .02)]
INTERACT = {"MUTASI": ("FILTER", .3), "PROMO": ("LIHAT_DETAIL", .35), "KARTU": ("LIHAT_LIMIT", .4), "CICILAN": ("LIHAT_JADWAL", .6),
            "PAYLATER": ("CEK_LIMIT", .5), "KTA": ("SIMULASI", .6), "JADWAL_BAYAR": ("LIHAT_JADWAL", .5), "PROFIL": ("UBAH_PENGATURAN", .2),
            "TAGIHAN": ("CEK_TAGIHAN", .5)}

def do_browse(c, t, forced=None):
    if not app_ok(c, t): return
    s = mobile_session(c, t, browse=True)
    s.fresh = False
    if forced:
        feats = list(forced)
    else:
        items = list(BROWSE_BASE)
        d = t.date()
        if c.loans_any and t >= FLAUNCH["CICILAN"]:
            near = any(abs((x - d).days) <= 3 for x in c.due_dates)
            items.append(("CICILAN", 0.24 if near else 0.05))
        if t >= SCHED_LAUNCH and (c.sched_adopter or c.sched_curious): items.append(("JADWAL_BAYAR", 0.03))
        if t >= PL_LAUNCH and c.pl_interest: items.append(("PAYLATER", 0.06))
        if t >= KTA_LAUNCH and c.kta_interest != "none": items.append(("KTA", 0.04 if c.kta_interest == "browse" else 0.03))
        k = 1 + poisson(0.8)
        feats = []
        pool = items[:]
        while pool and len(feats) < k:
            f = wpick([p[0] for p in pool], [p[1] for p in pool])
            feats.append(f); pool = [p for p in pool if p[0] != f]
    tt = max(t, s.login_t + td(seconds=2))
    for f in feats:
        ev(c, s, f, "OPEN", tt)
        it = INTERACT.get(f)
        if it and rng.random() < it[1]:
            tt += td(seconds=rng.randint(6, 40))
            ev(c, s, f, "INTERACT", tt, detail=it[0])
        tt += td(seconds=rng.randint(8, 50))
    s.last_t = max(s.last_t, tt)

def do_notif(c, t, payload):
    cat, loan_id, open_p, forced = payload[:4]
    due_ref = payload[4] if len(payload) > 4 else None
    if not c.push_enabled or not (c.reg_t <= t <= c.app_end): return
    nid_ = nid("notif")
    opened = None
    p = open_p * c.notif_open_mult
    if c.decay_start and t.date() >= c.decay_start: p *= 0.4
    if rng.random() < p:
        opened = t + td(minutes=int(rng.expovariate(1 / 90.0)) + 1)
        if opened > END: opened = None
    W("notifications", nid_, c.id, cat, t, opened, loan_id)
    if opened is not None:
        push(c, opened, "BROWSE", forced)
        if cat == "DUE_REMINDER" and due_ref is not None:
            due = due_ref
            if not due.done and rng.random() < 0.45:
                push(c, opened + td(minutes=rng.randint(10, 360)), "DUE_MANUAL", (due, 0))

# --------------------------------------------------------------------------------------
# 6. PEMBENTUKAN NASABAH + PROFIL PERILAKU (profil tersembunyi, tidak disimpan di database)
# --------------------------------------------------------------------------------------
BRANCH_ID = {}
OWN_ACCT, OWN_NAME = {}, {}          # nomor rekening utama & nama tiap nasabah (untuk transfer sesama bank)
TYPES = ["QRIS_PAYMENT", "EWALLET_TOPUP", "TRANSFER_BANK", "TRANSFER_INTERNAL", "BILL_PAYMENT", "PULSA_DATA", "ATM_WITHDRAWAL",
         "CARD_POS", "CARD_CONTACTLESS", "CARD_ONLINE", "CARD_INTL"]
BASE_RATE = [7.0, 2.2, 2.6, 1.3, 1.0, 1.0, 1.4, 1.8, 2.2, 2.0, 0.12]    # kejadian per bulan (nasabah rata-rata)
MEAN_AMT = [90000, 150000, 600000, 500000, 250000, 60000, 700000, 150000, 70000, 250000, 1200000]
IS_MOBILE = [1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0]
TYPE_IDX = {t: i for i, t in enumerate(TYPES)}
NICK = ["Ibu", "Bapak", "Kakak", "Adik", "Teman", "Kantor", "Saudara", "Tetangga"]

def rand_name():
    return (rng.choice(MALE) if rng.random() < .5 else rng.choice(FEMALE)) + " " + rng.choice(LAST)

def zipf_w(n, s=1.0): return [1.0 / (k + 1) ** s for k in range(n)]

def pick_weighted_unique(pool, k, widx):
    idx, ws, out = list(range(len(pool))), [p[widx] for p in pool], []
    for _ in range(min(k, len(pool))):
        j = wpick(idx, ws); out.append(j); ws[idx.index(j)] = 0.0
        if sum(ws) <= 0: break
    return out

def new_beneficiary(c, t, bank=None, nick=None, name=None):
    bank = bank or ("999" if rng.random() < .25 else rng.choice(BANK_CODES))
    if bank == "999" and N_CUST > 1:
        oc = rng.randint(1, N_CUST)
        acc, nm = OWN_ACCT[oc], OWN_NAME[oc]
    else:
        acc, nm = "%010d" % rng.randrange(10 ** 10), name or rand_name()
    b = NS(id=nid("bene"), bank=bank, acct=acc, name=nm, nick=nick)
    W("beneficiaries", b.id, c.id, bank, acc, nm, nick, t)
    c.benes.append(b)
    return b

def make_obl(c, kind, label, ttype, start, end=None, biller=None, bene=None, amount=0, cv=0.0, due_day=None, mode="FIXED"):
    o = NS(id=nid("obl"), kind=kind, label=label, ttype=ttype, biller=biller, bene=bene, amount=amount, cv=cv,
           due_day=due_day or rng.randint(1, 28), start=start, end=end, sched=None, sched_from=None, sched_cancel=None,
           mode=mode, off_mean=c.off_mean, off_sd=c.off_sd, insts=[])
    c.obls.append(o)
    plan_sched_for(c, o)
    return o

def plan_sched_for(c, o, base_t=None):
    """Tentukan apakah / kapan nasabah mengaktifkan Jadwal Bayar untuk kewajiban ini."""
    if not c.sched_adopter: return
    aff = {"LOAN": .85, "PAYLATER": .45, "BILL": .55, "RENT": .35, "ARISAN": .3}[o.kind]
    if rng.random() > aff: return
    base = base_t or DT.combine(o.start, dt.time(9, 0))
    mean = 10 if o.kind in ("LOAN", "PAYLATER") and rng.random() < .7 else (120 if o.kind != "LOAN" else 60)
    t = max(base, SCHED_LAUNCH + td(days=rng.random() * 20), c.act_t) + td(days=rng.expovariate(1.0 / mean))
    if t >= END - td(days=5) or t > c.app_end: return
    o.sched_from = t
    if c.app_churn:
        if rng.random() < .6 and c.app_end - td(days=rng.randint(0, 25)) > t:
            o.sched_cancel = c.app_end - td(days=rng.randint(0, 25))
    elif rng.random() < .05:
        o.sched_cancel = t + td(days=rng.randint(60, 400))
        if o.sched_cancel > END: o.sched_cancel = None
    push(c, t, "CREATE_SCHED", o)

def make_customer(i):
    c = NS()
    c.id = i
    c.now = START; c.seq = 0; c.plan = defaultdict(list); c.heap = []; c.cur_day = None
    gender, first, second, last = PRE[i]
    city, prov, nikp, _w = wpick(CITIES, [x[3] for x in CITIES])
    # Ibu Rumah Tangga hanya untuk nasabah perempuan (jenis kelamin, nama, dan pekerjaan harus selaras)
    occ = wpick(OCCS, [0 if (o[0] == "Ibu Rumah Tangga" and gender == "L") else o[4] for o in OCCS])
    c.occ, c.salaried = occ[0], occ[3]
    c.income = int(round(rng.uniform(occ[1], occ[2]) / 100000.0) * 100000)
    # umur minimum yang wajar untuk tiap pekerjaan (mis. dosen/profesional tidak mungkin berusia 20 tahun)
    min_age = {"Karyawan Swasta": 21, "PNS/ASN": 23, "Guru/Dosen": 24, "Pegawai BUMN": 23, "Profesional": 27,
               "Wiraswasta": 21, "Freelancer": 20, "Ibu Rumah Tangga": 24}.get(c.occ, 21)
    latest_birth = min(DATE(2005, 12, 31), END_D - td(days=int(min_age * 365.25) + 1))
    birth = DATE(1962, 1, 1) + td(days=rng.randrange((latest_birth - DATE(1962, 1, 1)).days))
    if c.occ == "Mahasiswa": birth = DATE(2001, 1, 1) + td(days=rng.randrange(1800))
    c.age = (END_D - birth).days / 365.25
    c.full_name = first + second + " " + last
    old = rng.random() < .35
    if old:
        created = DT.combine(DATE(2020, 1, 1) + td(days=rng.randrange(1460)), dt.time(8, 0)) + td(seconds=int(rng.random() * 9 * 3600))
    else:
        span = max(30, (END_D - td(days=40) - START_D).days)
        created = DT.combine(START_D + td(days=int(rng.random() ** 1.4 * span)), dt.time(8, 0)) + td(seconds=int(rng.random() * 9 * 3600))
    c.old, c.created = old, created
    c.first_day = max(created.date(), START_D)
    kyc = "FULL" if rng.random() < .7 else "BASIC"
    W("customers", i, "CIF%09d" % i, "%s%02d%02d%s%04d" % (nikp, rng.randint(1, 28), birth.day + (40 if gender == "P" else 0), birth.strftime("%m%y"), i % 10000),
      c.full_name, gender, birth, rng.choice(PHONE_PFX) + "%07d" % ((i * 7919) % 10000000),
      "%s.%s%d@%s" % ((first + second.replace(" ", "")).lower(), last.lower(), i, rng.choice(DOMAINS)),
      "Jl. %s No. %d, RT %02d/RW %02d, %s" % (rng.choice(STREETS), rng.randint(1, 150), rng.randint(1, 12), rng.randint(1, 10), city),
      city, prov, c.occ, c.income, kyc, BRANCH_ID[city], created)
    c.branch = BRANCH_ID[city]
    # ---- profil perilaku ----
    c.tech = clamp(1.05 - (c.age - 25) * 0.012 + rng.gauss(0, .2), .1, 1.0)
    c.prof = PROF_FOR_OCC[c.occ]
    c.activity = lognorm(1.0, .4) * INTENS
    dig = clamp(.35 + 1.6 * c.tech, .3, 2.0) * lognorm(1, .25)
    cash = clamp(1.8 - 1.2 * c.tech + rng.gauss(0, .4), .3, 2.2)
    xfer = lognorm(1, .4)
    c.traveler = rng.random() < .12
    c.mult = [dig, dig, xfer, xfer, lognorm(1, .3), lognorm(1, .3), cash, clamp(lognorm(1, .5) * (1.3 - .5 * c.tech), .2, 2.5),
              dig ** .7 * lognorm(1, .3), dig * lognorm(1, .4), (6.0 if c.traveler else .5)]
    c.boost = wpick([0.0, .7, 2.0], [.25, .4, .35]) if c.salaried or c.occ in ("Mahasiswa", "Ibu Rumah Tangga") else 0.0
    # gajian
    c.employer = c.payday = None
    c.allow_day = None
    if c.salaried:
        c.employer, c.payday = rng.choice(EMPLOYERS[c.occ])
        c.salary = max(500000, rnd(c.income * rng.uniform(.97, 1.0), 50000))
    elif c.occ in ("Mahasiswa", "Ibu Rumah Tangga"):
        c.allow_day = rng.choice([1, 2, 3, 4, 5, 25, 26, 27, 28]) if c.occ == "Mahasiswa" else rng.randint(1, 7)
        c.parent_name = ("BAPAK " if c.occ == "Mahasiswa" else "SUAMI ") + last.upper()
    c.pay_ref_day = c.payday if c.salaried else c.allow_day
    c.sticky = {}
    # kelas ketepatan bayar
    kls = wpick([(-3, 1.2), (0, 1.0), (1, 1.5), (4, 3.0)], [.25, .40, .20, .15])
    c.off_mean, c.off_sd = kls
    c.ratio = clamp(rng.gauss(.95, .24), .45, 1.45)
    # akun
    c.main = NS(id=i, number="88%08d" % i, bal=0, product="TABUNGAN" if rng.random() < .75 else "TABUNGAN_PLUS", opened=created,
                xfer={}, last_debit=None, status="ACTIVE")
    OWN_ACCT[i] = c.main.number
    c.sav = None
    c.save_pct = 0.0
    if rng.random() < .30:
        ot = created + (END - created) * rng.uniform(.05, .6) if old is False else START + (END - START) * rng.uniform(0, .6)
        if ot < END - td(days=60):
            k = nid("acct2")
            c.sav = NS(id=N_CUST + k, number="88%08d" % (N_CUST + k), bal=0, opened=ot, xfer={}, last_debit=None, status="ACTIVE",
                       product="GIRO" if c.occ in ("Wiraswasta", "Profesional") and rng.random() < .5 else "TABUNGAN_PLUS")
            c.save_pct = rng.uniform(.05, .2)
    # kartu debit
    net = wpick(["VISA", "GPN", "MASTERCARD"], [.5 if c.traveler else .38, .12 if c.traveler else .47, .38 if c.traveler else .15])
    issue = max(created.date(), DATE(2022, 1, 1)) if old else created.date() + td(days=rng.randint(0, 5))
    c.card = NS(id=i, net=net, issue=issue, contactless=rng.random() < .7 + .1 * c.tech, last4="%04d" % rng.randrange(10000))
    c.online_at = c.intl_at = None
    if rng.random() < (.45 + .4 * c.tech):
        d0 = (DT.combine(issue, dt.time(10, 0)) + td(days=rng.expovariate(1 / 90.0))) if not old else \
             (DT(2023, 6, 1) + td(days=rng.random() * 900))
        if d0 < END - td(days=20): c.online_at = d0
    if net != "GPN" and c.online_at and rng.random() < (.95 if c.traveler else .08):
        d1 = c.online_at + td(days=rng.expovariate(1 / 100.0))
        if d1 < END - td(days=20): c.intl_at = d1
    # pengguna aplikasi
    if old:
        c.reg_t = START + td(hours=8) + td(seconds=rng.random() * 300 * 86400)
    else:
        c.reg_t = created + td(seconds=rng.random() * 3 * 86400)
    if c.reg_t > END - td(days=25): c.reg_t = END - td(days=25 + rng.random() * 5)
    lag = rng.uniform(20, 60) if rng.random() < .08 else rng.expovariate(1 / (5.0 if old else 3.0))
    c.act_t = c.reg_t + td(days=lag)
    c.app_churn = rng.random() < .09
    c.app_end, c.decay_start, c.full_end = END + td(days=1), None, None
    if c.app_churn and (END - c.act_t).days > 240:
        life = (END - c.reg_t).total_seconds()
        c.app_end = c.reg_t + td(seconds=life * rng.uniform(.4, .9))
        if (c.app_end - c.act_t).days < 120: c.app_end = c.act_t + td(days=120)
        c.decay_start = (c.app_end - td(days=75)).date()
        if rng.random() < .5: c.full_end = (c.app_end + td(days=rng.randint(15, 45))).date()
    else:
        c.app_churn = False
    c.user_id = i
    c.push_enabled = rng.random() < .82
    c.notif_open_mult = clamp(.5 + c.tech * .7 + rng.gauss(0, .15), .2, 1.4)
    c.login_fail_p = clamp(rng.gauss(.035, .015), .01, .09) * (1.4 - c.tech * .5)
    c.ip_pfx = rng.choice(IP_PFX)
    c.bio_from = None
    if rng.random() < (.25 + .65 * c.tech):
        c.bio_from = c.reg_t + td(days=rng.expovariate(1 / 120.0))
    c.bio_used = False
    c.last_login = None
    c.browse_rate = clamp(lognorm(.17 * c.tech ** .5 * c.activity, .35), .04, .9)
    # perangkat
    ios = rng.random() < .3
    def mkdev(reg, ios):
        return NS(id=uuid.UUID(int=rng.getrandbits(128), version=4),
                  name=rng.choice(IOS if ios else ANDROID), os="IOS" if ios else "ANDROID",
                  osv=rng.choice(IOS_V if ios else AND_V), app=rng.choice(APP_V), reg=reg, last=None, active=True)
    c.devices = [mkdev(c.reg_t, ios)]
    if rng.random() < .3:
        r2 = c.reg_t + td(days=90 + rng.random() * 600)
        if r2 < min(c.app_end - td(days=30), END - td(days=20)):
            c.devices.append(mkdev(r2, rng.random() < .3))
    c.sess = None
    # penerima tersimpan awal
    c.benes = []
    for _ in range(rng.randint(3, 9)):
        new_beneficiary(c, max(created, START) + td(days=rng.random() * max(1, (min(END, c.act_t) - max(created, START)).days)),
                        nick=rng.choice(NICK) if rng.random() < .4 else None)
    # merchant favorit
    c.fav_off = pick_weighted_unique(OFFLINE, rng.randint(6, 14), 3)
    c.fav_on = pick_weighted_unique(ONLINE, rng.randint(3, 7), 3)
    c.fav_w_off = zipf_w(len(c.fav_off), .8)
    c.fav_w_on = zipf_w(len(c.fav_on), .8)
    # scheduler, paylater, KTA
    c.sched_adopter = rng.random() < (.15 + .4 * c.tech)
    c.sched_curious = rng.random() < .25
    c.pl_interest = False
    c.pl = None
    c.pl_obl = None
    c.pl_due_day = rng.choice([5, 10, 15, 20, 25])
    c.pl_rate = 0.0
    c.loans, c.loans_any, c.sps, c.obls, c.due_dates = [], False, [], [], []
    c.inst_by_month = defaultdict(list)
    c.subs = []
    c.kta_interest = "none"
    c.kta_retry = 0
    return c

def estimate_and_obligations(c):
    """Bentuk kewajiban rutin (kos, internet, BPJS, dst), lalu kalibrasi skala nominal belanja agar sesuai penghasilan."""
    fd = c.first_day
    def start_after(lo=1, hi=45):
        return fd + td(days=rng.randint(lo, hi)) if not c.old else fd - td(days=rng.randint(0, 200))
    inc = c.income
    fixed = 0.0
    if rng.random() < (.42 if inc >= 5e6 else .2):
        bl = wpick(["INDIHOME", "FIRSTMEDIA", "BIZNET"], [.55, .2, .25])
        amt = rng.choice([275000, 330000, 385000, 450000, 550000])
        make_obl(c, "BILL", "Internet " + bl, "BILL_PAYMENT", start_after(), biller=BID[bl], amount=amt); fixed += amt
    if rng.random() < (.55 if c.occ != "PNS/ASN" else .2):
        amt = 42000 * rng.randint(1, 5)
        make_obl(c, "BILL", "BPJS Kesehatan", "BILL_PAYMENT", start_after(), biller=BID["BPJSKES"], amount=amt,
                 due_day=10 if rng.random() < .7 else None); fixed += amt
    if rng.random() < (.22 if inc >= 6e6 else .08):
        amt = int(clamp(inc * .05, 150000, 1200000))
        make_obl(c, "BILL", "PLN Pascabayar", "BILL_PAYMENT", start_after(), biller=BID["PLNPOST"], amount=amt, cv=.2,
                 due_day=20 if rng.random() < .6 else None, mode="FULL_DUE"); fixed += amt
    if rng.random() < (.14 if inc >= 8e6 else 0):
        amt = rnd(rng.uniform(300000, 1500000), 50000)
        make_obl(c, "BILL", "Premi Asuransi", "BILL_PAYMENT", start_after(), biller=BID["ASURANSI1"], amount=amt); fixed += amt
    if rng.random() < (.2 if 4e6 <= inc <= 3e7 else .03):
        amt = rnd(clamp(inc * rng.uniform(.12, .25), 500000, 3000000), 50000)
        st = (fd - td(days=rng.randint(0, 300))) if rng.random() < .6 else start_after()
        en = st + td(days=30 * rng.randint(12, 36))
        make_obl(c, "BILL", "Cicilan Multifinance", "BILL_PAYMENT", st, end=en, biller=BID[rng.choice(["ADIRA", "FIF"])], amount=amt); fixed += amt
    p_rent = .55 if c.occ in ("Mahasiswa", "Freelancer") or c.age < 28 else .25
    if rng.random() < p_rent:
        amt = rnd(clamp(inc * rng.uniform(.15, .3), 600000, 4000000), 50000)
        b = new_beneficiary(c, c.created, bank=rng.choice(BANK_CODES), nick=rng.choice(["Kos", "Kontrakan"]))
        make_obl(c, "RENT", "Sewa " + b.nick, "TRANSFER_BANK", start_after(), bene=b, amount=amt,
                 due_day=rng.choice([1, 1, 2, 3, 5, 10, 25])); fixed += amt
    if rng.random() < .12:
        amt = rnd(clamp(inc * .05, 100000, 1000000), 50000)
        b = new_beneficiary(c, c.created, bank=rng.choice(BANK_CODES), nick="Arisan")
        make_obl(c, "ARISAN", "Arisan", "TRANSFER_BANK", start_after(), bene=b, amount=amt); fixed += amt
    # langganan kartu (Netflix dkk) - hanya bila belanja online sudah aktif
    if c.online_at:
        for _ in range(wpick([0, 1, 2, 3], [.55, .25, .15, .05])):
            nm, lo, hi = rng.choice(SUBS)
            if any(s.name == nm for s in c.subs): continue
            c.subs.append(NS(name=nm, amt=rng.choice([lo, hi]) if lo != hi else lo, day=rng.randint(1, 28),
                             hour=rng.randint(0, 9), start=max(c.online_at.date(), fd) + td(days=rng.randint(0, 20))))
            fixed += c.subs[-1].amt
    # kalibrasi
    sc_inc = (inc / 8e6) ** .6
    expd = sum(BASE_RATE[k] * c.mult[k] * c.activity * MEAN_AMT[k] * sc_inc for k in range(len(TYPES)))
    sav = c.save_pct * inc if c.sav else 0
    c.s = clamp((c.ratio * inc - fixed - sav) / max(expd, 1), .3, 2.2)
    c.sc_inc = sc_inc
    # rencana KTA / Paylater
    if rng.random() < .38 * clamp(c.tech + .3, .3, 1.0):
        t0 = max(PL_LAUNCH, c.act_t + td(days=14)) + td(days=rng.expovariate(1 / 200.0))
        if t0 < END - td(days=45) and t0 < c.app_end - td(days=45):
            c.pl_interest = True
            for k in range(rng.randint(0, 3)): push(c, t0 - td(days=rng.randint(1, 20)), "BROWSE", ["PAYLATER"])
            push(c, t0, "PL_ACTIVATE", None)
            c.pl_rate = clamp(lognorm(.9, .6) * (1.4 - .6 * c.tech if c.ratio > 1 else 1) * clamp(c.tech + .3, .3, 1.3), .15, 3.5)
    elif rng.random() < .25: c.pl_interest = True
    r = rng.random()
    if r < .12 * clamp(inc / 8e6, .4, 1.3):
        c.kta_interest = "applicant"
        t0 = max(KTA_LAUNCH, c.act_t + td(days=30)) + td(days=rng.expovariate(1 / 250.0))
        if t0 < END - td(days=40) and t0 < c.app_end - td(days=60):
            for k in range(rng.randint(2, 5)):
                push(c, t0 - td(days=rng.randint(1, 14), hours=rng.randint(0, 6)), "BROWSE_KTA", None)
            push(c, t0, "KTA_APPLY", None)
        else: c.kta_interest = "browse"
    elif r < .32: c.kta_interest = "browse"
    # tindakan toggle kartu via aplikasi
    for att, tag in ((c.online_at, "ENABLE_ONLINE"), (c.intl_at, "ENABLE_INTL")):
        if att and att >= START and att > c.act_t and att < c.app_end and rng.random() < .8:
            push(c, att, "CARD_TOGGLE", tag)
    return fixed

# --------------------------------------------------------------------------------------
# 7. GENERATOR HARIAN + HANDLER KEJADIAN
# --------------------------------------------------------------------------------------
DOW_A = [1.0, 1.0, 1.0, 1.05, 1.15, 1.4, 1.3]
DOW_B = [1.1, 1.1, 1.05, 1.05, 1.1, .7, .65]
DOW_GRP = [0, 0, 1, 1, 1, 1, 1, 0, 0, 2, 0]      # 0 = A (belanja), 1 = B (urusan/transfer), 2 = netral
SIG = [.8, .6, 1.1, 1.1, .8, .5, .5, .8, .8, .9, .8]
EW_CODES, EW_W = ["GOPAY", "OVO", "DANA", "SHOPEEPAY", "LINKAJA"], [.3, .25, .25, .15, .05]
BILL_CODES, BILL_W = ["PLNPRE", "PDAM", "BPJSTK", "MNCVISION", "SAMSAT", "PBB", "SPPKAMPUS"], [.5, .15, .08, .05, .05, .05, .12]
CARRIERS, CARRIER_W = ["TSEL", "ISAT", "XL", "TRI", "SMART"], [.4, .2, .2, .12, .08]
OFF_W, ON_W = [m[3] for m in OFFLINE], [m[3] for m in ONLINE]
INTL_W = [m[4] for m in INTL]
EW_CHOICES = [20000, 25000, 50000, 75000, 100000, 150000, 200000, 300000, 500000, 1000000]
TOKEN_CH = [20000, 50000, 100000, 200000, 500000, 1000000]
PULSA_CH = [10000, 15000, 20000, 25000, 50000, 100000, 150000, 200000]
MON_ID = ["", "JAN", "FEB", "MAR", "APR", "MEI", "JUN", "JUL", "AGU", "SEP", "OKT", "NOV", "DES"]

def nearest(a, ch): return min(ch, key=lambda x: abs(x - a))
def mob_mult(c, d):
    if d < c.act_t.date() or d > c.app_end.date(): return 0.0
    if c.decay_start and d >= c.decay_start:
        return max(.15, 1 - .85 * (d - c.decay_start).days / max(1, (c.app_end.date() - c.decay_start).days))
    return 1.0
def card_mult(c, d):
    if c.full_end:
        if d > c.full_end: return 0.0
        if c.decay_start and d >= c.decay_start:
            return max(.15, 1 - .85 * (d - c.decay_start).days / max(1, (c.full_end - c.decay_start).days))
    return 1.0

def last_pay_k(c, d):
    pd = c.pay_ref_day
    if pd is None: return None
    f = (lambda y, m: payday_date(y, m, pd)) if c.salaried else (lambda y, m: DATE(y, m, pd))
    this = f(d.year, d.month)
    if d >= this: return (d - this).days
    y, m = (d.year, d.month - 1) if d.month > 1 else (d.year - 1, 12)
    return (d - f(y, m)).days

def gen_spend(c, d, mobact, cardact):
    tr = (d - START_D).days / 365.0
    k = last_pay_k(c, d)
    pf = (1 + c.boost * math.exp(-k / 5.0)) / (1 + c.boost * 5.5 / 30.4) if (k is not None and c.boost > 0) else 1.0
    seas = seas_x = 1.0
    for L in LEBARAN:
        dd = (L - d).days
        if 0 <= dd <= 14: seas, seas_x = 1.3, 1.6
        elif -7 <= dd < 0: seas, seas_x = .6, .5
    harb = 2.5 if (d.month, d.day) in ((11, 11), (12, 12)) else 1.0
    dw = d.weekday()
    ws = []
    for i in range(11):
        if IS_MOBILE[i]:
            if mobact <= 0: ws.append(0.0); continue
            g = mobact
        else:
            if cardact <= 0: ws.append(0.0); continue
            g = cardact
            if i == 7 and d < c.card.issue: ws.append(0.0); continue
            if i == 8 and (not c.card.contactless or d < c.card.issue): ws.append(0.0); continue
            if i == 9 and (c.online_at is None or d < c.online_at.date()): ws.append(0.0); continue
            if i == 10 and (c.intl_at is None or d < c.intl_at.date()): ws.append(0.0); continue
        w = BASE_RATE[i] * c.mult[i] * c.activity / 30.4 * g * pf
        grp = DOW_GRP[i]
        w *= DOW_A[dw] if grp == 0 else DOW_B[dw] if grp == 1 else 1.0
        if i == 0: w *= 1 + .30 * tr
        elif i == 1: w *= (1 + .15 * tr) * (harb if harb > 1 else 1)
        elif i == 6: w *= max(.45, 1 - .14 * tr)
        elif i == 8: w *= 1 + .25 * tr
        elif i == 9: w *= (1 + .12 * tr) * harb
        if i in (2, 3): w *= seas_x
        elif i in (0, 7, 8, 9): w *= seas
        ws.append(w)
    tot = sum(ws)
    if tot <= 0: return
    n = poisson(tot)
    for _ in range(n):
        i = wpick(range(11), ws)
        push(c, sample_time(d, PROF["online"] if i == 9 else PROF[c.prof]), "SPEND", i)
    # pembelian paylater
    if c.pl and mobact > 0 and d >= c.pl.since.date():
        for _ in range(poisson(c.pl_rate / 30.4 * mobact * c.activity * (1.2 if (k is not None and k > 18) else 1.0))):
            push(c, sample_time(d, PROF["online"]), "PL_BUY", None)

def gen_browse(c, d, mobact):
    if mobact <= 0: return
    k = last_pay_k(c, d)
    f = 1.0 + (.6 * c.boost * math.exp(-k / 3.0) if k is not None else 0)
    n = poisson(c.browse_rate * mobact * f * (1.15 if d.weekday() >= 5 else 1.0))
    for _ in range(n):
        push(c, sample_time(d, PROF["online"] if rng.random() < .3 else PROF[c.prof]), "BROWSE", None)

def gen_income(c, d):
    if c.full_end and d > c.full_end: return
    y, m = d.year, d.month
    if d < c.first_day + td(days=3): return
    inc = c.income
    credit = lambda ttype, amt, desc, name, t, tag=None: push(c, t, "CREDIT", NS(ttype=ttype, amt=int(amt), desc=desc, name=name, tag=tag))
    tm = lambda: DT(d.year, d.month, d.day, rng.randint(0, 7), rng.randrange(60), rng.randrange(60))
    if c.salaried:
        if d == payday_date(y, m, c.payday):
            amt = c.salary
            if m in (3, 12) and rng.random() < .25: amt += rnd(c.salary * rng.uniform(.3, 1.0), 50000)
            credit("SALARY_CREDIT", amt, "GAJI %s %d %s" % (MON_ID[m], y, c.employer), c.employer, tm(), "PAY")
        for L in LEBARAN:
            if d == L - td(days=ISLAMIC_BONUS_DAYS):
                credit("SALARY_CREDIT", rnd(c.salary * rng.uniform(.8, 1.0), 50000), "THR %d %s" % (y, c.employer), c.employer, tm(), "PAY")
    elif c.occ in ("Mahasiswa", "Ibu Rumah Tangga"):
        if d.day == c.allow_day:
            credit("TRANSFER_IN", rnd(inc * rng.uniform(.75, 1.0), 50000), "KIRIMAN " + c.parent_name, c.parent_name, tm(), "PAY")
        if rng.random() < .012:
            credit("TRANSFER_IN", rnd(inc * rng.uniform(.1, .4), 50000), "TAMBAHAN " + c.parent_name, c.parent_name, tm())
    else:
        key = (y, m)
        if key not in c.sticky:
            c.sticky[key] = (lognorm(1, .25), rng.randint(3, 8))
        mf, kk = c.sticky[key]
        if rng.random() < kk / 30.4:
            nm = rand_name().upper()
            credit("TRANSFER_IN", max(50000, rnd(lognorm(inc * mf * .62 / kk, .7), 10000)), "TRANSFER DARI " + nm, nm, tm())
        if rng.random() < (1.2 if c.occ == "Wiraswasta" else .12) / 30.4:
            push(c, sample_time(d, PROF["flex"]), "CREDIT", NS(ttype="CASH_DEPOSIT", amt=max(100000, rnd(lognorm(inc * .12, .7), 50000)), desc="SETOR TUNAI", name=None, tag="CASH"))
    if rng.random() < 1.0 / 30.4:
        nm = rand_name().upper()
        credit("TRANSFER_IN", max(20000, rnd(lognorm(250000 * c.sc_inc, .9), 10000)), "TRF DARI " + nm, nm, DT(d.year, d.month, d.day, rng.randint(7, 22), rng.randrange(60), rng.randrange(60)))

def h_credit(c, t, p):
    ch = "SYSTEM" if p.tag != "CASH" else "ATM"
    cp_bank = cp_acct = None
    if p.ttype == "TRANSFER_IN" and p.name:
        # bank & rekening pengirim ditentukan dari nama (hash), tanpa memakai rng, sehingga pengirim yang sama selalu
        # punya rekening yang sama dan data lain tidak berubah.
        hh = int(hashlib.md5(p.name.encode()).hexdigest(), 16)
        cp_bank, cp_acct = BANK_CODES[hh % len(BANK_CODES)], "%010d" % ((hh // 997) % 10**10)
    st, tid, _ = post(c, c.main, p.ttype, "C", p.amt, t, ch, desc=p.desc, cp_name=p.name, cp_bank=cp_bank, cp_acct=cp_acct, user_initiated=False)
    if st == "SUCCESS" and p.tag == "PAY":
        if c.push_enabled and app_ok(c, t):
            push(c, t + td(seconds=rng.randint(20, 120)), "NOTIF", ("GAJI_MASUK", None, .66, ["CEK_SALDO"] if rng.random() < .65 else ["CEK_SALDO", "MUTASI"]))
        if c.sav and t >= c.sav.opened:
            push(c, sample_time(t.date() + td(days=rng.randint(1, 3)), PROF[c.prof]), "SAVE", None)

def h_open_deposit(c, t, p):
    amt = c.open_amt
    desc = "SALDO AWAL (MIGRASI DATA 2024)" if c.old else "SETORAN AWAL PEMBUKAAN REKENING"
    post(c, c.main, "CASH_DEPOSIT", "C", amt, t, "SYSTEM", desc=desc, user_initiated=False)

def h_fee(c, t, p):
    acct = p
    if t < acct.opened or acct.status == "DORMANT_FULL": return
    if c.full_end and t.date() > c.full_end + td(days=60): return
    fee = {"TABUNGAN": 6500, "TABUNGAN_PLUS": 12500, "GIRO": 25000}[acct.product]
    if getattr(acct, "fee_pending", None) is None: acct.fee_pending = []
    if acct.bal >= fee and not acct.fee_pending:
        post(c, acct, "ADMIN_FEE", "D", fee, t, "SYSTEM", desc="BIAYA ADMIN BULANAN", user_initiated=False)
    else:
        # saldo kurang: biaya tidak hilang, ditagih otomatis begitu ada dana masuk yang cukup (lihat collect_fees)
        acct.fee_pending.append(fee)
        collect_fees(c, acct, t)

def h_interest(c, t, p):
    acct = p
    if t < acct.opened: return
    amt = int(acct.bal * (.012 if acct.product == "TABUNGAN" else .025) / 12)
    if amt >= 1: post(c, acct, "INTEREST", "C", amt, t, "SYSTEM", desc="BUNGA TABUNGAN", user_initiated=False)

def h_save(c, t, p):
    if not c.sav or t < c.sav.opened: return
    amt = max(50000, rnd(c.save_pct * c.income, 50000))
    if rng.random() < .07 and c.sav.bal >= amt:
        src, dst, dsc = c.sav, c.main, "TARIK DANA DARI TABUNGAN"
        amt = min(rnd(c.sav.bal * rng.uniform(.2, .7), 50000), c.sav.bal)
        if amt < 50000: return
    else:
        src, dst, dsc = c.main, c.sav, "PINDAH DANA KE REKENING " + c.sav.number
    if src is c.sav:
        st, tid, _ = mobile_post(c, src, "SAVINGS_OUT", "D", amt, t, feat="TRANSFER", desc=dsc, attempt_p=.5)
    else:
        st, tid, _ = mobile_post(c, src, "SAVINGS_OUT", "D", amt, t, feat="TRANSFER", desc=dsc, attempt_p=.5) if app_ok(c, t) else \
            _ret(post(c, src, "SAVINGS_OUT", "D", amt, t, "ATM", desc=dsc, attempt_p=.5))
    if st == "SUCCESS":
        post(c, dst, "SAVINGS_IN", "C", amt, t, "SYSTEM", desc="PINDAH DANA MASUK", user_initiated=False)

def pick_fav(c, which):
    if which == "off":
        if rng.random() < .6: return OFFLINE[wpick(c.fav_off, c.fav_w_off)]
        return OFFLINE[wpick(range(len(OFFLINE)), OFF_W)]
    if rng.random() < .6: return ONLINE[wpick(c.fav_on, c.fav_w_on)]
    return ONLINE[wpick(range(len(ONLINE)), ON_W)]

def spend_amount(c, i, mult=1.0):
    a = lognorm(MEAN_AMT[i] * c.s * c.sc_inc * mult, SIG[i])
    if i == 0: a = clamp(rnd(a, 500), 5000, 3_000_000)
    elif i == 1: a = nearest(a, EW_CHOICES)
    elif i in (2, 3): a = clamp(rnd(a, 100000 if a > 2e6 else 50000 if a > 5e5 else 10000), 10000, 20_000_000)
    elif i == 4: a = nearest(a, TOKEN_CH)
    elif i == 5: a = nearest(a, PULSA_CH)
    elif i == 6: a = clamp(rnd(a, 50000), 100000, 3_000_000)
    else: a = clamp(rnd(a, 1000), 10000, 15_000_000)
    return int(a)

def h_spend(c, t, i):
    ty = TYPES[i]
    if IS_MOBILE[i] and not app_ok(c, t): return
    if i == 0:
        m = pick_fav(c, "off")
        mobile_post(c, c.main, ty, "D", spend_amount(c, 0, m[2]), t, desc="QRIS " + m[0], cp_name=m[0], merch=m[0], mcat=m[1], attempt_p=.18)
    elif i == 1:
        code = c.ew if (rng.random() < .75) else wpick(EW_CODES, EW_W)
        mobile_post(c, c.main, ty, "D", spend_amount(c, 1), t, fee=1000, desc="TOP UP " + code, biller=BID[code], attempt_p=.18)
    elif i in (2, 3):
        amt = spend_amount(c, i)
        bank_req = "999" if i == 3 else None
        pool = [b for b in c.benes if (b.bank == "999") == (i == 3)]
        new = (not pool) or rng.random() < .22
        if new:
            bank = "999" if i == 3 else rng.choice(BANK_CODES)
            if bank == "999":
                oc = rng.randint(1, N_CUST); acc, nm = OWN_ACCT[oc], OWN_NAME[oc]
            else:
                acc, nm = "%010d" % rng.randrange(10 ** 10), rand_name()
            b = NS(id=None, bank=bank, acct=acc, name=nm, nick=None)
        else:
            b = pool[wpick(range(len(pool)), zipf_w(len(pool)))]
        st, tid, _ = mobile_post(c, c.main, ty, "D", amt, t, fee=2500 if i == 2 else 0,
                                 desc=("TRF %s ke %s" % ("BI-FAST" if i == 2 else "SESAMA", b.name)), cp_bank=b.bank, cp_acct=b.acct,
                                 cp_name=b.name, attempt_p=.18)
        if st == "SUCCESS" and new and rng.random() < .3:
            nb = new_beneficiary(c, t + td(seconds=20), bank=b.bank, nick=None, name=b.name)
    elif i == 4:
        code = wpick(BILL_CODES, BILL_W)
        mobile_post(c, c.main, ty, "D", spend_amount(c, 4), t, fee=2500, desc="BAYAR " + code, biller=BID[code], attempt_p=.18)
    elif i == 5:
        code = c.carrier if rng.random() < .8 else wpick(CARRIERS, CARRIER_W)
        mobile_post(c, c.main, ty, "D", spend_amount(c, 5), t, desc="PULSA/DATA " + code, biller=BID[code], attempt_p=.18)
    elif i == 6:
        post(c, c.main, ty, "D", spend_amount(c, 6), t, "ATM", desc="TARIK TUNAI ATM", attempt_p=.1)
    elif i in (7, 8):
        m = pick_fav(c, "off")
        post(c, c.main, ty, "D", spend_amount(c, i, m[2]), t, "CARD", desc=("POS " if i == 7 else "TAP ") + m[0], cp_name=m[0],
             card=c.card.id, merch=m[0], mcat=m[1], attempt_p=.18)
    elif i == 9:
        m = pick_fav(c, "on")
        post(c, c.main, ty, "D", spend_amount(c, 9, m[2]), t, "CARD", desc="ONLINE " + m[0], cp_name=m[0], card=c.card.id,
             merch=m[0], mcat=m[1], attempt_p=.18)
    else:
        m = INTL[wpick(range(len(INTL)), INTL_W)]
        cur = m[2]; ao = lognorm(m[3] * (c.sc_inc ** .5), .7)
        ao = rnd(ao, 100) if cur == "JPY" else round(ao, 2)
        rate = fx_rate(cur, t.date())
        idr = max(10000, int(ao * rate))
        post(c, c.main, ty, "D", idr, t, "CARD", fee=int(idr * .0125), desc="INTL " + m[0], cp_name=m[0], card=c.card.id, merch=m[0],
             mcat=m[1], orig=(cur, ao, rate), attempt_p=.18)

def h_sub(c, t, s):
    if s.start > t.date() or (c.full_end and t.date() > c.full_end): return
    post(c, c.main, "CARD_ONLINE", "D", s.amt, t, "CARD", desc="LANGGANAN " + s.name, cp_name=s.name, card=c.card.id,
         merch=s.name, mcat="ENTERTAINMENT", attempt_p=1.0, user_initiated=False)

def h_card_toggle(c, t, tag):
    if not app_ok(c, t): return
    s = mobile_session(c, t)
    s.fresh = False
    ev(c, s, "KARTU", "OPEN", max(t, s.login_t + td(seconds=2)))
    ev(c, s, "KARTU", "INTERACT", t + td(seconds=15), detail=tag)
    ev(c, s, "KARTU", "COMPLETE", t + td(seconds=30), detail=tag)

# ---------------- kewajiban rutin & cicilan ----------------
def plan_dues_for_month(c, d):
    y, m = d.year, d.month
    first_of_month = DATE(y, m, 1)
    for o in c.obls:
        if o.kind in ("LOAN", "PAYLATER"): continue
        due = DATE(y, m, o.due_day)
        if due < o.start or (o.end and due > o.end) or due < d: continue
        if c.full_end and due > c.full_end: continue
        amt = o.amount if o.cv == 0 else rnd(lognorm(o.amount, o.cv), 1000)
        plan_due(c, NS(obl=o, date=due, amount=amt, insts=[], done=False), d)
    groups = defaultdict(list)
    for inst in c.inst_by_month.pop((y, m), []):
        if inst.loan.status == "DEFAULT": continue
        groups[inst.obl.id].append(inst)
    for oid, insts in groups.items():
        o = insts[0].obl
        plan_due(c, NS(obl=o, date=min(i.due for i in insts), amount=sum(i.amt for i in insts), insts=insts, done=False), d)

def plan_due(c, due, today):
    o = due.obl
    sp = o.sched
    if o.kind in ("LOAN", "PAYLATER"):
        c.due_dates.append(due.date)
    if sp is not None and sp.status == "ACTIVE" and sp.created.date() < due.date:
        t = DT(due.date.year, due.date.month, due.date.day, 6, rng.randrange(0, 25), rng.randrange(60))
        push(c, t, "DUE_SCHED", (due, sp, 0))
        if c.push_enabled and sp.notify > 0:
            nt = DT.combine(max(due.date - td(days=sp.notify), today), dt.time(9, rng.randrange(60)))
            push(c, nt, "NOTIF", ("SCHEDULE_REMINDER", getattr(o, "loan_id", None), .5, ["JADWAL_BAYAR"]))
    else:
        off = int(round(rng.gauss(o.off_mean, o.off_sd)))
        pay_date = max(due.date + td(days=off), today)
        t = sample_time(pay_date, PROF[c.prof])
        push(c, t, "DUE_MANUAL", (due, 0))
        if c.push_enabled and rng.random() < .7:
            nt = DT.combine(max(due.date - td(days=3), today), dt.time(9, rng.randrange(60)))
            push(c, nt, "NOTIF", ("DUE_REMINDER", getattr(o, "loan_id", None), .45,
                                  ["CICILAN"] if o.kind in ("LOAN", "PAYLATER") else ["TAGIHAN"], due))

def settle_due(c, due, t, tid, channel):
    due.done = True
    first = True
    for inst in due.insts:
        inst.paid_at, inst.paid_amount, inst.txid, inst.channel = t, inst.amt, tid, channel
        inst.days_late = max(0, (t.date() - inst.due).days)
        inst.late_fee = due.late_fee if first else 0
        first = False
        inst.status = "PAID"
        loan = inst.loan
        loan.paid += 1
        if c.pl and loan.product == "PAYLATER": c.pl.out -= inst.pp
        if loan.paid == loan.tenor:
            loan.status, loan.closed = "PAID_OFF", t

def mark_default(c, due, t):
    due.done = True
    for inst in due.insts:
        inst.loan.status = "DEFAULT"
        inst.loan.closed = t
        if c.pl and inst.loan.product == "PAYLATER": c.pl.out -= inst.loan.principal * 0   # tetap dianggap terpakai

def h_due(c, t, payload, mode):
    due, x, attempt = (payload[0], None, payload[1]) if mode == "MANUAL" else (payload[0], payload[1], payload[2])
    if due.done: return
    o = due.obl
    sp = x
    late_days = (t.date() - due.date).days
    prod_fee = 25000 if o.kind == "LOAN" else 15000 if o.kind == "PAYLATER" else 0
    due.late_fee = prod_fee if (prod_fee and late_days > 3) else 0
    kw, fee = {}, DEFAULT_FEE[o.ttype] + due.late_fee
    if o.kind in ("LOAN", "PAYLATER"):
        loan_id = o.loan.id if o.kind == "LOAN" else None
        kw = dict(loan=loan_id, desc=("BAYAR CICILAN KTA #%d" % loan_id) if loan_id else "TAGIHAN PAYLATER")
        fee = due.late_fee
    elif o.kind == "BILL":
        kw = dict(biller=o.biller, desc="BAYAR " + o.label)
    else:
        b = o.bene
        kw = dict(cp_bank=b.bank, cp_acct=b.acct, cp_name=b.name, desc="TRF RUTIN %s" % o.label)
    kw["user_initiated"] = (mode == "MANUAL")
    feat = "CICILAN" if o.kind in ("LOAN", "PAYLATER") else ("TRANSFER" if o.ttype == "TRANSFER_BANK" else "TAGIHAN")
    if mode == "SCHED":
        if sp.status != "ACTIVE":
            push(c, t + td(hours=rng.randint(5, 30)), "DUE_MANUAL", (due, 0)); return
        st, tid, reason = _ret(post(c, c.main, o.ttype, "D", due.amount, t, "SYSTEM", fee=fee, sched=sp.id, **kw))
        sp.runs += 1; sp.last = t
        if st == "SUCCESS":
            sp.ok += 1
            settle_due(c, due, t, tid, "SCHEDULER")
        else:
            sp.fail += 1
            if reason != "SALDO TIDAK CUKUP" and attempt < 3:
                push(c, t + td(hours=2), "DUE_SCHED", (due, sp, attempt + 1))
            elif attempt < 2:
                push(c, DT.combine(t.date() + td(days=1), dt.time(6, rng.randrange(0, 25))), "DUE_SCHED", (due, sp, attempt + 1))
            else:
                push(c, sample_time(t.date() + td(days=rng.randint(1, 5)), PROF[c.prof]), "DUE_MANUAL", (due, 0))
                if rng.random() < .18:
                    cancel_sched(c, sp, t + td(days=rng.randint(1, 6)), "DIBATALKAN NASABAH SETELAH GAGAL BAYAR")
        return
    # manual
    if app_ok(c, t):
        st, tid, reason = mobile_post(c, c.main, o.ttype, "D", due.amount, t, feat=feat, fee=fee, **kw)
        ch = "MOBILE"
    else:
        st, tid, reason = _ret(post(c, c.main, o.ttype, "D", due.amount, t, "ATM", fee=fee, **kw))
        ch = "ATM"
    if st == "SUCCESS":
        settle_due(c, due, t, tid, ch)
    else:
        if reason == "SALDO TIDAK CUKUP": nxt = t.date() + td(days=wpick([1, 2, 3], [.5, .3, .2]))
        else: nxt = None
        if nxt is not None and (nxt - due.date).days > 90:
            mark_default(c, due, t)
        elif nxt is None:
            push(c, t + td(hours=rng.randint(1, 5)), "DUE_MANUAL", (due, attempt + 1))
        else:
            push(c, sample_time(nxt, PROF[c.prof]), "DUE_MANUAL", (due, attempt + 1))

def cancel_sched(c, sp, t, reason):
    if sp.status != "ACTIVE": return
    push(c, t, "CANCEL_SCHED", (sp, reason))

def h_cancel_sched(c, t, p):
    sp, reason = p
    if sp.status != "ACTIVE": return
    sp.status, sp.cancelled, sp.reason = "CANCELLED", t, reason
    if app_ok(c, t) and "SETELAH" not in reason:
        s = mobile_session(c, t); s.fresh = False
        ev(c, s, "JADWAL_BAYAR", "OPEN", t); ev(c, s, "JADWAL_BAYAR", "INTERACT", t + td(seconds=12), detail="BATALKAN_JADWAL")
        ev(c, s, "JADWAL_BAYAR", "COMPLETE", t + td(seconds=25), detail="JADWAL_DIBATALKAN")

def h_create_sched(c, t, o):
    if o.sched is not None or not app_ok(c, t) or t < SCHED_LAUNCH: return
    if o.kind == "LOAN" and o.loan.status != "ACTIVE": return
    s = mobile_session(c, t); s.fresh = False
    ev(c, s, "JADWAL_BAYAR", "OPEN", t)
    ev(c, s, "JADWAL_BAYAR", "INTERACT", t + td(seconds=rng.randint(10, 40)), detail="BUAT_JADWAL")
    ptype = {"LOAN": "LOAN_INSTALLMENT", "PAYLATER": "LOAN_INSTALLMENT", "BILL": "BILL"}.get(o.kind, "TRANSFER")
    sp = NS(id=nid("sched"), obl=o, created=t, status="ACTIVE", cancelled=None, reason=None, runs=0, ok=0, fail=0, last=None,
            notify=wpick([0, 1, 3], [.35, .25, .4]), device=s.device_id, ptype=ptype)
    c.sps.append(sp); o.sched = sp
    ev(c, s, "JADWAL_BAYAR", "COMPLETE", t + td(seconds=rng.randint(45, 90)), detail="JADWAL_DIBUAT", loan=o.loan.id if o.kind == "LOAN" else None)
    if o.sched_cancel and o.sched_cancel > t:
        push(c, o.sched_cancel, "CANCEL_SCHED", (sp, "DIBATALKAN NASABAH" if not c.app_churn else "NASABAH BERHENTI MEMAKAI APLIKASI"))

# ---------------- paylater & KTA ----------------
def open_loan(c, product, principal, tenor, rate, t, obl, app_id, due_day, merch=None, mcat=None, rate_override=None):
    pm = max(rnd(pmt(principal, tenor, rate) + 49, 100), math.ceil(principal / tenor)) if tenor > 1 else principal
    if tenor > 1 and rate == 0: pm = math.ceil(principal / tenor / 100) * 100
    loan = NS(id=nid("loan"), product=product, principal=principal, tenor=tenor, rate=rate, inst=pm, due_day=due_day, t=t,
              app_id=app_id, merch=merch, mcat=mcat, status="ACTIVE", closed=None, paid=0, disb_tx=None, insts=[])
    first = t.date() + td(days=25)
    due0 = DATE(first.year, first.month, due_day)
    if due0 < first: due0 = add_months(due0, 1)
    if due0.month == t.month and due0.year == t.year: due0 = add_months(due0, 1)
    bal, r = principal, rate / 1200.0
    for k in range(1, tenor + 1):
        ip = int(round(bal * r))
        if k == tenor: pp = bal; amt = bal + ip
        else: pp = min(bal, pm - ip) if pm - ip > 0 else min(bal, pm); amt = pm
        pp = max(1, int(pp)); amt = int(max(amt, pp + ip))
        bal -= pp
        inst = NS(no=k, due=add_months(due0, k - 1), amt=amt, pp=pp, ip=ip, loan=loan, obl=obl, status="UPCOMING", paid_at=None,
                  paid_amount=None, days_late=None, late_fee=0, txid=None, channel=None)
        loan.insts.append(inst)
        c.inst_by_month[(inst.due.year, inst.due.month)].append(inst)
    loan.first_due, loan.maturity = loan.insts[0].due, loan.insts[-1].due
    c.loans.append(loan)
    c.loans_any = True
    return loan

def h_pl_activate(c, t, p):
    if not app_ok(c, t) or c.pl is not None: return
    s = mobile_session(c, t); s.fresh = False
    ev(c, s, "PAYLATER", "OPEN", t)
    ev(c, s, "PAYLATER", "INTERACT", t + td(seconds=rng.randint(15, 60)), detail="PENGAJUAN")
    approved = rng.random() < .88 and c.income >= 2_000_000
    lim = int(clamp(rnd(c.income * rng.uniform(.5, 1.3), 500000), 1_000_000, 25_000_000))
    req = int(clamp(rnd(c.income * 1.5, 500000), 1_000_000, 25_000_000))
    aid = nid("app")
    dt_ = t + td(minutes=rng.randint(1, 20))
    W("loan_applications", aid, c.id, "PAYLATER", t + td(seconds=70), req, 12, "APPROVED" if approved else "REJECTED", dt_,
      lim if approved else None, None if approved else rng.choice(["SKOR KREDIT RENDAH", "PENGHASILAN TIDAK MEMENUHI"]), s.device_id)
    if approved:
        ev(c, s, "PAYLATER", "COMPLETE", t + td(seconds=90), detail="PAYLATER_AKTIF")
        c.pl = NS(limit=lim, app_id=aid, out=0, since=dt_)
        c.pl_obl = make_obl(c, "PAYLATER", "Tagihan Paylater", "LOAN_INSTALLMENT", dt_.date(), due_day=c.pl_due_day, mode="FULL_DUE")
    else:
        ev(c, s, "PAYLATER", "FAILED", t + td(seconds=90), detail="PENGAJUAN_DITOLAK")
        c.pl_rate = 0.0

def h_pl_buy(c, t, p):
    if not c.pl or not app_ok(c, t): return
    s = mobile_session(c, t); s.fresh = False
    ev(c, s, "PAYLATER", "OPEN", t)
    m = rng.choice(PL_MERCH)
    avail = c.pl.limit - c.pl.out
    amt = int(clamp(rnd(lognorm(clamp(c.pl.limit * .07, 150000, 1_200_000), .8), 10000), 50000, 25_000_000))
    if amt > avail or amt < 50000:
        ev(c, s, "PAYLATER", "INTERACT", t + td(seconds=20), detail="LIMIT_TIDAK_CUKUP")
        return
    tenor = wpick([1, 3, 6, 12], [.35, .3, .25, .1])
    rate = 0 if tenor == 1 else round(rng.uniform(18, 30), 2)
    ev(c, s, "PAYLATER", "INTERACT", t + td(seconds=25), detail="PILIH_TENOR")
    loan = open_loan(c, "PAYLATER", amt, tenor, rate, t, c.pl_obl, c.pl.app_id, c.pl_due_day, merch=m[0], mcat=m[1])
    c.pl.out += amt
    ev(c, s, "PAYLATER", "COMPLETE", t + td(seconds=50), detail="BELI_PAYLATER", loan=loan.id)
    s.last_t = max(s.last_t, t + td(seconds=50))

def h_browse_kta(c, t, p):
    if not app_ok(c, t): return
    s = mobile_session(c, t, browse=True); s.fresh = False
    ev(c, s, "KTA", "OPEN", t + td(seconds=2))
    ev(c, s, "KTA", "INTERACT", t + td(seconds=rng.randint(20, 90)), detail="SIMULASI")
    s.last_t = t + td(seconds=100)

def h_kta_apply(c, t, p):
    if not app_ok(c, t): return
    s = mobile_session(c, t); s.fresh = False
    ev(c, s, "KTA", "OPEN", t)
    ev(c, s, "KTA", "INTERACT", t + td(seconds=30), detail="PENGAJUAN")
    inc = c.income
    tenor = rng.choice([12, 24, 36, 48])
    req = int(clamp(rnd(inc * rng.uniform(2, 8), 1_000_000), 5_000_000, 50_000_000))
    rate = round(rng.uniform(11.5, 18), 2)
    pm = pmt(req, tenor, rate)
    existing = sum(l.inst for l in c.loans if l.status == "ACTIVE")
    dsr = (pm + existing) / max(inc, 1)
    ok = (rng.random() < (.78 if inc >= 5e6 else .35)) and dsr <= .5
    aid = nid("app")
    dec = t + td(minutes=rng.randint(2, 45))
    reason = None if ok else ("RASIO CICILAN TINGGI" if dsr > .5 else rng.choice(["PENGHASILAN TIDAK MEMENUHI", "SKOR KREDIT RENDAH"]))
    appr = int(rnd(req * rng.uniform(.7, 1.0), 1_000_000)) if ok else None
    if ok and appr < 5_000_000: appr = 5_000_000
    W("loan_applications", aid, c.id, "KTA", t + td(seconds=60), req, tenor, "APPROVED" if ok else "REJECTED", dec, appr, reason, s.device_id)
    if not ok:
        ev(c, s, "KTA", "FAILED", t + td(seconds=60), detail="PENGAJUAN_DITOLAK")
        if c.kta_retry < 1 and rng.random() < .35:
            c.kta_retry += 1
            push(c, t + td(days=rng.randint(40, 150)), "KTA_APPLY", None)
        return
    ev(c, s, "KTA", "COMPLETE", t + td(seconds=60), detail="PENGAJUAN_DISETUJUI")
    pm = pmt(appr, tenor, rate)
    if c.salaried and rng.random() < .8:
        dd = c.payday + rng.randint(1, 4) if c.payday else rng.randint(1, 3)
        due_day = ((dd - 1) % 28) + 1 if (c.payday and c.payday <= 24) else (dd % 28 or 1) if c.payday else dd
        if c.payday and c.payday > 24: due_day = rng.randint(1, 3)
    else:
        due_day = rng.randint(1, 28)
    due_day = int(clamp(due_day, 1, 28))
    dt0 = dec + td(minutes=rng.randint(1, 20))
    o = NS(id=nid("obl"), kind="LOAN", label="Cicilan KTA", ttype="LOAN_INSTALLMENT", biller=None, bene=None, amount=0, cv=0.0,
           due_day=due_day, start=dt0.date(), end=None, sched=None, sched_from=None, sched_cancel=None, mode="FIXED",
           off_mean=c.off_mean, off_sd=c.off_sd, insts=[], loan=None)
    c.obls.append(o)
    loan = open_loan(c, "KTA", appr, tenor, rate, dt0, o, aid, due_day)
    o.loan = loan; o.loan_id = loan.id
    push(c, dt0, "DISBURSE", (loan, appr))
    plan_sched_for(c, o, base_t=dt0)
    for k in range(rng.randint(1, 3)):
        push(c, sample_time(dt0.date() + td(days=rng.randint(1, 7)), PROF[c.prof]), "BIGSPEND", int(appr * rng.uniform(.15, .45)))

def h_disburse(c, t, p):
    loan, appr = p
    st, tid, _ = post(c, c.main, "LOAN_DISBURSEMENT", "C", appr, t, "SYSTEM", desc="PENCAIRAN KTA #%d" % loan.id, loan=loan.id, user_initiated=False)
    loan.disb_tx = tid

def h_bigspend(c, t, amt):
    amt = int(max(100000, rnd(amt, 100000)))
    b = c.benes[wpick(range(len(c.benes)), zipf_w(len(c.benes)))]
    if b.bank == "999": ty, fee = "TRANSFER_INTERNAL", 0
    else: ty, fee = "TRANSFER_BANK", 2500
    mobile_post(c, c.main, ty, "D", amt, t, fee=fee, desc="TRF ke %s" % b.name, cp_bank=b.bank, cp_acct=b.acct, cp_name=b.name, attempt_p=.5)

HANDLERS = {
    "CREDIT": h_credit, "OPEN_DEPOSIT": h_open_deposit, "FEE": h_fee, "INTEREST": h_interest, "SAVE": h_save, "SPEND": h_spend,
    "SUB": h_sub, "CARD_TOGGLE": h_card_toggle, "BROWSE": lambda c, t, p: do_browse(c, t, p), "BROWSE_KTA": h_browse_kta,
    "NOTIF": do_notif, "DUE_MANUAL": lambda c, t, p: h_due(c, t, p, "MANUAL"), "DUE_SCHED": lambda c, t, p: h_due(c, t, p, "SCHED"),
    "CREATE_SCHED": h_create_sched, "CANCEL_SCHED": h_cancel_sched, "PL_ACTIVATE": h_pl_activate, "PL_BUY": h_pl_buy,
    "KTA_APPLY": h_kta_apply, "BIGSPEND": h_bigspend, "DISBURSE": h_disburse,
}

# --------------------------------------------------------------------------------------
# 8. LOOP UTAMA PER NASABAH
# --------------------------------------------------------------------------------------
def month_start(c, d):
    y, m = d.year, d.month
    if d.day == 1:
        push(c, DT(y, m, 1, 2, rng.randrange(5, 30)), "FEE", c.main)
        if c.sav: push(c, DT(y, m, 1, 2, rng.randrange(30, 55)), "FEE", c.sav)
    ld = last_day(y, m)
    # bunga tabungan: sekali sebulan (akhir bulan), hanya pada rekening tabungan kedua (bukan rekening utama; giro tidak berbunga tabungan)
    if c.sav and c.sav.product != "GIRO": push(c, DT(ld.year, ld.month, ld.day, 23, rng.randrange(40, 58)), "INTEREST", c.sav)
    if c.push_enabled:
        for _ in range(poisson(2.8)):
            day = DATE(y, m, rng.randint(max(d.day, 1), 28)) if d.day <= 28 else d
            push(c, DT(day.year, day.month, day.day, rng.randint(10, 19), rng.randrange(60)), "NOTIF", ("PROMO", None, .08, ["PROMO"]))
    for s in c.subs:
        sd = DATE(y, m, s.day)
        if sd >= d: push(c, DT(sd.year, sd.month, sd.day, s.hour, rng.randrange(60)), "SUB", s)
    plan_dues_for_month(c, d)

def run_customer(c):
    c.ew = wpick(EW_CODES, EW_W)
    c.carrier = wpick(CARRIERS, CARRIER_W)
    estimate_and_obligations(c)
    c.open_amt = int(rnd(c.income * (rng.uniform(.3, 5) if c.old else rng.uniform(.2, 1.2)), 1000)) or 100000
    push(c, (START + td(minutes=5)) if c.old else c.created, "OPEN_DEPOSIT", None)
    push(c, c.act_t + td(seconds=30), "BROWSE", ["CEK_SALDO"])
    d = c.first_day
    while d <= END_D:
        c.cur_day = d
        c.heap = c.plan.pop(d, [])
        heapq.heapify(c.heap)
        if d.day == 1 or d == c.first_day: month_start(c, d)
        gen_income(c, d)
        mobact, cardact = mob_mult(c, d), card_mult(c, d)
        gen_spend(c, d, mobact, cardact)
        gen_browse(c, d, mobact)
        while c.heap:
            t, _, kind, payload = heapq.heappop(c.heap)
            c.now = t
            HANDLERS[kind](c, t, payload)
        d += td(days=1)
    finalize(c)

def finalize(c):
    cid = c.id
    for a in [c.main] + ([c.sav] if c.sav else []):
        st = "ACTIVE"
        if a.last_debit is None or (END - a.last_debit).days > 180: st = "DORMANT"
        if a is c.main and c.full_end: st = "DORMANT"
        W("accounts", a.id, a.number, cid, c.branch, a.product, "IDR", a.bal, st, a.opened, None)
    k = c.card
    pre = {"VISA": "4%05d", "MASTERCARD": "5%05d", "GPN": "9360%02d"}[k.net] % rng.randrange(10 ** (5 if k.net != "GPN" else 2))
    W("cards", k.id, c.main.id, "DEBIT", k.net, "%s******%s" % (pre[:6], k.last4), c.full_name.upper(), k.issue, k.issue + td(days=1826),
      None, "ACTIVE", k.contactless, c.online_at, c.intl_at)
    h = lambda: "$2b$12$" + ("%053x" % rng.getrandbits(212))[:53]
    W("mobile_users", c.user_id, cid, c.full_name.split()[0].lower() + str(cid), h(), h(), "INACTIVE" if c.app_churn else "ACTIVE",
      c.reg_t, c.last_login, c.push_enabled, bool(c.bio_used or (c.bio_from is not None and c.bio_from < END and c.tech > .5)))
    for i, dv in enumerate(c.devices):
        W("devices", dv.id, c.user_id, dv.name, dv.os, dv.osv, dv.app, i == len(c.devices) - 1, dv.reg, dv.last)
    for sp in c.sps:
        o = sp.obl
        loan_id = o.loan.id if o.kind == "LOAN" else None
        if o.kind == "LOAN": mode, fixed = "FIXED", o.loan.inst
        elif o.kind == "PAYLATER": mode, fixed = "FULL_DUE", None
        else: mode, fixed = o.mode, (o.amount if o.mode == "FIXED" else None)
        W("scheduled_payments", sp.id, cid, c.main.id, sp.ptype, loan_id, o.biller, o.bene.id if o.bene else None, o.label[:60], mode,
          fixed, "MONTHLY", o.due_day, sp.notify, sp.created, sp.device, sp.status, sp.cancelled, sp.reason, sp.runs, sp.ok, sp.fail, sp.last)
    for ln in c.loans:
        W("loans", ln.id, cid, c.main.id, ln.product, ln.app_id, ln.principal, ln.tenor, ln.rate, ln.inst, ln.due_day, ln.t,
          ln.first_due, ln.maturity, ln.status, ln.closed, ln.merch, ln.mcat, ln.disb_tx)
        for inst in ln.insts:
            if inst.status == "PAID": stt = "PAID"
            elif ln.status == "DEFAULT" and ln.closed is not None and inst.due > ln.closed.date(): stt = "CANCELLED"
            elif inst.due <= END_D: stt = "UNPAID"
            else: stt = "UPCOMING"
            W("loan_installments", nid("inst"), ln.id, inst.no, inst.due, inst.amt, inst.pp, inst.ip, inst.paid_at, inst.paid_amount,
              inst.days_late, inst.late_fee, inst.txid, inst.channel, stt)

# --------------------------------------------------------------------------------------
# 9. REFERENSI + EKSEKUSI
# --------------------------------------------------------------------------------------
log("Membuat referensi ...")
for i, cty in enumerate(sorted(CITIES, key=lambda x: x[0]), 1):
    BRANCH_ID[cty[0]] = i
    W("branches", i, "KC%03d" % i, "KC " + cty[0], cty[0], cty[1])
for b in BANKS: W("banks", *b)
for i, b in enumerate(BILLERS, 1): W("billers", i, *b)
for t_ in TTYPES: W("transaction_types", *t_)
for f_ in FEATURES: W("app_features", *f_)
for p_ in LOANPRODS: W("loan_products", *p_)
assert N_CUST <= 9999, "maksimum 9999 nasabah per eksekusi"

# nomor rekening utama harus tersedia dulu (untuk transfer sesama bank antar nasabah)
PRE = {}
for i in range(1, N_CUST + 1):
    g = "L" if rng.random() < .5 else "P"
    f_ = rng.choice(MALE if g == "L" else FEMALE)
    s_ = (" " + rng.choice(MALE if g == "L" else FEMALE)) if rng.random() < .35 else ""
    while s_.strip() == f_: s_ = " " + rng.choice(MALE if g == "L" else FEMALE)   # nama depan dan tengah tidak boleh sama
    l_ = rng.choice(LAST)
    PRE[i] = (g, f_, s_, l_)
    OWN_ACCT[i] = "88%08d" % i
    OWN_NAME[i] = (f_ + s_ + " " + l_).upper()

log("Menghasilkan %d nasabah (seed=%d, intensity=%.2f) ..." % (N_CUST, ARGS.seed, INTENS))
import time as _time
_t0 = _time.time()
for i in range(1, N_CUST + 1):
    c = make_customer(i)
    run_customer(c)
    if i % 50 == 0:
        log("  %d/%d nasabah, %s transaksi, %.0f dtk" % (i, N_CUST, format(ROWCOUNT["transactions"], ","), _time.time() - _t0))

# --------------------------------------------------------------------------------------
# 10. KAMUS DATA (COMMENT ON) + PENULISAN FILE SQL
# --------------------------------------------------------------------------------------
OLD_COMMENTS = r"""COMMENT ON SCHEMA mbanking IS 'Data dummy mobile banking (nasabah, rekening, aplikasi, transaksi) periode 2024-01-01 s/d 2026-10-09. Semua data fiktif.';

-- branches
COMMENT ON TABLE  branches IS 'Kantor cabang, satu per kota.';
COMMENT ON COLUMN branches.branch_id   IS 'PK, auto-increment.';
COMMENT ON COLUMN branches.branch_code IS 'Kode cabang unik, mis. KC001.';
COMMENT ON COLUMN branches.branch_name IS 'Nama cabang, mis. "KC Bandung".';
COMMENT ON COLUMN branches.city        IS 'Kota lokasi cabang.';
COMMENT ON COLUMN branches.province    IS 'Provinsi lokasi cabang.';

-- banks
COMMENT ON TABLE  banks IS 'Daftar bank untuk transfer. Kode 999 adalah bank sendiri (fiktif).';
COMMENT ON COLUMN banks.bank_code IS 'PK, kode bank 3 digit (mis. 014 = BCA, 999 = bank sendiri).';
COMMENT ON COLUMN banks.bank_name IS 'Nama bank.';

-- customers
COMMENT ON TABLE  customers IS 'Data nasabah (profil dan KYC). Sekitar 35% adalah nasabah lama (terdaftar 2020-2023), sisanya nasabah baru 2024-2026.';
COMMENT ON COLUMN customers.customer_id    IS 'PK.';
COMMENT ON COLUMN customers.cif_number     IS 'Nomor induk nasabah (CIF + 9 digit), unik.';
COMMENT ON COLUMN customers.nik            IS 'NIK 16 digit, dibuat acak (bukan NIK asli), unik.';
COMMENT ON COLUMN customers.full_name      IS 'Nama lengkap nasabah.';
COMMENT ON COLUMN customers.gender         IS 'Jenis kelamin: L = laki-laki, P = perempuan.';
COMMENT ON COLUMN customers.birth_date     IS 'Tanggal lahir (semua nasabah dewasa).';
COMMENT ON COLUMN customers.phone_number   IS 'Nomor ponsel, unik.';
COMMENT ON COLUMN customers.email          IS 'Alamat email, unik.';
COMMENT ON COLUMN customers.address        IS 'Alamat lengkap (jalan, RT/RW, kota).';
COMMENT ON COLUMN customers.city           IS 'Kota domisili.';
COMMENT ON COLUMN customers.province       IS 'Provinsi domisili.';
COMMENT ON COLUMN customers.occupation     IS 'Pekerjaan: Karyawan Swasta, PNS/ASN, Guru/Dosen, Pegawai BUMN, Profesional, Wiraswasta, Freelancer, Mahasiswa, Ibu Rumah Tangga.';
COMMENT ON COLUMN customers.monthly_income IS 'Penghasilan bulanan (rupiah). Dasar pembentuk pola transaksi nasabah.';
COMMENT ON COLUMN customers.kyc_level      IS 'Tingkat verifikasi: BASIC (e-KYC) atau FULL (verifikasi lengkap).';
COMMENT ON COLUMN customers.branch_id      IS 'FK ke branches: cabang asal nasabah.';
COMMENT ON COLUMN customers.created_at     IS 'Waktu menjadi nasabah.';

-- accounts
COMMENT ON TABLE  accounts IS 'Rekening nasabah. Satu nasabah memiliki satu rekening utama dan sebagian memiliki rekening kedua.';
COMMENT ON COLUMN accounts.account_id     IS 'PK. Rekening utama selalu memiliki account_id terkecil per nasabah.';
COMMENT ON COLUMN accounts.account_number IS 'Nomor rekening 10 digit berawalan 88, unik.';
COMMENT ON COLUMN accounts.customer_id    IS 'FK ke customers: pemilik rekening.';
COMMENT ON COLUMN accounts.branch_id      IS 'FK ke branches: cabang pembuka rekening.';
COMMENT ON COLUMN accounts.product_type   IS 'Produk: TABUNGAN, TABUNGAN_PLUS, GIRO.';
COMMENT ON COLUMN accounts.currency       IS 'Mata uang rekening, selalu IDR.';
COMMENT ON COLUMN accounts.balance        IS 'Saldo terkini (rupiah). Sama dengan balance_after pada transaksi terakhir. Tidak boleh negatif.';
COMMENT ON COLUMN accounts.status         IS 'ACTIVE, DORMANT (tidak ada debit oleh nasabah dalam 180 hari terakhir), BLOCKED, CLOSED. BLOCKED dan CLOSED belum dipakai pada data dummy.';
COMMENT ON COLUMN accounts.opened_at      IS 'Waktu rekening dibuka.';
COMMENT ON COLUMN accounts.closed_at      IS 'Waktu rekening ditutup (kosong pada data dummy).';

-- cards
COMMENT ON TABLE  cards IS 'Kartu debit dan kartu kredit yang terhubung ke rekening.';
COMMENT ON COLUMN cards.card_id          IS 'PK.';
COMMENT ON COLUMN cards.account_id       IS 'FK ke accounts: rekening yang terhubung ke kartu.';
COMMENT ON COLUMN cards.card_type        IS 'DEBIT atau CREDIT.';
COMMENT ON COLUMN cards.network          IS 'Jaringan kartu: VISA, MASTERCARD, GPN.';
COMMENT ON COLUMN cards.masked_number    IS 'Nomor kartu tersamar: 6 digit awal + ****** + 4 digit akhir. Nomor penuh tidak disimpan.';
COMMENT ON COLUMN cards.card_holder_name IS 'Nama pada kartu (huruf kapital).';
COMMENT ON COLUMN cards.issued_date      IS 'Tanggal kartu diterbitkan.';
COMMENT ON COLUMN cards.expiry_date      IS 'Tanggal kedaluwarsa (5 tahun setelah terbit).';
COMMENT ON COLUMN cards.credit_limit     IS 'Limit kartu kredit (rupiah). Kosong untuk kartu debit.';
COMMENT ON COLUMN cards.status           IS 'ACTIVE, BLOCKED, atau EXPIRED.';

-- mobile_users
COMMENT ON TABLE  mobile_users IS 'Akun aplikasi mobile banking, satu per nasabah.';
COMMENT ON COLUMN mobile_users.user_id       IS 'PK.';
COMMENT ON COLUMN mobile_users.customer_id   IS 'FK ke customers, unik (satu nasabah satu akun).';
COMMENT ON COLUMN mobile_users.username      IS 'Nama pengguna unik.';
COMMENT ON COLUMN mobile_users.password_hash IS 'Hash kata sandi. Isi dummy berformat bcrypt, bukan hash sungguhan.';
COMMENT ON COLUMN mobile_users.pin_hash      IS 'Hash PIN transaksi. Isi dummy berformat bcrypt, bukan hash sungguhan.';
COMMENT ON COLUMN mobile_users.status        IS 'ACTIVE, INACTIVE (berhenti memakai aplikasi), LOCKED. LOCKED belum dipakai pada data dummy.';
COMMENT ON COLUMN mobile_users.registered_at IS 'Waktu registrasi aplikasi.';
COMMENT ON COLUMN mobile_users.last_login_at IS 'Waktu login sukses terakhir.';

-- devices
COMMENT ON TABLE  devices IS 'Perangkat pengguna yang terdaftar. Pengguna yang berganti HP memiliki dua baris.';
COMMENT ON COLUMN devices.device_id     IS 'PK, UUID.';
COMMENT ON COLUMN devices.user_id       IS 'FK ke mobile_users.';
COMMENT ON COLUMN devices.device_name   IS 'Model perangkat, mis. Samsung Galaxy A54, iPhone 14.';
COMMENT ON COLUMN devices.os            IS 'Sistem operasi: ANDROID atau IOS.';
COMMENT ON COLUMN devices.os_version    IS 'Versi sistem operasi.';
COMMENT ON COLUMN devices.app_version   IS 'Versi aplikasi mobile banking yang terpasang.';
COMMENT ON COLUMN devices.is_active     IS 'false bila pengguna sudah berganti ke perangkat yang lebih baru.';
COMMENT ON COLUMN devices.registered_at IS 'Waktu perangkat didaftarkan.';
COMMENT ON COLUMN devices.last_used_at  IS 'Waktu login sukses terakhir dari perangkat ini.';

-- login_logs
COMMENT ON TABLE  login_logs IS 'Riwayat percobaan login ke aplikasi (sukses dan gagal).';
COMMENT ON COLUMN login_logs.login_id       IS 'PK, berurutan menurut waktu.';
COMMENT ON COLUMN login_logs.user_id        IS 'FK ke mobile_users.';
COMMENT ON COLUMN login_logs.device_id      IS 'FK ke devices: perangkat yang dipakai login.';
COMMENT ON COLUMN login_logs.login_at       IS 'Waktu percobaan login.';
COMMENT ON COLUMN login_logs.ip_address     IS 'Alamat IP (dibuat acak).';
COMMENT ON COLUMN login_logs.login_method   IS 'BIOMETRIC, PASSWORD, atau PIN.';
COMMENT ON COLUMN login_logs.status         IS 'SUCCESS atau FAILED (sekitar 4% gagal).';
COMMENT ON COLUMN login_logs.failure_reason IS 'PASSWORD_SALAH, PIN_SALAH, BIOMETRIK_GAGAL. Kosong bila login sukses.';

-- beneficiaries
COMMENT ON TABLE  beneficiaries IS 'Daftar penerima transfer yang disimpan nasabah.';
COMMENT ON COLUMN beneficiaries.beneficiary_id IS 'PK.';
COMMENT ON COLUMN beneficiaries.customer_id    IS 'FK ke customers: pemilik daftar penerima.';
COMMENT ON COLUMN beneficiaries.bank_code      IS 'FK ke banks: bank penerima.';
COMMENT ON COLUMN beneficiaries.account_number IS 'Nomor rekening penerima (fiktif).';
COMMENT ON COLUMN beneficiaries.account_name   IS 'Nama pemilik rekening penerima (fiktif).';
COMMENT ON COLUMN beneficiaries.nickname       IS 'Nama panggilan, mis. Ibu, Kos, Arisan. Boleh kosong.';
COMMENT ON COLUMN beneficiaries.created_at     IS 'Waktu penerima disimpan.';

-- billers
COMMENT ON TABLE  billers IS 'Penyedia tagihan, pulsa, dan e-wallet.';
COMMENT ON COLUMN billers.biller_id   IS 'PK.';
COMMENT ON COLUMN billers.biller_code IS 'Kode unik, mis. PLNPRE, GOPAY.';
COMMENT ON COLUMN billers.biller_name IS 'Nama penyedia.';
COMMENT ON COLUMN billers.category    IS 'LISTRIK, AIR, BPJS, INTERNET, TV, ASURANSI, MULTIFINANCE, PAJAK, PENDIDIKAN, PULSA, EWALLET.';

-- transaction_types
COMMENT ON TABLE  transaction_types IS 'Kamus jenis transaksi. Kredit: SALARY_CREDIT, TRANSFER_IN, CASH_DEPOSIT, INTEREST. Debit: TRANSFER_INTERNAL, TRANSFER_BANK, QRIS_PAYMENT, EWALLET_TOPUP, BILL_PAYMENT, PULSA_DATA, ATM_WITHDRAWAL, ADMIN_FEE.';
COMMENT ON COLUMN transaction_types.type_code   IS 'PK, mis. QRIS_PAYMENT.';
COMMENT ON COLUMN transaction_types.type_name   IS 'Nama jenis transaksi yang mudah dibaca.';
COMMENT ON COLUMN transaction_types.direction   IS 'D = debit (uang keluar), C = kredit (uang masuk).';
COMMENT ON COLUMN transaction_types.default_fee IS 'Biaya standar per transaksi (rupiah).';

-- transactions
COMMENT ON TABLE  transactions IS 'Buku mutasi: satu baris per transaksi per rekening, berurutan menurut waktu. Transaksi sesama bank hanya mencatat sisi pengirim. Saldo awal nasabah lama dicatat sebagai CASH_DEPOSIT bertanggal 2024-01-01 ("SALDO AWAL"), setoran pembukaan rekening baru berdeskripsi "SETORAN AWAL"; keduanya sebaiknya dikecualikan dari analisis arus kas.';
COMMENT ON COLUMN transactions.transaction_id         IS 'PK, berurutan menurut waktu transaksi.';
COMMENT ON COLUMN transactions.reference_no           IS 'Nomor referensi unik: TRX + YYMMDD + nomor urut.';
COMMENT ON COLUMN transactions.account_id             IS 'FK ke accounts: rekening yang dimutasi.';
COMMENT ON COLUMN transactions.transaction_type       IS 'FK ke transaction_types.';
COMMENT ON COLUMN transactions.direction              IS 'D = uang keluar, C = uang masuk (sama dengan transaction_types.direction, diduplikasi agar agregasi tanpa join).';
COMMENT ON COLUMN transactions.amount                 IS 'Nominal transaksi (rupiah), selalu > 0.';
COMMENT ON COLUMN transactions.fee                    IS 'Biaya transaksi, terpisah dari amount. Total debit = amount + fee. Bernilai 0 untuk transaksi FAILED.';
COMMENT ON COLUMN transactions.balance_after          IS 'Saldo rekening setelah transaksi. Untuk transaksi FAILED sama dengan saldo sebelumnya.';
COMMENT ON COLUMN transactions.currency               IS 'Mata uang, selalu IDR.';
COMMENT ON COLUMN transactions.counterparty_bank_code IS 'FK ke banks: bank lawan transaksi (transfer keluar dan transfer masuk).';
COMMENT ON COLUMN transactions.counterparty_account   IS 'Rekening lawan, nomor e-wallet, nomor ponsel pulsa, atau ID pelanggan tagihan.';
COMMENT ON COLUMN transactions.counterparty_name      IS 'Nama penerima/pengirim, merchant QRIS, perusahaan pemberi gaji, atau nama penyedia.';
COMMENT ON COLUMN transactions.biller_id              IS 'FK ke billers. Terisi hanya untuk EWALLET_TOPUP, BILL_PAYMENT, PULSA_DATA.';
COMMENT ON COLUMN transactions.channel                IS 'MOBILE (inisiasi nasabah di aplikasi), ATM, BRANCH (loket), SYSTEM (otomatis: gaji, bunga, biaya admin, transfer masuk, saldo awal).';
COMMENT ON COLUMN transactions.device_id              IS 'FK ke devices: perangkat pelaku. Terisi hanya untuk channel MOBILE.';
COMMENT ON COLUMN transactions.description            IS 'Keterangan mutasi, mis. "QRIS Indomaret", "GAJI OKT 2025".';
COMMENT ON COLUMN transactions.status                 IS 'SUCCESS atau FAILED. PENDING belum dipakai pada data dummy.';
COMMENT ON COLUMN transactions.failure_reason         IS 'SALDO TIDAK CUKUP, TIMEOUT, GANGGUAN JARINGAN, LIMIT HARIAN TERLAMPAUI, TRANSAKSI DITOLAK BANK PENERIMA. Kosong bila SUCCESS.';
COMMENT ON COLUMN transactions.created_at             IS 'Waktu transaksi (timestamptz). Gunakan AT TIME ZONE ''Asia/Jakarta'' untuk WIB.';

"""

NEW_COMMENTS = r"""
-- ---- penyesuaian v2 (menimpa komentar lama bila perlu) ----
COMMENT ON SCHEMA mbanking IS 'Data dummy mobile banking v2 (nasabah, rekening, kartu, aplikasi, transaksi, paylater/KTA, cicilan, jadwal bayar, event aplikasi, notifikasi). Semua data fiktif.';
COMMENT ON TABLE  accounts IS 'Rekening nasabah. Satu nasabah punya satu rekening utama (account_id = customer_id) dan sebagian punya rekening tabungan kedua untuk menabung. Bunga tabungan (INTEREST) hanya dikreditkan ke rekening tabungan kedua itu, satu kali per bulan; rekening utama tidak berbunga.';
COMMENT ON COLUMN accounts.account_id IS 'PK. Rekening utama selalu memiliki account_id = customer_id; rekening kedua bernomor lebih besar.';
COMMENT ON COLUMN accounts.status     IS 'ACTIVE atau DORMANT (tidak ada debit oleh nasabah dalam 180 hari terakhir, atau nasabah sudah berhenti total). BLOCKED dan CLOSED belum dipakai.';
COMMENT ON TABLE  cards IS 'Kartu debit yang terhubung ke rekening utama (satu per nasabah). Kartu kredit belum disimulasikan.';
COMMENT ON COLUMN cards.network IS 'Jaringan kartu: VISA, MASTERCARD, GPN. Transaksi luar negeri hanya mungkin untuk VISA/MASTERCARD.';
COMMENT ON COLUMN cards.is_contactless    IS 'true bila kartu mendukung tap (contactless).';
COMMENT ON COLUMN cards.online_enabled_at IS 'Waktu nasabah mengaktifkan belanja online. NULL = tidak pernah diaktifkan (tidak ada transaksi CARD_ONLINE).';
COMMENT ON COLUMN cards.intl_enabled_at   IS 'Waktu nasabah mengaktifkan transaksi luar negeri. NULL = tidak pernah (tidak ada transaksi CARD_INTL).';
COMMENT ON COLUMN mobile_users.status IS 'ACTIVE, INACTIVE (berhenti memakai aplikasi, aktivitas menurun beberapa bulan sebelumnya), LOCKED (belum dipakai).';
COMMENT ON COLUMN mobile_users.push_enabled      IS 'true bila notifikasi push diizinkan. Nasabah dengan false tidak menerima baris di tabel notifications.';
COMMENT ON COLUMN mobile_users.biometric_enabled IS 'true bila nasabah pernah login memakai biometrik (sidik jari/wajah).';
COMMENT ON COLUMN login_logs.login_id IS 'PK. Urutan ID per nasabah (bukan urutan waktu global); gunakan login_at untuk urutan waktu.';
COMMENT ON COLUMN login_logs.status   IS 'SUCCESS atau FAILED (sekitar 3-5% gagal; naik tajam saat gangguan layanan).';
COMMENT ON TABLE  transaction_types IS 'Kamus jenis transaksi. Kredit: SALARY_CREDIT, TRANSFER_IN, CASH_DEPOSIT, INTEREST, LOAN_DISBURSEMENT, SAVINGS_IN. Debit: TRANSFER_INTERNAL, TRANSFER_BANK, QRIS_PAYMENT, EWALLET_TOPUP, BILL_PAYMENT, PULSA_DATA, ATM_WITHDRAWAL, ADMIN_FEE, CARD_POS, CARD_CONTACTLESS, CARD_ONLINE, CARD_INTL, LOAN_INSTALLMENT, SAVINGS_OUT.';
COMMENT ON TABLE  transactions IS 'Buku mutasi: satu baris per transaksi per rekening (termasuk yang GAGAL). Transaksi sesama bank hanya mencatat sisi pengirim, kecuali pindah dana antar rekening sendiri (SAVINGS_OUT + SAVINGS_IN). Saldo awal nasabah lama dicatat sebagai CASH_DEPOSIT bertanggal 2024-01-01 berdeskripsi SALDO AWAL (MIGRASI DATA 2024); nasabah baru: SETORAN AWAL PEMBUKAAN REKENING. Pinjaman paylater TIDAK menyentuh rekening saat dibeli, hanya cicilannya (LOAN_INSTALLMENT).';
COMMENT ON COLUMN transactions.transaction_id    IS 'PK. Urutan ID per nasabah (blok per nasabah), bukan urutan waktu global; gunakan created_at.';
COMMENT ON COLUMN transactions.reference_no      IS 'Nomor referensi unik: TRX + YYYYMMDD + nomor urut 9 digit.';
COMMENT ON COLUMN transactions.channel           IS 'MOBILE (inisiasi nasabah di aplikasi), ATM (tarik tunai, setor tunai, bayar lewat ATM bagi yang sudah berhenti memakai aplikasi), CARD (gesek/tap/online/luar negeri dengan kartu debit), BRANCH (belum dipakai), SYSTEM (otomatis: gaji, bunga, biaya admin, transfer masuk, pencairan KTA, jadwal bayar/scheduler).';
COMMENT ON COLUMN transactions.device_id         IS 'FK ke devices: perangkat pelaku. Terisi hanya untuk channel MOBILE.';
COMMENT ON COLUMN transactions.status            IS 'SUCCESS atau FAILED. Baris FAILED tidak mengubah saldo (balance_after = saldo sebelum percobaan, fee = 0). PENDING belum dipakai.';
COMMENT ON COLUMN transactions.failure_reason    IS 'SALDO TIDAK CUKUP, TIMEOUT, GANGGUAN JARINGAN, LIMIT HARIAN TERLAMPAUI, TRANSAKSI DITOLAK BANK PENERIMA. Kosong bila SUCCESS.';
COMMENT ON COLUMN transactions.card_id           IS 'FK ke cards untuk transaksi CARD_* (juga langganan kartu bulanan). NULL untuk channel lain.';
COMMENT ON COLUMN transactions.merchant_name     IS 'Nama merchant untuk QRIS dan transaksi kartu. NULL untuk lainnya. (counterparty_name berisi nama yang sama.)';
COMMENT ON COLUMN transactions.merchant_category IS 'Kategori merchant: FNB, GROCERY, FUEL, HEALTH, SHOPPING, ENTERTAINMENT, TRANSPORT, TRAVEL, EDUCATION, OTHER.';
COMMENT ON COLUMN transactions.loan_id           IS 'FK ke loans: pencairan KTA atau cicilan KTA. NULL untuk tagihan paylater gabungan (satu pembayaran bisa melunasi beberapa pinjaman; lihat loan_installments.payment_transaction_id).';
COMMENT ON COLUMN transactions.scheduled_payment_id IS 'FK ke scheduled_payments bila transaksi dijalankan otomatis oleh fitur Jadwal Bayar. NULL = dibayar manual.';
COMMENT ON COLUMN transactions.currency_original IS 'Mata uang asli transaksi luar negeri (USD, EUR, SGD, MYR, JPY). NULL untuk transaksi rupiah.';
COMMENT ON COLUMN transactions.amount_original   IS 'Nominal dalam mata uang asli (hanya CARD_INTL).';
COMMENT ON COLUMN transactions.fx_rate           IS 'Kurs rupiah per 1 unit mata uang asli (hanya CARD_INTL). amount = amount_original x fx_rate. fee berisi biaya konversi 1,25%.';

COMMENT ON TABLE  app_features IS 'Katalog fitur/layar aplikasi mobile banking beserta tanggal rilisnya (dasar analisis adopsi fitur).';
COMMENT ON COLUMN app_features.feature_code IS 'PK, mis. TRANSFER, QRIS, JADWAL_BAYAR, PAYLATER, KTA.';
COMMENT ON COLUMN app_features.feature_name IS 'Nama fitur yang mudah dibaca.';
COMMENT ON COLUMN app_features.category     IS 'INQUIRY (hanya melihat), TRANSACTION, CREDIT (produk kredit), SETTING, INFO.';
COMMENT ON COLUMN app_features.launched_at  IS 'Waktu fitur dirilis. Event fitur tidak ada sebelum tanggal ini (JADWAL_BAYAR 2024-04-01, PAYLATER dan CICILAN 2024-07-01, KTA 2025-01-15).';
COMMENT ON COLUMN app_features.description  IS 'Penjelasan singkat.';

COMMENT ON TABLE  loan_products IS 'Produk kredit di aplikasi: PAYLATER dan KTA.';
COMMENT ON COLUMN loan_products.product_code IS 'PK: PAYLATER atau KTA.';
COMMENT ON COLUMN loan_products.product_name IS 'Nama produk.';
COMMENT ON COLUMN loan_products.min_amount   IS 'Plafon minimum (rupiah).';
COMMENT ON COLUMN loan_products.max_amount   IS 'Plafon maksimum (rupiah).';
COMMENT ON COLUMN loan_products.min_tenor    IS 'Tenor minimum (bulan).';
COMMENT ON COLUMN loan_products.max_tenor    IS 'Tenor maksimum (bulan).';
COMMENT ON COLUMN loan_products.rate_pa_min  IS 'Bunga minimum per tahun (persen). Paylater tenor 1 bulan bebas bunga.';
COMMENT ON COLUMN loan_products.rate_pa_max  IS 'Bunga maksimum per tahun (persen).';
COMMENT ON COLUMN loan_products.late_fee     IS 'Denda keterlambatan tetap per pembayaran bila terlambat lebih dari 3 hari (rupiah).';
COMMENT ON COLUMN loan_products.launched_at  IS 'Waktu produk dirilis.';

COMMENT ON TABLE  loan_applications IS 'Pengajuan produk kredit lewat aplikasi: aktivasi limit PAYLATER dan pengajuan KTA, beserta keputusannya. Dasar funnel: lihat -> simulasi -> ajukan -> disetujui.';
COMMENT ON COLUMN loan_applications.application_id   IS 'PK.';
COMMENT ON COLUMN loan_applications.customer_id      IS 'FK ke customers.';
COMMENT ON COLUMN loan_applications.product_code     IS 'FK ke loan_products.';
COMMENT ON COLUMN loan_applications.applied_at       IS 'Waktu pengajuan dikirim.';
COMMENT ON COLUMN loan_applications.requested_amount IS 'Nominal yang diminta (paylater: limit yang diminta).';
COMMENT ON COLUMN loan_applications.requested_tenor  IS 'Tenor yang diminta (bulan).';
COMMENT ON COLUMN loan_applications.status           IS 'APPROVED atau REJECTED.';
COMMENT ON COLUMN loan_applications.decided_at       IS 'Waktu keputusan.';
COMMENT ON COLUMN loan_applications.approved_amount  IS 'Plafon disetujui (paylater: limit). NULL bila ditolak.';
COMMENT ON COLUMN loan_applications.reject_reason    IS 'RASIO CICILAN TINGGI, PENGHASILAN TIDAK MEMENUHI, SKOR KREDIT RENDAH. NULL bila disetujui.';
COMMENT ON COLUMN loan_applications.device_id        IS 'FK ke devices: perangkat yang dipakai mengajukan.';

COMMENT ON TABLE  loans IS 'Pinjaman: satu baris per pencairan KTA atau per pembelian paylater (tiap pembelian paylater = satu pinjaman dengan tenor 1/3/6/12 bulan).';
COMMENT ON COLUMN loans.loan_id            IS 'PK.';
COMMENT ON COLUMN loans.customer_id        IS 'FK ke customers.';
COMMENT ON COLUMN loans.account_id         IS 'FK ke accounts: rekening yang didebet untuk cicilan (dan dikredit saat pencairan KTA).';
COMMENT ON COLUMN loans.product_code       IS 'FK ke loan_products: PAYLATER atau KTA.';
COMMENT ON COLUMN loans.application_id     IS 'FK ke loan_applications (paylater: aplikasi aktivasi limit).';
COMMENT ON COLUMN loans.principal          IS 'Pokok pinjaman / nilai pembelian (rupiah).';
COMMENT ON COLUMN loans.tenor_months       IS 'Jumlah cicilan.';
COMMENT ON COLUMN loans.interest_rate_pa   IS 'Bunga per tahun (persen).';
COMMENT ON COLUMN loans.installment_amount IS 'Cicilan bulanan standar (cicilan terakhir menyesuaikan sisa pokok).';
COMMENT ON COLUMN loans.due_day            IS 'Tanggal jatuh tempo tiap bulan (1-28). KTA umumnya dipilih 1-4 hari setelah tanggal gajian; paylater memakai tanggal tagihan tetap nasabah.';
COMMENT ON COLUMN loans.disbursed_at       IS 'Waktu pencairan (KTA) atau pembelian (paylater).';
COMMENT ON COLUMN loans.first_due_date     IS 'Jatuh tempo cicilan pertama.';
COMMENT ON COLUMN loans.maturity_date      IS 'Jatuh tempo cicilan terakhir.';
COMMENT ON COLUMN loans.status             IS 'ACTIVE, PAID_OFF, DEFAULT (cicilan menunggak lebih dari 90 hari; cicilan sisanya CANCELLED).';
COMMENT ON COLUMN loans.closed_at          IS 'Waktu lunas atau gagal bayar. NULL bila masih aktif.';
COMMENT ON COLUMN loans.merchant_name      IS 'Merchant pembelian paylater. NULL untuk KTA.';
COMMENT ON COLUMN loans.merchant_category  IS 'Kategori merchant paylater: SHOPPING, TRAVEL, ELEKTRONIK, FURNITUR. NULL untuk KTA.';
COMMENT ON COLUMN loans.disbursement_transaction_id IS 'FK ke transactions: kredit LOAN_DISBURSEMENT (hanya KTA).';

COMMENT ON TABLE  loan_installments IS 'Jadwal dan realisasi cicilan per pinjaman. Satu pembayaran paylater bulanan dapat melunasi beberapa cicilan sekaligus (transaksi yang sama).';
COMMENT ON COLUMN loan_installments.installment_id         IS 'PK.';
COMMENT ON COLUMN loan_installments.loan_id                IS 'FK ke loans.';
COMMENT ON COLUMN loan_installments.installment_no         IS 'Cicilan ke-n.';
COMMENT ON COLUMN loan_installments.due_date               IS 'Tanggal jatuh tempo.';
COMMENT ON COLUMN loan_installments.amount_due             IS 'Total cicilan (pokok + bunga), tanpa denda.';
COMMENT ON COLUMN loan_installments.principal_part         IS 'Bagian pokok.';
COMMENT ON COLUMN loan_installments.interest_part          IS 'Bagian bunga.';
COMMENT ON COLUMN loan_installments.paid_at                IS 'Waktu pembayaran sukses. NULL bila belum dibayar.';
COMMENT ON COLUMN loan_installments.paid_amount            IS 'Nominal cicilan yang dibayar (tanpa denda).';
COMMENT ON COLUMN loan_installments.days_late              IS 'Hari keterlambatan = paid_at - due_date (0 bila tepat waktu atau lebih awal). NULL bila belum dibayar.';
COMMENT ON COLUMN loan_installments.late_fee               IS 'Denda yang dibebankan (juga tercatat di transactions.fee).';
COMMENT ON COLUMN loan_installments.payment_transaction_id IS 'FK ke transactions: transaksi pembayaran sukses.';
COMMENT ON COLUMN loan_installments.payment_channel        IS 'MOBILE (bayar manual di aplikasi), SCHEDULER (auto-debit Jadwal Bayar), ATM (bayar manual di luar aplikasi).';
COMMENT ON COLUMN loan_installments.status                 IS 'PAID, UNPAID (lewat jatuh tempo belum dibayar pada akhir data), UPCOMING (belum jatuh tempo), CANCELLED (pinjaman gagal bayar).';

COMMENT ON TABLE  scheduled_payments IS 'Fitur JADWAL BAYAR (scheduler): instruksi pembayaran otomatis bulanan untuk cicilan, tagihan, atau transfer rutin. Satu baris per instruksi; instruksi yang dibatalkan tetap tersimpan.';
COMMENT ON COLUMN scheduled_payments.schedule_id       IS 'PK. Dirujuk oleh transactions.scheduled_payment_id.';
COMMENT ON COLUMN scheduled_payments.customer_id       IS 'FK ke customers.';
COMMENT ON COLUMN scheduled_payments.account_id        IS 'FK ke accounts: rekening sumber dana.';
COMMENT ON COLUMN scheduled_payments.payment_type      IS 'LOAN_INSTALLMENT, BILL (tagihan ke biller), TRANSFER (transfer rutin, mis. kos/arisan).';
COMMENT ON COLUMN scheduled_payments.loan_id           IS 'FK ke loans untuk jadwal cicilan KTA. NULL untuk paylater (satu jadwal menagih seluruh tagihan paylater bulan itu).';
COMMENT ON COLUMN scheduled_payments.biller_id         IS 'FK ke billers (tipe BILL).';
COMMENT ON COLUMN scheduled_payments.beneficiary_id    IS 'FK ke beneficiaries (tipe TRANSFER).';
COMMENT ON COLUMN scheduled_payments.label             IS 'Nama jadwal.';
COMMENT ON COLUMN scheduled_payments.amount_mode       IS 'FIXED (nominal tetap) atau FULL_DUE (membayar total tagihan saat jatuh tempo).';
COMMENT ON COLUMN scheduled_payments.fixed_amount      IS 'Nominal tetap. NULL bila FULL_DUE.';
COMMENT ON COLUMN scheduled_payments.frequency         IS 'Selalu MONTHLY.';
COMMENT ON COLUMN scheduled_payments.day_of_month      IS 'Tanggal eksekusi tiap bulan (1-28), dijalankan sekitar pukul 06:00 WIB.';
COMMENT ON COLUMN scheduled_payments.notify_before_days IS 'Pengingat push H-n sebelum eksekusi. 0 = tanpa pengingat.';
COMMENT ON COLUMN scheduled_payments.created_at        IS 'Waktu nasabah membuat jadwal. Eksekusi pertama paling cepat H+1.';
COMMENT ON COLUMN scheduled_payments.created_device_id IS 'FK ke devices: perangkat pembuat jadwal.';
COMMENT ON COLUMN scheduled_payments.status            IS 'ACTIVE atau CANCELLED.';
COMMENT ON COLUMN scheduled_payments.cancelled_at      IS 'Waktu dibatalkan.';
COMMENT ON COLUMN scheduled_payments.cancel_reason     IS 'DIBATALKAN NASABAH, DIBATALKAN NASABAH SETELAH GAGAL BAYAR, NASABAH BERHENTI MEMAKAI APLIKASI.';
COMMENT ON COLUMN scheduled_payments.run_count         IS 'Jumlah percobaan eksekusi otomatis (termasuk percobaan ulang).';
COMMENT ON COLUMN scheduled_payments.success_count     IS 'Jumlah eksekusi sukses.';
COMMENT ON COLUMN scheduled_payments.fail_count        IS 'Jumlah eksekusi gagal (umumnya saldo tidak cukup; sistem mencoba ulang hingga 3 hari berturut-turut, lalu nasabah membayar manual).';
COMMENT ON COLUMN scheduled_payments.last_run_at       IS 'Waktu eksekusi terakhir.';

COMMENT ON TABLE  app_events IS 'Jejak layar/fitur yang dibuka dan dipakai nasabah di aplikasi, per sesi login. Membedakan fitur yang HANYA DIBUKA (OPEN tanpa COMPLETE) dari yang benar-benar dipakai (COMPLETE).';
COMMENT ON COLUMN app_events.event_id       IS 'PK.';
COMMENT ON COLUMN app_events.user_id        IS 'FK ke mobile_users.';
COMMENT ON COLUMN app_events.device_id      IS 'FK ke devices.';
COMMENT ON COLUMN app_events.login_id       IS 'FK ke login_logs: sesi login. Seluruh event dengan login_id sama = satu sesi aplikasi.';
COMMENT ON COLUMN app_events.feature_code   IS 'FK ke app_features.';
COMMENT ON COLUMN app_events.event_type     IS 'OPEN (layar dibuka), INTERACT (melakukan sesuatu di layar: filter, simulasi, dst), COMPLETE (transaksi/aksi selesai), FAILED (transaksi/pengajuan gagal).';
COMMENT ON COLUMN app_events.detail         IS 'Rincian aksi, mis. SIMULASI, PENGAJUAN, BUAT_JADWAL, BATALKAN_JADWAL, ENABLE_ONLINE, alasan gagal.';
COMMENT ON COLUMN app_events.event_at       IS 'Waktu event.';
COMMENT ON COLUMN app_events.transaction_id IS 'FK ke transactions untuk event COMPLETE/FAILED transaksi.';
COMMENT ON COLUMN app_events.loan_id        IS 'FK ke loans untuk event KTA/paylater/jadwal cicilan yang terkait pinjaman.';

COMMENT ON TABLE  notifications IS 'Notifikasi push ke nasabah dan apakah dibuka. Membuka notifikasi memicu sesi aplikasi.';
COMMENT ON COLUMN notifications.notification_id IS 'PK.';
COMMENT ON COLUMN notifications.user_id         IS 'FK ke mobile_users.';
COMMENT ON COLUMN notifications.category        IS 'PROMO, GAJI_MASUK (gaji/uang saku masuk), DUE_REMINDER (jatuh tempo bayar manual), SCHEDULE_REMINDER (pengingat jadwal bayar).';
COMMENT ON COLUMN notifications.sent_at         IS 'Waktu dikirim.';
COMMENT ON COLUMN notifications.opened_at       IS 'Waktu dibuka. NULL bila tidak pernah dibuka.';
COMMENT ON COLUMN notifications.related_loan_id IS 'FK ke loans bila pengingat terkait cicilan KTA.';

COMMENT ON VIEW v_monthly_transaction_summary IS 'Ringkasan transaksi SUKSES per bulan (WIB) per jenis transaksi.';
COMMENT ON VIEW v_customer_overview IS 'Ringkasan per nasabah: jumlah rekening, total saldo, status dan login terakhir aplikasi.';
"""

HEADER = """-- =====================================================================
--  MOBILE BANKING v2 - SKEMA & DATA DUMMY  (PostgreSQL 13+)
--  Dibuat oleh generate_mobile_banking.py
--  Nasabah: %d | seed: %d | intensity: %.2f | periode: %s s/d %s
--
--  Cara pakai:
--     createdb mbanking
--     psql -d mbanking -f %s
--  (di DBeaver/pgAdmin: jalankan SEBAGAI SCRIPT; bagian COPY ... FROM STDIN
--   butuh psql. Bila memakai DBeaver, jalankan lewat terminal dengan psql.)
--
--  Semua objek ada di schema "mbanking" (aman dijalankan ulang, schema lama di-DROP CASCADE).
--  Semua data FIKTIF.
-- =====================================================================
"""

log("Menulis %s ..." % ARGS.out)
for f_ in _files.values(): f_.close()
with open(ARGS.out, "w", encoding="utf-8", newline="\n") as out:
    out.write(HEADER % (N_CUST, ARGS.seed, INTENS, START_D, END.strftime("%Y-%m-%d %H:%M"), os.path.basename(ARGS.out)))
    out.write(DDL)
    for t_, cols in TABLES.items():
        out.write("\nCOPY %s (%s) FROM STDIN;\n" % (t_, cols))
        with open(os.path.join(TMPDIR, t_ + ".tsv"), encoding="utf-8") as fh:
            while True:
                chunk = fh.read(1 << 22)
                if not chunk: break
                out.write(chunk)
        out.write("\\.\n")
    out.write(POST_DDL)
    out.write("\n-- =====================  KAMUS DATA (COMMENT ON)  =====================\n")
    out.write(OLD_COMMENTS)
    out.write(NEW_COMMENTS)
    out.write("\nANALYZE;\n")
for t_ in TABLES:
    try: os.remove(os.path.join(TMPDIR, t_ + ".tsv"))
    except OSError: pass
try: os.rmdir(TMPDIR)
except OSError: pass

log("Selesai. Baris per tabel:")
for t_ in TABLES: log("  %-20s %s" % (t_, format(ROWCOUNT[t_], ",")))
log("Ukuran file: %.1f MB" % (os.path.getsize(ARGS.out) / 1e6))
if ARGS.load:
    log("Memuat ke database %s lewat psql ..." % ARGS.load)
    subprocess.run(["psql", "-v", "ON_ERROR_STOP=1", "-q", "-d", ARGS.load, "-f", ARGS.out], check=True)
    log("Database siap: schema mbanking")
