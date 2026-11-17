# -*- coding: utf-8 -*-
"""
Göz İçi Lens (GİL) Stok ve Son Kullanma Tarihi Takibi.

İki depodaki lens stoğunu tek ekranda gösterir, minimum / kritik stok
uyarısı verir ve son kullanma tarihine (SKT) göre iade penceresini takip eder:
firmanın iade almayacağı lensleri zamanında yakalamak ve israfı önlemek için.
Yalnızca okuma (SELECT) yapar.

Sorgular HBYS'nin Oracle veritabanındaki HASTANE şemasına göre yazılmıştır.
Farklı bir HBYS'de tablo / kolon adlarını ve depo numaralarını uyarlayın.
"""
from datetime import datetime, timedelta

from flask import jsonify, request

import demo

SAYFA = {"baslik": "Lens Stok Takibi", "alt": "Göz içi lens stoğu ve son kullanma tarihi (SKT) takibi",
         "ikon": "👁️", "renk1": "#0d47a1", "renk2": "#00897b", "port": 5052}


# Lens stok takibi. Tarihten bağımsız, o anki stoğu gösterir.
# Kaynak: DEPO_DURUM (HBYS stok modülünün depo bazında tuttuğu güncel stok).
# Her lens tek satır; iki deponun stoğu yan yana. Stok durumu, iki deponun toplamının
# HBYS'de tanımlı MINIMUM_STOK / KRITIK_STOK toplamlarıyla karşılaştırılmasıdır.
LENS_ANA_DEPO = 16     # DEPOLAR tablosundaki depo numarası: kendi numaranızı yazın
LENS_AMELIYATHANE_DEPO = 109   # DEPOLAR tablosundaki depo numarası: kendi numaranızı yazın


LENS_STOK_SQL = f"""
SELECT D.MALZEME_NO,
       (SELECT MAX(M.ADI)    FROM HASTANE.MALZEME M WHERE M.MALZEME_NO = D.MALZEME_NO) LENS_ADI,
       NVL(D.ANA, 0) ANA_ADET,
       NVL(D.AMH, 0) AMH_ADET,
       NVL(D.ANA, 0) + NVL(D.AMH, 0) TOPLAM,
       -- iki deponun toplamı, iki deponun minimum / kritik stok tanımlarının toplamıyla karşılaştırılır
       CASE WHEN NVL(D.ANA, 0) + NVL(D.AMH, 0) < NVL(D.ANA_MIN, 0) + NVL(D.AMH_MIN, 0)
              OR NVL(D.ANA, 0) + NVL(D.AMH, 0) <= NVL(D.ANA_KRIT, 0) + NVL(D.AMH_KRIT, 0)
            THEN 'Kritik' ELSE 'Yeterli' END STOK_DURUMU
FROM (
  SELECT MALZEME_NO,
         SUM(CASE WHEN DEPO_NO = {LENS_ANA_DEPO}  THEN NVL(ADET_MIKTARI, 0) END) ANA,
         MAX(CASE WHEN DEPO_NO = {LENS_ANA_DEPO}  THEN MINIMUM_STOK END)        ANA_MIN,
         MAX(CASE WHEN DEPO_NO = {LENS_ANA_DEPO}  THEN KRITIK_STOK END)         ANA_KRIT,
         SUM(CASE WHEN DEPO_NO = {LENS_AMELIYATHANE_DEPO} THEN NVL(ADET_MIKTARI, 0) END) AMH,
         MAX(CASE WHEN DEPO_NO = {LENS_AMELIYATHANE_DEPO} THEN MINIMUM_STOK END)        AMH_MIN,
         MAX(CASE WHEN DEPO_NO = {LENS_AMELIYATHANE_DEPO} THEN KRITIK_STOK END)         AMH_KRIT
  FROM HASTANE.DEPO_DURUM
  WHERE DEPO_NO IN ({LENS_ANA_DEPO}, {LENS_AMELIYATHANE_DEPO})
  GROUP BY MALZEME_NO
) D"""


def _lens_sql(kosul):
    """Lens stok listesi; kosul: hangi satırların geleceği (ör. ANA_ADET > 0)."""
    return f"""
SELECT MALZEME_NO, LENS_ADI, ANA_ADET, AMH_ADET, TOPLAM, STOK_DURUMU
FROM ({LENS_STOK_SQL})
WHERE {kosul}
ORDER BY LENS_ADI"""


