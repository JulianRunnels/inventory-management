<template>
  <div class="restocking">
    <div class="page-header">
      <h2>{{ t('restocking.title') }}</h2>
      <p>{{ t('restocking.description') }}</p>
    </div>

    <div class="card">
      <div class="budget-control">
        <label for="budget-slider" class="budget-label">{{ t('restocking.budget') }}</label>
        <input
          id="budget-slider"
          v-model.number="budget"
          type="range"
          min="0"
          max="5000"
          step="100"
          class="budget-slider"
        />
        <span class="budget-value">{{ formatCurrency(budget) }}</span>
      </div>
    </div>

    <div v-if="loading && !recommendations" class="loading">{{ t('common.loading') }}</div>
    <div v-else-if="error" class="error">{{ error }}</div>
    <div v-else-if="recommendations">
      <div class="stats-grid">
        <div class="stat-card">
          <div class="stat-label">{{ t('restocking.budget') }}</div>
          <div class="stat-value">{{ formatCurrency(recommendations.budget) }}</div>
        </div>
        <div class="stat-card info">
          <div class="stat-label">{{ t('restocking.totalCost') }}</div>
          <div class="stat-value">{{ formatMoney(recommendations.total_cost) }}</div>
        </div>
        <div class="stat-card success">
          <div class="stat-label">{{ t('restocking.remainingBudget') }}</div>
          <div class="stat-value">{{ formatMoney(recommendations.remaining_budget) }}</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">{{ t('restocking.itemCount') }}</div>
          <div class="stat-value">{{ items.length }}</div>
        </div>
      </div>

      <div class="card">
        <div class="card-header">
          <h3 class="card-title">{{ t('restocking.recommendations') }} ({{ items.length }})</h3>
          <button
            class="place-order-btn"
            :disabled="placeDisabled"
            @click="placeOrder"
          >
            {{ submitting ? t('restocking.placing') : t('restocking.placeOrder') }}
          </button>
        </div>

        <div v-if="placedOrder" class="confirmation">
          {{ t('restocking.orderPlaced', { orderNumber: placedOrder.order_number }) }}
          <router-link to="/orders">{{ t('restocking.viewOrders') }}</router-link>
        </div>
        <div v-if="submitError" class="error">{{ submitError }}</div>

        <div v-if="items.length === 0" class="loading">{{ t('restocking.noItems') }}</div>
        <div v-else class="table-container">
          <table>
            <thead>
              <tr>
                <th>{{ t('restocking.sku') }}</th>
                <th>{{ t('restocking.name') }}</th>
                <th>{{ t('restocking.trend') }}</th>
                <th>{{ t('restocking.quantity') }}</th>
                <th>{{ t('restocking.unitCost') }}</th>
                <th>{{ t('restocking.lineTotal') }}</th>
                <th>{{ t('restocking.leadTime') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in items" :key="item.sku">
                <td><strong>{{ item.sku }}</strong></td>
                <td>{{ translateProductName(item.name) }}</td>
                <td>
                  <span :class="['badge', item.trend]">{{ trendLabel(item.trend) }}</span>
                </td>
                <td>{{ item.quantity.toLocaleString() }}</td>
                <td>{{ formatMoney(item.unit_cost) }}</td>
                <td><strong>{{ formatMoney(item.line_total) }}</strong></td>
                <td>{{ t('restocking.days', { count: item.lead_time_days }) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div>
</template>

<script>
import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { api } from '../api'
import { useI18n } from '../composables/useI18n'
import { formatCurrency as formatCurrencyUtil, formatCurrencyWithDecimals } from '../utils/currency'

// Note: the global filters (useFilters) are intentionally not used here;
// demand forecasts have no warehouse or category data.
export default {
  name: 'Restocking',
  setup() {
    const { t, currentCurrency, translateProductName } = useI18n()

    const budget = ref(2500)
    const recommendations = ref(null)
    const loading = ref(true)
    const error = ref(null)
    const submitting = ref(false)
    const submitError = ref(null)
    const placedOrder = ref(null)

    const items = computed(() => recommendations.value?.items ?? [])

    const formatCurrency = (value) => formatCurrencyUtil(value, currentCurrency.value)

    const formatMoney = (value) => formatCurrencyWithDecimals(value, currentCurrency.value, 2)

    const trendLabel = (trend) => {
      return ['increasing', 'stable', 'decreasing'].includes(trend) ? t(`restocking.${trend}`) : trend
    }

    // Each request gets an id; only the latest request may update state,
    // so slow responses to older slider positions are discarded.
    let latestRequestId = 0

    const loadRecommendations = async () => {
      const requestId = ++latestRequestId
      loading.value = true
      error.value = null
      try {
        const data = await api.getRestockRecommendations(budget.value)
        if (requestId !== latestRequestId) return
        recommendations.value = data
      } catch (err) {
        if (requestId !== latestRequestId) return
        error.value = 'Failed to load restock recommendations: ' + err.message
      } finally {
        if (requestId === latestRequestId) loading.value = false
      }
    }

    // Debounce slider changes so dragging doesn't fire a request per step
    let debounceTimer = null
    watch(budget, () => {
      // Any budget change invalidates a previous result/error message
      placedOrder.value = null
      submitError.value = null
      clearTimeout(debounceTimer)
      debounceTimer = setTimeout(loadRecommendations, 300)
    })

    // Disable while the slider value differs from the loaded recommendation
    // (debounced refetch pending), so the order never uses a stale budget.
    const placeDisabled = computed(() =>
      loading.value ||
      submitting.value ||
      items.value.length === 0 ||
      !recommendations.value ||
      budget.value !== recommendations.value.budget
    )

    const placeOrder = async () => {
      submitting.value = true
      submitError.value = null
      placedOrder.value = null
      try {
        const rec = recommendations.value
        placedOrder.value = await api.createRestockOrder({
          budget: rec.budget,
          items: rec.items.map(i => ({ sku: i.sku, quantity: i.quantity }))
        })
      } catch (err) {
        console.error('Failed to place order:', err)
        const detail = err.response?.data?.detail
        // 422 returns detail as an array of validation errors; only show strings
        submitError.value = typeof detail === 'string' && detail ? detail : t('restocking.orderFailed')
      } finally {
        submitting.value = false
      }
    }

    onMounted(loadRecommendations)
    onBeforeUnmount(() => {
      clearTimeout(debounceTimer)
      latestRequestId++ // drop any in-flight response after leaving the view
    })

    return {
      t,
      budget,
      recommendations,
      items,
      loading,
      error,
      submitting,
      submitError,
      placedOrder,
      formatCurrency,
      formatMoney,
      placeDisabled,
      trendLabel,
      placeOrder,
      translateProductName
    }
  }
}
</script>

<style scoped>
.budget-control {
  display: flex;
  align-items: center;
  gap: 1rem;
  padding: 1rem 1.25rem;
}

.budget-label {
  font-weight: 600;
  color: #0f172a;
}

.budget-slider {
  flex: 1;
}

.budget-value {
  min-width: 100px;
  text-align: right;
  font-weight: 700;
  color: #0f172a;
}

.place-order-btn {
  background: #0f172a;
  color: white;
  border: none;
  border-radius: 6px;
  padding: 0.5rem 1rem;
  font-size: 0.875rem;
  font-weight: 600;
  cursor: pointer;
}

.place-order-btn:disabled {
  background: #e2e8f0;
  color: #64748b;
  cursor: not-allowed;
}

.confirmation {
  background: #f0fdf4;
  border: 1px solid #bbf7d0;
  color: #166534;
  padding: 1rem;
  border-radius: 8px;
  margin: 1rem;
  font-size: 0.938rem;
}

.confirmation a {
  margin-left: 0.5rem;
  font-weight: 600;
  color: #166534;
}
</style>
