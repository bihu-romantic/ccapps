'use strict';

angular.module('operationplandetailapp').directive('showproblemspanelDrv', showproblemspanelDrv);

showproblemspanelDrv.$inject = ['$window', 'gettextCatalog'];

function showproblemspanelDrv($window, gettextCatalog) {

  var directive = {
    restrict: 'EA',
    scope: { operationplan: '=data' },
    link: linkfunc
  };
  return directive;

  function linkfunc(scope, elem, attrs) {
    scope.$watchGroup(['operationplan.id', 'operationplan.problems.length', , 'operationplan.info'], function (newValue, oldValue) {
      angular.element(document).find('#attributes-operationproblems').empty().append(
        '<div class="card-header d-flex align-items-center" data-bs-toggle="collapse" data-bs-target="#widget_problems" aria-expanded="false" aria-controls="widget_problems">' +
        '<h5 class="card-title text-capitalize fs-5 me-auto">' +
        gettextCatalog.getString("problems") +
        '</h5><span class="fa fa-arrows align-middle w-auto widget-handle"></span></div>' +
        '<div class="card-body collapse' +
        (scope.$parent.widget[1]["collapsed"] ? '' : ' show') +
        '" id="widget_problems">' +
        '<table class="table table-sm table-hover table-borderless"><thead><tr><td>' +
        '<b class="text-capitalize">' + gettextCatalog.getString("name") + '</b>' +
        '</td><td>' +
        '<b class="text-capitalize">' + gettextCatalog.getString("start") + '</b>' +
        '</td><td>' +
        '<b class="text-capitalize">' + gettextCatalog.getString("end") + '</b>' +
        '</td></tr></thead>' +
        '<tbody></tbody>' +
        '</table></div>'
      );
      var rows = (
        scope.operationplan.hasOwnProperty('problems')
        || scope.operationplan.hasOwnProperty('info')
      ) ? "" : ('<tr><td colspan="3">' + gettextCatalog.getString('no problems') + '</td></tr>');
      if (typeof scope.operationplan !== 'undefined') {
        if (scope.operationplan.hasOwnProperty('problems')) {
          angular.forEach(scope.operationplan.problems, function (theproblem) {
            rows += '<tr><td>' +
              theproblem.description + '</td><td>' +
              theproblem.start + '</td><td>' +
              theproblem.end + '</td></tr>';
          });
        }
        if (scope.operationplan.hasOwnProperty('info')) {
          angular.forEach(scope.operationplan.info.split('\n'), function (info) {
            rows += '<tr><td colspan="3">' + info + '</td><tr>';
          });
        }
      }
      angular.element(document).find('#attributes-operationproblems tbody').append(rows);
      angular.element(elem).find('.collapse')
        .on("shown.bs.collapse", grid.saveColumnConfiguration)
        .on("hidden.bs.collapse", grid.saveColumnConfiguration);
    }); //watch end

  } //link end
} //directive end
