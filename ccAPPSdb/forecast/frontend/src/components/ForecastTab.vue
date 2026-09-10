<script setup lang="js">
import ForecastEditFormCard from "@/components/ForecastEditFormCard.vue";
import ForecastParametersCard from "@/components/ForecastParametersCard.vue";
import ForecastGraph from "@/components/ForecastGraph.vue";
import ForecastTable from "@/components/ForecastTable.vue";

</script>

<template>
  <div>
    <ForecastGraph />
    <ForecastTable />
    <div class="row mb-3" data-ng-show="showtab=='forecast'">
      <div class="col-lg-8 col-md-12 mb-3" style="max-width: 900px">
        <ForecastEditFormCard />
      </div>
      <div class="col-md-4 col-md-12 mb-3" style="max-width: 540px">
        <ForecastParametersCard />
      </div>
    </div>
  </div>
</template>

