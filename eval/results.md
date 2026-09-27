# DocFlow değerlendirme sonuçları

`python eval/evaluate.py` ile üretildi. Örnekler `samples/generate.py` ile sentetik olarak üretilir;
aynı fatura dört farklı kalitede işlenir.

| Kalite | Belge | Alan | Alan doğruluğu | İnsan kontrolüne giden | Sessiz hata | Yakalanan hata | Süre ort. | Süre p95 |
|---|---|---|---|---|---|---|---|---|
| Dijital PDF | 20 | 632 | %100.0 | %0.0 | %0.0 | %100.0 | 97 ms | 124 ms |
| Tarama (200 dpi) | 20 | 632 | %100.0 | %5.2 | %0.0 | %100.0 | 3937 ms | 4661 ms |
| Telefon fotoğrafı | 20 | 632 | %99.5 | %5.5 | %0.0 | %100.0 | 2999 ms | 3118 ms |
| Kötü fotoğraf | 20 | 632 | %83.1 | %29.6 | %0.0 | %100.0 | 2662 ms | 2988 ms |

- **Alan doğruluğu**: beklenen değerlerden birebir doğru çıkarılanların oranı (başlık alanları + kalem hücreleri).
- **İnsan kontrolüne giden**: sistemin emin olmadığı için kullanıcı onayına bıraktığı alanların oranı.
- **Sessiz hata**: yanlış olduğu halde otomatik kabul edilen alanların oranı. Hedef sıfıra yakın olması.
- **Yakalanan hata**: yanlış çıkarılan alanlardan kontrol listesine düşenlerin oranı.
- **Süre**: yükleme sonrası kuyruktan çıkıp alanların hazır olmasına kadar geçen süre (tek iş parçacığı).

Kalite ayrıntıları: Dijital PDF: başlık %100.0, kalem %100.0, doğru öneri 0, Tarama (200 dpi): başlık %100.0, kalem %100.0, doğru öneri 0, Telefon fotoğrafı: başlık %99.1, kalem %99.8, doğru öneri 0, Kötü fotoğraf: başlık %80.9, kalem %84.2, doğru öneri 3
