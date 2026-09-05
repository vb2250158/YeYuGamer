import { onMounted, ref, shallowRef, watch, type Ref } from 'vue'
import { managerApi } from '../api/client'
import { extractItems, type AutomationAssessment, type PageResult } from '../api/contracts'
import { useManagerStore } from '../stores/manager'

export interface AutomationAssessmentResourceState {
  items: Ref<AutomationAssessment[]>
  loading: Ref<boolean>
  error: Ref<string | undefined>
  load: () => Promise<void>
}

/**
 * Reads Manager-owned AutomationAssessment records for Todo presentation.
 * Assessments remain diagnostic context: this composable never mutates Todo
 * state or derives dispatch eligibility from the assessment content.
 */
export function useAutomationAssessments(): AutomationAssessmentResourceState {
  const manager = useManagerStore()
  const items = shallowRef<AutomationAssessment[]>([])
  const loading = ref(false)
  const error = ref<string>()

  async function load(): Promise<void> {
    loading.value = true
    error.value = undefined
    try {
      const response = await managerApi.get<PageResult<AutomationAssessment>>('/automation-assessments?limit=500')
      items.value = extractItems(response)
    } catch (caught) {
      error.value = caught instanceof Error ? caught.message : String(caught)
    } finally {
      loading.value = false
    }
  }

  onMounted(load)
  watch(() => manager.snapshot.stateVersion, (current, previous) => {
    if (previous !== 0 && current !== previous) void load()
  })

  return { items, loading, error, load }
}
