import { api } from './api.js';

export const forecastService = {
  async getItemtree(measure, item =null, location =null, customer=null) {
    let additional = "";
    if (item) additional += "&item=" + window.admin_escape(item);
    if (location) additional += "&location=" + window.admin_escape(location);
    if (customer) additional += "&customer=" + window.admin_escape(customer);
    return api.get('forecast/itemtree/?measure=' + measure + additional);
  },

  async getLocationtree(measure, item =null, location =null, customer=null) {
    let additional = "";
    if (item) additional += "&item=" + window.admin_escape(item);
    if (location) additional += "&location=" + window.admin_escape(location);
    if (customer) additional += "&customer=" + window.admin_escape(customer);
    return api.get('forecast/locationtree/?measure=' + measure + additional);
  },

  async getCustomertree(measure, item =null, location =null, customer=null) {
    let additional = "";
    if (item) additional += "&item=" + window.admin_escape(item);
    if (location) additional += "&location=" + window.admin_escape(location);
    if (customer) additional += "&customer=" + window.admin_escape(customer);
    return api.get('forecast/customertree/?measure=' + measure + additional);
  },

  async getForecastDetails(measure, item =null, location =null, customer=null) {
    let additional = "";
    if (item) additional += "&item=" + window.admin_escape(item);
    if (location) additional += "&location=" + window.admin_escape(location);
    if (customer) additional += "&customer=" + window.admin_escape(customer);
    return api.get('forecast/detail/?measure=' + measure + additional);
  },

  async postForecastDetails(postData) {
    return api.wspost('forecast/detail/', postData );
  },

  async savePreferences(preferencesData) {
    return api.post('settings/', preferencesData );
  }
};
