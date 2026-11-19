# -*- coding: utf-8 -*-
"""
BOT ALERT SQUEEZE MOMENTUM (LazyBear) + SUPERTREND 12/3  -  TF 4 JAM  -  TOP 200 KRIPTO GATE.IO PERPETUAL
Hanya alert Telegram. Tidak membuka posisi, tidak ada akses ke akun trading.

ATURAN (sesuai kesepakatan dengan Bos)
  Semua dihitung dari candle 4 jam yang SUDAH CLOSE.
  1. PANTAUAN (tanpa pesan): Supertrend (ATR 12, Factor 3) berbalik arah, dan pada candle pembalikan itu
     squeeze masih ON (titik hitam).
  2. Koin tetap dipantau selama titik terus hitam dan Supertrend tidak berbalik lagi.
  3. ALERT: pada candle close pertama yang titiknya putih (squeeze lepas), asalkan histogram berwarna TERANG
     searah tren:
        naik  : histogram hijau terang (di atas nol dan lebih tinggi dari candle sebelumnya)
        turun : histogram merah terang (di bawah nol dan lebih rendah dari candle sebelumnya)
  4. Keluar dari pantauan bila: Supertrend berbalik lagi sebelum squeeze lepas (lalu dihitung sebagai pantauan
     baru untuk arah sebaliknya), titik putih tetapi histogram belum terang (tanpa alert), alert sudah terkirim,
     atau lewat BATAS_PANTAUAN candle sejak pembalikan.
  Koin: 200 kontrak kripto Gate dengan volume 24 jam terbesar (saham/indeks/forex/komoditas tidak ikut).

Cara pakai:  python alert_sqz_st_4h.py github   (di GitHub Actions; token dari Secrets)
             python alert_sqz_st_4h.py          (di laptop; token dari config.txt)
             python alert_sqz_st_4h.py cek      (hanya tampilkan hasil, tidak kirim Telegram)
"""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

import requests

# ------------------------------------------------------------------ PENGATURAN
JUMLAH_KOIN = 200          # top volume 24 jam
ST_ATR = 12                # Supertrend ATR length
ST_FAKTOR = 3.0            # Supertrend factor
SQZ_PANJANG = 20           # Squeeze LazyBear: panjang
SQZ_BB_MULT = 2.0          # Bollinger multiplier
SQZ_KC_MULT = 1.5          # Keltner multiplier (pakai True Range)
BATAS_PANTAUAN = 15        # candle sejak pembalikan; lewat ini koin keluar dari pantauan
CANDLE_BARU = 2            # alert hanya bila candle titik-putih pertama termasuk 2 candle closed terakhir
                           # (jadwal GitHub bisa telat; pesan yang sama tidak pernah dikirim dua kali)
N_CANDLE = 600             # jumlah candle 4h yang diambil per koin
TF = "4h"
TF_MS = 4 * 3600 * 1000
SIMPAN_HARI = 10
THREADS = 6

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(BASE, "config.txt")
SEEN = os.path.join(BASE, "sqz_st_sudah_terkirim.json")
API = "https://api.gateio.ws/api/v4"
WIB = timezone(timedelta(hours=7))


def log(msg):
    print(f"[{datetime.now(WIB):%d-%b %H:%M:%S}] {msg}", flush=True)


# ------------------------------------------------------------------ INDIKATOR
def atr_rma(candles, n):
    """ATR dengan RMA (seperti ta.atr di TradingView). Isi None sebelum data cukup."""
    m = len(candles)
    tr = [candles[0][1] - candles[0][2]] + [
        max(candles[i][1] - candles[i][2], abs(candles[i][1] - candles[i - 1][3]), abs(candles[i][2] - candles[i - 1][3]))
        for i in range(1, m)]
    out = [None] * m
    if m < n:
        return out
    out[n - 1] = sum(tr[:n]) / n
    for i in range(n, m):
        out[i] = (out[i - 1] * (n - 1) + tr[i]) / n
    return out


