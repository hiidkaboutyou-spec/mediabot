# بات دانلودر رسانه تلگرام

نسخه آماده آپلود مستقیم در GitHub و استقرار با Docker.

## امکانات
- YouTube / Instagram / X: بررسی لینک، نمایش کیفیت‌ها، MP3 و کپشن
- Spotify: ترک، آلبوم و پلی‌لیست با spotDL
- محدودسازی دسترسی با ALLOWED_USER_IDS
- Health check روی PORT برای سرویس‌های ابری

## متغیرهای لازم
- BOT_TOKEN: الزامی
- ALLOWED_USER_IDS: اختیاری، آیدی‌های مجاز با کاما
- MAX_UPLOAD_MB: پیش‌فرض 49
- MAX_PLAYLIST_ITEMS: پیش‌فرض 30
- SPOTIFY_AUDIO_FORMAT: پیش‌فرض mp3
- SPOTIFY_BITRATE: پیش‌فرض 320k

توجه: spotDL فایل DRM شده اسپاتیفای را استخراج نمی‌کند؛ آهنگ مطابق را از منبع پشتیبانی‌شده پیدا می‌کند و متادیتای اسپاتیفای را اضافه می‌کند.
