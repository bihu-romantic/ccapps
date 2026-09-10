'use strict';

angular.module('operationplandetailapp').directive('showupstreamoperationplansDrv', showupstreamoperationplansDrv);

showupstreamoperationplansDrv.$inject = ['$window', 'gettextCatalog'];

function showupstreamoperationplansDrv($window, gettextCatalog) {

	var directive = {
		restrict: 'EA',
		scope: { operationplan: '=data' },
		templateUrl: '/static/operationplandetail/upstreamoperationplans.html',
		link: linkfunc
	};
	return directive;

	function linkfunc(scope, elem, attrs) {

		function expandOrCollapse(i) {
			// 0: collapsed, 1: expanded, 2: hidden, 3: leaf
			var j = i + 1;
			var mylevel = scope.operationplan.upstreamoperationplans[i][0];
			if (scope.operationplan.upstreamoperationplans[i][11] == 0)
				scope.operationplan.upstreamoperationplans[i][11] = 1;
			else
				scope.operationplan.upstreamoperationplans[i][11] = 0;
			while (j < scope.operationplan.upstreamoperationplans.length) {
				if (scope.operationplan.upstreamoperationplans[j][0] <= mylevel)
					break;
				else if (scope.operationplan.upstreamoperationplans[j][0] > mylevel + 1
					|| scope.operationplan.upstreamoperationplans[i][11] == 0)
					scope.operationplan.upstreamoperationplans[j][11] = 2;
				else if (j == scope.operationplan.upstreamoperationplans.length - 1 ||
					scope.operationplan.upstreamoperationplans[j][0] >= scope.operationplan.upstreamoperationplans[j + 1][0]) {
					if (scope.operationplan.upstreamoperationplans[j][12] != null
						&& scope.operationplan.upstreamoperationplans[j][12] == scope.operationplan.upstreamoperationplans[j + 1][12])
						scope.operationplan.upstreamoperationplans[j][11] = 1;
					else
						scope.operationplan.upstreamoperationplans[j][11] = 3;
				}
				else if (scope.operationplan.upstreamoperationplans[j][0] == mylevel + 1
					&& scope.operationplan.upstreamoperationplans[i][11] == 1)
					scope.operationplan.upstreamoperationplans[j][11] = 0;
				++j;
			}
		}
		scope.expandOrCollapse = expandOrCollapse;

		scope.url_prefix = url_prefix;

        angular.element(elem).find('#widget_upstream')
         .on("shown.bs.collapse", grid.saveColumnConfiguration)
         .on("hidden.bs.collapse", grid.saveColumnConfiguration);
	} //link end
} //directive end