def supertrend(candles, n=ST_ATR, faktor=ST_FAKTOR):
    """Supertrend ala TradingView ta.supertrend. Return (naik, garis): naik[i] True = tren naik (garis hijau)."""
    m = len(candles)
    atr = atr_rma(candles, n)
    naik = [None] * m
    garis = [None] * m
    ub = [None] * m
    lb = [None] * m
    for i in range(m):
        if atr[i] is None:
            continue
        hl2 = (candles[i][1] + candles[i][2]) / 2
        u = hl2 + faktor * atr[i]
        l = hl2 - faktor * atr[i]
        c = candles[i][3]
        if i > 0 and ub[i - 1] is not None:
            pu, pl, pc = ub[i - 1], lb[i - 1], candles[i - 1][3]
            l = l if (l > pl or pc < pl) else pl
            u = u if (u < pu or pc > pu) else pu
            if garis[i - 1] == pu:                  # sebelumnya garis atas (tren turun)
                arah = -1 if c > u else 1
            else:
                arah = 1 if c < l else -1
        else:
            arah = 1                                 # candle pertama yang punya ATR: turun (seperti Pine)
        ub[i], lb[i] = u, l
        naik[i] = (arah == -1)
        garis[i] = l if arah == -1 else u
    return naik, garis


def squeeze(candles):
    """Squeeze Momentum LazyBear. Return (sqz_on, val). sqz_on True = titik hitam. val = tinggi histogram."""
    n = len(candles)
    L = SQZ_PANJANG
    hi = [c[1] for c in candles]
    lo = [c[2] for c in candles]
    cl = [c[3] for c in candles]
    tr = [hi[0] - lo[0]] + [max(hi[i] - lo[i], abs(hi[i] - cl[i - 1]), abs(lo[i] - cl[i - 1])) for i in range(1, n)]
    sqz_on = [None] * n
    x = [None] * n
    sma = [None] * n
    for i in range(L - 1, n):
        w = cl[i - L + 1:i + 1]
        m = sum(w) / L
        sma[i] = m
        sd = (sum((v - m) ** 2 for v in w) / L) ** 0.5          # simpangan baku populasi, seperti stdev Pine
        dev = SQZ_BB_MULT * sd
        ubb, lbb = m + dev, m - dev
        rng = sum(tr[i - L + 1:i + 1]) / L
        ukc, lkc = m + rng * SQZ_KC_MULT, m - rng * SQZ_KC_MULT
        sqz_on[i] = (lbb > lkc) and (ubb < ukc)
        hh, ll = max(hi[i - L + 1:i + 1]), min(lo[i - L + 1:i + 1])
        x[i] = cl[i] - (((hh + ll) / 2) + m) / 2
    val = [None] * n
    mx = (L - 1) / 2
    sxx = sum((k - mx) ** 2 for k in range(L))
    for i in range(2 * L - 2, n):
        w = x[i - L + 1:i + 1]
        my = sum(w) / L
        slope = sum((k - mx) * (w[k] - my) for k in range(L)) / sxx
        val[i] = my + slope * (L - 1 - mx)                        # linreg, nilai di candle terakhir
    return sqz_on, val


# ------------------------------------------------------------------ ATURAN
def analisa(candles):
    """candles = hanya candle yang sudah CLOSE. Return dict:
       {'status': 'alert'|'pantau'|None, 'naik': bool, 'f': idx pembalikan, 'i': idx titik putih pertama, ...}"""
    m = len(candles)
    if m < 2 * SQZ_PANJANG + 5:
        return {"status": None}
    naik, garis = supertrend(candles)
    on, val = squeeze(candles)
    n = m - 1
    # pembalikan Supertrend terakhir
    f = None
    for k in range(n, 0, -1):
        if naik[k] is None or naik[k - 1] is None:
            break
        if naik[k] != naik[k - 1]:
            f = k
            break
    if f is None or on[f] is None or not on[f]:
        return {"status": None}
    arah_naik = naik[f]
    # titik harus hitam terus sejak pembalikan sampai candle putih pertama
    i = f + 1
    while i <= n and on[i]:
        i += 1
    if i > n:
        if n - f > BATAS_PANTAUAN:
            return {"status": None}
        return {"status": "pantau", "naik": arah_naik, "f": f, "n": n}
    if i - f > BATAS_PANTAUAN:
        return {"status": None}
    # i = candle titik putih pertama (tren pasti masih sama, karena f adalah pembalikan terakhir)
    v, vp = val[i], val[i - 1]
    if v is None or vp is None:
        return {"status": None}
    terang = (v > 0 and v > vp) if arah_naik else (v < 0 and v < vp)
    return {"status": "alert" if terang else "gugur", "naik": arah_naik, "f": f, "i": i, "n": n,
            "val": v, "garis": garis[i], "harga": candles[i][3], "jarak": i - f}