# ----------------------------------------------------------------------------
# LENS SON KULLANMA TARİHİ (SKT) TAKİBİ
# Firma, SKT'sine IADE_SON_AY aydan az kalmış lensi iade almıyor. Uyarı IADE_UYARI_AY
# ay kala başlar; aradaki süre "iade penceresi"dir (kullanılmayacaksa iade edilmeli).
# Depoda kalan her lens, karekod (yoksa seri no) bazında bulunur: bu yıl (yıl başı
# devri dahil) depoya giren ve o tarihten sonra o depodan çıkmamış karekodlar.
# HBYS stok modülündeki "Karekodları Göster" ekranıyla aynı mantık.
# ----------------------------------------------------------------------------
IADE_UYARI_AY = 9    # bu kadar ay kala uyarı başlar
IADE_SON_AY = 7      # bundan az kalmışsa firma iade almaz

_SKT_DURUM = f"""CASE WHEN K.SON_KULLANMA IS NULL THEN 'SKT yok'
            WHEN K.SON_KULLANMA < TRUNC(SYSDATE) THEN 'SKT geçmiş'
            WHEN MONTHS_BETWEEN(K.SON_KULLANMA, TRUNC(SYSDATE)) < {IADE_SON_AY} THEN 'İade süresi geçti'
            WHEN MONTHS_BETWEEN(K.SON_KULLANMA, TRUNC(SYSDATE)) < {IADE_UYARI_AY} THEN 'İade edilmeli'
            ELSE 'Normal' END"""

