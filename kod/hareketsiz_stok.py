#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hareketsiz Raf Stoğu Raporu  —  PobolEti (depo.yemgross.com.tr) için

Ne yapar?
  1) PobolEti API v2'den raf listesini, raf stoklarını, markaları, alış fiyatlarını
     ve STOK ÇIKIŞ hareketlerini okur (sadece GET; hiçbir şeyi değiştirmez).
  2) "Satış hareketi" sayılan tek şey: GERÇEK DEPO RAFINDAN (raf tipi 1),
     kayıp sebebi OLMAYAN (sayım / düzeltme / defolu vb. değil), miktarı > 0 olan çıkış.
  3) Raflarda stoğu olup belirli gündür satış çıkışı görmeyen ürünleri
     2 hafta / 30 gün / 6 ay sekmeleriyle, marka filtreli bir ekranda (rapor.html) listeler.

Çalıştırma:
  python hareketsiz_stok.py                  -> raporu üretir ve tarayıcıda açar
  python hareketsiz_stok.py --test           -> sadece bağlantıyı ve veri yapısını test eder
  python hareketsiz_stok.py --sessiz         -> raporu üretir, tarayıcı açmaz (zamanlanmış görev için)
  python hareketsiz_stok.py --onbellek-sil   -> önbelleği silip her şeyi baştan çeker

