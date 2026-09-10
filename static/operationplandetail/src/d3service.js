angular.module('d3', [])

  // D3 Factory
  .factory('d3service', function() {
    /* this place can be used to declare locals or other D3.js
       specific configurations. */

    return d3;
  });