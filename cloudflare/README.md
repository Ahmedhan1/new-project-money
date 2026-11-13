# NEXUS Cloud — لوحة تحكم على Cloudflare، والشغل التقيل على Kaggle

**النسخة الشغالة:** <https://nexus-cloud.nexus-ahmed.workers.dev>

```
المتصفح (موبايل/كمبيوتر)
      │ HTTPS + كلمة سر المالك
      ▼
Cloudflare Worker (مجاني)  ── Durable Object (SQLite): سجل الشغلانات + مفتاح Kaggle متشفّر
      │  Cron كل 5 دقايق: يسأل Kaggle عن الشغلانات
      ▼
Kaggle API  ──►  نوتبوك خاص (Private) على GPU T4 ×2 مجاني  ──►  result.json + artifacts/
```

- **Cloudflare** = المكان اللي بتشغّل منه: تسجيل دخول، صفحة إعدادات فيها مكان مفتاح Kaggle السري، تبعت شغلانات، تتابعها، وتحمّل النتايج.
- **Kaggle** = كل الحاجات التقيلة: الفيديو، الـ LLM، توليد الفيديو (Wan)، التحويل. بيتشغل نفس الـ worker بتاع NEXUS
  (`nexus/cloud/worker_kit`) من غير أي تغيير.
- **مجاني**: خطة Workers المجانية + Durable Object بـ SQLite + Cron، من غير كارت ومن غير قاعدة بيانات منفصلة. مفيش R2 ولا أي خدمة محتاجة وسيلة دفع. حساب Kaggle واحد بس.

## النشر (مرة واحدة)

### الطريقة 1 — من لوحة Cloudflare (من غير أي أوامر)
1. اعمل حساب مجاني على <https://dash.cloudflare.com/sign-up> (مش محتاج كارت).
2. **Workers & Pages** ← **Create** ← **Import a repository** ← اربط GitHub واختار `new-project-money`.
3. في إعدادات البناء:
   - **Root directory**: `cloudflare`
   - **Deploy command**: `npm run deploy`
4. اضغط **Deploy**. التخزين (Durable Object بـ SQLite) والجداول بيتعملوا لوحدهم.
5. بعد ما يخلص: **Settings** ← **Variables and Secrets** ← **Add** ← النوع **Secret**:
   - الاسم `OWNER_PASSWORD`، والقيمة كلمة سر طويلة (12 حرف على الأقل). دي كلمة سر الدخول للوحة.
6. افتح الرابط `https://nexus-cloud.<اسمك>.workers.dev` ← ادخل بكلمة السر ← **الإعدادات** ← الصق مفتاح Kaggle ← **اختبر واحفظ**.

### الطريقة 2 — من الترمينال
```bash
cd cloudflare
npm install
npx wrangler login
npm run deploy
npx wrangler secret put OWNER_PASSWORD
```

## مفتاح Kaggle — فين بيتحط؟
- من **kaggle.com ← Settings ← API ← Generate New Token**. تقدر تلصق التوكن (`KGAT_...`) أو محتوى `kaggle.json` كله.
- **في صفحة الإعدادات في اللوحة** (الأسهل): بيتجرب على Kaggle الأول، وبعدين بيتحفظ متشفّر AES-256-GCM في تخزين الـ Durable Object، بمفتاح مشتق
  من `OWNER_PASSWORD`. عمره ما بيرجع للمتصفح ولا بيظهر في اللوج. لو غيّرت كلمة السر لازم تدخّل المفتاح تاني.
- **أو كـ Cloudflare Secret** باسم `KAGGLE_API_TOKEN` (ومعاه `KAGGLE_USERNAME` لو كان مفتاح قديم من kaggle.json): ده بيتقدم
  على المحفوظ في الصفحة.

## الشغلانات المتاحة
| المهمة | بتعمل إيه |
|---|---|
| `probe` | فحص الـ GPU والإنترنت على Kaggle — ابدأ بيها |
| `torch_bench` | قياس سرعة PyTorch |
| `ffmpeg_transcode` | تحويل فيديو من رابط https لـ MP4 (NVENC) |
| `chunked_transcode` | نفس اللي فوق على أجزاء، ولو وقعت بتكمل من آخر جزء |
| `batch_llm` | موديل Hugging Face على قائمة أسئلة (حتى 500) |
| `a2_wan_video` | توليد فيديو بـ Wan 2.1 / 2.2 |
| `a2_render_steps` | خطوات رندر من محرك المونتاج |

## الحماية
- دخول بكلمة سر واحدة للمالك، كوكي `HttpOnly; Secure; SameSite=Strict` لمدة 7 أيام، وقفل بعد 10 محاولات غلط في 15 دقيقة.
- كل طلب بيغيّر حاجة لازم يكون من نفس الموقع (CSRF)، وفيه CSP صارمة.
- النوتبوكات على Kaggle دايما **Private**. اللوحة بترفض شغل GPU جديد لو الرصيد الأسبوعي المتبقي أقل من ساعة (`MIN_GPU_RESERVE_S`).

## حدود لازم تعرفها
- **الإلغاء**: Kaggle API مش بيدي طريقة توقف نسخة شغالة من غير ما تمسح النوتبوك كله. الإلغاء بيوقف المتابعة، والنسخة بتقف لوحدها عند
  الحد الزمني — أو اضغط Stop من صفحة النوتبوك على Kaggle.
- **الملفات الكبيرة**: المدخلات بتتبعت كرابط https مباشر (الـ worker بينزّلها على Kaggle). رفع ملفات من المتصفح مش مدعوم (محتاج R2، وR2 محتاج كارت).
- **النتايج** بتفضل على Kaggle (روابط التحميل بتتجدد كل مرة تضغط). لو عملت إعادة محاولة بتتعمل نوتبوك جديد (`-a2`, `-a3`) فالقديمة بتفضل موجودة.
- خطة Workers المجانية: 100 ألف طلب في اليوم و10ms CPU لكل طلب — أكتر من كفاية للوحة تحكم.

## للمطورين
```bash
npm test                                         # اختبارات سريعة (من غير نت)
KAGGLE_API_TOKEN=KGAT_... node test/live_e2e.mjs # اختبار حقيقي: probe على CPU على Kaggle
python ../scripts/cloudflare/build_worker_script.py   # بعد أي تعديل في nexus/cloud/worker_kit
```
الكود: `src/index.js` (الراوتر)، `src/security.js` (الدخول والتشفير)، `src/kaggle.js` (Kaggle API)، `src/jobs.js` (دورة حياة الشغلانة)،
`src/schema.js` (الجداول)، `public/` (الواجهة).