Gereksinim: Python 3.8+ (ek paket gerekmez)
"""

import argparse
import configparser
import datetime as dt
import json
import os
import pathlib
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

SURUM = "1.1.0"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AYAR_DOSYASI = os.path.join(BASE_DIR, "ayarlar.ini")
ONBELLEK_DOSYASI = os.path.join(BASE_DIR, "onbellek.db")
SABLON_DOSYASI = os.path.join(BASE_DIR, "rapor_sablonu.html")
RAPOR_DOSYASI = os.path.join(BASE_DIR, "rapor.html")
ARSIV_KLASORU = os.path.join(BASE_DIR, "raporlar")

# Windows konsolunda Türkçe karakterler bozulmasın
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


class ApiHatasi(Exception):
    pass


# --------------------------------------------------------------------------- #
#  Ayarlar
# --------------------------------------------------------------------------- #
class Ayarlar:
    def __init__(self, yol):
        cp = configparser.ConfigParser(interpolation=None)  # şifrede % olsa da bozulmasın
        if not os.path.exists(yol):
            raise SystemExit(f"Ayar dosyası bulunamadı: {yol}")
        cp.read(yol, encoding="utf-8")

        b = cp["baglanti"] if cp.has_section("baglanti") else {}
        r = cp["rapor"] if cp.has_section("rapor") else {}
        p = cp["performans"] if cp.has_section("performans") else {}

        self.adres = (b.get("adres") or "https://depo.yemgross.com.tr/api").rstrip("/")
        # Ortam değişkeni varsa dosyadakinin önüne geçer (şifreyi dosyada tutmak istemeyenler için)
        self.kullanici = os.environ.get("POBOL_KULLANICI") or (b.get("kullanici") or "").strip()
        self.sifre = os.environ.get("POBOL_SIFRE") or (b.get("sifre") or "").strip()

        self.pencereler = sorted({int(x) for x in (r.get("pencereler") or "14,30,180").split(",") if x.strip()})
        self.gecmis_gun = max(int(r.get("gecmis_gun") or 200), max(self.pencereler) + 1)
        self.satis_raf_tipleri = {x.strip() for x in (r.get("satis_raf_tipleri") or "1").split(",") if x.strip()}
        self.haric_raflar = {x.strip().upper() for x in (r.get("haric_raflar") or "").split(",") if x.strip()}
        self.alis_fiyati = (r.get("alis_fiyati") or "evet").strip().lower() in ("evet", "1", "true", "yes")
        self.firma = (r.get("baslik") or "YemGross Depo").strip()

        self.sayfa_boyutu = int(p.get("sayfa_boyutu") or 100)
        self.bekleme = float(p.get("istek_arasi_bekleme") or 0.1)
        self.urun_gecmis_sayfa = int(p.get("urun_gecmis_sayfa") or 3)
        self.onbellek_gun = int(p.get("onbellek_gun") or 7)
        self.zaman_asimi = int(p.get("zaman_asimi") or 60)
        self.aralik_ust_sinir = int(p.get("aralik_ust_sinir") or 250000)

        if not self.kullanici or not self.sifre:
            raise SystemExit(
                "ayarlar.ini içinde [baglanti] kullanici / sifre boş.\n"
                "Programa özel, sadece okuma yetkili bir depo kullanıcısı açıp bilgilerini oraya yazın."
            )


# --------------------------------------------------------------------------- #
#  API istemcisi (sadece GET)
# --------------------------------------------------------------------------- #
class PobolApi:
    def __init__(self, ayar: Ayarlar):
        self.a = ayar
        self.token = None
        self.bearer = True  # "Authorization: Bearer xxx"; olmazsa çıplak token denenir
        self.istek_sayisi = 0

    # -- düşük seviye
    def _get(self, url, headers):
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=self.a.zaman_asimi) as r:
            return r.read().decode("utf-8", "replace")

    @staticmethod
    def _token_bul(js):
        """gettoken cevabından token'ı esnek şekilde çıkarır."""
        adaylar = []
        if isinstance(js, dict):
            for k in ("token", "access_token", "param", "data"):
                if k in js:
                    adaylar.append(js[k])
        for c in adaylar:
            if isinstance(c, str) and c.strip():
                s = c.strip()
                if s.startswith("{"):
                    try:
                        return PobolApi._token_bul(json.loads(s))
                    except Exception:
                        pass
                return s
            if isinstance(c, dict):
                t = PobolApi._token_bul(c)
                if t:
                    return t
        return None

    def giris(self):
        q = urllib.parse.urlencode({"username": self.a.kullanici, "password": self.a.sifre})
        try:
            body = self._get(f"{self.a.adres}/gettoken?{q}", {"Accept": "application/json"})
        except urllib.error.HTTPError as e:
            raise ApiHatasi(f"Giriş isteği başarısız (HTTP {e.code}). Adres veya IP izni kontrol edilmeli.")
        except urllib.error.URLError as e:
            raise ApiHatasi(f"Sunucuya ulaşılamadı: {e.reason}")
        try:
            js = json.loads(body)
        except json.JSONDecodeError:
            raise ApiHatasi("Giriş cevabı anlaşılamadı: " + body[:200])
        if isinstance(js, dict) and js.get("status") == "failure":
            raise ApiHatasi("Giriş başarısız: " + str(js.get("message", "")))
        tok = self._token_bul(js)
        if not tok:
            raise ApiHatasi("Giriş cevabında token bulunamadı: " + body[:200])
        self.token = tok
        log("API girişi başarılı.")

    def get(self, model, params):
        if not self.token:
            self.giris()
        url = f"{self.a.adres}/v2/{model}?{urllib.parse.urlencode(params, doseq=True)}"
        token_denemesi = 0
        for deneme in range(6):
            hdr = {
                "Accept": "application/json",
                "Authorization": ("Bearer " + self.token) if self.bearer else self.token,
            }
            try:
                time.sleep(self.a.bekleme)
                self.istek_sayisi += 1
                body = self._get(url, hdr)
                js = json.loads(body)
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    js = {"status": "failure", "message": f"token yetkisiz (HTTP {e.code})"}
                else:
                    log(f"  HTTP {e.code} — tekrar denenecek ({deneme + 1}/6)")
                    time.sleep(2 * (deneme + 1))
                    continue
            except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
                log(f"  Bağlantı/okuma hatası: {e} — tekrar denenecek ({deneme + 1}/6)")
                time.sleep(2 * (deneme + 1))
                continue

            if isinstance(js, dict) and js.get("status") == "failure":
                msg = str(js.get("message", ""))
                if "token" in msg.lower() or "yetki" in msg.lower():
                    token_denemesi += 1
                    if token_denemesi == 1:
                        self.bearer = not self.bearer  # diğer başlık biçimini dene
                        continue
                    if token_denemesi == 2:
                        self.bearer = True
                        self.giris()  # token süresi dolmuş olabilir
                        continue
                    if token_denemesi == 3:
                        self.bearer = False
                        continue
                raise ApiHatasi(f"{model}: {msg}")
            return js
        raise ApiHatasi(f"{model}: istek 6 denemede de başarısız oldu.")

    @staticmethod
    def satirlar(js):
        if isinstance(js, dict):
            if isinstance(js.get("data"), list):
                return js["data"]
            prm = js.get("param")
            if isinstance(prm, list):
                return prm
            if isinstance(prm, dict) and isinstance(prm.get("data"), list):
                return prm["data"]
        if isinstance(js, list):
            return js
        return []

    def sayfali(self, model, sorgu=None, en_fazla_sayfa=None, ust_sinir=None):
        """Tüm sayfaları dolaşır; her sayfanın satırlarını yield eder."""
        sorgu = dict(sorgu or {})
        sayfa = 1
        onceki_ilk = None
        while True:
            params = {"pagination[page]": sayfa, "pagination[perpage]": self.a.sayfa_boyutu}
            params.update(sorgu)
            js = self.get(model, params)
            rows = self.satirlar(js)
            meta = js.get("meta") if isinstance(js, dict) else None
            meta = meta if isinstance(meta, dict) else {}
            toplam = int(meta.get("total") or 0)
            sayfalar = int(meta.get("pages") or 0)
            if sayfa == 1 and ust_sinir and toplam > ust_sinir:
                raise ApiHatasi(
                    f"{model}: filtre sonucu beklenenden büyük ({toplam} kayıt). "
                    "Tarih filtresi çalışmıyor olabilir; işlem durduruldu."
                )
            if not rows:
                break
            ilk = str(rows[0].get("id")) if isinstance(rows[0], dict) else None
            if ilk is not None and ilk == onceki_ilk:
                break  # sayfalama dikkate alınmıyorsa sonsuz döngüye girme
            onceki_ilk = ilk
            yield rows, sayfa, sayfalar, toplam
            if sayfalar and sayfa >= sayfalar:
                break
            if not sayfalar and len(rows) < self.a.sayfa_boyutu:
                break
            if en_fazla_sayfa and sayfa >= en_fazla_sayfa:
                break
            sayfa += 1