def _lens_skt_sql(ek=""):
    """Depoda kalan her lens (seri no bazında). ek: hareketlere ek koşul (ör. tek malzeme).
    NOT: WITH kullanılmaz; sorgu başka sorguların içine konduğunda eski Oracle sürümleri
    iç içe WITH'i kabul etmiyor (ORA-32034)."""
    # Bu yılın (yıl başı devri dahil) iki lens deposuna giren / çıkan hareketleri.
    # Tekil lens anahtarı: seri no (giriş ve çıkış satırlarının ikisinde de dolu);
    # seri no yoksa karekod. Çıkış satırlarında karekod çoğu zaman BOŞ yazılıyor.
    hareket = f"""(SELECT MALZEME_NO, ALAN_DEPO, GONDEREN_DEPO, ISLEM_TARIHI, SON_KULLANMA,
                SERI_NO, LOT_NO, KAREKOD, NVL(SERI_NO, KAREKOD) ANAHTAR
         FROM HASTANE.V_HAREKET
         WHERE ISLEM_TARIHI >= TRUNC(SYSDATE, 'YYYY')
           AND (ALAN_DEPO IN ({LENS_ANA_DEPO}, {LENS_AMELIYATHANE_DEPO})
                OR GONDEREN_DEPO IN ({LENS_ANA_DEPO}, {LENS_AMELIYATHANE_DEPO}))
           {ek})"""
    # Tek tek takip edilen lensler (seri no ya da karekod ile)
    birim = f"""
  -- Güvenlik: bir lensin bir depodaki SKT kaydı sayısı HBYS stoğunu (DEPO_DURUM) geçemez.
  -- Fazlası varsa en son girenler bırakılır; stoğu 0 olan lens hiç gelmez.
  SELECT S.MALZEME_NO, S.ALAN_DEPO, S.SERI_NO, S.LOT_NO, S.SON_KULLANMA FROM (
    SELECT G.*, ROW_NUMBER() OVER (PARTITION BY G.MALZEME_NO, G.ALAN_DEPO
                                   ORDER BY G.ISLEM_TARIHI DESC, G.SON_KULLANMA DESC) STOK_SIRA
    FROM (
      -- depoya girip o tarihten sonra o depodan çıkmamış lensler
      SELECT H.*, ROW_NUMBER() OVER (PARTITION BY H.MALZEME_NO, H.ALAN_DEPO, H.ANAHTAR
                                     ORDER BY H.ISLEM_TARIHI DESC) SIRA
      FROM {hareket} H
      WHERE H.ALAN_DEPO IN ({LENS_ANA_DEPO}, {LENS_AMELIYATHANE_DEPO})
        AND H.ANAHTAR IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM {hareket} C
                        WHERE C.GONDEREN_DEPO = H.ALAN_DEPO
                          AND C.MALZEME_NO    = H.MALZEME_NO
                          AND (C.SERI_NO = H.SERI_NO OR C.KAREKOD = H.KAREKOD)
                          AND C.ISLEM_TARIHI >= H.ISLEM_TARIHI)
    ) G
    WHERE G.SIRA = 1
  ) S
  JOIN (SELECT DEPO_NO, MALZEME_NO, SUM(NVL(ADET_MIKTARI, 0)) ADET
        FROM HASTANE.DEPO_DURUM
        WHERE DEPO_NO IN ({LENS_ANA_DEPO}, {LENS_AMELIYATHANE_DEPO})
        GROUP BY DEPO_NO, MALZEME_NO) D
    ON D.DEPO_NO = S.ALAN_DEPO AND D.MALZEME_NO = S.MALZEME_NO
  WHERE S.STOK_SIRA <= D.ADET"""
    # LOT ile gelen lensler: aynı lottan birden fazla lens aynı karekodu taşır ve
    # yukarıdaki sorgu bunları tek lens sayar; lottan biri çıkınca lotun tamamını
    # çıkmış sanar. Bu yüzden HBYS stoğunun (DEPO_DURUM) tek tek bulunan lenslerden
    # fazlası, o depoya bu yıl giren EN SON lotun SKT'si ile gösterilir.
    lot_ek = f"""
  SELECT D.MALZEME_NO, D.DEPO_NO ALAN_DEPO, CAST(NULL AS VARCHAR2(200)) SERI_NO,
         L.LOT_NO, L.SON_KULLANMA
  FROM (SELECT DEPO_NO, MALZEME_NO, SUM(NVL(ADET_MIKTARI, 0)) ADET
        FROM HASTANE.DEPO_DURUM
        WHERE DEPO_NO IN ({LENS_ANA_DEPO}, {LENS_AMELIYATHANE_DEPO})
          {ek}
        GROUP BY DEPO_NO, MALZEME_NO) D
  JOIN (SELECT MALZEME_NO, ALAN_DEPO, LOT_NO, SON_KULLANMA,
               ROW_NUMBER() OVER (PARTITION BY MALZEME_NO, ALAN_DEPO
                                  ORDER BY ISLEM_TARIHI DESC) RN
        FROM {hareket}
        WHERE SERI_NO IS NULL AND LOT_NO IS NOT NULL
          AND ALAN_DEPO IN ({LENS_ANA_DEPO}, {LENS_AMELIYATHANE_DEPO})) L
    ON L.MALZEME_NO = D.MALZEME_NO AND L.ALAN_DEPO = D.DEPO_NO AND L.RN = 1
  LEFT JOIN (SELECT MALZEME_NO, ALAN_DEPO, COUNT(*) N
             FROM ({birim})
             GROUP BY MALZEME_NO, ALAN_DEPO) B
    ON B.MALZEME_NO = D.MALZEME_NO AND B.ALAN_DEPO = D.DEPO_NO
  JOIN (SELECT LEVEL N FROM DUAL CONNECT BY LEVEL <= 500) SAY
    ON SAY.N <= D.ADET - NVL(B.N, 0)"""
    return f"""
SELECT K.MALZEME_NO,
       (SELECT MAX(M.ADI) FROM HASTANE.MALZEME M WHERE M.MALZEME_NO = K.MALZEME_NO) LENS_ADI,
       CASE K.ALAN_DEPO WHEN {LENS_ANA_DEPO} THEN 'Lens Ana Depo' ELSE 'Ameliyathane Lens Depo' END DEPO,
       NVL(K.SERI_NO, 'Lot ' || K.LOT_NO) SERI_NO,
       K.SON_KULLANMA,
       FLOOR(MONTHS_BETWEEN(TRUNC(K.SON_KULLANMA), TRUNC(SYSDATE))) KALAN_AY,  -- tam ay (geçmişse eksi)
       TRUNC(K.SON_KULLANMA) - TRUNC(SYSDATE) KALAN_GUN,   -- son kullanmaya kalan gün (geçmişse eksi)
       {_SKT_DURUM} SKT_DURUMU
FROM (
{birim}
  UNION ALL
{lot_ek}
) K"""


LENS_SKT_SQL = _lens_skt_sql()

