import { useEffect, useState } from 'react'

const MOBILE_QUERY = '(max-width: 767px)'
const NATIVE_MOBILE = import.meta.env.VITE_NATIVE_MOBILE === 'true'

export default function useMobileViewport(): boolean {
  const [mobile, setMobile] = useState(() => (
    NATIVE_MOBILE
    || (typeof window !== 'undefined' && window.matchMedia(MOBILE_QUERY).matches)
  ))

  useEffect(() => {
    if (NATIVE_MOBILE) {
      setMobile(true)
      return undefined
    }

    const media = window.matchMedia(MOBILE_QUERY)
    const update = () => setMobile(media.matches)
    update()
    media.addEventListener('change', update)
    return () => media.removeEventListener('change', update)
  }, [])

  return mobile
}
