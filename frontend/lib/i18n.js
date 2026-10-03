// Tiny language + RTL context, no external dep.
//
// Usage:
//   const { t, lang, setLang, dir } = useT();
//   t("dictate.title")  -> string in the active language
//
// Persian is RTL — when language === "fa" we set <html dir="rtl"> and the
// shell flips automatically (CSS uses logical properties).
import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { CLINICAL_STRINGS } from "./i18n_clinical";

export const STRINGS = {
  en: {
    ...CLINICAL_STRINGS.en,
    "nav.workspace":   "Workspace",
    "nav.models":      "Models",
    "nav.education":   "Education",
    "nav.account":     "Account",
    "nav.dictate":     "Dictate",
    "nav.radiology":   "Vision Chat",
    "nav.reports":     "Reports",
    "nav.templates":   "Templates",
    "nav.ehr":         "EHR",
    "nav.alerts":      "Alerts",
    "nav.tutor":       "Tutor",
    "nav.asr":         "Speech Model",
    "nav.llm":         "Core LLM",
    "nav.vision":      "Radiology VL",
    "nav.settings":    "Settings",
    "nav.logout":      "Sign out",
    "nav.brandSub":    "aranmed · Bilingual Radiology AI",
    "dictate.title":   "Bilingual radiology dictation",
    "dictate.sub":     "Record or upload a study dictation in Persian, English, or both. Whisper handles code-switched speech, then MedicalRAG / the core LLM drafts a structured report — finalise with one click.",
    "dictate.sub.radiologist": "Your workspace: record dictation and review structured reports. Switch to Vision Chat to analyse images.",
    "topbar.lang":     "Language",
    "topbar.user":     "User",
    "auth.title":      "Sign in",
    "auth.username":   "Username",
    "auth.password":   "Password",
    "auth.email":      "Email (optional)",
    "auth.role":       "Role",
    "auth.signIn":     "Sign in",
    "auth.register":   "Create account",
    "auth.toRegister": "No account yet? Register",
    "auth.toLogin":    "Already have an account? Sign in",
    "auth.error":      "Authentication failed",
    "auth.role.doctor":   "Doctor",
    "auth.role.radiologist": "Radiologist",
    "auth.role.resident": "Resident",
    "auth.role.student":  "Student",
    "auth.pickRole":      "Choose your role",
    "auth.pickRoleSub":   "Your workspace is tailored to what you do — pick the role that fits.",
    "auth.changeRole":    "Change",
    "auth.roleDesc.radiologist": "Focused dictation + vision chat workspace",
    "auth.roleDesc.doctor":      "Full clinical workspace with EHR and alerts",
    "auth.roleDesc.resident":    "Clinical workspace with education tools",
    "auth.roleDesc.student":     "Education tutor: MCQs, cases and exams",
    "ehr.title":       "Electronic Health Record",
    "ehr.sub":         "Paste raw patient information in Persian or English. The core LLM turns it into a structured EHR you can edit.",
    "ehr.input":       "Patient information",
    "ehr.build":       "Build EHR",
    "ehr.savedTitle":  "Saved patients",
    "ehr.empty":       "No patients yet — build your first record above.",
    "ehr.open":        "Open",
    "ehr.delete":      "Delete",
    "ehr.medications": "Medications",
    "ehr.problems":    "Problems",
    "ehr.allergies":   "Allergies",
    "ehr.vitals":      "Vitals",
    "ehr.recordDose":  "Record dose",
    "alerts.title":    "Medication alerts",
    "alerts.sub":      "Compute which medications are due and notify the responsible clinician.",
    "alerts.patient":  "Patient",
    "alerts.check":    "Check medications",
    "alerts.send":     "Send alert",
    "alerts.channel":  "Channel",
    "alerts.to":       "Recipient",
    "alerts.history":  "Alert history",
    "alerts.due":      "due",
    "alerts.scheduled":"scheduled",
    "alerts.never":    "never administered",
    "alerts.unknown":  "schedule unknown",
    "edu.title":       "Medical education tutor",
    "edu.sub":         "Generate MCQs, case studies and mock exams. Designed for residents and students.",
    "edu.topic":       "Topic",
    "edu.count":       "Number of questions",
    "edu.difficulty":  "Difficulty",
    "edu.kind":        "Activity",
    "edu.mcq":         "Multiple-choice questions",
    "edu.case":        "Case study",
    "edu.exam":        "Mock exam",
    "edu.explain":     "Explain a concept",
    "edu.run":         "Generate",
    "edu.saved":       "Saved sets",
    "edu.show":        "Show answer",
    "edu.next":        "Next",
    "edu.score":       "Score",
    "common.language": "Language",
    "common.en":       "English",
    "common.fa":       "Persian (فارسی)",
    "common.loading":  "Loading…",
    "common.error":    "Error",
    "common.close":    "Close",
  },
  fa: {
    ...CLINICAL_STRINGS.fa,
    "nav.workspace":   "میز کار",
    "nav.models":      "مدل‌ها",
    "nav.education":   "آموزش",
    "nav.account":     "حساب",
    "nav.dictate":     "دیکته",
    "nav.radiology":   "گفتگوی تصویری",
    "nav.reports":     "گزارش‌ها",
    "nav.templates":   "قالب‌ها",
    "nav.ehr":         "پرونده الکترونیک",
    "nav.alerts":      "هشدارها",
    "nav.tutor":       "آموزگار",
    "nav.asr":         "مدل گفتار",
    "nav.llm":         "هسته LLM",
    "nav.vision":      "بینایی رادیولوژی",
    "nav.settings":    "تنظیمات",
    "nav.logout":      "خروج",
    "nav.brandSub":    "aranmed · هوش مصنوعی رادیولوژی دوزبانه",
    "dictate.title":   "دیکته رادیولوژی دوزبانه",
    "dictate.sub":     "دیکته مطالعه را به فارسی، انگلیسی یا هر دو ضبط یا بارگذاری کنید. Whisper گفتار ترکیبی را پردازش می‌کند؛ MedicalRAG / مدل هسته گزارش ساخت‌یافته می‌نویسد.",
    "dictate.sub.radiologist": "میز کار شما: دیکته و بررسی گزارش. برای تحلیل تصویر به گفتگوی تصویری بروید.",
    "topbar.lang":     "زبان",
    "topbar.user":     "کاربر",
    "auth.title":      "ورود",
    "auth.username":   "نام کاربری",
    "auth.password":   "گذرواژه",
    "auth.email":      "ایمیل (اختیاری)",
    "auth.role":       "نقش",
    "auth.signIn":     "ورود",
    "auth.register":   "ایجاد حساب",
    "auth.toRegister": "حساب ندارید؟ ثبت‌نام کنید",
    "auth.toLogin":    "حساب دارید؟ وارد شوید",
    "auth.error":      "ورود ناموفق بود",
    "auth.role.doctor":   "پزشک",
    "auth.role.radiologist": "رادیولوژیست",
    "auth.role.resident": "دستیار",
    "auth.role.student":  "دانشجو",
    "auth.pickRole":      "نقش خود را انتخاب کنید",
    "auth.pickRoleSub":   "میز کار شما متناسب با نقش‌تان تنظیم می‌شود — نقش مناسب را انتخاب کنید.",
    "auth.changeRole":    "تغییر",
    "auth.roleDesc.radiologist": "میز کار متمرکز دیکته و گفتگوی تصویری",
    "auth.roleDesc.doctor":      "میز کار کامل بالینی با پرونده و هشدارها",
    "auth.roleDesc.resident":    "میز کار بالینی همراه ابزارهای آموزشی",
    "auth.roleDesc.student":     "آموزگار: سوالات، مطالعه موردی و آزمون",
    "ehr.title":       "پرونده الکترونیک سلامت",
    "ehr.sub":         "اطلاعات خام بیمار را به فارسی یا انگلیسی وارد کنید. مدل هسته آن را به پرونده ساختاریافته تبدیل می‌کند.",
    "ehr.input":       "اطلاعات بیمار",
    "ehr.build":       "ساخت پرونده",
    "ehr.savedTitle":  "بیماران ذخیره‌شده",
    "ehr.empty":       "هنوز بیماری ثبت نشده است — اولین پرونده را در بالا بسازید.",
    "ehr.open":        "باز کردن",
    "ehr.delete":      "حذف",
    "ehr.medications": "داروها",
    "ehr.problems":    "مشکلات",
    "ehr.allergies":   "حساسیت‌ها",
    "ehr.vitals":      "علائم حیاتی",
    "ehr.recordDose":  "ثبت دوز",
    "alerts.title":    "هشدارهای دارویی",
    "alerts.sub":      "بررسی کنید کدام داروها سررسید شده‌اند و به پزشک مسئول اطلاع دهید.",
    "alerts.patient":  "بیمار",
    "alerts.check":    "بررسی داروها",
    "alerts.send":     "ارسال هشدار",
    "alerts.channel":  "کانال",
    "alerts.to":       "گیرنده",
    "alerts.history":  "تاریخچه هشدارها",
    "alerts.due":      "سررسید",
    "alerts.scheduled":"برنامه‌ریزی‌شده",
    "alerts.never":    "هرگز تجویز نشده",
    "alerts.unknown":  "زمان‌بندی نامشخص",
    "edu.title":       "آموزگار پزشکی",
    "edu.sub":         "تولید سوالات چندگزینه‌ای، مطالعه موردی و آزمون آزمایشی. برای دستیاران و دانشجویان.",
    "edu.topic":       "موضوع",
    "edu.count":       "تعداد سوالات",
    "edu.difficulty":  "سطح دشواری",
    "edu.kind":        "فعالیت",
    "edu.mcq":         "سوالات چندگزینه‌ای",
    "edu.case":        "مطالعه موردی",
    "edu.exam":        "آزمون آزمایشی",
    "edu.explain":     "شرح یک مفهوم",
    "edu.run":         "تولید",
    "edu.saved":       "مجموعه‌های ذخیره‌شده",
    "edu.show":        "نمایش پاسخ",
    "edu.next":        "بعدی",
    "edu.score":       "امتیاز",
    "common.language": "زبان",
    "common.en":       "English",
    "common.fa":       "فارسی",
    "common.loading":  "در حال بارگذاری…",
    "common.error":    "خطا",
    "common.close":    "بستن",
  },
};

const Ctx = createContext({ lang: "en", t: (k) => k, setLang: () => {}, dir: "ltr" });

export function LanguageProvider({ children }) {
  const [lang, setLangState] = useState("en");

  useEffect(() => {
    if (typeof window === "undefined") return;
    const saved = window.localStorage.getItem("asr.lang");
    if (saved && STRINGS[saved]) setLangState(saved);
  }, []);

  useEffect(() => {
    if (typeof document === "undefined") return;
    document.documentElement.lang = lang;
    document.documentElement.dir = lang === "fa" ? "rtl" : "ltr";
  }, [lang]);

  const setLang = useCallback((l) => {
    if (!STRINGS[l]) return;
    setLangState(l);
    if (typeof window !== "undefined") window.localStorage.setItem("asr.lang", l);
  }, []);

  const t = useCallback((key) => {
    const dict = STRINGS[lang] || STRINGS.en;
    return dict[key] ?? STRINGS.en[key] ?? key;
  }, [lang]);

  return (
    <Ctx.Provider value={{ lang, setLang, t, dir: lang === "fa" ? "rtl" : "ltr" }}>
      {children}
    </Ctx.Provider>
  );
}

export const useT = () => useContext(Ctx);
