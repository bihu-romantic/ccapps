angular.module("ccAPPS.common", []);

// Global variable database is passed from Django.
angular.module("ccAPPS.common")
  .constant('getURLprefix', function getURLprefix() {
    return database === 'default' ? '' : '/' + database;
  }
  );

// Date formatting filter, expecting a moment instance as input
angular.module("ccAPPS.common")
  .filter('dateTimeFormat', function dateTimeFormat() {
    return function (input, fmt) {
      return input ? input.format(fmt || datetimeformat) : '';
    };
  });