# Lens Stok listesinde bir lense tıklayınca açılan seri no / SKT ayrıntısı
LENS_DETAY_SQL = f"""
SELECT DEPO, SERI_NO, SON_KULLANMA, KALAN_AY, KALAN_GUN, SKT_DURUMU
FROM ({_lens_skt_sql("AND MALZEME_NO = :mno")})
ORDER BY DEPO, SON_KULLANMA NULLS LAST"""
LENS_DETAY_KOLONLAR = [("Depo", "metin"), ("Seri No", "metin"), ("Son Kullanma", "gun"),
                       ("Kalan (Ay)", "kalan_ay"), ("Kalan (Gün)", "kalan_gun"), ("SKT Durumu", "skt")]


def _skt_sql(kosul):
    return f"""
SELECT LENS_ADI, DEPO, SERI_NO, SON_KULLANMA, KALAN_AY, KALAN_GUN, SKT_DURUMU
FROM ({LENS_SKT_SQL})
WHERE {kosul}
ORDER BY SON_KULLANMA NULLS LAST, LENS_ADI"""


# Lens Stok kutuları için SKT sayıları (sıra: iade penceresi, iade süresi geçti, SKT geçmiş, tümü)
# SKT'si bilinmeyen lensler: HBYS stoğu (DEPO_DURUM), SKT'si bilinen adetten fazlaysa
# aradaki fark. Sebep: girişte seri no / karekod ya da SKT girilmemiş.
LENS_SKTSIZ_SQL = f"""
SELECT D.MALZEME_NO,
       (SELECT MAX(M.ADI) FROM HASTANE.MALZEME M WHERE M.MALZEME_NO = D.MALZEME_NO) LENS_ADI,
       D.DEPO,
       D.STOK,
       NVL(S.SKTLI, 0) SKTLI,
       D.STOK - NVL(S.SKTLI, 0) EKSIK
FROM (SELECT MALZEME_NO,
             CASE DEPO_NO WHEN {LENS_ANA_DEPO} THEN 'Lens Ana Depo' ELSE 'Ameliyathane Lens Depo' END DEPO,
             SUM(NVL(ADET_MIKTARI, 0)) STOK
      FROM HASTANE.DEPO_DURUM
      WHERE DEPO_NO IN ({LENS_ANA_DEPO}, {LENS_AMELIYATHANE_DEPO})
      GROUP BY MALZEME_NO, DEPO_NO
      HAVING SUM(NVL(ADET_MIKTARI, 0)) > 0) D
LEFT JOIN (SELECT MALZEME_NO, DEPO, COUNT(SON_KULLANMA) SKTLI
           FROM ({LENS_SKT_SQL})
           GROUP BY MALZEME_NO, DEPO) S
  ON S.MALZEME_NO = D.MALZEME_NO AND S.DEPO = D.DEPO
WHERE D.STOK - NVL(S.SKTLI, 0) > 0
ORDER BY LENS_ADI, D.DEPO"""

# Lens Stok kutuları için SKT sayıları
# (sıra: iade edilmeli, iade süresi geçti, SKT geçmiş, tümü, SKT girilmemiş adet)
# NOT: iki tek satırlık sorgu yan yana birleştirilir; COUNT'ların yanına alt sorgu
# yazmak Oracle'da ORA-00937 hatası verir.
LENS_SKT_TOPLAM_SQL = f"""
SELECT A.IADE, A.GECTI, A.GECMIS, A.TUM, B.EKSIK
FROM (SELECT COUNT(CASE WHEN SKT_DURUMU = 'İade edilmeli' THEN 1 END)     IADE,
             COUNT(CASE WHEN SKT_DURUMU = 'İade süresi geçti' THEN 1 END) GECTI,
             COUNT(CASE WHEN SKT_DURUMU = 'SKT geçmiş' THEN 1 END)        GECMIS,
             COUNT(*)                                                     TUM
      FROM ({LENS_SKT_SQL})) A,
     (SELECT NVL(SUM(EKSIK), 0) EKSIK
      FROM ({LENS_SKTSIZ_SQL})) B"""

LENS_SKT_KOLONLAR = [
    ("Lens Adı", "metin", 42), ("Depo", "metin", 16), ("Seri No", "metin", 16),
    ("Son Kullanma", "gun", 13), ("Kalan (Ay)", "kalan_ay", 11), ("Kalan (Gün)", "kalan_gun", 12),
    ("SKT Durumu", "skt", 18),
]