# --------------------------------------------------------------------------- #
#  Yardımcılar
# --------------------------------------------------------------------------- #
def alan(row, *adlar, son_ek=None):
    """Satırdan ilk dolu alanı döndürür; bulamazsa adı son_ek ile biten alanı arar."""
    for a in adlar:
        v = row.get(a)
        if v not in (None, ""):
            return v
    if son_ek:
        for k, v in row.items():
            if k.endswith(son_ek) and v not in (None, ""):
                return v
    return None


def sayi(v, varsayilan=0.0):
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return varsayilan


TARIH_BICIMLERI = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M",
                   "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%Y-%m-%d", "%d.%m.%Y")


def tarih(v):
    if not v:
        return None
    s = str(v).strip()
    for f in TARIH_BICIMLERI:
        try:
            return dt.datetime.strptime(s, f)
        except ValueError:
            pass
    return None


def ts(d):
    return d.strftime("%Y-%m-%d %H:%M:%S") if d else None


def kayip_yok(v):
    return str(v if v is not None else "0").strip() in ("", "0", "-", "None", "null")


def anahtar(s):
    return (str(s or "")).strip().upper()


# --------------------------------------------------------------------------- #
#  Önbellek
# --------------------------------------------------------------------------- #
class Onbellek:
    def __init__(self, yol):
        self.db = sqlite3.connect(yol)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS cikislar(
                id INTEGER PRIMARY KEY, barkod TEXT, raf TEXT, miktar REAL,
                kayip TEXT, kullanici TEXT, tarih TEXT);
            CREATE INDEX IF NOT EXISTS ix_cikis_barkod ON cikislar(barkod);
            CREATE INDEX IF NOT EXISTS ix_cikis_tarih ON cikislar(tarih);
            CREATE TABLE IF NOT EXISTS cekilen_gunler(gun TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS kv(anahtar TEXT PRIMARY KEY, deger TEXT, guncelleme TEXT);
            CREATE TABLE IF NOT EXISTS urun_gecmisi(
                pid TEXT PRIMARY KEY, son_satis TEXT, son_satis_raf TEXT, son_giris_raf TEXT,
                ilk_gorulen TEXT, tamami INTEGER, kontrol TEXT);
            """
        )
        self.db.commit()

    def kv_al(self, k, en_fazla_gun):
        r = self.db.execute("SELECT deger, guncelleme FROM kv WHERE anahtar=?", (k,)).fetchone()
        if not r:
            return None
        g = tarih(r[1])
        if g and (dt.datetime.now() - g).days < en_fazla_gun:
            return json.loads(r[0])
        return None

    def kv_yaz(self, k, v):
        self.db.execute("INSERT OR REPLACE INTO kv VALUES(?,?,?)", (k, json.dumps(v, ensure_ascii=False), ts(dt.datetime.now())))
        self.db.commit()


# --------------------------------------------------------------------------- #
#  Veri toplama
# --------------------------------------------------------------------------- #
class Toplayici:
    def __init__(self, api: PobolApi, ob: Onbellek, ayar: Ayarlar):
        self.api, self.ob, self.a = api, ob, ayar
        self.raf_tipi = {}  # RAF KODU -> tip

    # -- raflar
    def raflar(self):
        veri = self.ob.kv_al("raflar", 1)
        if veri is None:
            log("Raf listesi çekiliyor...")
            veri = {}
            for rows, s, ss, _ in self.api.sayfali("repository_shelves"):
                for r in rows:
                    kod = alan(r, "repository_shelves_code", "code")
                    tip = alan(r, "repository_shelves_type", "type")
                    if kod:
                        veri[anahtar(kod)] = str(tip if tip is not None else "")
            self.ob.kv_yaz("raflar", veri)
        self.raf_tipi = veri
        tipler = {}
        for t in veri.values():
            tipler[t] = tipler.get(t, 0) + 1
        log(f"  {len(veri)} raf (tiplere göre: {tipler})")
        return veri

    def satis_rafi_mi(self, raf):
        k = anahtar(raf)
        if not k or k in self.a.haric_raflar:
            return False
        return self.raf_tipi.get(k) in self.a.satis_raf_tipleri

    # -- stok
    def stoklar(self):
        log("Raf stokları çekiliyor...")
        liste = []
        for rows, s, ss, top in self.api.sayfali("repository_stock_info"):
            if s == 1 or s % 20 == 0:
                log(f"  sayfa {s}/{ss or '?'}")
            for r in rows:
                liste.append({
                    "pid": str(alan(r, "repository_stock_info_pid", "product_id", "pid") or ""),
                    "ad": alan(r, "repository_stock_info_product_name", "product_name") or "",
                    "stok_kodu": alan(r, "repository_stock_info_product_stockcode", "product_stockcode") or "",
                    "barkod": str(alan(r, "repository_stock_info_product_barcode", "product_barcode") or ""),
                    "raf_stok": sayi(alan(r, "repository_stock_info_stock_count_at_shelves", "stock_count_at_shelves")),
                    "ana_depo": sayi(alan(r, "repository_stock_info_stock_count_at_main_rep", "stock_count_at_main_rep")),
                    "rezerve": sayi(alan(r, "repository_stock_info_stock_count_reserved", "stock_count_reserved")),
                    "marka": alan(r, "brands_brandid", "brands_name", son_ek="brandid"),
                    "resim": alan(r, "products_pic_product_id") or "",
                })
        rafta = [x for x in liste if x["raf_stok"] > 0]
        log(f"  {len(liste)} ürün kaydı, raflarda stoğu olan: {len(rafta)}")
        return rafta

    # -- markalar
    def markalar(self, stoklar):
        dolu = sum(1 for x in stoklar if x["marka"] and not str(x["marka"]).isdigit())
        if stoklar and dolu >= len(stoklar) * 0.5:
            return  # API markayı zaten isim olarak veriyor
        harita = self.ob.kv_al("marka_harita", self.a.onbellek_gun)
        if harita is None:
            log("Markalar çekiliyor (stok bilgisinde marka adı yok; marka marka eşleştirilecek)...")
            marka_ad = {}
            for rows, s, ss, _ in self.api.sayfali("brands"):
                for r in rows:
                    bid = str(r.get("id") or "")
                    ad = alan(r, "brands_name", "name")
                    if bid and ad:
                        marka_ad[bid] = ad
            log(f"  {len(marka_ad)} marka bulundu, ürünlerle eşleştiriliyor...")
            harita = {}
            for i, (bid, ad) in enumerate(marka_ad.items(), 1):
                if i % 25 == 0:
                    log(f"  marka {i}/{len(marka_ad)}")
                for rows, *_ in self.api.sayfali("repository_stock_info", {"query[brandid][]": bid}):
                    for r in rows:
                        pid = str(alan(r, "repository_stock_info_pid", "product_id") or "")
                        if pid:
                            harita[pid] = ad
            harita["__id_ad__"] = marka_ad
            self.ob.kv_yaz("marka_harita", harita)
        id_ad = harita.get("__id_ad__", {})
        for x in stoklar:
            m = x["marka"]
            if m and str(m).isdigit():
                x["marka"] = id_ad.get(str(m))
            if not x["marka"]:
                x["marka"] = harita.get(x["pid"])
            x["marka"] = (str(x["marka"]).strip() if x["marka"] else "") or "MARKASIZ"
            if x["marka"].upper() in ("MARKASIZ", "(MARKASIZ)", "-"):
                x["marka"] = "MARKASIZ"

    # -- alış fiyatları
    def fiyatlar(self):
        if not self.a.alis_fiyati:
            return {}
        veri = self.ob.kv_al("alis_fiyatlari", self.a.onbellek_gun)
        if veri is None:
            log("Alış fiyatları çekiliyor (haftada bir yenilenir)...")
            veri = {}
            for rows, s, ss, _ in self.api.sayfali("repository_products"):
                if s == 1 or s % 50 == 0:
                    log(f"  sayfa {s}/{ss or '?'}")
                for r in rows:
                    bk = anahtar(alan(r, "repository_products_barcode", "barcode"))
                    fy = sayi(alan(r, "repository_products_price_purchase", "price_purchase"), 0)
                    if bk and fy > 0:
                        veri[bk] = fy
            self.ob.kv_yaz("alis_fiyatlari", veri)
        return veri

    # -- çıkış hareketleri (artımlı)
    def cikislari_guncelle(self):
        bugun = dt.date.today()
        bas = bugun - dt.timedelta(days=self.a.gecmis_gun)
        cekilmis = {r[0] for r in self.ob.db.execute("SELECT gun FROM cekilen_gunler")}
        eksik = []
        g = bas
        while g <= bugun:
            if g.isoformat() not in cekilmis or g == bugun:
                eksik.append(g)
            g += dt.timedelta(days=1)

        # ardışık günleri en fazla 31 günlük aralıklara grupla
        araliklar = []
        for g in eksik:
            if araliklar and (g - araliklar[-1][1]).days == 1 and (g - araliklar[-1][0]).days < 31:
                araliklar[-1][1] = g
            else:
                araliklar.append([g, g])

        if len(eksik) > 2:
            log(f"Stok çıkış hareketleri çekiliyor: {len(eksik)} gün, {len(araliklar)} parça "
                f"(ilk çalıştırmada uzun sürer, sonra sadece yeni günler çekilir)...")
        else:
            log("Stok çıkış hareketleri güncelleniyor (son günler)...")

        for g1, g2 in araliklar:
            filtre = f"{g1:%d/%m/%Y} 0:00 - {g2:%d/%m/%Y} 23:59"
            adet = 0
            for rows, s, ss, top in self.api.sayfali(
                "repository_stock_transactions",
                {"query[type][]": 2, "query[date_created]": filtre},
                ust_sinir=self.a.aralik_ust_sinir,
            ):
                if s == 1 and ss and ss > 5:
                    log(f"  {g1:%d.%m.%Y}–{g2:%d.%m.%Y}: {top} çıkış, {ss} sayfa")
                kayitlar = []
                for r in rows:
                    tip = str(alan(r, "repository_stock_transactions_type", "type") or "")
                    if tip not in ("2", "Stok Çıkışı", "Stock Out"):
                        continue
                    t = tarih(alan(r, "repository_stock_transactions_date_created", son_ek="date_created"))
                    if t and not (g1 <= t.date() <= g2):
                        continue  # filtre dışı kayıt (güvenlik)
                    kayitlar.append((
                        int(r.get("id")),
                        anahtar(alan(r, "products_product_id", "product_barcode", son_ek="product_id")),
                        anahtar(alan(r, "repository_shelves_shelf_id", son_ek="shelf_id")),
                        sayi(alan(r, "repository_stock_transactions_count", "count")),
                        str(alan(r, "repository_stock_transactions_missing", "missing") or "0"),
                        alan(r, "repository_users_user_created", son_ek="user_created") or "",
                        ts(t),
                    ))
                self.ob.db.executemany("INSERT OR REPLACE INTO cikislar VALUES(?,?,?,?,?,?,?)", kayitlar)
                adet += len(kayitlar)
            gun = g1
            while gun <= g2:
                if gun < bugun:
                    self.ob.db.execute("INSERT OR IGNORE INTO cekilen_gunler VALUES(?)", (gun.isoformat(),))
                gun += dt.timedelta(days=1)
            self.ob.db.commit()
        # pencerenin çok gerisinde kalan kayıtları temizle
        sinir = ts(dt.datetime.combine(bas - dt.timedelta(days=30), dt.time()))
        self.ob.db.execute("DELETE FROM cikislar WHERE tarih < ?", (sinir,))
        self.ob.db.execute("DELETE FROM cekilen_gunler WHERE gun < ?", ((bas - dt.timedelta(days=30)).isoformat(),))
        self.ob.db.commit()

    def son_hareketler(self):
        """Barkod -> {son_satis, son_satis_raf, son_duzeltme} (önbellekteki dönem için)."""
        bas = ts(dt.datetime.combine(dt.date.today() - dt.timedelta(days=self.a.gecmis_gun), dt.time()))
        sonuc = {}
        bilinmeyen_raf = set()
        for barkod, raf, miktar, kayip, tarih_s in self.ob.db.execute(
            "SELECT barkod, raf, miktar, kayip, tarih FROM cikislar WHERE tarih >= ? ORDER BY tarih", (bas,)
        ):
            d = sonuc.setdefault(barkod, {"son_satis": None, "son_satis_raf": None, "son_duzeltme": None})
            if not kayip_yok(kayip):
                d["son_duzeltme"] = tarih_s
                continue
            if miktar <= 0:
                continue
            if raf and raf not in self.raf_tipi:
                bilinmeyen_raf.add(raf)
            if self.satis_rafi_mi(raf):
                d["son_satis"] = tarih_s
                d["son_satis_raf"] = raf
        if bilinmeyen_raf:
            log(f"  Not: raf listesinde olmayan {len(bilinmeyen_raf)} raf kodu satış sayılmadı "
                f"(örn. {', '.join(sorted(bilinmeyen_raf)[:5])})")
        return sonuc

    # -- tek ürün geçmişi (dönem içinde hiç satış çıkışı olmayanlar için)
    def urun_gecmisi(self, pid):
        r = self.ob.db.execute("SELECT son_satis, son_satis_raf, son_giris_raf, ilk_gorulen, tamami, kontrol "
                               "FROM urun_gecmisi WHERE pid=?", (pid,)).fetchone()
        if r:
            # Kontrolden sonra olan her satış zaten toplu çıkış önbelleğinde (son gecmis_gun gün) görünür.
            # Toplu veride satış yoksa, kontrol tarihi bu pencerenin içinde kaldıkça kayıt geçerlidir.
            k = tarih(r[5])
            if k and (dt.datetime.now() - k).days < self.a.gecmis_gun - 1:
                return {"son_satis": r[0], "son_satis_raf": r[1], "son_giris_raf": r[2],
                        "ilk_gorulen": r[3], "tamami": bool(r[4])}
        son_satis = son_satis_raf = son_giris_raf = ilk = None
        tamami = False
        for rows, s, ss, top in self.api.sayfali(
            "repository_stock_transactions", {"query[product_id][]": pid}, en_fazla_sayfa=self.a.urun_gecmis_sayfa
        ):
            for row in rows:  # en yeniden eskiye
                tip = str(alan(row, "repository_stock_transactions_type", "type") or "")
                raf = anahtar(alan(row, "repository_shelves_shelf_id", son_ek="shelf_id"))
                t = alan(row, "repository_stock_transactions_date_created", son_ek="date_created")
                t = ts(tarih(t))
                ilk = t or ilk
                if (tip == "2" and son_satis is None and kayip_yok(alan(row, "repository_stock_transactions_missing"))
                        and sayi(alan(row, "repository_stock_transactions_count")) > 0 and self.satis_rafi_mi(raf)):
                    son_satis, son_satis_raf = t, raf
                if tip == "1" and son_giris_raf is None and self.satis_rafi_mi(raf):
                    son_giris_raf = raf
            if not ss or s >= ss:
                tamami = True
            if son_satis and son_giris_raf:
                break
        self.ob.db.execute("INSERT OR REPLACE INTO urun_gecmisi VALUES(?,?,?,?,?,?,?)",
                           (pid, son_satis, son_satis_raf, son_giris_raf, ilk, int(tamami), ts(dt.datetime.now())))
        self.ob.db.commit()
        return {"son_satis": son_satis, "son_satis_raf": son_satis_raf, "son_giris_raf": son_giris_raf,
                "ilk_gorulen": ilk, "tamami": tamami}


# --------------------------------------------------------------------------- #
#  Hesaplama
# --------------------------------------------------------------------------- #
def hesapla(top: Toplayici, stoklar, fiyatlar, hareket):
    a = top.a
    simdi = dt.datetime.now()
    en_kucuk = min(a.pencereler)
    sonuc = []
    gecmise_bakilacak = [x for x in stoklar if not (hareket.get(anahtar(x["barkod"])) or {}).get("son_satis")]
    if gecmise_bakilacak:
        log(f"Son {a.gecmis_gun} günde hiç satış çıkışı olmayan {len(gecmise_bakilacak)} ürünün geçmişine "
            f"bakılıyor (önbellekte olanlar tekrar sorgulanmaz)...")
    sayac = 0
    for x in stoklar:
        h = hareket.get(anahtar(x["barkod"])) or {}
        son_satis = h.get("son_satis")
        raf = h.get("son_satis_raf")
        referans, kaynak = son_satis, "satis"
        if not son_satis:
            sayac += 1
            if sayac % 100 == 0:
                log(f"  {sayac}/{len(gecmise_bakilacak)}")
            g = top.urun_gecmisi(x["pid"]) if x["pid"] else {}
            raf = g.get("son_satis_raf") or g.get("son_giris_raf")
            if g.get("son_satis"):
                referans, kaynak = g["son_satis"], "satis"
                son_satis = g["son_satis"]
            elif g.get("tamami") and g.get("ilk_gorulen"):
                referans, kaynak = g["ilk_gorulen"], "hic_satilmadi"  # depoya girdiğinden beri satılmamış
            else:
                referans, kaynak = None, "bilinmiyor"  # bakılan geçmişte hiç satış yok
        if referans:
            gun = (simdi - tarih(referans)).days
        else:
            gun = a.gecmis_gun  # en az bu kadar
        if gun < en_kucuk:
            continue
        duz = h.get("son_duzeltme")
        not_ = ""
        if duz and (not son_satis or duz > son_satis):
            not_ = "Sayım/düzeltme var, satış yok"
        fiyat = fiyatlar.get(anahtar(x["barkod"]), 0)
        sonuc.append({
            "b": x["barkod"], "k": x["stok_kodu"], "a": x["ad"], "m": x["marka"],
            "r": raf or "", "s": x["raf_stok"], "ad": x["ana_depo"],
            "t": (son_satis or "")[:10], "g": gun, "gp": kaynak != "bilinmiyor",
            "src": kaynak, "f": round(fiyat, 2), "v": round(fiyat * x["raf_stok"], 2), "n": not_,
        })
    sonuc.sort(key=lambda r: (-r["g"], -r["v"]))
    return sonuc


def veri_olustur(ayar: Ayarlar, satirlar, toplam_rafta, istek_sayisi, sure):
    return {
        "olusturma": dt.datetime.now().strftime("%d.%m.%Y %H:%M"),
        "firma": ayar.firma,
        "pencereler": ayar.pencereler,
        "gecmis_gun": ayar.gecmis_gun,
        "toplam_rafta": toplam_rafta,
        "fiyat_var": ayar.alis_fiyati,
        "istek": istek_sayisi,
        "sure": round(sure),
        "surum": SURUM,
        "satirlar": satirlar,
    }


def rapor_yaz(veri):
    with open(SABLON_DOSYASI, encoding="utf-8") as f:
        sablon = f.read()
    js = json.dumps(veri, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html_ = sablon.replace("/*__VERI__*/null", js)
    with open(RAPOR_DOSYASI, "w", encoding="utf-8") as f:
        f.write(html_)
    os.makedirs(ARSIV_KLASORU, exist_ok=True)
    arsiv = os.path.join(ARSIV_KLASORU, f"rapor_{dt.date.today():%Y-%m-%d}.html")
    shutil.copyfile(RAPOR_DOSYASI, arsiv)
    return RAPOR_DOSYASI


def rapor_uret(ayar: Ayarlar, api: "PobolApi", onbellek_yolu=ONBELLEK_DOSYASI):
    """Tüm veri toplama + hesaplama. Hem bilgisayarda hem bulutta (GitHub) kullanılır."""
    bas = time.time()
    ob = Onbellek(onbellek_yolu)
    top = Toplayici(api, ob, ayar)
    try:
        top.raflar()
        stoklar = top.stoklar()
        top.markalar(stoklar)
        fiyatlar = top.fiyatlar()
        top.cikislari_guncelle()
        hareket = top.son_hareketler()
        satirlar = hesapla(top, stoklar, fiyatlar, hareket)
    finally:
        ob.db.close()
    sure = time.time() - bas
    veri = veri_olustur(ayar, satirlar, len(stoklar), api.istek_sayisi, sure)
    ozet = ", ".join(f"{p}+ gün: {sum(1 for r in satirlar if r['g'] >= p)}" for p in ayar.pencereler)
    log(f"Rapor hazır ({api.istek_sayisi} istek, {sure:.0f} sn) → {ozet}")
    return veri


# --------------------------------------------------------------------------- #
#  Depo siparişleri (Sipariş Kontrol ekranı için)
# --------------------------------------------------------------------------- #
def depo_siparisleri(api: "PobolApi", gun=4):
    """Depoya düşmüş siparişlerin son `gun` günlük listesini döndürür (sadece okuma).

    Her satır: [kod, pazaryeri_sip_no, kanal, orijinal_kanal, durum, depo_durumu, tarih]
    Kişisel bilgi (isim, adres, telefon) alınmaz.
    """
    simdi = dt.datetime.now()
    bas = (simdi - dt.timedelta(days=gun - 1)).date()
    aralik = f"{bas:%d/%m/%Y} 0:00 - {simdi:%d/%m/%Y} 23:59"
    log(f"Depo siparişleri çekiliyor ({bas:%d.%m.%Y} – {simdi:%d.%m.%Y})...")
    liste, gorulen = [], set()
    for rows, s, ss, top in api.sayfali("orders", {"query[orderdate]": aralik}, ust_sinir=50000):
        for r in rows:
            kod = alan(r, "orders_ordercode", "ordercode")
            if not kod:
                continue
            kod = str(kod).strip()
            if kod in gorulen:
                continue
            gorulen.add(kod)
            t = tarih(alan(r, "orders_orderdate", "orderdate", son_ek="orderdate"))
            liste.append([
                kod,
                str(alan(r, "orders_market_place_order_id", "market_place_order_id") or ""),
                str(alan(r, "orders_type", "orders_flttype", "type") or ""),
                str(alan(r, "orders_market_place_type", "market_place_type") or ""),
                str(alan(r, "orders_status", "status") or ""),
                str(alan(r, "orders_substatus", "substatus") or ""),
                ts(t) or "",
            ])
    # Tarih filtresi çalışmadıysa (aralık dışı kayıt geldiyse) uyar ama listeyi aralıkla sınırla
    alt = f"{bas:%Y-%m-%d}"
    disarida = sum(1 for x in liste if x[6] and x[6] < alt)
    if disarida:
        log(f"  Uyarı: {disarida} sipariş tarih aralığı dışında geldi, ayıklandı.")
        liste = [x for x in liste if not x[6] or x[6] >= alt]
    log(f"  {len(liste)} depo siparişi")
    return {
        "cekim": simdi.strftime("%Y-%m-%d %H:%M:%S"),
        "bas": f"{bas:%Y-%m-%d}",
        "gun": gun,
        "sutunlar": ["kod", "pz_no", "kanal", "orj_kanal", "durum", "depo_durum", "tarih"],
        "liste": liste,
    }


# --------------------------------------------------------------------------- #
#  Bağlantı testi
# --------------------------------------------------------------------------- #
def baglanti_testi(api: PobolApi):
    api.giris()
    for model, sorgu in [
        ("repository_shelves", {}),
        ("repository_stock_info", {}),
        ("repository_stock_transactions", {"query[type][]": 2}),
        ("brands", {}),
        ("repository_products", {}),
    ]:
        try:
            js = api.get(model, {"pagination[page]": 1, "pagination[perpage]": 2, **sorgu})
            rows = api.satirlar(js)
            meta = js.get("meta") if isinstance(js, dict) else None
            print(f"\n[{model}]  toplam: {(meta or {}).get('total', '?')}  sayfa boyutu: {(meta or {}).get('perpage', '?')}")
            if rows:
                print("  alanlar:", ", ".join(rows[0].keys()))
            else:
                print("  (satır gelmedi) cevap:", json.dumps(js, ensure_ascii=False)[:300])
        except ApiHatasi as e:
            print(f"\n[{model}]  HATA: {e}")
    gun = dt.date.today()
    js = api.get("repository_stock_transactions", {
        "pagination[page]": 1, "pagination[perpage]": 1, "query[type][]": 2,
        "query[date_created]": f"{gun:%d/%m/%Y} 0:00 - {gun:%d/%m/%Y} 23:59"})
    print(f"\nTarih filtresi testi (bugün): {(js.get('meta') or {}).get('total', '?')} çıkış kaydı")
    print("\nBağlantı testi bitti. Hata yoksa 'python hareketsiz_stok.py' ile raporu üretebilirsiniz.")


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Hareketsiz raf stoğu raporu")
    ap.add_argument("--test", action="store_true", help="sadece bağlantı ve veri yapısı testi")
    ap.add_argument("--sessiz", action="store_true", help="raporu tarayıcıda açma")
    ap.add_argument("--onbellek-sil", action="store_true", help="önbelleği silip baştan çek")
    ap.add_argument("--ayar", default=AYAR_DOSYASI, help="ayar dosyası yolu")
    args = ap.parse_args()

    ayar = Ayarlar(args.ayar)
    api = PobolApi(ayar)

    if args.test:
        baglanti_testi(api)
        return

    if args.onbellek_sil and os.path.exists(ONBELLEK_DOSYASI):
        os.remove(ONBELLEK_DOSYASI)
        log("Önbellek silindi.")

    try:
        veri = rapor_uret(ayar, api)
    except ApiHatasi as e:
        log("HATA: " + str(e))
        sys.exit(1)
    yol = rapor_yaz(veri)
    log(yol)
    if not args.sessiz:
        webbrowser.open(pathlib.Path(yol).resolve().as_uri())


if __name__ == "__main__":
    main()
