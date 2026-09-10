<script setup lang="js">
import { computed, onMounted } from 'vue';
import { useI18n } from 'vue-i18n';
import { useOperationplansStore } from "@/stores/operationplansStore.js";

const { t: ttt } = useI18n({
  useScope: 'global',
  inheritLocale: true
});

const store = useOperationplansStore();

const props = defineProps({
  widget: {
    type: Array,
    default: () => []
  }
});

const isCollapsed = computed(() => props.widget[1]?.collapsed ?? false);

const supplyInformation = computed(() => {
  return store.operationplan?.attributes?.supply || [];
});

const hasSupplyInformation = computed(() => {
  return supplyInformation.value.length > 0;
});

// Table headers for supply information
const headers = [
  'priority',
  'types',
  'origin',
  'lead time',
  'cost',
  'size minimum',
  'size multiple',
  'effective start',
  'effective end'
];

// Save column configuration on collapse/expand
function onCollapseToggle() {
  if (typeof window.grid !== 'undefined' && window.grid.saveColumnConfiguration) {
    window.grid.saveColumnConfiguration();
  }
}

onMounted(() => {
  // Set up Bootstrap collapse listeners
  const collapseElement = document.getElementById('widget_supply');
  if (collapseElement) {
    collapseElement.addEventListener('shown.bs.collapse', onCollapseToggle);
    collapseElement.addEventListener('hidden.bs.collapse', onCollapseToggle);
  }
});
</script>

<template>
  <div>
    <div
        class="card-header d-flex align-items-center"
        data-bs-toggle="collapse"
        data-bs-target="#widget_supply"
        aria-expanded="false"
        aria-controls="widget_supply"
    >
      <h5 class="card-title text-capitalize fs-5 me-auto">
        {{ ttt('supply information') }}
      </h5>
      <span class="fa fa-arrows align-middle w-auto widget-handle"></span>
    </div>

    <div
        id="widget_supply"
        class="card-body collapse"
        :class="{ 'show': !isCollapsed }"
    >
      <div class="table-responsive">
        <table class="table table-hover table-sm">
          <thead>
          <tr>
            <td v-for="header in headers" :key="header">
              <b class="text-capitalize">{{ ttt(header) }}</b>
            </td>
          </tr>
          </thead>
          <tbody>
          <tr v-if="!hasSupplyInformation">
            <td colspan="9">{{ ttt('no supply information') }}</td>
          </tr>

          <tr v-for="(supply, index) in supplyInformation" :key="index">
            <td v-for="(value, key) in supply" :key="key">
              {{ value }}
            </td>
          </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>