# Ana liste: iki depodan en az birinde stoğu olan lensler (stoğu sıfır olanlar gelmez)

RAPORLAR = {
    "lens_stok": {
        "renk": "#1565c0", "ikon": "👁️",
        "menu": "Lens Stok",
        "alt": "Lens Ana Depo ve Ameliyathane Lens Depo",
        "aciklama": "<b>Lens Ana Depo</b> ve <b>Ameliyathane Lens Depo</b>'daki tüm lenslerin <b>şu anki</b> stoğu gösterilir "
                    "(HBYS stok modülüyle aynı kaynak). <b>Stok Durumu</b>, iki deponun <b>toplam</b> adedine göre belirlenir: "
                    "toplam, HBYS'de iki depo için tanımlı <b>minimum</b> ya da <b>kritik stok</b> değerlerinin toplamına ulaşmıyorsa "
                    "<b>Kritik</b>, ulaşıyorsa <b>Yeterli</b>. "
                    "İki depoda da stoğu olmayan lensler listelenmez. "
                    f"<br><b>Son kullanma takibi:</b> firma SKT'sine <b>{IADE_SON_AY} aydan az</b> kalmış lensi iade almaz. "
                    f"SKT'sine <b>{IADE_SON_AY}-{IADE_UYARI_AY} ay</b> kalan lensler <b>İade edilmeli</b> kutusunda görünür: "
                    "yakın ameliyatlarda kullanılmayacaksa <b>hemen iade edin</b>. <b>İade süresi geçti</b> kutusundakileri ameliyatlarda "
                    "<b>öncelikle kullanın</b>. "
                    "Lens adı ya da dioptri ile aramak için <b>LENS ARA</b> kutusunu kullanın (ör. <i>SA60AT +22</i>). "
                    "<b>Kontrol edilecek lens</b> kutusunda sayı varsa: lens rafta sayılıp HBYS kaydıyla karşılaştırılmalı "
                    "(SKT girilmemiş ya da iade / kullanım HBYS'den düşülmemiş olabilir).",
        "tarih_etiketi": "",
        "tarihsiz": True,
        "sayac_gizle": True,
        "ara_etiketi": "🔍 LENS ARA",       # kompakt düzen: arama solda, Excel/Yazdır hemen yanında
        "ara_hiza": 1,                      # arama kutusu bu sütunun (Lens Adı) hizasında durur
        "ara_ipucu": "Lens adı ya da dioptri (ör. SA60AT +22)",
        "ozet": {"tur": "lens"},
        "toplam_sql": LENS_SKT_TOPLAM_SQL,
        "detay": "lens",                    # satıra tıklayınca seri no / SKT ayrıntısı açılır
        "sql": _lens_sql("TOPLAM > 0"),
        "kolonlar": [
            ("Malzeme Kodu", "kod", 12), ("Lens Adı", "metin", 42),
            ("Lens Ana Depo", "adet", 14),
            ("Ameliyathane Lens Depo", "adet", 15), ("Toplam", "adet", 10),
            ("Stok Durumu", "stok", 14),
        ],
    },
}

ALT_RAPORLAR = {}
# Lens Stok'taki depo kutularına tıklayınca açılan listeler (sadece o depoda stoğu olanlar)
# SKT kutularına tıklayınca açılan listeler (karekod bazında, SKT'si en yakın en üstte)
for _kod, _kosul, _alt in (
        ("lens_skt_iade", "SKT_DURUMU = 'İade edilmeli'", f"İade edilmeli: SKT'sine {IADE_SON_AY}-{IADE_UYARI_AY} ay kalan lensler"),
        ("lens_skt_gecti", "SKT_DURUMU = 'İade süresi geçti'", f"İade süresi geçti: SKT'sine {IADE_SON_AY} aydan az kalan lensler"),
        ("lens_skt_gecmis", "SKT_DURUMU = 'SKT geçmiş'", "SKT'si geçmiş lensler"),
        ("lens_skt_tum", "1 = 1", "Tüm lensler, son kullanma tarihine göre")):
    ALT_RAPORLAR[_kod] = {
        "menu": "Lens Stok", "alt": _alt, "tarih_etiketi": "", "tarihsiz": True,
        "sql": _skt_sql(_kosul), "kolonlar": LENS_SKT_KOLONLAR,
    }
