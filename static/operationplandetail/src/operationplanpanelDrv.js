'use strict';

angular.module('operationplandetailapp').directive('showoperationplanDrv', showoperationplanDrv);

showoperationplanDrv.$inject = ['$window', 'gettextCatalog'];

function showoperationplanDrv($window, gettextCatalog) {

  var directive = {
    restrict: 'EA',
    scope: {operationplan: '=data'},
    templateUrl: '/static/operationplandetail/operationplanpanel.html',
    link: linkfunc
  };
  return directive;

  function linkfunc(scope, elem, attrs) {
    scope.actions = actions;
    scope.editable = editable;
    scope.opptype = { //just a translation
      'MO': gettextCatalog.getString('manufacturing order'),
      'PO': gettextCatalog.getString('purchase order'),
      'DO': gettextCatalog.getString('distribution order'),
      'STCK': gettextCatalog.getString('stock'),
      'DLVR': gettextCatalog.getString('delivery'),
    }

    scope.$on("cardChanged", function (event, field, oldvalue, newvalue) {
      if (typeof scope.operationplan == undefined)
        return;
      else if (field === "startdate")
        scope.operationplan["start"] = newvalue;
      else if (field === "enddate")
        scope.operationplan["end"] = newvalue;
      else
        scope.operationplan[field] = newvalue;
    });

    //need to watch all of these because a webservice may change them on the fly
    scope.$watchGroup([
      'operationplan.id', 'operationplan.start', 'operationplan.end', 'operationplan.quantity',
      'operationplan.completed_quantity', 'operationplan.criticality', 'operationplan.delay',
      'operationplan.status', 'operationplan.remark'
    ], function (newValue, oldValue) {
      if (scope.operationplan === undefined || scope.operationplan === null)
        return;
      if (scope.operationplan.id == -1 || scope.operationplan.type === 'STCK') {
        // Multiple operationplans selected
        angular.element(elem).find('input').attr('disabled', 'disabled');
      }
      else if (typeof scope.operationplan.id !== 'undefined') {
        // Single operationplan selected
        angular.element(elem).find('input[disabled]').attr('disabled', false);
        if (scope.operationplan.hasOwnProperty('start'))
          angular.element(elem).find("#setStart").val(moment.utc(scope.operationplan.start, datetimeformat).format(datetimeformat));
        if (scope.operationplan.hasOwnProperty('end'))
          angular.element(elem).find("#setEnd").val(moment.utc(scope.operationplan.end, datetimeformat).format(datetimeformat));
      }
      else {
        // No operationplan selected
        angular.element(elem).find("#setStart").val('');
        angular.element(elem).find("#setEnd").val('');
      }
      angular.element(elem).find("#statusrow").css(
        "display", (scope.operationplan.status && scope.operationplan.type !== 'STCK') ? "table-row" : "none"
      );
      angular.element(elem).find('.collapse')
        .on("shown.bs.collapse", grid.saveColumnConfiguration)
        .on("hidden.bs.collapse", grid.saveColumnConfiguration);
    }); //watch end

  } //link end
} //directive end
