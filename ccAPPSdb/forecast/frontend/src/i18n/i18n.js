import { createI18n } from 'vue-i18n'
import messages from '@intlify/unplugin-vue-i18n/messages'

// Create i18n instance - IMPORTANT CHANGES HERE
export const i18n = createI18n({
  legacy: false,
  globalInjection: true,
  locale: document.documentElement.lang,
  fallbackLocale: 'en',
  messages,
  missingWarn: false,
  fallbackWarn: false,
  silentTranslationWarn: true
})
