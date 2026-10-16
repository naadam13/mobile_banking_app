#!/usr/bin/env python3
"""Bangun docs/nasabah.html dan docs/bank.html dari template + snapshot data.

Pakai:  python3 src/build.py            (dari folder mana saja)
        python3 src/build.py --no-bump  (jangan naikkan VERSION di docs/sw.js)

Alur: template (HTML/CSS/JS bersih) + data/*.json -> sisipkan data ->
tanam font (base64) -> bungkus jadi dokumen HTML utuh dengan PWA -> docs/.
Hanya memakai pustaka standar Python.
"""
import base64, collections, json, re, sys
from pathlib import Path

SRC = Path(__file__).resolve().parent
ROOT = SRC.parent
DOCS = ROOT / 'docs'
DATA = SRC / 'data'
FONTS = SRC / 'fonts'
QSQL = ROOT / 'sql' / 'analisis_behaviour_per_nasabah.sql'


def load(name):
    return json.loads((DATA / name).read_text(encoding='utf-8'))


def J(v):
    # JSON aman untuk ditanam di <script>
    return json.dumps(v, ensure_ascii=False, separators=(',', ':'), default=str).replace('</', '<\\/')


def inject(t, **kv):
    for k, v in kv.items():
        assert '/*%s*/null' % k in t, 'placeholder %s tidak ada di template' % k
        t = t.replace('/*%s*/null' % k, J(v))
    return t


# ---------------------------------------------------------------- halaman nasabah
def page_nasabah():
    d = load('export.json')
    sel = load('sel.json')
    keep = 'Z1 B1 B2 B3 B5 D1 E4 F1 G1 G2 C2 H1 H2 I2 I3'.split()
    d = {k: d[k] for k in keep}
    pers = {s['customer_id']: s['persona'] for s in sel}
    rc = load('rec.json')
    x1 = load('x1.json')

    # lengkapi transfer dengan bank & no. rekening tujuan yang paling sering dipakai
    m = collections.defaultdict(collections.Counter)
    for cid, mo, tp, it, cat, n, a in x1:
        if tp.startswith('TRANSFER') and it.count('|') == 2:
            nm, bk, ac = it.split('|')
            m[(str(cid), tp, nm)][(bk.split(' (')[0], ac)] += n
    for k in ('rec', 'upc'):
        for cid, lst in rc[k].items():
            for u in lst:
                if u['tp'].startswith('TRANSFER'):
                    c = m.get((cid, u['tp'], u['item']))
                    if c:
                        (bk, ac), _ = c.most_common(1)[0]
                        u['bank'] = bk
                        u['acct'] = ac

    # hanya jenis transaksi yang berulang (bukan ATM, QRIS, dll.)
    ok = {'BILL_PAYMENT', 'LOAN_INSTALLMENT', 'TRANSFER_BANK', 'TRANSFER_INTERNAL', 'CARD_ONLINE', 'ADMIN_FEE'}
    for k in ('rec', 'upc'):
        rc[k] = {i: [u for u in lst if u['tp'] in ok] for i, lst in rc[k].items()}

    t = (SRC / 'templates' / 'nasabah.html').read_text(encoding='utf-8')
    return inject(t, DATA=d, PERSONA=pers, X1=x1, X7=load('x7.json'), X2=load('x2.json'),
                  X5=load('x5.json'), REC=rc['rec'], UPC=rc['upc'])


# ---------------------------------------------------------------- halaman bank
def page_bank():
    d = load('export.json')
    sel = load('sel.json')
    titles = {m.group(1): m.group(2).strip()
              for m in re.finditer(r'^-- ([A-Z]\d+)\. (.+)$', QSQL.read_text(encoding='utf-8'), re.M)}
    pers = {s['customer_id']: s['persona'] for s in sel}
    for k, v in d.items():
        if v:
            dk = next((c for c in v[0] if c in ('bulan', 'kuartal', 'tgl_gaji', 'tanggal_gaji', 'periode')), None)
            if dk:
                v.sort(key=lambda r: (r['customer_id'], str(r[dk])))
    z = {r['customer_id']: r for r in d['Z1']}
    c4 = {r['customer_id']: r for r in d['C4']}
    meta = []
    for s in sel:
        i = s['customer_id']
        sp = (c4.get(i) or {}).get('sesi_90h') or (z.get(i) or {}).get('sesi_90h') or 0
        meta.append(dict(customer_id=i, persona=pers[i], spark=sp))
    t = (SRC / 'templates' / 'bank.html').read_text(encoding='utf-8')
    return inject(t, DATA=d, TITLES=titles, PERSONA=pers, META=meta)


# ---------------------------------------------------------------- bungkus PWA
def fontcss(pkg_slug, fam, weights):
    o = ''
    for w in weights:
        b = base64.b64encode((FONTS / f'{pkg_slug}-latin-{w}-normal.woff2').read_bytes()).decode()
        o += ("@font-face{font-family:'%s';font-style:normal;font-weight:%d;font-display:swap;"
              "src:url(data:font/woff2;base64,%s) format('woff2')}\n" % (fam, w, b))
    return '<style>\n' + o + '</style>'


HEAD_EXTRA = ('<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
              '<link rel="manifest" href="manifest.webmanifest"><meta name="theme-color" content="#1553e6">'
              '<link rel="apple-touch-icon" href="icons/apple-touch-icon.png">')
SW_REG = "<script>if('serviceWorker' in navigator)navigator.serviceWorker.register('sw.js').catch(()=>{});</script>"


def wrap(t, css):
    t = re.sub(r'<link rel="preconnect"[^>]*>\s*', '', t)
    t, c = re.subn(r'<link href="https://fonts\.googleapis\.com[^>]*>', lambda m: css, t)
    assert c == 1, 'link Google Fonts tidak ditemukan di template'
    pos = None
    for m in re.finditer(r'<(/?)(\w+)[^>]*>', t):
        if m.group(2) in ('title', 'meta', 'link', 'style'):
            continue
        pos = m.start()
        break
    head, body = t[:pos], t[pos:]
    head = head.replace('<meta name="claude:viewport-fit-sides" content="cover">', '')
    return ('<!doctype html>\n<html lang="id"><head>' + head + HEAD_EXTRA + '</head><body>\n'
            + body + SW_REG + '\n</body></html>\n')


def main():
    bump = '--no-bump' not in sys.argv
    jobs = [
        ('nasabah.html', page_nasabah(), fontcss('roboto', 'Roboto', [400, 500, 700])),
        ('bank.html', page_bank(),
         fontcss('plus-jakarta-sans', 'Plus Jakarta Sans', [400, 500, 600, 700, 800])
         + fontcss('jetbrains-mono', 'JetBrains Mono', [400, 500])),
    ]
    changed = False
    for name, html, css in jobs:
        out = wrap(html, css)
        p = DOCS / name
        old = p.read_text(encoding='utf-8') if p.exists() else None
        if out != old:
            p.write_text(out, encoding='utf-8')
            changed = True
        print('%-14s %8d byte  %s' % (name, len(out), 'diubah' if out != old else 'sama'))
    sw = DOCS / 'sw.js'
    if changed and bump and sw.exists():
        s = sw.read_text(encoding='utf-8')
        m = re.search(r"VERSION\s*=\s*'v(\d+)'", s)
        if m:
            n = int(m.group(1)) + 1
            sw.write_text(s.replace(m.group(0), m.group(0).replace(m.group(1), str(n)), 1), encoding='utf-8')
            print('sw.js VERSION -> v%d' % n)


if __name__ == '__main__':
    main()
