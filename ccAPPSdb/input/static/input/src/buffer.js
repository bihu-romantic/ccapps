angular.module('ccAPPS.input').factory('Buffer', BufferFactory);

BufferFactory.$inject = ['$http', 'getURLprefix', 'Item', 'Location'];

function BufferFactory($http, getURLprefix, Item, Location) {

  var debug = false;

  function Buffer(data) {
    if (data)
      this.extend(data);
  };

  Buffer.prototype = {
    extend: extend,
    get: get,
    save: save,
    remove: remove
  };

  return Buffer;

  function extend(data) {
    angular.extend(this, data);
  };

  // REST API GET
  function get() {
    var buf = this;
    return $http
      .get(getURLprefix() + '/api/input/buffer/' + encodeURIComponent(buf.name) + "/")
      .then(
        function (response) {
          if (debug)
            console.log("Buffer get '" + buf.name + "': ", response.data);
          buf.extend(response.data);
          return buf;
        }
      );
  };

  // REST API PUT
  function save() {
    var buf = this;
    return $http
      .put(getURLprefix() + '/api/input/buffer/' + encodeURIComponent(buf.name) + "/", buf)
      .then(
        function (response) {
          if (debug)
            console.log("Buffer save '" + buf.name + "': ", response.data);
          buf.extend(response.data);
          return buf;
        }
      );
  };

  // REST API DELETE
  function remove() {
    var buf = this;
    return $http
      .delete(getURLprefix() + '/api/input/buffer/' + encodeURIComponent(buf.name) + "/")
      .then(
        function (response) {
          if (debug)
            console.log("Buffer delete '" + buf.name + "': ", response.data);
          return buf;
        }
      );
  };
};
