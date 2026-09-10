import { onMounted, onUnmounted, ref } from 'vue';

export function useBootstrapTooltips(options = {}) {
  const { autoDispose = true } = options;
  const tooltipInstances = ref([]);

  const initTooltips = () => {
    const tooltipTriggerList = document.querySelectorAll('[data-bs-toggle="tooltip"]');

    tooltipInstances.value = [...tooltipTriggerList].map(tooltipTriggerEl => {
      // Check if tooltip is already initialized to avoid duplicates
      const existingTooltip = window.bootstrap.Tooltip.getInstance(tooltipTriggerEl);
      if (existingTooltip) {
        return existingTooltip;
      }
      return new window.bootstrap.Tooltip(tooltipTriggerEl);
    });
  };

  const disposeTooltips = () => {
    tooltipInstances.value.forEach(tooltip => {
      if (tooltip) {
        tooltip.dispose();
      }
    });
    tooltipInstances.value = [];
  };

  onMounted(() => {
    initTooltips();
  });

  if (autoDispose) {
    onUnmounted(() => {
      disposeTooltips();
    });
  }

  return {
    initTooltips,
    disposeTooltips
  };
}
