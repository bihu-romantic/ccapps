angular.module('ccAPPS.input').factory('Demand', DemandFactory);

DemandFactory.$inject = ['$http', 'getURLprefix', 'Location', 'Item', 'Customer'];

function DemandFactory($http, getURLprefix, Location, Item, Customer) {

  var debug = false;

  function Demand(data) {
    if (data) this.extend(data);
  };

  Demand.prototype = {
    extend: extend,
    getData: getData,
    // REST API methods
    get: get,
    save: save,
    remove: remove
  };

  return Demand;

  function extend(data) {
    // Convert new values to the correct type
    angular.forEach(data, function (value, key) {
      switch (key) {
        case "due":
          if (typeof value === "date" || value instanceof Date)
            data['due'] = value;
          else if (typeof value === "string" || value instanceof String)
            data['due'] = new Date(value);
          else if (!moment.isMoment(value))
            data['due'] = moment(value);
          break;
        case "delivery":
          if (!moment.isMoment(value))
            data['delivery'] = moment(value);
          break;
        case "pegging":
          value.forEach(function (i, indx) {
            if (!moment.isMoment(i.operationplan.start))
              i.operationplan.start = moment(i.operationplan.start);
            if (!moment.isMoment(i.operationplan.end))
              i.operationplan.end = moment(i.operationplan.end);
          });
          break;
        case "problems":
          value.forEach(function (i, indx) {
            if (!moment.isMoment(i.start))
              i.start = moment(i.start);
            if (!moment.isMoment(i.end))
              i.end = moment(i.end);
          });
          break;
        case "constraints":
          value.forEach(function (i, indx) {
            if (!moment.isMoment(i.start))
              i.start = moment(i.start);
            if (!moment.isMoment(i.end))
              i.end = moment(i.end);
          });
          break;
        case "maxlateness":
          // Convert from seconds to days
          data['maxlateness'] = value / 86400;
          break;
      }
    });

    // Merge update values in demand object
    angular.extend(this, data);
  };

  function getData() {
    var fields = {
      name: this.name
    };
    if (this.quantity !== undefined)
      fields.quantity = this.quantity;
    if (this.description !== undefined)
      fields.description = this.description;
    if (this.category !== undefined)
      fields.category = this.category;
    if (this.subcategory !== undefined)
      fields.subcategory = this.subcategory;
    if (this.due !== undefined) {
      if (this.due instanceof Date)
        fields.due = this.due.toISOString();
      else
        fields.due = this.due.format('YYYY-MM-DDTHH:mm:ss');
    }
    if (this.item !== undefined && this.item)
      fields.item = { name: this.item.name };
    if (this.location !== undefined && this.location)
      fields.location = { name: this.location.name };
    if (this.customer !== undefined && this.customer)
      fields.customer = { name: this.customer.name };
    if (this.minshipment !== undefined)
      fields.minshipment = this.minshipment;
    if (this.maxlateness !== undefined)
      // Convert from days to seconds
      fields.maxlateness = this.maxlateness * 86400;
    if (this.priority !== undefined)
      fields.priority = this.priority;
    return fields;
  };

  // REST API GET
  function get() {
    var dmd = this;
    return $http
      .get(getURLprefix() + '/api/input/demand/' + encodeURIComponent(dmd.name) + "/")
      .then(function (response) {
        if (debug)
          console.log("Demand get '" + dmd.name + "': ", response.data);
        dmd.extend(response.data);
        return dmd;
      });
  };

  // REST API PUT
  function save() {
    var dmd = this;
    return $http
      .put(getURLprefix() + '/api/input/demand/' + encodeURIComponent(dmd.name) + "/", dmd.getData())
      .then(function (response) {
        if (debug)
          console.log("Demand save '" + dmd.name + "': ", response.data);
        dmd.extend(response.data);
        return dmd;
      });
  };

  // REST API DELETE
  function remove() {
    var dmd = this;
    return $http
      .delete(getURLprefix() + '/api/input/demand/' + encodeURIComponent(dmd.name) + "/")
      .then(function (response) {
        if (debug)
          console.log("Demand delete '" + dmd.name + "': ", response.data);
        dmd.status = 'closed';
        return dmd;
      });
  };

};
