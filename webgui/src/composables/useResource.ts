import { onMounted, ref, shallowRef, type Ref } from 'vue'
import { ApiError, managerApi } from '../api/client'
import { extractItems, type PageResult } from '../api/contracts'

export interface ResourceState<T> {
  items: Ref<T[]>
  loading: Ref<boolean>
  error: Ref<string | undefined>
  load: () => Promise<void>
}

export function useResource<T>(path: string, immediate = true): ResourceState<T> {
  const items = shallowRef<T[]>([])
  const loading = ref(false)
  const error = ref<string>()

  async function load(): Promise<void> {
    loading.value = true
    error.value = undefined
    try {
      const response = await managerApi.get<T[] | PageResult<T> | { data?: T[] }>(path)
      items.value = extractItems(response)
    } catch (caught) {
      const requestId = caught instanceof ApiError && caught.requestId ? ` · ${caught.requestId}` : ''
      error.value = `${caught instanceof Error ? caught.message : String(caught)}${requestId}`
    } finally {
      loading.value = false
    }
  }

  if (immediate) onMounted(load)
  return { items, loading, error, load }
}
