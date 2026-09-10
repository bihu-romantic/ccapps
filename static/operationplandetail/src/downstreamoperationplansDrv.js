'use strict';

angular.module('operationplandetailapp').directive('showdownstreamoperationplansDrv', showdownstreamoperationplansDrv);

showdownstreamoperationplansDrv.$inject = ['$window', 'gettextCatalog'];

function showdownstreamoperationplansDrv($window, gettextCatalog) {

	var directive = {
		restrict: 'EA',
		scope: { operationplan: '=data' },
		templateUrl: '/static/operationplandetail/downstreamoperationplans.html',
		link: linkfunc
	};
	return directive;

	function linkfunc(scope, elem, attrs) {

		function expandOrCollapse(i) {
			// 0: collapsed, 1: expanded, 2: hidden, 3: leaf node
			var j = i + 1;
			var mylevel = scope.operationplan.downstreamoperationplans[i][0];
			if (scope.operationplan.downstreamoperationplans[i][11] == 0)
				scope.operationplan.downstreamoperationplans[i][11] = 1;
			else
				scope.operationplan.downstreamoperationplans[i][11] = 0;
			while (j < scope.operationplan.downstreamoperationplans.length) {
				if (scope.operationplan.downstreamoperationplans[j][0] <= mylevel)
					break;
				else if (scope.operationplan.downstreamoperationplans[j][0] > mylevel + 1
					|| scope.operationplan.downstreamoperationplans[i][11] == 0)
					scope.operationplan.downstreamoperationplans[j][11] = 2;
				else if (j == scope.operationplan.downstreamoperationplans.length - 1 ||
					scope.operationplan.downstreamoperationplans[j][0] >= scope.operationplan.downstreamoperationplans[j + 1][0]) {
					if (scope.operationplan.downstreamoperationplans[j][12] != null
						&& scope.operationplan.downstreamoperationplans[j][12] == scope.operationplan.downstreamoperationplans[j + 1][12])
						scope.operationplan.downstreamoperationplans[j][11] = 1;
					else
						scope.operationplan.downstreamoperationplans[j][11] = 3;
				}
				else if (scope.operationplan.downstreamoperationplans[j][0] == mylevel + 1
					&& scope.operationplan.downstreamoperationplans[i][11] == 1)
					scope.operationplan.downstreamoperationplans[j][11] = 0;
				++j;
			}
		}
		scope.expandOrCollapse = expandOrCollapse;

		scope.url_prefix = url_prefix;

		angular.element(document).find('#widget_downstream')
         .on("shown.bs.collapse", grid.saveColumnConfiguration)
         .on("hidden.bs.collapse", grid.saveColumnConfiguration);
	} //link end
} //directive end