# ------------------------------------------------------------------ DATA GATE
def http_get(url, params=None, retries=4):
    last = None
    for k in range(retries):
        try:
            r = requests.get(url, params=params, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 429:
                time.sleep(2 + k * 2)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            time.sleep(1.5)
    raise last


def daftar_top(jumlah=JUMLAH_KOIN):
    """200 kontrak KRIPTO (contract_type kosong) dengan volume 24 jam (USDT) terbesar."""
    kontrak = http_get(f"{API}/futures/usdt/contracts")
    kripto = set()
    for d in kontrak:
        if d.get("contract_type") or d.get("in_delisting") or d.get("status", "trading") != "trading":
            continue
        kripto.add(d["name"])
    tick = http_get(f"{API}/futures/usdt/tickers")
    baris = []
    for d in tick:
        c = d.get("contract")
        if c in kripto:
            try:
                baris.append((float(d.get("volume_24h_quote") or 0), c))
            except (TypeError, ValueError):
                pass
    baris.sort(reverse=True)
    return [c for _, c in baris[:jumlah]]


def ambil_candle(kontrak):
    k = http_get(f"{API}/futures/usdt/candlesticks", {"contract": kontrak, "interval": TF, "limit": N_CANDLE})
    cs = [(float(x["o"]), float(x["h"]), float(x["l"]), float(x["c"]), int(x["t"]) * 1000) for x in k]
    cs.sort(key=lambda z: z[4])
    sekarang = int(time.time() * 1000)
    while cs and cs[-1][4] + TF_MS > sekarang:                # buang candle yang masih berjalan
        cs.pop()
    return cs


# ------------------------------------------------------------------ TELEGRAM & RIWAYAT
def baca_config():
    cfg = {}
    if os.path.exists(CONFIG):
        for ln in open(CONFIG, encoding="utf-8"):
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                cfg[k.strip().upper()] = v.strip()
    return cfg


def kirim(token, chat_id, teks):
    for _ in range(3):
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              data={"chat_id": chat_id, "text": teks[:4000], "disable_web_page_preview": "true"}, timeout=20)
            j = r.json()
            if j.get("ok"):
                return True
            log(f"Telegram menolak: {j.get('description')}")
        except Exception as e:
            log(f"Telegram error: {type(e).__name__}")
        time.sleep(3)
    return False


def muat_seen():
    try:
        return json.load(open(SEEN, encoding="utf-8"))
    except Exception:
        return None


def simpan_seen(d):
    batas = time.time() - SIMPAN_HARI * 86400
    d = {k: v for k, v in d.items() if v >= batas}
    json.dump(d, open(SEEN, "w", encoding="utf-8"), indent=0)


def fmt(x):
    if x >= 100:
        return f"{x:,.2f}"
    if x >= 1:
        return f"{x:.4f}"
    return f"{x:.6g}"


def wib(ms):
    return datetime.fromtimestamp(ms / 1000, WIB).strftime("%d-%b %H:%M WIB")