ALT_RAPORLAR["lens_sktsiz"] = {
    "menu": "Lens Stok",
    "alt": "Kontrol edilecek lensler: SKT yok ya da stok kaydı tutmuyor (rafta sayıp HBYS ile karşılaştırın)",
    "tarih_etiketi": "", "tarihsiz": True, "detay": "lens",
    "sql": LENS_SKTSIZ_SQL,
    "kolonlar": [
        ("Malzeme Kodu", "kod", 12), ("Lens Adı", "metin", 42), ("Depo", "metin", 16),
        ("Stok", "adet", 8), ("SKT'si Bilinen", "adet", 13), ("Kontrol Edilecek", "adet", 15),
    ],
}
for _kod, _kosul, _alt in (("lens_ana", "ANA_ADET > 0", "Lens Ana Depo'daki lensler"),
                           ("lens_amh", "AMH_ADET > 0", "Ameliyathane Lens Depo'daki lensler")):
    ALT_RAPORLAR[_kod] = {
        "menu": "Lens Stok", "alt": _alt, "tarih_etiketi": "", "tarihsiz": True,
        "sql": _lens_sql(_kosul), "detay": "lens",
        "kolonlar": RAPORLAR["lens_stok"]["kolonlar"],
    }

def _skt_kutulari(toplam):
    """Lens Stok'taki SKT kutuları (LENS_SKT_TOPLAM_SQL sırası)."""
    iade, gecti, gecmis, tum, sktsiz = toplam or (0, 0, 0, 0, 0)
    yazi = "LENSLERİ GÖRMEK İÇİN TIKLAYIN"
    return [
        {"ad": f"İade edilmeli ({IADE_SON_AY}-{IADE_UYARI_AY} ay kaldı)", "deger": iade,
         "alt": "lens_skt_iade", "alt_ad": "İade edilmesi gereken lensler", "alt_yazi": yazi,
         "renk": "#b26a00", "renk_acik": "#fff8e1"},
        {"ad": f"İade süresi geçti (<{IADE_SON_AY} ay)", "deger": gecti,
         "alt": "lens_skt_gecti", "alt_ad": "İade süresi geçmiş lensler", "alt_yazi": yazi,
         "renk": "#e65100", "renk_acik": "#fff3e0"},
        {"ad": "SKT geçmiş", "deger": gecmis,
         "alt": "lens_skt_gecmis", "alt_ad": "SKT'si geçmiş lensler", "alt_yazi": yazi,
         "renk": "#c62828", "renk_acik": "#ffebee"},
        {"ad": "SKT listesi (tüm lensler)", "deger": tum,
         "alt": "lens_skt_tum", "alt_ad": "Tüm lensler, SKT'ye göre", "alt_yazi": yazi,
         "renk": "#1565c0", "renk_acik": "#e3f2fd"},
        {"ad": "Kontrol edilecek lens (adet)", "not": "SKT yok - stok kaydı tutmuyor", "deger": sktsiz,
         "alt": "lens_sktsiz", "alt_ad": "Kontrol edilecek lensler", "alt_yazi": yazi,
         "renk": "#455a64", "renk_acik": "#eceff1"},
    ]



def ozet_hesapla(rapor, satirlar, toplam=None):
    """Lens Stok'un üstündeki kutular: depo adetleri ve SKT kutuları."""
    oz = rapor.get("ozet")
    if not oz or oz["tur"] != "lens":
        return None
    # kolon sırası: 2 ana depo adet, 3 ameliyathane depo adet. Kutuya tıklayınca o deponun lensleri açılır.
    return [
        {"ad": "Lens Ana Depo (adet)", "deger": sum(int(r[2] or 0) for r in satirlar),
         "alt": "lens_ana", "alt_ad": "Lens Ana Depo'daki lensler", "alt_yazi": "LENSLERİ GÖRMEK İÇİN TIKLAYIN",
         "renk": "#2e7d32", "renk_acik": "#e8f5e9"},
        {"ad": "Ameliyathane Lens Depo (adet)", "deger": sum(int(r[3] or 0) for r in satirlar),
         "alt": "lens_amh", "alt_ad": "Ameliyathane Lens Depo'daki lensler", "alt_yazi": "LENSLERİ GÖRMEK İÇİN TIKLAYIN",
         "renk": "#6a1b9a", "renk_acik": "#f3e5f5"},
    ] + _skt_kutulari(toplam)


