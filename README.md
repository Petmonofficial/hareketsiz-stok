# Hareketsiz Raf Stoğu

Pobol depo verisinden, raflarda stoğu olup belirli süredir satış çıkışı görmeyen ürünleri listeler.

- **Rapor sayfası:** bu deponun GitHub Pages adresi. Tek şifreyle girilir.
- **Güncelleme:** sayfadaki "Güncelle" butonu ya da Actions sekmesi > "Raporu güncelle" > Run workflow.
- **Güvenlik:** Bu depoda hiçbir şifre ya da düz veri yoktur. Pobol bilgileri ve rapor şifresi
  GitHub "Secrets" içinde durur; rapor `veri` dalında AES-256 ile şifrelenmiş olarak saklanır.

Gerekli Secrets: `POBOL_KULLANICI`, `POBOL_SIFRE`, `RAPOR_SIFRESI` (en az 12 karakter), `WEB_ERISIM_TOKEN`.

Kural: Sadece gerçek depo rafından (raf tipi 1), kayıp sebebi olmayan çıkış "satış" sayılır.
Sayım / stok düzeltme / personel sepeti / kovan / hasarlı-SKT rafı hareketleri sayılmaz.
