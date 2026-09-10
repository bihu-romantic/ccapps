import { createI18n } from 'vue-i18n'
import messages from '@intlify/unplugin-vue-i18n/messages'

function normalizeLocale(rawLocale) {
  const locale = (rawLocale || '').toLowerCase().replace(/_/g, '-');

  if (!locale) return 'en';

  // Map browser / Django variants to the translation bundle names.
  if (locale === 'zh' || locale.startsWith('zh-cn') || locale.startsWith('zh-sg') || locale.startsWith('zh-hans')) {
    return 'zh-hans';
  }
  if (locale.startsWith('zh-tw') || locale.startsWith('zh-hk') || locale.startsWith('zh-mo') || locale.startsWith('zh-hant')) {
    return 'zh-hant';
  }

  return locale;
}

function getRuntimeLanguageHint() {
  if (typeof window !== 'undefined' && window.language) return window.language;
  if (typeof document !== 'undefined' && document.documentElement?.lang) return document.documentElement.lang;
  if (typeof navigator !== 'undefined' && navigator.language) return navigator.language;
  return '';
}

export function resolveLocale() {
  const locale = normalizeLocale(getRuntimeLanguageHint());

  // In this app the zh-hans catalog is the complete one.
  // If english catalog is empty, fallback to zh-hans to avoid showing raw english keys.
  if (
    locale === 'en' &&
    messages &&
    messages.en &&
    Object.keys(messages.en).length === 0 &&
    messages['zh-hans'] &&
    Object.keys(messages['zh-hans']).length > 0
  ) {
    return 'zh-hans';
  }
  return locale;
}

const locale = resolveLocale();

// Create i18n instance - IMPORTANT CHANGES HERE
export const i18n = createI18n({
  legacy: false,
  globalInjection: true,
  locale,
  fallbackLocale: ['zh-hans', 'en'],
  messages,
  missingWarn: false,
  fallbackWarn: false,
  silentTranslationWarn: true
})
