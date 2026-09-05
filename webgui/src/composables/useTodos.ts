import { onMounted, ref, shallowRef, watch, type Ref } from 'vue'
import { managerApi } from '../api/client'
import type { PageResult, TodoCadence, TodoInstance } from '../api/contracts'
import { extractItems } from '../api/contracts'
import { useManagerStore } from '../stores/manager'

export interface TodoResourceState {
  items: Ref<TodoInstance[]>
  loading: Ref<boolean>
  error: Ref<string | undefined>
  load: () => Promise<void>
}

export function useCurrentTodos(cadence: TodoCadence): TodoResourceState {
  const manager = useManagerStore()
  const items = shallowRef<TodoInstance[]>([])
  const loading = ref(false)
  const error = ref<string>()

  async function load(): Promise<void> {
    loading.value = true
    error.value = undefined
    try {
      const query = new URLSearchParams({ cadence, current: 'true', limit: '1000' })
      const response = await managerApi.get<PageResult<TodoInstance>>(`/todo-instances?${query}`)
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
