<script setup>
import { computed, watch, nextTick } from "vue";
import { useOperationplansStore } from './stores/operationplansStore.js';
import OperationplanDetails from "@/components/OperationplanDetails.vue";
import ErrorDialog from '@common/components/ErrorDialog.vue';
import { useBootstrapTooltips } from '@common/useBootstrapTooltips.js';

const store = useOperationplansStore();

const showErrorDialog = computed({
  get: () => !!store.error && store.error.showError,
  set: (value) => {
    if (!value) {
      store.error = { showError: false, message: '', details: '', type: 'error', title: '' };
    }
  }
});

const { initTooltips } = useBootstrapTooltips({ autoDispose: false });

// Reinitialize tooltips when tab changes
watch(() => store.showTab, async () => {
  await nextTick();
  initTooltips();
});

// Reinitialize tooltips when operationplans change
watch(() => store.operationplan, async (newOperationplans) => {
  if (newOperationplans && newOperationplans.length > 0) {
    await nextTick();
    initTooltips();
  }
});

</script>

<template>
  <main>
    <div>
      <div class="row mb-3">
        <operationplan-details />
      </div>
      <ErrorDialog
          v-model="showErrorDialog"
          :title="store.error?.title || 'Error'"
          :message="store.error?.message || ''"
          :details="store.error?.details || ''"
          :type="store.error?.type || 'error'"
          :show-details="!!(store.error?.details)"
      />
    </div>
  </main>
</template>

