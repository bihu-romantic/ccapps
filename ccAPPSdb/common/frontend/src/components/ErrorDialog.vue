<script setup>
import { computed } from 'vue';

// Props
const props = defineProps({
  modelValue: {
    type: Boolean,
    default: false
  },
  title: {
    type: String,
    default: 'Error'
  },
  message: {
    type: String,
    default: ''
  },
  details: {
    type: String,
    default: ''
  },
  type: {
    type: String,
    default: 'error', // 'error', 'warning', 'info'
    validator: (value) => ['error', 'warning', 'info'].includes(value)
  }
});

// Emits
const emit = defineEmits(['update:modelValue', 'close']);

// Computed properties
const isVisible = computed({
  get: () => props.modelValue,
  set: (value) => emit('update:modelValue', value)
});

// Methods
const closeDialog = () => {
  isVisible.value = false;
  emit('close');
};

const handleBackdropClick = (event) => {
  if (event.target === event.currentTarget) {
    closeDialog();
  }
};
</script>

<template>
  <div>
  <div v-if="isVisible"
       class="modal fade show"
       style="display: block; z-index: 10000"
       tabindex="-1"
       role="dialog"
       @click="handleBackdropClick">
    <div class="modal-dialog modal-dialog-centered">
      <div class="modal-content">
        <!-- Modal Header -->
        <div class="modal-header bg-danger">
          <h5 class="modal-title">{{ title }}</h5>
          <button type="button" class="btn-close" data-bs-dismiss="modal" @click="closeDialog">
          </button>
        </div>

        <!-- Modal Body -->
        <div class="modal-body" style="max-height: 350px; overflow: auto;">
          <div v-if="message" class="mb-3">
            <p class="mb-0">{{ message }}</p>
          </div>

          <!-- Error Details (collapsible) -->
          <div v-if="details" class="mt-3">
            {{ details }}
          </div>

          <!-- Slot for custom content -->
          <slot name="content"></slot>
        </div>

        <!-- Modal Footer -->
        <div class="modal-footer">
          <slot name="actions">
            <!-- Default actions -->
            <button type="button" class="btn btn-primary" @click="closeDialog">
              Close
            </button>
          </slot>
        </div>
      </div>
    </div>
  </div>

  <!-- Modal backdrop -->
  <div v-if="isVisible" class="modal-backdrop fade show"></div>
  </div>
</template>
