import { api } from './api.js';

export const operationplanService = {
  async getOperationplanDetails(params) {
    return api.get('operationplan/?reference=' + encodeURIComponent(params.reference), {});
  },

  async postOperationplanDetails(postData) {
    return api.wspost('operationplan/', postData);
  },

  async savePreferences(preferencesData) {
    return api.post('settings/', preferencesData);
  },

  async exportToERP(postData) {
    return api.post('erp/upload/', JSON.stringify(postData));
  },

  async getKanbanData(params) {
    const searchParams = new URLSearchParams(params).toString();
    const endpoint =
      (location.pathname.startsWith(window.url_prefix)
        ? location.pathname.substring(window.url_prefix.length)
        : location.pathname) +
      '?' +
      searchParams;
    return api.get(endpoint.startsWith('/') ? endpoint.substring(1) : endpoint, {});
  },

  async getCalendarData(endpoint) {
    return api.get(endpoint, {});
  },
};
