'use strict';

angular.module('operationplandetailapp').directive('showoperationpeggingpanelDrv', showoperationpeggingpanelDrv);

showoperationpeggingpanelDrv.$inject = ['$window', 'gettextCatalog', '$filter'];

function showoperationpeggingpanelDrv($window, gettextCatalog, $filter) {

  var directive = {
    restrict: 'EA',
    scope: { operationplan: '=data' },
    link: linkfunc
  };
  return directive;

  function linkfunc(scope, elem, attrs, transclude) {
    scope.$watchGroup(['operationplan.id', 'operationplan.pegging_demand.length'], function (newValue, oldValue) {
      angular.element(document).find('#attributes-operationdemandpegging').empty().append(
        '<div class="card-header d-flex align-items-center" data-bs-toggle="collapse" data-bs-target="#widget_demandpegging" aria-expanded="false" aria-controls="widget_demandpegging">' + 
        '<h5 class="card-title text-capitalize fs-5 me-auto">' +
        gettextCatalog.getString("demand") +
        '</h5><span class="fa fa-arrows align-middle w-auto widget-handle"></span></div>' +
        '<div class="card-body table-responsive collapse' + 
        (scope.$parent.widget[1]["collapsed"] ? '' : ' show') + 
        '" id="widget_demandpegging" style="max-height:15em; overflow:auto">' +        
        '</div>'
      );
      var rows = '';
      if (typeof scope.operationplan !== 'undefined') {
        if (scope.operationplan.hasOwnProperty('pegging_demand')) {          
          angular.forEach(scope.operationplan.pegging_demand, function (thedemand) {
            rows += '<tr><td>' + $.jgrid.htmlEncode(thedemand.demand.name)
              + "<a href=\"" + url_prefix
              + (thedemand.demand.forecast ? "/detail/forecast/forecast/" : "/detail/input/demand/")
              + admin_escape(thedemand.demand.name)
              + "/\" onclick='event.stopPropagation()'><span class='ps-2 fa fa-caret-right'></span></a>"
              + '</td><td>';
            if (thedemand.demand.item.description)
              rows += '<span onmouseenter="$(this).tooltip(\'show\')" title="'
                + $.jgrid.htmlEncode(thedemand.demand.item.description) + '">'
                + $.jgrid.htmlEncode(thedemand.demand.item.name)
                + "</span>";
            else
              rows += $.jgrid.htmlEncode(thedemand.demand.item.name);
            rows += "<a href=\"" + url_prefix + "/detail/input/item/" + admin_escape(thedemand.demand.item.name)
              + "/\" onclick='event.stopPropagation()'><span class='ps-2 fa fa-caret-right'></span></a>"
              + '</td><td>' + $filter('formatdate')(thedemand.demand.due)
              + '</td><td>' + grid.formatNumber(thedemand.quantity) + '</td></tr>';
          });
        }
      }

      if (rows == '')
        angular.element(document).find('#widget_demandpegging').append(
          '<div>' + gettextCatalog.getString('There is no demand requiring this supply.') + '</div>'
        );
      else 
        angular.element(document).find('#widget_demandpegging').append(
          '<table class="table table-sm table-hover table-borderless"><thead><tr><td>' +
          '<b class="text-capitalize">' + gettextCatalog.getString("name") + '</b>' +
          '</td><td>' +
          '<b class="text-capitalize">' + gettextCatalog.getString("item") + '</b>' +
          '</td><td>' +
          '<b class="text-capitalize">' + gettextCatalog.getString("due") + '</b>' +
          '</td><td>' +
          '<b class="text-capitalize">' + gettextCatalog.getString("quantity") + '</b>' +
          '</td></thead>' +
          '<tbody>' + rows + '</tbody></table>'
          );
      angular.element(elem).find('.collapse')
        .on("shown.bs.collapse", grid.saveColumnConfiguration)
        .on("hidden.bs.collapse", grid.saveColumnConfiguration);     
    }); //watch end

  } //link end
} //directive end
