<script setup lang="js">
import { useForecastsStore } from '@/stores/forecastsStore';
import AttributesCard from "@/components/AttributesCard.vue";
import {computed} from "vue";

const store = useForecastsStore();

const currentSequence = computed(() =>  store.currentSequence);

</script>

<template>
  <div class="attributes-tab row">
    <div class="col-sm-4" v-for="panel in currentSequence" :key="panel">
      <div>
        <AttributesCard v-if="panel" :panelid="panel" />
      </div>
    </div>
  </div>
</template>
