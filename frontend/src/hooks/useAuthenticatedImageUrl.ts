import { useEffect, useState } from 'react'
import { fetchAuthenticatedImageBlob } from '../api/client'

interface AuthenticatedImageState {
  url?: string
  loading: boolean
  failed: boolean
}

export default function useAuthenticatedImageUrl(source?: string | null): AuthenticatedImageState {
  const [state, setState] = useState<AuthenticatedImageState>({
    url: undefined,
    loading: Boolean(source),
    failed: false,
  })

  useEffect(() => {
    if (!source) {
      setState({ url: undefined, loading: false, failed: false })
      return
    }
    if (source.startsWith('data:') || source.startsWith('blob:')) {
      setState({ url: source, loading: false, failed: false })
      return
    }

    let cancelled = false
    let objectUrl = ''
    setState({ url: undefined, loading: true, failed: false })
    void fetchAuthenticatedImageBlob(source)
      .then(blob => {
        objectUrl = URL.createObjectURL(blob)
        if (cancelled) {
          URL.revokeObjectURL(objectUrl)
          objectUrl = ''
          return
        }
        setState({ url: objectUrl, loading: false, failed: false })
      })
      .catch(() => {
        if (!cancelled) setState({ url: undefined, loading: false, failed: true })
      })

    return () => {
      cancelled = true
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [source])

  return state
}
