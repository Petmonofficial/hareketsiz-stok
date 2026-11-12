#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Actions üzerinde çalışan sarmalayıcı.

- Önceki çalıştırmanın şifreli önbelleğini (onbellek.enc) açar,
- hareketsiz_stok.rapor_uret() ile Pobol'dan veriyi çeker,
- Raporu, web sayfasının kullanacağı erişim anahtarını ve önbelleği
  RAPOR_SIFRESI ile AES-256-GCM şifreleyip "cikti/" klasörüne yazar.

Gerekli ortam değişkenleri (GitHub > Settings > Secrets and variables > Actions):
  POBOL_KULLANICI, POBOL_SIFRE, RAPOR_SIFRESI, WEB_ERISIM_TOKEN
İsteğe bağlı: POBOL_ADRES (varsayılan https://depo.yemgross.com.tr/api)
GitHub kendisi verir: GITHUB_REPOSITORY (sahip/depo)
"""
import base64
import gzip
import hashlib
import json
import os
import sys

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.exceptions import InvalidTag

KOD = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, KOD)
import hareketsiz_stok as hs  # noqa: E402

TEKRAR = 600_000  # PBKDF2 tekrar sayısı (web tarafıyla aynı olmalı)


def tuz(depo: str) -> bytes:
    # Depoya özgü sabit tuz: web sayfası aynı tuzu sahip/depo adından üretir.
    return hashlib.sha256(("hareketsiz-stok:" + depo.lower()).encode()).digest()[:16]


def anahtar_turet(sifre: str, depo: str) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=tuz(depo), iterations=TEKRAR)
    return kdf.derive(sifre.encode("utf-8"))


def b64(b): return base64.b64encode(b).decode()


def sifrele(veri: bytes, key: bytes, depo: str) -> str:
    nonce = os.urandom(12)
    c = AESGCM(key).encrypt(nonce, gzip.compress(veri, 9), None)
    return json.dumps({"v": 1, "s": b64(tuz(depo)), "i": TEKRAR, "n": b64(nonce), "c": b64(c)})


def coz(metin: str, key: bytes) -> bytes:
    o = json.loads(metin)
    return gzip.decompress(AESGCM(key).decrypt(base64.b64decode(o["n"]), base64.b64decode(o["c"]), None))


def gerekli(ad):
    v = (os.environ.get(ad) or "").strip()
    if not v:
        sys.exit(f"HATA: '{ad}' gizli ayarı (secret) tanımlı değil. GitHub > Settings > Secrets and variables > Actions")
    return v


def onceki(ad):
    """Önceki çalıştırmanın 'veri' dalındaki dosyası (workflow kod/onceki/ altına koyar)."""
    yol = os.path.join(KOD, "onceki", ad)
    if os.path.exists(yol) and os.path.getsize(yol) > 0:
        with open(yol, encoding="utf-8") as f:
            return f.read()
    return None


def main():
    depo = gerekli("GITHUB_REPOSITORY")
    sifre = gerekli("RAPOR_SIFRESI")
    token = gerekli("WEB_ERISIM_TOKEN")
    mod = (os.environ.get("BULUT_MOD") or "tam").strip().lower()
    if mod not in ("tam", "siparis"):
        mod = "tam"
    if len(sifre) < 12:
        sys.exit("HATA: RAPOR_SIFRESI en az 12 karakter olmalı (şifreli dosyalar herkese açık depoda duruyor).")

    # Programın ayar dosyası gizli değerlerden oluşturulur (depoda şifre YOK)
    ayar_yolu = os.path.join(KOD, "ayarlar_bulut.ini")
    with open(ayar_yolu, "w", encoding="utf-8") as f:
        f.write("[baglanti]\n")
        f.write(f"adres = {os.environ.get('POBOL_ADRES') or 'https://depo.yemgross.com.tr/api'}\n")
        f.write("kullanici = \nsifre = \n")  # ortam değişkeninden okunur (POBOL_KULLANICI/POBOL_SIFRE)
        f.write("[rapor]\npencereler = 14,30,180\ngecmis_gun = 200\nsatis_raf_tipleri = 1\n")
        f.write(f"haric_raflar = {os.environ.get('HARIC_RAFLAR', '')}\nalis_fiyati = evet\nbaslik = YemGross Depo\n")
        f.write("[performans]\nsayfa_boyutu = 100\nistek_arasi_bekleme = 0.05\nurun_gecmis_sayfa = 3\nonbellek_gun = 7\n")
    gerekli("POBOL_KULLANICI"); gerekli("POBOL_SIFRE")

    hs.log(f"Mod: {'TAM (stok raporu + siparişler)' if mod == 'tam' else 'SADECE DEPO SİPARİŞLERİ'}")
    hs.log("Şifre anahtarı hazırlanıyor...")
    key = anahtar_turet(sifre, depo)

    ayar = hs.Ayarlar(ayar_yolu)
    api = hs.PobolApi(ayar)

    cikti = os.path.join(os.getcwd(), "cikti")
    os.makedirs(cikti, exist_ok=True)
    yaz = lambda ad, metin: open(os.path.join(cikti, ad), "w", encoding="utf-8").write(metin)

    def onceki_dosyayi_koru(ad):
        eski = onceki(ad)
        if eski:
            yaz(ad, eski)
            return True
        return False

    if mod == "tam":
        db_yolu = os.path.join(KOD, "onbellek.db")
        eski_onbellek = onceki("onbellek.enc")
        if eski_onbellek:
            try:
                acik = coz(eski_onbellek, key)
                with open(db_yolu, "wb") as f:
                    f.write(acik)
                hs.log(f"Önceki önbellek açıldı ({len(acik) / 1e6:.1f} MB) — sadece yeni günler çekilecek.")
            except (InvalidTag, ValueError, KeyError, OSError) as e:
                hs.log(f"Önbellek açılamadı (şifre değişmiş olabilir: {type(e).__name__}). Baştan çekilecek; uzun sürer.")
                if os.path.exists(db_yolu):
                    os.remove(db_yolu)
        else:
            hs.log("Önbellek yok — ilk çalıştırma, son 200 günün verisi çekilecek (uzun sürer).")

        try:
            veri = hs.rapor_uret(ayar, api, onbellek_yolu=db_yolu)
        except hs.ApiHatasi as e:
            sys.exit("HATA: " + str(e))
        yaz("rapor.enc", sifrele(json.dumps(veri, ensure_ascii=False).encode("utf-8"), key, depo))
        with open(db_yolu, "rb") as f:
            yaz("onbellek.enc", sifrele(f.read(), key, depo))
    else:
        # Stok raporu ve önbelleğe dokunma: önceki dosyalar aynen kalsın
        for ad in ("rapor.enc", "onbellek.enc"):
            if not onceki_dosyayi_koru(ad):
                hs.log(f"  Not: önceki {ad} yok (henüz tam güncelleme yapılmamış olabilir).")

    # Depo siparişleri sadece 'siparis' modunda denenir (tam güncelleme eskisi gibi yalnızca stok raporu).
    if mod == "siparis":
        try:
            sip = hs.depo_siparisleri(api, gun=int(os.environ.get("SIPARIS_GUN") or 4))
            yaz("siparis.enc", sifrele(json.dumps(sip, ensure_ascii=False).encode("utf-8"), key, depo))
        except hs.ApiHatasi as e:
            sys.exit("HATA: depo siparişleri çekilemedi: " + str(e))

    yaz("erisim.enc", sifrele(json.dumps({"token": token}).encode("utf-8"), key, depo))
    yaz("README.md", "Bu dal otomatik oluşturulur. Dosyalar şifrelidir; elle değiştirmeyin.\n")
    hs.log("Şifreli dosyalar hazır.")


if __name__ == "__main__":
    main()
