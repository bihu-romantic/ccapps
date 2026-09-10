'use strict';

angular.module('ccAPPS.input').service('Model', ModelService);

ModelService.$inject = ['Buffer', 'Demand', 'Item', 'Location', 'Operation', 'Resource'];

function ModelService(Buffer, Demand, Item, Location, Operation, Resource) {

  var masterdata = {
    demands: {},
    operations: {},
    items: {},
    locations: {},
    buffers: {},
    resources: {}
  };

  // Populate all master data from a json document
  function load(jsondoc) {
    for (var i in jsondoc.items)
      masterdata.items[jsondoc.items[i].name] = new Item(jsondoc.items[i]);
    for (var i in jsondoc.operations)
      masterdata.operations[jsondoc.operations[i].name] = new Operation(jsondoc.operations[i]);
    for (var i in jsondoc.demands)
      masterdata.demands[jsondoc.demands[i].name] = new Demand(jsondoc.demands[i]);
    for (var i in jsondoc.locations)
      masterdata.locations[jsondoc.locations[i].name] = new Location(jsondoc.locations[i]);
    for (var i in jsondoc.buffers)
      masterdata.buffers[jsondoc.buffers[i].name] = new Buffer(jsondoc.buffers[i]);
    for (var i in jsondoc.resources)
      masterdata.resources[jsondoc.resources[i].name] = new Resource(jsondoc.resources[i]);
  }

  var service = {
    masterdata: masterdata,
    load: load,
  };
  return service;
};