def rotalar_ekle(app, sorgula, deger_yaz, demo_mu):
    @app.route("/api/lens_detay")
    def lens_detay():
        """Bir lensin depodaki adetleri tek tek: depo, seri no, SKT."""
        try:
            mno = int(request.args["malzeme_no"])
        except (KeyError, ValueError):
            return jsonify(hata="Malzeme kodu geçersiz."), 400
        satirlar = _demo_detay(mno) if demo_mu() else sorgula(LENS_DETAY_SQL, mno=mno)
        satirlar = [[deger_yaz(v, LENS_DETAY_KOLONLAR[i][1]) for i, v in enumerate(r)] for r in satirlar]
        return jsonify(kolonlar=[{"ad": a, "tip": t} for a, t in LENS_DETAY_KOLONLAR], satirlar=satirlar)


# ----------------------------------------------------------------------------
# DEMO VERİSİ (DEMO=1 ile veritabanısız çalıştırma)
# ----------------------------------------------------------------------------
_DEMO_MODELLER = ["Hidrofobik Akrilik Monofokal", "Hidrofobik Akrilik Torik T3", "Trifokal Difraktif",
                  "EDOF Monofokal+", "Hidrofilik Akrilik 3 Parça", "Sulkus Lensi"]
_DEPOLAR = ["Lens Ana Depo", "Ameliyathane Lens Depo"]


def _demo_stok():
    r = demo.tohum("lens-stok")
    satirlar = []
    for i, model in enumerate(_DEMO_MODELLER):
        for dp in (18.0, 19.5, 20.5, 21.0, 21.5, 22.0, 23.0, 24.5):
            if r.random() < .35:
                continue
            ana, amh = r.choice([0, 1, 2, 3, 4, 6]), r.choice([0, 0, 1, 1, 2])
            if ana + amh == 0:
                continue
            satirlar.append([40000 + i * 100 + int(dp * 2), f"{model} {dp:+.1f} D", ana, amh, ana + amh,
                             "Kritik" if ana + amh <= 1 else "Yeterli"])
    return sorted(satirlar, key=lambda s: s[1])


def _demo_tekil():
    """Her lens tek tek: (malzeme no, ad, depo, seri no, SKT)."""
    r = demo.tohum("lens-skt")
    bugun = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    out = []
    for m in _demo_stok():
        for depo, adet in ((_DEPOLAR[0], m[2]), (_DEPOLAR[1], m[3])):
            for _ in range(adet):
                gun = r.choice([r.randint(-20, 0), r.randint(30, 200), r.randint(215, 270)] + [r.randint(300, 900)] * 12)
                out.append([m[0], m[1], depo, f"SN{demo.numara(r, 10000000, 99999999)}", bugun + timedelta(days=gun)])
    return out


def _skt_satiri(s):
    bugun = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    kalan_gun = (s[4] - bugun).days
    kalan_ay = round(kalan_gun / 30.44, 1)
    durum = ("SKT geçmiş" if kalan_gun < 0 else "İade süresi geçti" if kalan_ay < IADE_SON_AY
             else "İade edilmeli" if kalan_ay < IADE_UYARI_AY else "Normal")
    return [s[1], s[2], s[3], s[4], kalan_ay, kalan_gun, durum]


def _demo_detay(mno):
    return [_skt_satiri(s)[1:] for s in _demo_tekil() if s[0] == mno]


def demo_veri(kod, tar1, tar2):
    stok = _demo_stok()
    skt = sorted((_skt_satiri(s) for s in _demo_tekil()), key=lambda x: x[3])
    sayi = lambda d: sum(1 for x in skt if x[6] == d)
    toplam = [sayi("İade edilmeli"), sayi("İade süresi geçti"), sayi("SKT geçmiş"), len(skt), 0]
    if kod == "lens_stok":
        return stok, toplam
    if kod == "lens_ana":
        return [s for s in stok if s[2] > 0], None
    if kod == "lens_amh":
        return [s for s in stok if s[3] > 0], None
    if kod == "lens_sktsiz":
        return [], None
    secim = {"lens_skt_iade": "İade edilmeli", "lens_skt_gecti": "İade süresi geçti", "lens_skt_gecmis": "SKT geçmiş"}
    return ([x for x in skt if x[6] == secim[kod]] if kod in secim else skt), None