def pesan(kontrak, cs, r):
    naik = r["naik"]
    i, f = r["i"], r["f"]
    nama = kontrak.replace("_", "/")
    arah = "NAIK" if naik else "TURUN"
    warna = "hijau terang" if naik else "merah terang"
    tutup = cs[i][4] + TF_MS
    telat = (time.time() * 1000 - tutup) / 60000
    t = [
        f"🔔 SQUEEZE LEPAS + SUPERTREND {arah} (4H)",
        f"{nama} (Gate perpetual)",
        "",
        f"Supertrend 12/3: tren {arah.lower()} sejak {wib(cs[f][4])} ({r['jarak']} candle lalu, squeeze tertahan sampai sekarang)",
        f"Squeeze: titik hitam -> putih di candle close {wib(tutup)}",
        f"Histogram: {warna} (nilai {r['val']:.5g})",
        f"Harga close: {fmt(r['harga'])}",
        f"Garis Supertrend: {fmt(r['garis'])}",
    ]
    if telat > 25:
        t.append(f"(alert terlambat {telat / 60:.1f} jam karena jadwal GitHub)")
    t += ["", f"https://www.gate.com/futures/USDT/{kontrak}", "Hanya informasi kondisi indikator, bukan rekomendasi."]
    return "\n".join(t)


# ------------------------------------------------------------------ UTAMA
def periksa(kontrak):
    try:
        cs = ambil_candle(kontrak)
        return kontrak, cs, analisa(cs), None
    except Exception as e:
        return kontrak, [], {"status": None}, f"{type(e).__name__}"


def main():
    mode = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    cek = (mode == "cek")
    if mode == "github":
        token, chat_id = os.environ.get("TELEGRAM_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")
    else:
        cfg = baca_config()
        token, chat_id = cfg.get("TOKEN", ""), cfg.get("CHAT_ID", "")
    if not cek and (not token or not chat_id):
        log("TOKEN / CHAT_ID belum diisi (GitHub: Secrets TELEGRAM_TOKEN dan TELEGRAM_CHAT_ID; laptop: config.txt).")
        sys.exit(1)

    koin = daftar_top()
    log(f"{len(koin)} koin kripto teratas (volume 24 jam).")
    seen = muat_seen()
    pertama = seen is None
    seen = seen or {}
    hasil = []
    with ThreadPoolExecutor(THREADS) as ex:
        for h in ex.map(periksa, koin):
            hasil.append(h)
    gagal = [h[0] for h in hasil if h[3]]
    pantau = [(k, r) for k, cs, r, e in hasil if r["status"] == "pantau"]
    log(f"Pantauan: {len(pantau)} koin. Gagal ambil data: {len(gagal)}.")
    if len(gagal) > len(koin) * 0.3:
        log("Terlalu banyak koin gagal diambil; hentikan run ini supaya tidak ada sinyal terlewat diam-diam.")
        sys.exit(1)

    baru = 0
    for k, cs, r, e in hasil:
        if r["status"] != "alert":
            continue
        n = r["n"]
        if n - r["i"] >= CANDLE_BARU:
            continue
        kunci = f"{k}|{cs[r['i']][4]}"
        if kunci in seen:
            continue
        teks = pesan(k, cs, r)
        if cek:
            print("\n" + teks)
            continue
        if kirim(token, chat_id, teks):
            seen[kunci] = time.time()
            baru += 1
            log(f"Alert terkirim: {k} {'naik' if r['naik'] else 'turun'}")
    if pertama and not cek:
        kirim(token, chat_id, f"✅ Bot Squeeze + Supertrend 4H aktif. Memantau {len(koin)} kripto teratas (volume 24 jam). "
                              f"Alert hanya muncul saat squeeze lepas searah Supertrend (histogram terang).")
    if not cek:
        simpan_seen(seen)
    if cek:
        print("\nPANTAUAN SAAT INI:")
        for k, r in pantau:
            print(f"  {k:16s} {'naik ' if r['naik'] else 'turun'}  {r['n'] - r['f']} candle sejak pembalikan")
    log(f"Selesai. Alert baru: {baru}.")


if __name__ == "__main__":
    main()
