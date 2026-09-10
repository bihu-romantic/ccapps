angular.module('ccAPPS.input').factory('Location', LocationFactory);

LocationFactory.$inject = ['$http', 'getURLprefix'];

function LocationFactory($http, getURLprefix) {

  var debug = false;

  function Location(data) {
    if (data)
      this.extend(data);
  };

  Location.prototype = {
    extend: extend,
    get: get,
    save: save,
    remove: remove
  };

  return Location;

  function extend(data) {
    angular.extend(this, data);
  };

  //REST API GET
  function get() {
    var loc = this;
    return $http
      .get(getURLprefix() + '/api/input/location/' + encodeURIComponent(loc.name) + "/")
      .then(
        function (response) {
          if (debug)
            console.log("Location get '" + loc.name + "': ", response.data);
          loc.extend(response.data);
          return loc;
        }
      );
  };

  // REST API PUT
  function save() {
    var loc = this;
    return $http
      .put(getURLprefix() + '/api/input/location/' + encodeURIComponent(loc.name) + "/", loc)
      .then(
        function (response) {
          if (debug)
            console.log("Location save '" + loc.name + "': ", response.data);
          loc.extend(response.data);
          return loc;
        }
      );
  };

  // REST API DELETE
  function remove() {
    var loc = this;
    return $http
      .delete(getURLprefix() + '/api/input/location/' + encodeURIComponent(loc.name) + "/")
      .then(
        function (response) {
          if (debug)
            console.log("Location delete '" + loc.name + "': ", response.data);
          return loc;
        });
  };
};
